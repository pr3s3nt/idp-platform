"""Tests for orchestrate.py.

Run from the idp repo root:  python -m pytest test_orchestrate.py -v

The state-stability tests shell out to a real score-k8s, so they need it on PATH along
with the catalog in this repo (provisioners/ + patches/).
"""
from __future__ import annotations

import json
import os
import re
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


# ------------------------------------------------------------- kiểm cụm sau khi triển khai
def deploy_doc(name: str, image: str, replicas: int = 1) -> dict:
    return {"kind": "Deployment", "metadata": {"name": name},
            "spec": {"replicas": replicas,
                     "template": {"spec": {"containers": [{"name": "c", "image": image}]}}}}


def fake_kubectl_returning(objs: dict):
    """objs: {tên deployment: object trả về} — thiếu tên nào thì coi như chưa tồn tại."""
    def _k(args, *, kubeconfig=None, **kw):
        if args[:2] == ["get", "deploy"]:
            name = args[2]
            if name not in objs:
                return subprocess.CompletedProcess(args, 1, stdout="", stderr="NotFound")
            return subprocess.CompletedProcess(args, 0, stdout=json.dumps(objs[name]), stderr="")
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
    return _k


def live(image: str, avail: int = 1, replicas: int = 1,
         updated: int | None = None, total: int | None = None,
         generation: int = 1, observed: int | None = None) -> dict:
    """Một Deployment như cụm trả về.

    Mặc định mô tả trạng thái ĐÃ TRIỂN KHAI XONG: mọi bản sao đều là bản mới. Truyền
    `updated`/`total` khi cần dựng cảnh triển khai đang dở — đó là hình dạng của một lần
    triển khai hỏng mà bản kiểm cũ không nhìn thấy.
    """
    return {"metadata": {"generation": generation},
            "spec": {"replicas": replicas,
                     "template": {"spec": {"containers": [{"image": image}]}}},
            "status": {"availableReplicas": avail,
                       "updatedReplicas": replicas if updated is None else updated,
                       "replicas": replicas if total is None else total,
                       "observedGeneration": generation if observed is None else observed}}


def test_verify_passes_when_cluster_matches(tmp_path, monkeypatch):
    m = tmp_path / "m.yaml"
    orc.dump_all([deploy_doc("app", "r.io/app:v2")], m)
    monkeypatch.setattr(orc, "kubectl", fake_kubectl_returning({"app": live("r.io/app:v2")}))
    orc.cmd_verify(orc.argparse.Namespace(
        app="app", env="staging", manifests=str(m), kubeconfig=None, timeout=1))


def test_verify_fails_when_cluster_runs_a_different_image(tmp_path, monkeypatch):
    """Đúng sự cố đã xảy ra: manifest ghi đúng, Fleet đồng bộ không lỗi, nhưng cụm chạy
    ảnh cũ vì ảnh mới chưa từng được đẩy lên. Mọi bước khác đều báo xanh."""
    m = tmp_path / "m.yaml"
    orc.dump_all([deploy_doc("app", "r.io/app:khong-ton-tai")], m)
    monkeypatch.setattr(orc, "kubectl", fake_kubectl_returning({"app": live("r.io/app:cu")}))
    with pytest.raises(SystemExit, match="KHÔNG chạy đúng thứ vừa render"):
        orc.cmd_verify(orc.argparse.Namespace(
            app="app", env="staging", manifests=str(m), kubeconfig=None, timeout=1))


def test_verify_fails_when_deployment_never_appears(tmp_path, monkeypatch):
    """Quên tạo GitRepo của Fleet: manifest nằm trong repo cấu hình mà cụm trống trơn."""
    m = tmp_path / "m.yaml"
    orc.dump_all([deploy_doc("app", "r.io/app:v2")], m)
    monkeypatch.setattr(orc, "kubectl", fake_kubectl_returning({}))
    with pytest.raises(SystemExit, match="KHÔNG chạy đúng"):
        orc.cmd_verify(orc.argparse.Namespace(
            app="app", env="staging", manifests=str(m), kubeconfig=None, timeout=1))


def test_verify_fails_while_replicas_not_available(tmp_path, monkeypatch):
    """Ảnh đúng nhưng pod chưa lên đủ — vẫn chưa được coi là thành công."""
    m = tmp_path / "m.yaml"
    orc.dump_all([deploy_doc("app", "r.io/app:v2", replicas=3)], m)
    monkeypatch.setattr(orc, "kubectl",
                        fake_kubectl_returning({"app": live("r.io/app:v2", avail=1, replicas=3)}))
    with pytest.raises(SystemExit, match="KHÔNG chạy đúng"):
        orc.cmd_verify(orc.argparse.Namespace(
            app="app", env="staging", manifests=str(m), kubeconfig=None, timeout=1))


def test_verify_fails_when_old_pods_still_serve(tmp_path, monkeypatch):
    """Bản mới không lên được nhưng bản CŨ vẫn phục vụ đủ — bản kiểm cũ báo xanh ở đây.

    Đo trực tiếp trên cụm thật: đặt nhãn ảnh không tồn tại vào production thì Kubernetes
    tạo pod mới, pod đó ImagePullBackOff, còn 3 pod cũ chạy nguyên. Deployment báo
    availableReplicas=3/3 nên phép kiểm "đủ bản sao sẵn sàng" đạt — trong khi thứ vừa
    render THỰC SỰ chưa hề chạy. Đúng loại sự cố mà cmd_verify sinh ra để bắt, và nó đã
    bỏ lọt cho tới lần thử này.
    """
    m = tmp_path / "m.yaml"
    orc.dump_all([deploy_doc("app", "r.io/app:khong-ton-tai", replicas=3)], m)
    monkeypatch.setattr(orc, "kubectl", fake_kubectl_returning({
        # Ảnh trong spec ĐÚNG bằng manifest (Fleet đã áp), 3 bản sao sẵn sàng — nhưng cả
        # 3 đều là bản cũ, chỉ 1 bản mới được tạo và nó không khởi động được.
        "app": live("r.io/app:khong-ton-tai", avail=3, replicas=3, updated=1, total=4)}))
    with pytest.raises(SystemExit, match="KHÔNG chạy đúng"):
        orc.cmd_verify(orc.argparse.Namespace(
            app="app", env="staging", manifests=str(m), kubeconfig=None, timeout=1))


def test_verify_fails_when_old_replicas_not_reclaimed(tmp_path, monkeypatch):
    """Bản mới đã đủ nhưng bản cũ chưa bị thu hồi — triển khai chưa xong."""
    m = tmp_path / "m.yaml"
    orc.dump_all([deploy_doc("app", "r.io/app:v2", replicas=2)], m)
    monkeypatch.setattr(orc, "kubectl", fake_kubectl_returning({
        "app": live("r.io/app:v2", avail=2, replicas=2, updated=2, total=3)}))
    with pytest.raises(SystemExit, match="KHÔNG chạy đúng"):
        orc.cmd_verify(orc.argparse.Namespace(
            app="app", env="staging", manifests=str(m), kubeconfig=None, timeout=1))


def test_verify_waits_until_kubernetes_has_seen_the_edit(tmp_path, monkeypatch):
    """observedGeneration còn cũ: Kubernetes chưa xử lý bản sửa, trạng thái đang nói về
    phiên bản TRƯỚC. Tin vào nó là tin một câu trả lời lỗi thời."""
    m = tmp_path / "m.yaml"
    orc.dump_all([deploy_doc("app", "r.io/app:v2")], m)
    monkeypatch.setattr(orc, "kubectl", fake_kubectl_returning({
        "app": live("r.io/app:v2", generation=7, observed=6)}))
    with pytest.raises(SystemExit, match="KHÔNG chạy đúng"):
        orc.cmd_verify(orc.argparse.Namespace(
            app="app", env="staging", manifests=str(m), kubeconfig=None, timeout=1))


def test_verify_skips_when_there_is_nothing_to_check(tmp_path, monkeypatch):
    m = tmp_path / "m.yaml"
    orc.dump_all([{"kind": "Service", "metadata": {"name": "s"}}], m)
    monkeypatch.setattr(orc, "kubectl", fake_kubectl_returning({}))
    orc.cmd_verify(orc.argparse.Namespace(
        app="app", env="staging", manifests=str(m), kubeconfig=None, timeout=1))


# ======================================================================================
# NAMESPACE — quyền hạn chế của một đội
#
# Cả hai lỗi dưới đây chỉ lộ ra khi namespace_pattern KHÁC mặc định, tức đúng lúc mang
# nền tảng vào một đội chỉ được cấp sẵn vài namespace và không có quyền tự tạo.
# ======================================================================================
def test_apply_secrets_dùng_namespace_pattern_trong_cấu_hình(tmp_path, monkeypatch):
    """apply-secrets từng ghi cứng "{app}-{env}" trong khi render và verify đọc cấu hình.

    Hậu quả khi đổi pattern: manifest vào một namespace, secret vào namespace khác.
    apply-secrets vẫn báo thành công, orchestrator vẫn xanh — chỉ pod là không kéo nổi
    ảnh vì thiếu secret kéo ảnh.
    """
    monkeypatch.setattr(orc, "CONFIG", orc.EnvConfig(
        {"kubernetes": {"namespace_pattern": "doi-thanh-toan-{env}"}}))
    seen = []

    def fake(args, *, kubeconfig=None, **kw):
        seen.append(args)
        if args[:2] == ["get", "namespace"]:
            return subprocess.CompletedProcess(args, 0, stdout="namespace/x", stderr="")
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    monkeypatch.setattr(orc, "kubectl", fake)
    empty = tmp_path / "secrets.yaml"; empty.write_text("")
    orc.cmd_apply_secrets(orc.argparse.Namespace(
        app="thanh-toan", env="staging", secrets=str(empty), kubeconfig=None,
        harbor_host="h", harbor_user="u", harbor_pass="p"))

    dùng = [a for a in seen if "doi-thanh-toan-staging" in a]
    assert dùng, f"không dùng namespace theo cấu hình; đã gọi: {seen}"
    assert not [a for a in seen if "thanh-toan-staging" in a], "vẫn còn dựng tên theo mặc định"


def test_không_gọi_create_khi_namespace_đã_tồn_tại(monkeypatch):
    """Đội không có quyền create thì Kubernetes trả Forbidden, KHÔNG phải AlreadyExists —
    vì nó kiểm quyền trước khi kiểm tồn tại. Gọi create rồi mới tha lỗi là giết cả lần
    deploy dù namespace đã nằm sẵn đó. Phải hỏi trước."""
    gọi = []

    def fake(args, *, kubeconfig=None, **kw):
        gọi.append(args)
        if args[:2] == ["get", "namespace"]:
            return subprocess.CompletedProcess(args, 0, stdout="namespace/co-san", stderr="")
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    monkeypatch.setattr(orc, "kubectl", fake)
    orc.ensure_namespace("co-san", None)
    assert not [a for a in gọi if a[:2] == ["create", "namespace"]], \
        "vẫn gọi create dù namespace đã tồn tại"


def test_thiếu_quyền_tạo_namespace_thì_hỏng_ồn_ào(monkeypatch):
    """Namespace CHƯA có và cũng không có quyền tạo: phải dừng kèm thông báo rõ, không
    được đi tiếp rồi ghi secret vào hư không."""
    def fake(args, *, kubeconfig=None, **kw):
        if args[:2] == ["get", "namespace"]:
            return subprocess.CompletedProcess(args, 1, stdout="", stderr="NotFound")
        return subprocess.CompletedProcess(
            args, 1, stdout="",
            stderr='namespaces is forbidden: User "u" cannot create resource "namespaces"')

    monkeypatch.setattr(orc, "kubectl", fake)
    with pytest.raises(SystemExit, match="forbidden"):
        orc.ensure_namespace("chua-co", None)


# ======================================================================================
# ĐĂNG KÝ VỚI FLEET — fleet.yaml và GitRepo
# Hai thứ này thiếu thì mọi bước đều xanh mà cụm trống trơn. Gặp thật khi triển khai
# ở công ty, mất một buổi mới truy ra.
# ======================================================================================
def test_sinh_fleet_yaml_khi_chưa_có(tmp_path, monkeypatch):
    monkeypatch.setattr(orc, "CONFIG", orc.EnvConfig({}))
    d = tmp_path / "staging"; d.mkdir()
    orc.ensure_fleet_yaml(d, "thanh-toan", "staging")
    got = yaml.safe_load((d / "fleet.yaml").read_text())
    assert got == {"namespace": "thanh-toan-staging",
                   "defaultNamespace": "thanh-toan-staging"}


def test_không_ghi_đè_fleet_yaml_người_dùng_đã_sửa(tmp_path, monkeypatch):
    """Ai muốn tuỳ biến Bundle thì vẫn tuỳ biến được — platform không giẫm lên."""
    monkeypatch.setattr(orc, "CONFIG", orc.EnvConfig({}))
    d = tmp_path / "staging"; d.mkdir()
    (d / "fleet.yaml").write_text("namespace: tôi-tự-đặt\n")
    orc.ensure_fleet_yaml(d, "thanh-toan", "staging")
    assert "tôi-tự-đặt" in (d / "fleet.yaml").read_text()


def test_fleet_yaml_theo_namespace_pattern(tmp_path, monkeypatch):
    monkeypatch.setattr(orc, "CONFIG", orc.EnvConfig(
        {"kubernetes": {"namespace_pattern": "doi-abc-{env}"}}))
    d = tmp_path / "prod"; d.mkdir()
    orc.ensure_fleet_yaml(d, "thanh-toan", "prod")
    assert yaml.safe_load((d / "fleet.yaml").read_text())["defaultNamespace"] == "doi-abc-prod"


def _repo_gia(tmp_path, url="https://git.vi-du.vn/to-chuc/app-config"):
    r = tmp_path / "config"; r.mkdir()
    git(r, "init", "-q"); git(r, "remote", "add", "origin", url)
    return r


def test_tạo_gitrepo_khi_chưa_có(tmp_path, monkeypatch):
    monkeypatch.setattr(orc, "CONFIG", orc.EnvConfig(
        {"environments": {"staging": {"config_branch": "dev"}}}))
    r = _repo_gia(tmp_path); gọi = []

    def fake(args, *, kubeconfig=None, **kw):
        gọi.append(args)
        if args[:2] == ["get", "gitrepo"]:
            return subprocess.CompletedProcess(args, 1, stdout="", stderr="NotFound")
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    monkeypatch.setattr(orc, "kubectl", fake)
    orc.cmd_ensure_gitrepo(orc.argparse.Namespace(
        app="app", env="staging", config_dir=str(r), kubeconfig=None, work=str(tmp_path)))

    tạo = [a for a in gọi if a[0] == "create"]
    assert tạo, f"không gọi create; đã gọi: {gọi}"
    body = json.loads(Path(tạo[0][2]).read_text())
    assert body["metadata"]["name"] == "app-staging"          # tên KÈM môi trường
    assert body["spec"]["branch"] == "dev"
    assert body["spec"]["paths"] == ["staging"]
    assert body["spec"]["repo"] == "https://git.vi-du.vn/to-chuc/app-config"


def test_gitrepo_đã_có_trỏ_đúng_thì_để_yên(tmp_path, monkeypatch):
    monkeypatch.setattr(orc, "CONFIG", orc.EnvConfig({}))
    r = _repo_gia(tmp_path); gọi = []

    def fake(args, *, kubeconfig=None, **kw):
        gọi.append(args)
        if args[:2] == ["get", "gitrepo"]:
            return subprocess.CompletedProcess(args, 0, stdout=json.dumps(
                {"spec": {"repo": "https://git.vi-du.vn/to-chuc/app-config.git"}}), stderr="")
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    monkeypatch.setattr(orc, "kubectl", fake)
    orc.cmd_ensure_gitrepo(orc.argparse.Namespace(
        app="app", env="staging", config_dir=str(r), kubeconfig=None, work=str(tmp_path)))
    assert not [a for a in gọi if a[0] == "create"], "không được tạo lại"


def test_gitrepo_trùng_tên_trỏ_kho_khác_thì_DỪNG(tmp_path, monkeypatch):
    """Cụm thường đã có GitRepo của đội khác. Đè lên là ứng dụng của họ ngừng đồng bộ
    trong im lặng — phải dừng và báo, tuyệt đối không apply."""
    monkeypatch.setattr(orc, "CONFIG", orc.EnvConfig({}))
    r = _repo_gia(tmp_path); gọi = []

    def fake(args, *, kubeconfig=None, **kw):
        gọi.append(args)
        if args[:2] == ["get", "gitrepo"]:
            return subprocess.CompletedProcess(args, 0, stdout=json.dumps(
                {"spec": {"repo": "https://git.vi-du.vn/doi-khac/app-cua-ho"}}), stderr="")
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    monkeypatch.setattr(orc, "kubectl", fake)
    with pytest.raises(SystemExit, match="của ứng dụng khác|không phải"):
        orc.cmd_ensure_gitrepo(orc.argparse.Namespace(
            app="app", env="staging", config_dir=str(r), kubeconfig=None, work=str(tmp_path)))
    assert not [a for a in gọi if a[0] == "create"], "đã dừng thì tuyệt đối không tạo"


def test_gỡ_token_nhúng_trong_remote(tmp_path, monkeypatch):
    """Runner hay để remote dạng https://<token>@host/... — token đó KHÔNG được lọt vào
    GitRepo, vì nó nằm trong một tài nguyên ai đọc được cụm cũng xem được."""
    monkeypatch.setattr(orc, "CONFIG", orc.EnvConfig({}))
    r = _repo_gia(tmp_path, "https://ghp_bimat123@git.vi-du.vn/to-chuc/app-config.git")

    def fake(args, *, kubeconfig=None, **kw):
        if args[:2] == ["get", "gitrepo"]:
            return subprocess.CompletedProcess(args, 1, stdout="", stderr="NotFound")
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    monkeypatch.setattr(orc, "kubectl", fake)
    orc.cmd_ensure_gitrepo(orc.argparse.Namespace(
        app="app", env="staging", config_dir=str(r), kubeconfig=None, work=str(tmp_path)))
    body = json.loads((tmp_path / "gitrepo-app-staging.json").read_text())
    assert "ghp_" not in body["spec"]["repo"]
    assert body["spec"]["repo"] == "https://git.vi-du.vn/to-chuc/app-config"


def test_không_tạo_thêm_khi_kho_đã_đăng_ký_dưới_tên_khác(tmp_path, monkeypatch):
    """Bản cài cũ đặt tên GitRepo không kèm môi trường. Tạo thêm cái nữa thì hai GitRepo
    cùng đồng bộ một thư mục, sinh hai Bundle chồng nhau."""
    monkeypatch.setattr(orc, "CONFIG", orc.EnvConfig({}))
    r = _repo_gia(tmp_path); gọi = []

    def fake(args, *, kubeconfig=None, **kw):
        gọi.append(args)
        if args[:3] == ["get", "gitrepo", "app-staging"]:
            return subprocess.CompletedProcess(args, 1, stdout="", stderr="NotFound")
        if args[:2] == ["get", "gitrepo"]:          # liệt kê cả namespace
            return subprocess.CompletedProcess(args, 0, stdout=json.dumps({"items": [
                {"metadata": {"name": "app"},        # tên cũ, không kèm môi trường
                 "spec": {"repo": "https://git.vi-du.vn/to-chuc/app-config",
                          "paths": ["staging"]}}]}), stderr="")
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    monkeypatch.setattr(orc, "kubectl", fake)
    orc.cmd_ensure_gitrepo(orc.argparse.Namespace(
        app="app", env="staging", config_dir=str(r), kubeconfig=None, work=str(tmp_path)))
    assert not [a for a in gọi if a[0] == "create"], "không được tạo trùng"


def _fake_kubectl_gitrepo(items, monkeypatch, gọi):
    def fake(args, *, kubeconfig=None, **kw):
        gọi.append(args)
        if len(args) > 2 and args[:2] == ["get", "gitrepo"] and not args[2].startswith("-"):
            return subprocess.CompletedProcess(args, 1, stdout="", stderr="NotFound")
        if args[:2] == ["get", "gitrepo"]:
            return subprocess.CompletedProcess(args, 0,
                                               stdout=json.dumps({"items": items}), stderr="")
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
    monkeypatch.setattr(orc, "kubectl", fake)


def test_học_clientSecretName_theo_gitrepo_đang_chạy(tmp_path, monkeypatch):
    """Cụm công ty đã có Fleet chạy sẵn với cách xác thực riêng. Áp đặt tên secret mặc
    định là làm hỏng đúng thứ đang chạy được — Fleet không clone nổi, lỗi nằm trong status
    của GitRepo không ai nhìn, và triệu chứng y hệt 'quên tạo GitRepo'."""
    monkeypatch.setattr(orc, "CONFIG", orc.EnvConfig(
        {"kubernetes": {"fleet_git_secret": ""}}))
    r = _repo_gia(tmp_path); gọi = []
    _fake_kubectl_gitrepo([{"metadata": {"name": "app-cua-doi-khac"},
                            "spec": {"repo": "https://git.vi-du.vn/khac/kho",
                                     "paths": ["staging"],
                                     "clientSecretName": "creds-cua-cum"}}], monkeypatch, gọi)
    orc.cmd_ensure_gitrepo(orc.argparse.Namespace(
        app="app", env="staging", config_dir=str(r), kubeconfig=None, work=str(tmp_path)))
    body = json.loads((tmp_path / "gitrepo-app-staging.json").read_text())
    assert body["spec"]["clientSecretName"] == "creds-cua-cum"


def test_bỏ_hẳn_clientSecretName_khi_không_có_gì_để_học(tmp_path, monkeypatch):
    """Không khai, không học được thì BỎ TRỐNG — để Fleet tự xoay như nó vẫn làm với kho
    công khai. Khai bừa một tên không tồn tại còn tệ hơn không khai."""
    monkeypatch.setattr(orc, "CONFIG", orc.EnvConfig(
        {"kubernetes": {"fleet_git_secret": ""}}))
    r = _repo_gia(tmp_path); gọi = []
    _fake_kubectl_gitrepo([], monkeypatch, gọi)
    orc.cmd_ensure_gitrepo(orc.argparse.Namespace(
        app="app", env="staging", config_dir=str(r), kubeconfig=None, work=str(tmp_path)))
    body = json.loads((tmp_path / "gitrepo-app-staging.json").read_text())
    assert "clientSecretName" not in body["spec"]


def test_cấu_hình_thắng_việc_học(tmp_path, monkeypatch):
    monkeypatch.setattr(orc, "CONFIG", orc.EnvConfig(
        {"kubernetes": {"fleet_git_secret": "toi-tu-khai"}}))
    r = _repo_gia(tmp_path); gọi = []
    _fake_kubectl_gitrepo([{"metadata": {"name": "khac"},
                            "spec": {"repo": "https://git.vi-du.vn/khac/kho",
                                     "paths": ["staging"],
                                     "clientSecretName": "creds-cua-cum"}}], monkeypatch, gọi)
    orc.cmd_ensure_gitrepo(orc.argparse.Namespace(
        app="app", env="staging", config_dir=str(r), kubeconfig=None, work=str(tmp_path)))
    body = json.loads((tmp_path / "gitrepo-app-staging.json").read_text())
    assert body["spec"]["clientSecretName"] == "toi-tu-khai"


def test_kho_riêng_tư_không_có_credential_thì_DỪNG(tmp_path, monkeypatch):
    """Kho riêng tư + không tìm được thông tin đăng nhập = Fleet clone ẩn danh và hỏng
    CHẮC CHẮN với 'Anonymous access denied'. Nhưng nó hỏng trong status của GitRepo, không
    ai nhìn, và triệu chứng y hệt 'quên tạo GitRepo'. Gặp thật ở công ty."""
    monkeypatch.setattr(orc, "CONFIG", orc.EnvConfig(
        {"kubernetes": {"fleet_git_secret": ""}}))
    monkeypatch.setattr(orc, "repo_is_private", lambda url: True)
    r = _repo_gia(tmp_path); gọi = []
    _fake_kubectl_gitrepo([], monkeypatch, gọi)
    with pytest.raises(SystemExit, match="Anonymous access denied|RIÊNG TƯ"):
        orc.cmd_ensure_gitrepo(orc.argparse.Namespace(
            app="app", env="staging", config_dir=str(r), kubeconfig=None, work=str(tmp_path)))
    assert not [a for a in gọi if a[0] == "create"], "đã dừng thì không được tạo"


def test_kho_công_khai_không_có_credential_vẫn_tạo(tmp_path, monkeypatch):
    """Kho công khai thì clone ẩn danh chạy được — không có lý do gì để chặn."""
    monkeypatch.setattr(orc, "CONFIG", orc.EnvConfig(
        {"kubernetes": {"fleet_git_secret": ""}}))
    monkeypatch.setattr(orc, "repo_is_private", lambda url: False)
    r = _repo_gia(tmp_path); gọi = []
    _fake_kubectl_gitrepo([], monkeypatch, gọi)
    orc.cmd_ensure_gitrepo(orc.argparse.Namespace(
        app="app", env="staging", config_dir=str(r), kubeconfig=None, work=str(tmp_path)))
    body = json.loads((tmp_path / "gitrepo-app-staging.json").read_text())
    assert "clientSecretName" not in body["spec"]


# =====================================================================================
# PHASE 0 — naming, path and digest contracts
# =====================================================================================
# These functions decide the NAME of live Kubernetes objects and the PATH a secret is read
# from. A change to any of them renames a running resource or moves a secret out from under
# a running app, so they are pinned by test rather than left to whoever refactors next.

# ------------------------------------------------------------------------- vault paths
def test_vault_path_is_derived_from_config_not_from_the_app():
    """The app supplies `name` and nothing else; mount and layout come from platform config.

    Read the other way round: there is no app-supplied input that can change which prefix
    the path lands under, which is what makes the per-app Vault policy enforceable.
    """
    assert orc.vault_path("payment-api", "staging", "stripe") == "kv/apps/payment-api/staging/stripe"
    assert orc.vault_path("payment-api", "prod", "stripe") == "kv/apps/payment-api/prod/stripe"


def test_vault_path_follows_a_relocated_mount(monkeypatch):
    """Company Vault will not have a mount called `kv`. Moving it must be a config edit."""
    monkeypatch.setattr(orc, "CONFIG", orc.EnvConfig({"vault": {
        "kv_mount": "secret/platform",
        "path_template": "teams/{application}/{environment}/{name}",
    }}))
    assert orc.vault_path("order", "prod", "db") == "secret/platform/teams/order/prod/db"


def test_vault_path_carries_no_kv_v2_data_infix():
    """VSO inserts /data/ itself for kv-v2. Baking it in here double-prefixes on v2 and
    breaks v1 outright — and the failure surfaces as 'permission denied', which reads like
    a policy problem and sends you looking in the wrong place entirely."""
    assert "/data/" not in orc.vault_path("app", "staging", "creds")


@pytest.mark.parametrize("bad", [
    "../admin", "a/b", "stripe/../../root", "-leading", "trailing-",
    "", "UPPER", "sp ace", "a" * 64, "dots.not.allowed",
])
def test_vault_path_refuses_a_name_that_could_move_the_path(bad):
    """`name` is the ONLY app-controlled path segment, so it is the whole attack surface."""
    with pytest.raises(SystemExit, match="invalid secret name"):
        orc.vault_path("app", "staging", bad)


def test_vault_path_refuses_production_as_an_alias_for_prod():
    """Two spellings for one environment is how a values block silently never applies."""
    with pytest.raises(SystemExit, match="unknown environment"):
        orc.vault_path("app", "production", "stripe")


def test_unknown_template_placeholder_is_fatal(monkeypatch):
    monkeypatch.setattr(orc, "CONFIG", orc.EnvConfig(
        {"vault": {"path_template": "apps/{application}/{tenant}/{name}"}}))
    with pytest.raises(SystemExit, match="unknown placeholder"):
        orc.vault_path("app", "staging", "stripe")


# --------------------------------------------------------------- generated object names
def test_resource_name_is_readable_and_dns_safe():
    name = orc.resource_name("payment-api", "staging", "web", "stripe")
    assert orc.DNS_LABEL.match(name), name
    assert name.startswith("idp-payment-api-staging-web-stripe-")
    assert len(name) <= 63


def test_resource_name_fits_in_a_label_even_for_absurd_inputs():
    """63 characters is a hard Kubernetes limit. Exceeding it is an apply-time rejection
    for an object the renderer already promised, i.e. a green render and a dead deploy."""
    name = orc.resource_name("a" * 80, "staging", "b" * 80, "c" * 80)
    assert len(name) <= 63 and orc.DNS_LABEL.match(name), name


def test_truncated_names_still_separate():
    """The readable part of these two collides after truncation; the hash is over the FULL
    tuple, so the names must still differ. Without this, two apps share one Secret."""
    a = orc.resource_name("x" * 70, "staging", "web", "alpha")
    b = orc.resource_name("x" * 70, "staging", "web", "beta")
    assert a[:40] == b[:40] and a != b


def test_resource_name_component_boundaries_are_not_ambiguous():
    """('a-b','c') and ('a','b-c') slug to the same string; the separator must keep them
    apart or two different resources render to one name."""
    assert orc.resource_name("a-b", "c") != orc.resource_name("a", "b-c")


def test_resource_name_is_stable_across_processes():
    """Python's hash() is salted per process, so using it would rename every generated
    object on every render — the exact churn the state store exists to prevent, sneaking
    back in through the naming function."""
    import sys as _sys
    code = ("import orchestrate as o;"
            "print(o.resource_name('payment-api','staging','web','stripe'))")
    runs = {
        subprocess.run([_sys.executable, "-c", code], cwd=CATALOG, text=True,
                       capture_output=True, check=True,
                       env={**os.environ, "PYTHONHASHSEED": seed}).stdout.strip()
        for seed in ("0", "1", "12345")
    }
    assert len(runs) == 1, f"name is not process-stable: {runs}"
    assert runs.pop() == orc.resource_name("payment-api", "staging", "web", "stripe")


# --------------------------------------------------------------------- promotion digest
PROD_VALUES = {
    "application": {"LOG_LEVEL": "info", "FEATURE_X": "false"},
    "environments": {
        "staging": {"LOG_LEVEL": "debug"},
        "prod": {"PUBLIC_HOST": "payment-api.internal",
                 "STRIPE_KEY": {"secretRef": {"name": "stripe", "key": "api_key"}}},
    },
}


def test_prod_digest_ignores_key_order_and_comments():
    """Digest describes the DATA. Re-indenting a YAML file or sorting its keys is not a
    configuration change and must not make a promotion fail."""
    reordered = {
        "environments": {
            "prod": {"STRIPE_KEY": {"secretRef": {"key": "api_key", "name": "stripe"}},
                     "PUBLIC_HOST": "payment-api.internal"},
            "staging": {"LOG_LEVEL": "debug"},
        },
        "application": {"FEATURE_X": "false", "LOG_LEVEL": "info"},
    }
    assert orc.values_digest(PROD_VALUES) == orc.values_digest(reordered)


def test_prod_digest_is_blind_to_staging_only_changes():
    """Otherwise every staging tweak invalidates the prod promotion record, the guard cries
    wolf, and the first thing anyone does is stop trusting it."""
    changed = json.loads(json.dumps(PROD_VALUES))
    changed["environments"]["staging"]["LOG_LEVEL"] = "trace"
    assert orc.values_digest(PROD_VALUES) == orc.values_digest(changed)


def test_prod_digest_moves_when_prod_literal_changes():
    changed = json.loads(json.dumps(PROD_VALUES))
    changed["environments"]["prod"]["PUBLIC_HOST"] = "elsewhere.internal"
    assert orc.values_digest(PROD_VALUES) != orc.values_digest(changed)


def test_prod_digest_moves_when_the_shared_application_block_changes():
    """`application` feeds prod through precedence, so it is part of prod's inputs."""
    changed = json.loads(json.dumps(PROD_VALUES))
    changed["application"]["LOG_LEVEL"] = "warn"
    assert orc.values_digest(PROD_VALUES) != orc.values_digest(changed)


def test_prod_digest_moves_when_a_secret_is_repointed():
    """Same variable, different Vault key. No literal changed, but production would read a
    different secret — that has to count as a change."""
    changed = json.loads(json.dumps(PROD_VALUES))
    changed["environments"]["prod"]["STRIPE_KEY"]["secretRef"]["key"] = "restricted_key"
    assert orc.values_digest(PROD_VALUES) != orc.values_digest(changed)


def test_digest_of_absent_values_is_well_defined():
    """Apps with no values file must not crash the promotion guard."""
    assert orc.values_digest({}) == orc.values_digest({"environments": {}})


# =====================================================================================
# PHASE 0 — toolchain pinning
# =====================================================================================
def test_tool_version_parses_the_real_binary():
    """Guards the regex against the go version on the same line: score-k8s prints
    'score-k8s 0.15.0 (go1.26.4 - linux/amd64)' and 1.26.4 must not win."""
    if not HAS_SCORE_K8S:
        pytest.skip("score-k8s not installed")
    version = orc.tool_version("score-k8s")
    assert version and re.match(r"^\d+\.\d+\.\d+", version)
    assert not version.startswith("1.26"), "picked up the Go toolchain version"


def test_version_mismatch_is_fatal(monkeypatch):
    monkeypatch.setattr(orc, "CONFIG", orc.EnvConfig({"ci": {"score_k8s_version": "9.9.9"}}))
    monkeypatch.setattr(orc, "tool_version", lambda t: "0.15.0")
    monkeypatch.setattr(orc, "_version_checked", set())
    with pytest.raises(SystemExit, match="version mismatch"):
        orc.check_tool_versions(["score-k8s"])


def test_matching_version_passes(monkeypatch):
    monkeypatch.setattr(orc, "CONFIG", orc.EnvConfig({"ci": {"score_k8s_version": "0.15.0"}}))
    monkeypatch.setattr(orc, "tool_version", lambda t: "0.15.0")
    monkeypatch.setattr(orc, "_version_checked", set())
    orc.check_tool_versions(["score-k8s"])


def test_empty_pin_skips_the_check(monkeypatch):
    """The brownfield on-ramp. Runners that predate this feature must keep deploying."""
    monkeypatch.setattr(orc, "CONFIG", orc.EnvConfig({"ci": {"score_k8s_version": ""}}))
    monkeypatch.setattr(orc, "tool_version", lambda t: pytest.fail("must not be consulted"))
    monkeypatch.setattr(orc, "_version_checked", set())
    orc.check_tool_versions(["score-k8s"])


def test_pinned_but_missing_binary_is_fatal(monkeypatch):
    """A pin plus an unreadable version is not 'probably fine' — it is unknown, and the
    whole point of pinning is that unknown is not good enough."""
    monkeypatch.setattr(orc, "CONFIG", orc.EnvConfig({"ci": {"score_k8s_version": "0.15.0"}}))
    monkeypatch.setattr(orc, "tool_version", lambda t: None)
    monkeypatch.setattr(orc, "_version_checked", set())
    with pytest.raises(SystemExit, match="could not be determined"):
        orc.check_tool_versions(["score-k8s"])


@needs_score_k8s
def test_render_refuses_a_mismatched_binary_before_touching_the_catalog(tmp_path, monkeypatch):
    """The Phase 0 gate: a wrong binary fails BEFORE render, not after Fleet applied."""
    monkeypatch.setattr(orc, "CONFIG", orc.EnvConfig({"ci": {"score_k8s_version": "0.0.1-nope"}}))
    monkeypatch.setattr(orc, "_version_checked", set())
    app_dir = tmp_path / "app"
    write(app_dir / "score.yaml", score_spec("web"))
    work = tmp_path / "work"
    with pytest.raises(SystemExit, match="version mismatch"):
        orc.cmd_render(orc.argparse.Namespace(
            app="web", image="web", tag="sha1", env="staging", registry="h.io/p",
            catalog=str(CATALOG), app_dir=str(app_dir), work=str(work),
            out=str(tmp_path / "out.yaml"), kubeconfig=None,
            state_file=str(tmp_path / "state.yaml"), no_state=False, tag_strategy="commit"))
    assert not (work / "manifests.yaml").exists(), "render started despite the mismatch"


# =====================================================================================
# PHASE 0 — feature flags default off
# =====================================================================================
def test_every_new_capability_is_off_by_default():
    """A platform that already deploys real apps must not gain behaviour by upgrading."""
    fresh = orc.EnvConfig({})
    assert not any(fresh.get(f"features.{n}") for n in (
        "application_values", "vault_secrets", "postgres_application", "stack_onboarding"))


def test_the_repos_own_config_ships_with_every_feature_off():
    """platform.env.yaml is what the sandbox deploys with. If a flag lands on by accident,
    the 'off by default' promise is only true for installs that have no config file."""
    shipped = orc.EnvConfig.load(str(CATALOG / "platform.env.yaml"))
    for name in ("application_values", "vault_secrets", "postgres_application",
                 "stack_onboarding"):
        assert shipped.get(f"features.{name}") is False, name


def test_company_config_also_ships_with_every_feature_off():
    shipped = orc.EnvConfig.load(str(CATALOG / "platform.env.company.yaml"))
    for name in ("application_values", "vault_secrets", "postgres_application",
                 "stack_onboarding"):
        assert shipped.get(f"features.{name}") is False, name


# =====================================================================================
# PHASE 0 — integration gates against the pinned binary
# =====================================================================================
@needs_score_k8s
def test_placeholders_resolve_inside_resource_params(tmp_path):
    """The gate the node-fullstack golden path depends on.

    Same-origin routing means the route provisioner needs its hostname from an environment
    resource: `params.host: "${resources.hostname.host}"`. score-k8s substitutes inside
    `variables` and file contents for certain, but `resources.*.params` is a different code
    path — and if it does NOT substitute, the HTTPRoute is created with the literal string
    '${resources.hostname.host}' as its hostname. That attaches to the gateway, reports
    healthy, and is simply never routed to. Verified here against the pinned binary before
    any template ships that assumes it works.
    """
    app_dir = tmp_path / "app"
    write(app_dir / "score.yaml", {
        "apiVersion": "score.dev/v1b1",
        "metadata": {"name": "web"},
        "containers": {"main": {"image": "."}},
        "service": {"ports": {"http": {"port": 8080, "targetPort": 8080}}},
        "resources": {
            "hostname": {"type": "dns"},
            "route": {"type": "route",
                      "params": {"host": "${resources.hostname.host}", "port": 8080,
                                 "path": "/api"}},
        },
    })
    args = orc.argparse.Namespace(
        app="web", image="web", tag="sha1", env="staging", registry="h.io/p",
        catalog=str(CATALOG), app_dir=str(app_dir), work=str(tmp_path / "work"),
        out=str(tmp_path / "out.yaml"), kubeconfig=None,
        state_file=str(tmp_path / "state.yaml"), no_state=False, tag_strategy="commit")
    orc.cmd_render(args)

    route = next(d for d in orc.load_all(Path(args.work) / "manifests.yaml")
                 if d["kind"] == "HTTPRoute")
    hostnames = route["spec"]["hostnames"]
    assert not any("${" in h for h in hostnames), f"placeholder left unresolved: {hostnames}"
    # The dns provisioner builds <workload>.<env.domain> from platform.env.yaml.
    assert hostnames == ["web." + orc.CONFIG.get("environments.staging.domain")]
    assert route["spec"]["rules"][0]["matches"][0]["path"]["value"] == "/api"


@needs_score_k8s
def test_two_renders_of_one_input_are_byte_identical(tmp_path):
    """Determinism gate. Not just 'the names are stable' — the whole published manifest.

    This is what makes a re-render safe to diff against what is already in the config repo.
    If rendering twice produces two different files, every deploy shows a diff, real changes
    stop standing out, and reviewing the config repo becomes theatre.
    """
    app_dir = tmp_path / "app"
    app_dir.mkdir()
    for f in (CATALOG / "examples" / "microservices").glob("score-*.yaml"):
        shutil.copyfile(f, app_dir / f.name)
    state = tmp_path / "state.yaml"

    def render(tag: str) -> bytes:
        args = orc.argparse.Namespace(
            app="boutique", image="boutique", tag="abc123", env="staging",
            registry="h.io/p", catalog=str(CATALOG), app_dir=str(app_dir),
            work=str(tmp_path / tag), out=str(tmp_path / f"{tag}.yaml"),
            kubeconfig=None, state_file=str(state), no_state=False, tag_strategy="commit")
        orc.cmd_render(args)
        return Path(args.out).read_bytes()

    assert render("run1") == render("run2")
