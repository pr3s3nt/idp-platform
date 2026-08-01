"""Tests for orchestrate.py.

Run from the idp repo root:  python -m pytest test_orchestrate.py -v

The state-stability tests shell out to a real score-k8s, so they need it on PATH along
with the catalog in this repo (provisioners/ + patches/).
"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

import orchestrate as orc

CATALOG = Path(__file__).parent
# Tests render the real catalog, which now carries %%placeholders%%. Load the repo's own
# environment config so substitution has values — same file the workflow passes in.
orc.CONFIG = orc.EnvConfig.load(str(CATALOG / "platform.env.yaml"))


@pytest.fixture
def no_branch_config(monkeypatch):
    """Git fixtures create repos on whatever branch this git defaults to (often master),
    while the repo's own config names `main`. Tests about ordering are not about branch
    names, so drop the environments block for them."""
    monkeypatch.setattr(orc, "CONFIG", orc.EnvConfig({}))
HAS_SCORE_K8S = shutil.which("score-k8s") is not None
needs_score_k8s = pytest.mark.skipif(not HAS_SCORE_K8S, reason="score-k8s not installed")


def write(path: Path, spec: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(spec, sort_keys=False))


def score_spec(name: str, container: str = "main") -> dict:
    return {
        "apiVersion": "score.dev/v1b1",
        "metadata": {"name": name},
        "containers": {container: {"image": "."}},
        "service": {"ports": {"http": {"port": 8080, "targetPort": 8080}}},
    }


# ----------------------------------------------------------------------------- discovery
def test_discover_root_layout_and_container_name(tmp_path):
    """sample-nginx's real shape: root score.yaml with a container called 'web'.

    Guards the bug that a hardcoded 'main' would reintroduce — the image rewrite would
    silently no-op and ship an unpullable 'image: .' manifest.
    """
    write(tmp_path / "score.yaml", score_spec("nginx", container="web"))
    services = orc.discover(tmp_path)
    assert [(s.workload, s.container) for s in services] == [("nginx", "web")]


def test_discover_flat_layout(tmp_path):
    """OnlineBoutique's shape: flat score-*.yaml in one directory."""
    write(tmp_path / "score-frontend.yaml", score_spec("frontend", "frontend"))
    write(tmp_path / "score-cart.yaml", score_spec("cart", "cart"))
    write(tmp_path / "score-ad.yaml", score_spec("ad", "ad"))
    assert sorted(s.workload for s in orc.discover(tmp_path)) == ["ad", "cart", "frontend"]


def test_discover_dotted_flat_layout(tmp_path):
    write(tmp_path / "backend.score.yaml", score_spec("backend"))
    write(tmp_path / "frontend.score.yaml", score_spec("frontend"))
    assert sorted(s.workload for s in orc.discover(tmp_path)) == ["backend", "frontend"]


def test_discover_subdir_layout(tmp_path):
    write(tmp_path / "backend" / "score.yaml", score_spec("backend"))
    write(tmp_path / "frontend" / "score.yaml", score_spec("frontend"))
    assert sorted(s.workload for s in orc.discover(tmp_path)) == ["backend", "frontend"]


def test_discover_root_wins_over_subdirs(tmp_path):
    write(tmp_path / "score.yaml", score_spec("root"))
    write(tmp_path / "sub" / "score.yaml", score_spec("sub"))
    assert [s.workload for s in orc.discover(tmp_path)] == ["root"]


def test_discover_nothing_found_is_fatal(tmp_path):
    with pytest.raises(SystemExit, match="no score file found"):
        orc.discover(tmp_path)


def test_discover_requires_metadata_name(tmp_path):
    write(tmp_path / "score.yaml", {"containers": {"main": {"image": "."}}})
    with pytest.raises(SystemExit, match="metadata.name"):
        orc.discover(tmp_path)


# -------------------------------------------------------------------------- image naming
def test_image_ref_single_vs_multi(tmp_path):
    svc = orc.Service(path=tmp_path / "score.yaml", workload="frontend", container="main")
    assert orc.image_ref("r.io/p", "nginx", svc, "abc", multi=False) == "r.io/p/nginx:abc"
    assert orc.image_ref("r.io/p", "shop", svc, "abc", multi=True) == "r.io/p/shop-frontend:abc"


def test_rewrite_images_pins_every_workload(tmp_path):
    write(tmp_path / "score-frontend.yaml", score_spec("frontend", "frontend"))
    write(tmp_path / "score-cart.yaml", score_spec("cart", "cart"))
    services = orc.discover(tmp_path)
    plan = orc.plan_images(services, "r.io/p", "shop", "sha1", tmp_path, "commit")
    orc.rewrite_images(services, plan)
    images = {
        s.workload: yaml.safe_load(s.path.read_text())["containers"][s.container]["image"]
        for s in services
    }
    assert images == {
        "frontend": "r.io/p/shop-frontend:sha1",
        "cart": "r.io/p/shop-cart:sha1",
    }
    # No leftover build placeholder anywhere.
    assert "image: ." not in (tmp_path / "score-cart.yaml").read_text()


# --------------------------------------------------------------------------------- retag
@pytest.mark.parametrize(
    "ref,expected",
    [
        ("h.io/p/nginx:old", "h.io/p/nginx:v2"),
        ("h.io/p/nginx", "h.io/p/nginx:v2"),
        ("harbor:5000/p/nginx:old", "harbor:5000/p/nginx:v2"),
        ("harbor:5000/p/nginx", "harbor:5000/p/nginx:v2"),
    ],
)
def test_replace_tag_handles_registry_port(ref, expected):
    assert orc.replace_tag(ref, "v2") == expected


def test_retag_leaves_datastore_images_alone(tmp_path):
    manifests = tmp_path / "m.yaml"
    orc.dump_all(
        [
            {"kind": "Deployment", "metadata": {"name": "app"},
             "spec": {"template": {"spec": {"containers": [
                 {"name": "c", "image": "h.io/p/myapp:old"}]}}}},
            {"kind": "Deployment", "metadata": {"name": "be"},
             "spec": {"template": {"spec": {"containers": [
                 {"name": "c", "image": "h.io/p/myapp-backend:old"}]}}}},
            {"kind": "StatefulSet", "metadata": {"name": "pg"},
             "spec": {"template": {"spec": {"containers": [
                 {"name": "c", "image": "h.io/p/postgres:17-alpine"}]}}}},
        ],
        manifests,
    )
    assert orc.retag(manifests, "myapp", "v9") == 2
    images = [
        c["image"]
        for d in orc.load_all(manifests)
        for c in d["spec"]["template"]["spec"]["containers"]
    ]
    assert images == ["h.io/p/myapp:v9", "h.io/p/myapp-backend:v9", "h.io/p/postgres:17-alpine"]


# ------------------------------------------------------------------------ ancestry guard
def git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=cwd, text=True, check=True,
                          capture_output=True).stdout.strip()


@pytest.fixture
def repo_with_two_commits(tmp_path):
    repo = tmp_path / "app"
    repo.mkdir()
    git(repo, "init", "-q")
    git(repo, "config", "user.email", "t@t")
    git(repo, "config", "user.name", "t")
    (repo / "f").write_text("1")
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "one")
    old = git(repo, "rev-parse", "HEAD")
    (repo / "f").write_text("2")
    git(repo, "commit", "-qam", "two")
    new = git(repo, "rev-parse", "HEAD")
    return repo, old, new


def test_is_ancestor(repo_with_two_commits):
    repo, old, new = repo_with_two_commits
    assert orc.is_ancestor(repo, old, new) is True
    assert orc.is_ancestor(repo, new, old) is False
    assert orc.is_ancestor(repo, "0" * 40, new) is None  # unknown commit -> undecidable


def make_config_repo(tmp_path: Path) -> Path:
    """A config repo with a real bare remote, so the push path is genuinely exercised."""
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "-q", "--bare", str(remote)], check=True)

    config = tmp_path / "config"
    config.mkdir()
    git(config, "init", "-q")
    git(config, "config", "user.email", "t@t")
    git(config, "config", "user.name", "t")
    (config / "staging").mkdir()
    (config / "staging" / "manifests.yaml").write_text("{}\n")
    git(config, "add", ".")
    git(config, "commit", "-qm", "init")
    git(config, "remote", "add", "origin", str(remote))
    git(config, "push", "-q", "-u", "origin", "HEAD")
    return config


def test_commit_refuses_out_of_order_deploy(tmp_path, repo_with_two_commits, no_branch_config):
    """The ordering bug: build durations differ, so an older commit can dispatch second.
    Without this guard the config repo silently regresses to the older SHA."""
    app, old, new = repo_with_two_commits
    config = make_config_repo(tmp_path)
    record = config / orc.sha_record_dir() / "staging.sha"
    record.parent.mkdir(parents=True)
    record.write_text(new + "\n")

    args = orc.argparse.Namespace(
        config_dir=str(config), app="a", env="staging", sha=old,
        app_dir=str(app), catalog_ref=None,
    )
    with pytest.raises(SystemExit, match="ancestor of the already-deployed"):
        orc.cmd_commit(args)


def test_commit_allows_newer_deploy(tmp_path, repo_with_two_commits, no_branch_config):
    app, old, new = repo_with_two_commits
    config = make_config_repo(tmp_path)
    record = config / orc.sha_record_dir() / "staging.sha"
    record.parent.mkdir(parents=True)
    record.write_text(old + "\n")

    args = orc.argparse.Namespace(
        config_dir=str(config), app="a", env="staging", sha=new,
        app_dir=str(app), catalog_ref=None,
    )
    orc.cmd_commit(args)
    assert record.read_text().strip() == new
    # The commit really reached the remote (checked via the upstream tracking ref).
    assert new in git(config, "log", "--format=%s", "-1", "@{u}")


# ----------------------------------------------------------- state stability (the big one)
def render_postgres_app(tmp_path: Path, work_name: str, *, state_file: Path | None) -> dict:
    """Render examples/app-with-postgres and return {statefulset, secret, password}."""
    app_dir = tmp_path / f"app-{work_name}"
    app_dir.mkdir()
    shutil.copyfile(CATALOG / "examples" / "app-with-postgres" / "score.yaml",
                    app_dir / "score.yaml")

    args = orc.argparse.Namespace(
        app="pgapp", image="pgapp", tag="sha1", env="staging",
        registry="h.io/p", catalog=str(CATALOG), app_dir=str(app_dir),
        work=str(tmp_path / work_name), out=str(tmp_path / work_name / "out.yaml"),
        kubeconfig=None,
        state_file=str(state_file) if state_file else None,
        no_state=state_file is None,
    )
    orc.cmd_render(args)

    docs = orc.load_all(Path(args.work) / "manifests.yaml")
    sts = next(d for d in docs if d["kind"] == "StatefulSet")
    sec = next(d for d in docs if d["kind"] == "Secret")
    return {
        "statefulset": sts["metadata"]["name"],
        "secret": sec["metadata"]["name"],
        "password": sec["data"]["password"],
    }


@needs_score_k8s
def test_state_roundtrip_keeps_names_and_password_stable(tmp_path):
    """The fix. Two renders sharing state must produce identical resource identity.

    Without this, every deploy renames the Postgres StatefulSet and regenerates its
    password, orphaning the PVC and abandoning the database.
    """
    state = tmp_path / "state.yaml"
    first = render_postgres_app(tmp_path, "run1", state_file=state)
    second = render_postgres_app(tmp_path, "run2", state_file=state)
    assert first == second, "state round-trip failed to stabilise resource identity"


@needs_score_k8s
def test_without_state_everything_churns(tmp_path):
    """Proves the test above isn't passing vacuously: with persistence disabled the exact
    churn we are protecting against reappears."""
    first = render_postgres_app(tmp_path, "run1", state_file=None)
    second = render_postgres_app(tmp_path, "run2", state_file=None)
    assert first["statefulset"] != second["statefulset"]
    assert first["password"] != second["password"]


# ------------------------------------------------------------------------- multi-workload
@needs_score_k8s
def test_multi_workload_cross_references_resolve(tmp_path):
    """The OnlineBoutique shape: flat score-*.yaml with `type: service` peer references.

    All workloads must be generated in ONE score-k8s invocation — the service provisioner
    fails with "unknown workload" if a peer isn't yet in the project state.
    """
    app_dir = tmp_path / "app"
    app_dir.mkdir()
    for f in (CATALOG / "examples" / "microservices").glob("score-*.yaml"):
        shutil.copyfile(f, app_dir / f.name)

    args = orc.argparse.Namespace(
        app="boutique", image="boutique", tag="abc123", env="staging",
        registry="h.io/p", catalog=str(CATALOG), app_dir=str(app_dir),
        work=str(tmp_path / "work"), out=str(tmp_path / "out.yaml"),
        kubeconfig=None, state_file=str(tmp_path / "state.yaml"), no_state=False,
    )
    orc.cmd_render(args)
    docs = orc.load_all(Path(args.work) / "manifests.yaml")

    deployments = {d["metadata"]["name"]: d for d in docs if d["kind"] == "Deployment"}
    assert set(deployments) == {"ad", "cart", "frontend"}

    # Peer references resolved to real Service names.
    env = {
        e["name"]: e.get("value")
        for e in deployments["frontend"]["spec"]["template"]["spec"]["containers"][0]["env"]
    }
    assert env["AD_SERVICE_ADDR"] == "ad:9555"
    assert env["CART_SERVICE_ADDR"] == "cart:7070"

    # Each workload got its own image.
    assert {
        name: d["spec"]["template"]["spec"]["containers"][0]["image"]
        for name, d in deployments.items()
    } == {
        "ad": "h.io/p/boutique-ad:abc123",
        "cart": "h.io/p/boutique-cart:abc123",
        "frontend": "h.io/p/boutique-frontend:abc123",
    }

    # The redis datastore produced a Secret, which must not reach the config repo.
    assert any(d["kind"] == "Secret" for d in docs)
    assert not any(d.get("kind") == "Secret" for d in orc.load_all(Path(args.out)))


@needs_score_k8s
def test_patch_only_fills_unset_resources(tmp_path):
    """`ad` declares no container resources so the patch fills them in; `cart` declares its
    own and must keep them."""
    app_dir = tmp_path / "app"
    app_dir.mkdir()
    for f in (CATALOG / "examples" / "microservices").glob("score-*.yaml"):
        shutil.copyfile(f, app_dir / f.name)

    args = orc.argparse.Namespace(
        app="boutique", image="boutique", tag="t", env="staging", registry="h.io/p",
        catalog=str(CATALOG), app_dir=str(app_dir), work=str(tmp_path / "work"),
        out=str(tmp_path / "out.yaml"), kubeconfig=None,
        state_file=str(tmp_path / "state.yaml"), no_state=False,
    )
    orc.cmd_render(args)
    docs = orc.load_all(Path(args.work) / "manifests.yaml")
    cpu = {
        d["metadata"]["name"]: d["spec"]["template"]["spec"]["containers"][0]["resources"]["requests"]["cpu"]
        for d in docs if d["kind"] == "Deployment"
    }
    assert cpu["ad"] == "50m"      # filled in by staging.tpl
    assert cpu["cart"] == "200m"   # declared in the score file, preserved


@needs_score_k8s
def test_patch_changes_still_apply_across_state_restore(tmp_path):
    """State carries patching_templates, so restoring it must not pin us to stale patches:
    a staging render followed by a prod render must still switch replicas 1 -> 3."""
    state = tmp_path / "state.yaml"
    app_dir = tmp_path / "app"
    app_dir.mkdir()
    shutil.copyfile(CATALOG / "examples" / "simple-nginx" / "score.yaml", app_dir / "score.yaml")

    def render(env: str, work: str) -> int:
        args = orc.argparse.Namespace(
            app="nginx", image="nginx", tag="sha1", env=env, registry="h.io/p",
            catalog=str(CATALOG), app_dir=str(app_dir), work=str(tmp_path / work),
            out=str(tmp_path / work / "out.yaml"), kubeconfig=None,
            state_file=str(state), no_state=False,
        )
        orc.cmd_render(args)
        docs = orc.load_all(Path(args.work) / "manifests.yaml")
        return next(d for d in docs if d["kind"] == "Deployment")["spec"]["replicas"]

    assert render("staging", "w1") == 1
    assert render("prod", "w2") == 3


# ------------------------------------------------------------- managed-by / Fleet drift
def test_strip_managed_by_removes_only_top_level_label():
    """Helm rewrites the top-level managed-by label on apply, so keeping score-k8s's value
    in git guarantees a permanently Modified Fleet Bundle. Pod template labels are left
    alone — Helm does not touch those, so they still match the cluster."""
    docs = [
        {"kind": "Deployment",
         "metadata": {"labels": {"app.kubernetes.io/managed-by": "score-k8s", "env": "staging"}},
         "spec": {"template": {"metadata": {"labels": {"app.kubernetes.io/managed-by": "score-k8s"}}}}},
        {"kind": "HTTPRoute", "metadata": {"labels": {"app.kubernetes.io/name": "route-x"}}},
        {"kind": "Service", "metadata": {}},
    ]
    assert orc.strip_managed_by(docs) == 1
    assert docs[0]["metadata"]["labels"] == {"env": "staging"}
    # pod template untouched
    assert (docs[0]["spec"]["template"]["metadata"]["labels"]
            == {"app.kubernetes.io/managed-by": "score-k8s"})
    # manifests without the label are left exactly as they were
    assert docs[1]["metadata"]["labels"] == {"app.kubernetes.io/name": "route-x"}
    assert docs[2]["metadata"] == {}


@needs_score_k8s
def test_rendered_config_repo_output_has_no_managed_by(tmp_path):
    """End to end: whatever reaches the config repo must not carry the label at top level."""
    app_dir = tmp_path / "app"
    app_dir.mkdir()
    shutil.copyfile(CATALOG / "examples" / "simple-nginx" / "score.yaml", app_dir / "score.yaml")
    args = orc.argparse.Namespace(
        app="nginx", image="nginx", tag="sha1", env="staging", registry="h.io/p",
        catalog=str(CATALOG), app_dir=str(app_dir), work=str(tmp_path / "w"),
        out=str(tmp_path / "out.yaml"), kubeconfig=None,
        state_file=str(tmp_path / "state.yaml"), no_state=False,
    )
    orc.cmd_render(args)
    for doc in orc.load_all(Path(args.out)):
        assert "app.kubernetes.io/managed-by" not in (doc.get("metadata") or {}).get("labels", {})


# ------------------------------------------------------------------ concurrency / ordering
def clone_of(tmp_path: Path, name: str) -> Path:
    """A second, independent working copy of the same config repo — i.e. another runner."""
    other = tmp_path / name
    subprocess.run(["git", "clone", "-q", str(tmp_path / "remote.git"), str(other)], check=True)
    git(other, "config", "user.email", "t@t")
    git(other, "config", "user.name", "t")
    return other


def branch_of(repo: Path) -> str:
    """Tests must not assume main vs master — `git init` defaults differ per git version."""
    return git(repo, "rev-parse", "--abbrev-ref", "HEAD")


def record_deploy(repo: Path, env: str, sha: str, msg: str) -> None:
    rec = repo / orc.sha_record_dir() / f"{env}.sha"
    rec.parent.mkdir(parents=True, exist_ok=True)
    rec.write_text(sha + "\n")
    git(repo, "add", ".")
    git(repo, "commit", "-qm", msg)


def test_commit_refuses_stale_render_after_remote_moved_ahead(tmp_path, repo_with_two_commits, no_branch_config):
    """The rebase hole: the guard used to run ONCE, against the clone taken at job start.

    Another writer lands a newer deploy while we render. Our push is rejected, and the
    rebase-retry replays OUR older commit on top of theirs — including our deploy record —
    so the environment silently rolls back. The re-check before rebasing must catch it.
    """
    app, old, new = repo_with_two_commits
    config = make_config_repo(tmp_path)

    # Another runner deploys the NEWER commit while we are still rendering the older one.
    other = clone_of(tmp_path, "other")
    record_deploy(other, "staging", new, "deploy new")
    git(other, "push", "-q")

    # Our stale clone knows nothing about that and tries to commit the OLDER sha.
    (config / "staging" / "manifests.yaml").write_text("stale render\n")
    args = orc.argparse.Namespace(
        config_dir=str(config), app="a", env="staging", sha=old,
        app_dir=str(app), catalog_ref=None,
    )
    with pytest.raises(orc.OutOfOrder, match="ancestor of the already-deployed"):
        orc.cmd_commit(args)

    # And the remote still holds the newer deploy — nothing was rolled back.
    up = f"origin/{branch_of(other)}"
    assert git(other, "show", f"{up}:{orc.sha_record_dir()}/staging.sha").strip() == new


def test_commit_rebases_when_concurrent_change_is_unrelated(tmp_path, repo_with_two_commits, no_branch_config):
    """The same race, but the other writer touched something else (a human editing
    fleet.yaml). Rebasing is correct here and must still happen."""
    app, old, new = repo_with_two_commits
    config = make_config_repo(tmp_path)

    other = clone_of(tmp_path, "other")
    (other / "staging" / "fleet.yaml").write_text("namespace: a-staging\n")
    git(other, "add", ".")
    git(other, "commit", "-qm", "human edit")
    git(other, "push", "-q")

    (config / "staging" / "manifests.yaml").write_text("fresh render\n")
    args = orc.argparse.Namespace(
        config_dir=str(config), app="a", env="staging", sha=new,
        app_dir=str(app), catalog_ref=None,
    )
    orc.cmd_commit(args)

    # Both changes survived.
    git(config, "fetch", "-q", "origin")
    up = f"origin/{branch_of(config)}"
    assert git(config, "show", f"{up}:{orc.sha_record_dir()}/staging.sha").strip() == new
    assert "namespace" in git(config, "show", f"{up}:staging/fleet.yaml")


def test_guard_is_inert_without_app_dir():
    """Documents why the workflow MUST pass --app-dir on promote too: with no checkout there
    is no history to compare against, so the guard cannot fire at all."""
    orc.guard_ordering("deadbeef", "cafe1234", None, "prod")  # must not raise


# --------------------------------------------------------------- state Secret optimistic lock
class FakeKubectl:
    """Records kubectl invocations and replays canned results."""

    def __init__(self, results):
        self.results = results
        self.calls = []

    def __call__(self, args, *, kubeconfig=None, **kw):
        self.calls.append(args)
        rc, err = self.results.get(args[0], (0, ""))
        return subprocess.CompletedProcess(args, rc, stdout="", stderr=err)


def test_state_push_sends_resource_version_precondition(tmp_path, monkeypatch):
    """A replace carrying the observed resourceVersion is what makes the write checked."""
    src = tmp_path / "state.yaml"
    src.write_text("guid: abc\n")
    store = orc.SecretStateStore("a", "staging", None)
    store.resource_version = "4242"

    sent = {}

    def fake_kubectl(args, *, kubeconfig=None, stdin=None, **kw):
        sent["verb"] = args[0]
        sent["body"] = yaml.safe_load(stdin) if stdin else None
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    monkeypatch.setattr(orc, "kubectl", fake_kubectl)
    monkeypatch.setattr(orc, "ensure_namespace", lambda *a, **k: None)
    store.push(src)

    assert sent["verb"] == "replace"
    assert sent["body"]["metadata"]["resourceVersion"] == "4242"


def test_state_push_raises_on_concurrent_write(tmp_path, monkeypatch):
    """The API server rejects the replace when another render wrote in between. That must
    surface as a clear conflict, not a silent overwrite of someone else's GUIDs/password."""
    src = tmp_path / "state.yaml"
    src.write_text("guid: abc\n")
    store = orc.SecretStateStore("a", "staging", None)
    store.resource_version = "1"

    def fake_kubectl(args, *, kubeconfig=None, stdin=None, **kw):
        return subprocess.CompletedProcess(
            args, 1, stdout="",
            stderr='Operation cannot be fulfilled on secrets "a-staging-score-state": '
                   "the object has been modified; please apply your changes to the latest version",
        )

    monkeypatch.setattr(orc, "kubectl", fake_kubectl)
    monkeypatch.setattr(orc, "ensure_namespace", lambda *a, **k: None)
    with pytest.raises(orc.StateConflict, match="changed while this render was running"):
        store.push(src)


def test_state_push_first_write_uses_create(tmp_path, monkeypatch):
    """No Secret observed -> create, so a racing first deploy hits AlreadyExists."""
    src = tmp_path / "state.yaml"
    src.write_text("guid: abc\n")
    store = orc.SecretStateStore("a", "staging", None)
    seen = {}

    def fake_kubectl(args, *, kubeconfig=None, stdin=None, **kw):
        seen["verb"] = args[0]
        seen["body"] = yaml.safe_load(stdin) if stdin else None
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    monkeypatch.setattr(orc, "kubectl", fake_kubectl)
    monkeypatch.setattr(orc, "ensure_namespace", lambda *a, **k: None)
    store.push(src)

    assert seen["verb"] == "create"
    assert "resourceVersion" not in seen["body"]["metadata"]


# ------------------------------------------------- per-service tagging & stable ordering
def git_app_repo(tmp_path: Path) -> Path:
    """Two services in one repo, each in its own directory — the boutique shape."""
    repo = tmp_path / "app"
    for svc in ("frontend", "cart"):
        write(repo / svc / "score.yaml", score_spec(svc, svc))
    git(repo, "init", "-q")
    git(repo, "config", "user.email", "t@t")
    git(repo, "config", "user.name", "t")
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "one")
    return repo


def test_content_tags_change_only_for_the_service_that_changed(tmp_path):
    """The whole point: a commit touching `cart` must NOT re-tag `frontend`.

    With the repo commit SHA as the tag, every workload gets a new image reference on every
    commit, every Deployment changes, and every pod restarts — measured on the 11-service
    boutique, where a commit touching only .github/ rolled all eleven.
    """
    repo = git_app_repo(tmp_path)
    services = orc.discover(repo)
    before = orc.plan_images(services, "r.io/p", "ob", "ignored", repo, "content")

    (repo / "cart" / "extra.txt").write_text("changed\n")
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "touch cart only")
    after = orc.plan_images(orc.discover(repo), "r.io/p", "ob", "ignored", repo, "content")

    assert after["cart"] != before["cart"], "cart changed, its tag must change"
    assert after["frontend"] == before["frontend"], "frontend untouched, its tag must not move"


def test_commit_strategy_moves_every_tag(tmp_path):
    """Proves the test above is not vacuous: the old behaviour really did re-tag everything."""
    repo = git_app_repo(tmp_path)
    services = orc.discover(repo)
    a = orc.plan_images(services, "r.io/p", "ob", "sha1", repo, "commit")
    b = orc.plan_images(services, "r.io/p", "ob", "sha2", repo, "commit")
    assert a["frontend"] != b["frontend"]
    assert a["cart"] != b["cart"]


def test_content_strategy_falls_back_outside_git(tmp_path):
    """No git checkout -> no content hash. Warn and use --tag rather than fail the deploy."""
    write(tmp_path / "score.yaml", score_spec("solo"))
    services = orc.discover(tmp_path)
    plan = orc.plan_images(services, "r.io/p", "solo", "sha1", tmp_path, "content")
    assert plan["solo"] == "r.io/p/solo:sha1"


def test_sort_manifests_is_stable_regardless_of_input_order():
    docs = [
        {"kind": "Service", "metadata": {"name": "b"}},
        {"kind": "Deployment", "metadata": {"name": "b"}},
        {"kind": "Deployment", "metadata": {"name": "a"}},
        {"kind": "HTTPRoute", "metadata": {"name": "r"}},
    ]
    order = lambda ds: [(d["kind"], d["metadata"]["name"]) for d in orc.sort_manifests(ds)]
    assert order(docs) == order(list(reversed(docs)))
    assert order(docs)[0] == ("Deployment", "a")


# ------------------------------------------------------------------- promote from-staging
def manifest_with(path: Path, entries: list[tuple[str, str]]) -> None:
    """entries: (deployment name, image)."""
    docs = [
        {"kind": "Deployment", "metadata": {"name": name},
         "spec": {"template": {"spec": {"containers": [{"name": "main", "image": img}]}}}}
        for name, img in entries
    ]
    orc.dump_all(docs, path)


def test_promote_from_staging_copies_the_whole_image_set(tmp_path):
    """Once each service carries its own tag there is no single version to promote to —
    prod must be given exactly the set staging was verified on."""
    config = tmp_path / "config"
    manifest_with(config / "staging" / "manifests.yaml", [
        ("frontend", "r.io/p/ob-frontend:aaa"),
        ("cart", "r.io/p/ob-cart:bbb"),
    ])
    manifest_with(config / "prod" / "manifests.yaml", [
        ("frontend", "r.io/p/ob-frontend:old"),
        ("cart", "r.io/p/ob-cart:old"),
    ])
    orc.cmd_promote(orc.argparse.Namespace(
        mode="from-staging", config_dir=str(config), image="ob", tag="ignored"))

    got = {d["metadata"]["name"]: d["spec"]["template"]["spec"]["containers"][0]["image"]
           for d in orc.load_all(config / "prod" / "manifests.yaml")}
    assert got == {"frontend": "r.io/p/ob-frontend:aaa", "cart": "r.io/p/ob-cart:bbb"}


def test_promote_from_staging_leaves_datastore_images_alone(tmp_path):
    """A provisioner's postgres image is decided by the catalog, not by what the app built."""
    config = tmp_path / "config"
    manifest_with(config / "staging" / "manifests.yaml", [("frontend", "r.io/p/ob-frontend:aaa")])
    manifest_with(config / "prod" / "manifests.yaml", [
        ("frontend", "r.io/p/ob-frontend:old"),
        ("pg-x", "r.io/p/postgres:17-alpine"),
    ])
    orc.cmd_promote(orc.argparse.Namespace(
        mode="from-staging", config_dir=str(config), image="ob", tag="ignored"))

    got = {d["metadata"]["name"]: d["spec"]["template"]["spec"]["containers"][0]["image"]
           for d in orc.load_all(config / "prod" / "manifests.yaml")}
    assert got["pg-x"] == "r.io/p/postgres:17-alpine"


# ------------------------------------------------------------------- environment config
def cfg(**over) -> orc.EnvConfig:
    base = {
        "ingress": {"gateway_name": "gw", "gateway_namespace": "ns"},
        "kubernetes": {"storage_class": "sc"},
        "environments": {"staging": {"replicas": 1, "domain": "stg.example"},
                         "prod": {"replicas": 3, "domain": "prod.example"}},
    }
    return orc.EnvConfig(orc._deep_merge(base, over))


def test_config_exposes_the_chosen_environment_under_env_prefix():
    c = cfg()
    assert c.for_env("staging")["env.replicas"] == 1
    assert c.for_env("prod")["env.replicas"] == 3
    # shared values are visible in both
    assert c.for_env("prod")["ingress.gateway_name"] == "gw"


def test_config_substitutes_placeholders_per_environment():
    c = cfg()
    text = "gw=%%ingress.gateway_name%% n=%%env.replicas%% d=%%env.domain%%"
    assert c.render(text, "staging", where="t") == "gw=gw n=1 d=stg.example"
    assert c.render(text, "prod", where="t") == "gw=gw n=3 d=prod.example"


def test_config_leaves_go_templates_alone():
    """Provisioners are Go templates owned by score-k8s. The substitution must not touch
    {{ }} or ${ }, or it would corrupt the very files it is preparing."""
    text = "{{ .SourceWorkload }}.%%env.domain%% and ${resources.db.host}"
    assert cfg().render(text, "staging", where="t") == "{{ .SourceWorkload }}.stg.example and ${resources.db.host}"


def test_unknown_placeholder_is_fatal_not_silent():
    """A typo'd key must not render as empty text. Every infrastructure mistake in this
    project failed silently — a wrong gateway name simply never attaches a route."""
    with pytest.raises(SystemExit, match="unknown placeholder"):
        cfg().render("%%ingress.gatway_name%%", "staging", where="provisioners/x.yaml")


def test_config_file_overrides_defaults_without_dropping_them(tmp_path):
    f = tmp_path / "platform.env.yaml"
    f.write_text("registry:\n  path: r.io/team\n")
    c = orc.EnvConfig.load(str(f))
    assert c.get("registry.path") == "r.io/team"
    # untouched default survives the merge
    assert c.get("kubernetes.sha_record_dir") == ".platform"


def test_missing_config_file_is_fatal():
    with pytest.raises(SystemExit, match="env config not found"):
        orc.EnvConfig.load("/nope/platform.env.yaml")


# ----------------------------------------------------------------- PR flow (branch protection)
def fake_gh(tmp_path: Path) -> Path:
    """A stand-in `gh` on PATH, so the PR path is testable without a real remote."""
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    gh = bindir / "gh"
    gh.write_text(
        "#!/bin/sh\n"
        'echo "$@" >> "$GH_CALLS"\n'
        'echo https://example.invalid/pr/1\n'
    )
    gh.chmod(0o755)
    return bindir


def test_commit_via_pr_pushes_a_branch_and_opens_a_pr(tmp_path, repo_with_two_commits, monkeypatch, no_branch_config):
    """Production requires review, so the bot must NOT push onto the protected branch.
    It puts the change on its own branch and opens a PR for a human to merge."""
    app, _old, new = repo_with_two_commits
    config = make_config_repo(tmp_path)
    base = branch_of(config)

    calls = tmp_path / "gh-calls.txt"
    monkeypatch.setenv("GH_CALLS", str(calls))
    monkeypatch.setenv("PATH", f"{fake_gh(tmp_path)}:{os.environ['PATH']}")

    (config / "staging" / "manifests.yaml").write_text("rendered\n")
    orc.cmd_commit(orc.argparse.Namespace(
        config_dir=str(config), app="a", env="prod", sha=new, app_dir=str(app),
        catalog_ref="main", branch=base, via_pr=True,
    ))

    # A dedicated branch reached the remote...
    head = f"deploy/a-prod-{new[:8]}"
    assert head in git(config, "ls-remote", "--heads", "origin")
    # ...the protected base branch was NOT moved by us...
    assert git(config, "rev-parse", f"origin/{base}") == git(config, "rev-parse", f"{base}@{{u}}")
    # ...and a PR was opened against it.
    text = calls.read_text()
    assert "pr create" in text and f"--base {base}" in text and f"--head {head}" in text


def test_commit_without_via_pr_still_pushes_directly(tmp_path, repo_with_two_commits, no_branch_config):
    """Staging needs no review, so it keeps the fast path: a plain push, no PR."""
    app, _old, new = repo_with_two_commits
    config = make_config_repo(tmp_path)
    (config / "staging" / "manifests.yaml").write_text("rendered\n")
    orc.cmd_commit(orc.argparse.Namespace(
        config_dir=str(config), app="a", env="staging", sha=new, app_dir=str(app),
        catalog_ref=None, branch=None, via_pr=False,
    ))
    assert new in git(config, "log", "--format=%s", "-1", "@{u}")


def test_protected_branch_opens_a_pr(tmp_path, repo_with_two_commits, no_branch_config, monkeypatch):
    """Việc "môi trường này có cần duyệt không" đọc thẳng từ branch protection của GitHub,
    không phải từ một cờ trong file cấu hình. Hai nơi cùng khai một sự thật thì có lúc lệch,
    và một cờ ghi require_pr: false trong khi nhánh thật đang được bảo vệ là lời nói dối
    chỉ vỡ lúc push."""
    app, _old, new_sha = repo_with_two_commits
    config = make_config_repo(tmp_path)
    calls = tmp_path / "gh-calls.txt"
    monkeypatch.setenv("GH_CALLS", str(calls))
    monkeypatch.setenv("PATH", f"{fake_gh(tmp_path)}:{os.environ['PATH']}")
    monkeypatch.setattr(orc, "branch_is_protected", lambda *a: True)

    (config / "staging" / "manifests.yaml").write_text("rendered\n")
    orc.cmd_commit(orc.argparse.Namespace(
        config_dir=str(config), app="a", env="prod", sha=new_sha,
        app_dir=str(app), catalog_ref=None,
    ))
    assert "pr create" in calls.read_text()


def test_unprotected_branch_pushes_directly(tmp_path, repo_with_two_commits, no_branch_config, monkeypatch):
    """Repo demo không bật bảo vệ -> tự phục vụ hoàn toàn, không phải khai gì."""
    app, _old, new_sha = repo_with_two_commits
    config = make_config_repo(tmp_path)
    monkeypatch.setattr(orc, "branch_is_protected", lambda *a: False)
    (config / "staging" / "manifests.yaml").write_text("rendered\n")
    orc.cmd_commit(orc.argparse.Namespace(
        config_dir=str(config), app="a", env="staging", sha=new_sha,
        app_dir=str(app), catalog_ref=None,
    ))
    assert new_sha in git(config, "log", "--format=%s", "-1", "@{u}")


def test_undetectable_protection_falls_back_to_direct_push(tmp_path, repo_with_two_commits, no_branch_config, monkeypatch):
    """Không hỏi được GitHub thì đi đường push thẳng — CỐ Ý.

    Nếu nhánh thật ra có bảo vệ, GitHub từ chối kèm GH006, tức hỏng ỒN ÀO. Đoán ngược lại
    thì sinh ra một pull request nằm im trên repo demo chẳng ai chờ đợi. Việc cưỡng chế
    nằm ở phía GitHub, không nằm ở phán đoán của chúng ta.
    """
    app, _old, new_sha = repo_with_two_commits
    config = make_config_repo(tmp_path)
    monkeypatch.setattr(orc, "branch_is_protected", lambda *a: None)
    (config / "staging" / "manifests.yaml").write_text("rendered\n")
    orc.cmd_commit(orc.argparse.Namespace(
        config_dir=str(config), app="a", env="staging", sha=new_sha,
        app_dir=str(app), catalog_ref=None,
    ))
    assert new_sha in git(config, "log", "--format=%s", "-1", "@{u}")


def test_env_without_require_pr_pushes_directly(tmp_path, repo_with_two_commits, monkeypatch):
    """Nhánh không bảo vệ giữ đường nhanh: push thẳng, không pull request."""
    app, _old, new = repo_with_two_commits
    config = make_config_repo(tmp_path)
    monkeypatch.setattr(orc, "branch_is_protected", lambda *a: False)
    monkeypatch.setattr(orc, "CONFIG", orc.EnvConfig(
        {"environments": {"staging": {"config_branch": branch_of(config)}}}))
    (config / "staging" / "manifests.yaml").write_text("rendered\n")
    orc.cmd_commit(orc.argparse.Namespace(
        config_dir=str(config), app="a", env="staging", sha=new,
        app_dir=str(app), catalog_ref=None,
    ))
    assert new in git(config, "log", "--format=%s", "-1", "@{u}")


def test_branch_mismatch_between_checkout_and_config_is_fatal(tmp_path, repo_with_two_commits, monkeypatch):
    """The job clones one branch and the config names another: publishing there would put
    manifests on a branch this run never rendered against. Refuse rather than guess."""
    app, _old, new = repo_with_two_commits
    config = make_config_repo(tmp_path)
    monkeypatch.setattr(orc, "CONFIG", orc.EnvConfig(
        {"environments": {"staging": {"config_branch": "some-other-branch"}}}))
    (config / "staging" / "manifests.yaml").write_text("rendered\n")
    with pytest.raises(SystemExit, match="checkout and the target must match"):
        orc.cmd_commit(orc.argparse.Namespace(
            config_dir=str(config), app="a", env="staging", sha=new,
            app_dir=str(app), catalog_ref=None,
        ))


def test_rerun_pushes_a_commit_stranded_by_an_earlier_failed_push(tmp_path, repo_with_two_commits, no_branch_config):
    """Found while testing for real: a push failed, leaving a local commit behind. The
    re-run staged nothing new, hit the "no manifest changes" early return, and did nothing
    — so re-running a broken deploy could never fix it. Hand-replay is a design goal, so
    unpushed work must still reach the remote."""
    app, _old, new = repo_with_two_commits
    config = make_config_repo(tmp_path)
    base = branch_of(config)

    # Simulate the aftermath of a failed push: committed locally, remote untouched.
    (config / "staging" / "manifests.yaml").write_text("rendered\n")
    git(config, "add", ".")
    git(config, "commit", "-qm", "deploy(a): staging " + new)
    before = git(config, "rev-parse", f"origin/{base}")

    # Nothing new to stage this time.
    orc.cmd_commit(orc.argparse.Namespace(
        config_dir=str(config), app="a", env="staging", sha=new,
        app_dir=str(app), catalog_ref=None,
    ))
    assert git(config, "rev-parse", f"origin/{base}") != before, "stranded commit never pushed"


# --------------------------------------------------------- cross-repo service dependencies
def write_frontend_calling(tmp_path: Path, resource: dict) -> Path:
    app = tmp_path / "frontend-repo"
    write(app / "score.yaml", {
        "apiVersion": "score.dev/v1b1",
        "metadata": {"name": "frontend"},
        "containers": {"web": {"image": ".", "variables": {
            "BACKEND_ADDR": "${resources.backend.host}:${resources.backend.port}"}}},
        "service": {"ports": {"http": {"port": 80, "targetPort": 80}}},
        "resources": {"backend": resource},
    })
    return app


def render_app(tmp_path: Path, app_dir: Path, env: str, name="frontend"):
    args = orc.argparse.Namespace(
        app=name, image=name, tag="v1", env=env, registry="r.io/p",
        catalog=str(CATALOG), app_dir=str(app_dir), work=str(tmp_path / f"w-{env}"),
        out=str(tmp_path / f"out-{env}.yaml"), kubeconfig=None,
        state_file=str(tmp_path / "st.yaml"), no_state=False, tag_strategy="commit",
    )
    orc.cmd_render(args)
    return orc.load_all(Path(args.out))


@needs_score_k8s
def test_type_service_cannot_reach_another_repo(tmp_path):
    """Documents the real limitation. `type: service` looks the peer up in the workloads of
    THIS render; a second repo is a separate render, so it simply is not there."""
    app = write_frontend_calling(tmp_path, {"type": "service"})
    with pytest.raises(subprocess.CalledProcessError):
        render_app(tmp_path, app, "staging")


@needs_score_k8s
def test_external_service_resolves_across_repos_per_environment(tmp_path):
    """The fix: no lookup at all. Kubernetes already gives every Service a stable DNS name,
    and the namespace convention lives in platform.env.yaml — so staging points at the
    other app's staging namespace and prod at its prod one, with no shared render."""
    app = write_frontend_calling(
        tmp_path, {"type": "external-service", "params": {"app": "backend", "port": 8080}})

    got = {}
    for env in ("staging", "prod"):
        docs = render_app(tmp_path, app, env)
        dep = next(d for d in docs if d["kind"] == "Deployment")
        got[env] = {e["name"]: e.get("value")
                    for e in dep["spec"]["template"]["spec"]["containers"][0]["env"]}

    assert got["staging"]["BACKEND_ADDR"] == "backend.backend-staging.svc.cluster.local:8080"
    assert got["prod"]["BACKEND_ADDR"] == "backend.backend-prod.svc.cluster.local:8080"


@needs_score_k8s
def test_external_service_requires_app_and_port(tmp_path):
    """A missing param must fail at render, not produce a half-formed hostname that only
    breaks at runtime."""
    app = write_frontend_calling(tmp_path, {"type": "external-service", "params": {"port": 8080}})
    with pytest.raises(subprocess.CalledProcessError):
        render_app(tmp_path, app, "staging")


def test_committer_identity_never_uses_github_noreply(tmp_path, repo_with_two_commits, no_branch_config, monkeypatch):
    """`<name>@users.noreply.github.com` is how GitHub maps a commit to an ACCOUNT. The
    sandbox shipped `ci-bot@users.noreply.github.com`, and because a real user named
    `ci-bot` exists, every deploy commit was credited to a stranger — quietly ruining the
    one record that answers "who deployed this"."""
    app, _old, new = repo_with_two_commits
    config = make_config_repo(tmp_path)
    monkeypatch.setattr(orc, "CONFIG", orc.EnvConfig({}))

    (config / "staging" / "manifests.yaml").write_text("rendered\n")
    orc.cmd_commit(orc.argparse.Namespace(
        config_dir=str(config), app="a", env="staging", sha=new,
        app_dir=str(app), catalog_ref=None,
    ))
    email = git(config, "log", "-1", "--format=%ae")
    assert "users.noreply.github.com" not in email, (
        f"committer email {email} would be attributed to a real GitHub account")


# ------------------------------------------------------------------ app-owned secrets
@needs_score_k8s
def test_secret_resource_becomes_a_reference_never_a_value(tmp_path):
    """Platform-generated secrets (a Postgres password) were already safe. An app's OWN
    secret — a third-party API key — had no mechanism at all, so the only option was
    writing it into score.yaml, which puts it in the app's git AND in the config repo as
    plaintext. `type: secret` closes that: score names the Secret and key, the renderer
    emits a secretKeyRef, and the value never touches the platform."""
    app = tmp_path / "app"
    write(app / "score.yaml", {
        "apiVersion": "score.dev/v1b1",
        "metadata": {"name": "payapp"},
        "containers": {"web": {"image": ".", "variables": {
            "LOG_LEVEL": "info",
            "STRIPE_API_KEY": "${resources.stripe.value}"}}},
        "service": {"ports": {"http": {"port": 80, "targetPort": 80}}},
        "resources": {"stripe": {"type": "secret",
                                 "params": {"name": "stripe-credentials", "key": "api_key"}}},
    })
    docs = render_app(tmp_path, app, "staging", name="payapp")
    dep = next(d for d in docs if d["kind"] == "Deployment")
    env = {e["name"]: e for e in dep["spec"]["template"]["spec"]["containers"][0]["env"]}

    # Non-secret config stays a plain value — it belongs in git and is reviewable.
    assert env["LOG_LEVEL"]["value"] == "info"
    # The secret is a reference, with no value anywhere in what git will hold.
    assert env["STRIPE_API_KEY"]["valueFrom"]["secretKeyRef"] == {
        "name": "stripe-credentials", "key": "api_key"}
    assert "value" not in env["STRIPE_API_KEY"]
    assert "stripe-credentials" in yaml.safe_dump(docs)   # only the NAME travels


@needs_score_k8s
def test_secret_resource_requires_name_and_key(tmp_path):
    """A missing key must fail at render. Rendering a half-formed reference would surface
    much later as a pod stuck in CreateContainerConfigError."""
    app = tmp_path / "app"
    write(app / "score.yaml", {
        "apiVersion": "score.dev/v1b1",
        "metadata": {"name": "payapp"},
        "containers": {"web": {"image": ".", "variables": {"K": "${resources.s.value}"}}},
        "service": {"ports": {"http": {"port": 80, "targetPort": 80}}},
        "resources": {"s": {"type": "secret", "params": {"name": "only-name"}}},
    })
    with pytest.raises(subprocess.CalledProcessError):
        render_app(tmp_path, app, "staging", name="payapp")
