"""Tests for the IDP engine.

Run from the idp repo root:  python -m pytest test_engine.py -v

The state-stability tests shell out to a real score-k8s, so they need it on PATH along
with the catalog in this repo (provisioners/ + patches/).
"""
from __future__ import annotations

import argparse
import base64
import copy
import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

import engine as orc

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
    code = ("import engine as o;"
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


def test_the_harness_profile_explicitly_enables_runtime_capabilities():
    """Defaults remain off, while the harness opts in so its E2E tests exercise features."""
    shipped = orc.EnvConfig.load(str(CATALOG / "platform.env.yaml"))
    for name in ("application_values", "vault_secrets", "postgres_application",
                 "stack_onboarding"):
        assert shipped.get(f"features.{name}") is True, name


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


# =====================================================================================
# PHASE 1 — ApplicationValues: schema and precedence
# =====================================================================================
def values_doc(application=None, staging=None, prod=None) -> dict:
    spec = {}
    if application is not None:
        spec["application"] = application
    envs = {k: v for k, v in (("staging", staging), ("prod", prod)) if v is not None}
    if envs:
        spec["environments"] = envs
    return {"apiVersion": "idp.company/v1", "kind": "ApplicationValues", "spec": spec}


def check(doc) -> dict:
    return orc.validate_application_values(doc, ".score-values/values.yaml")


@pytest.fixture
def values_enabled(monkeypatch):
    """The repo's real config with features.application_values switched on."""
    data = json.loads(json.dumps(orc.CONFIG.data))
    data.setdefault("features", {})["application_values"] = True
    monkeypatch.setattr(orc, "CONFIG", orc.EnvConfig(data))


def test_environment_overrides_application():
    spec = check(values_doc(application={"LOG_LEVEL": "info", "FEATURE_X": "false"},
                            staging={"LOG_LEVEL": "debug"}))
    assert orc.resolve_application_values(spec, "staging") == {
        "LOG_LEVEL": "debug", "FEATURE_X": "false"}
    assert orc.resolve_application_values(spec, "prod") == {
        "LOG_LEVEL": "info", "FEATURE_X": "false"}


def test_an_environment_with_no_block_still_gets_the_shared_values():
    spec = check(values_doc(application={"LOG_LEVEL": "info"}, staging={"LOG_LEVEL": "debug"}))
    assert orc.resolve_application_values(spec, "prod")["LOG_LEVEL"] == "info"


@pytest.mark.parametrize("bad_value", [False, True, 8080, 1.10, None, ["a"]])
def test_non_string_literals_are_refused_with_the_quoting_fix(bad_value):
    """YAML types `false`, `8080` and `yes` for you. Environment variables are strings, and
    str()-ing them quietly turns 1.10 into '1.1'."""
    with pytest.raises(SystemExit, match="not a string|quote it"):
        check(values_doc(application={"FEATURE_X": bad_value}))


def test_yaml_reads_unquoted_no_as_a_boolean_and_we_refuse_it():
    """The specific trap the docs warn about: yes/no/on/off are YAML 1.1 booleans."""
    doc = yaml.safe_load("""
apiVersion: idp.company/v1
kind: ApplicationValues
spec:
  application:
    ENABLED: no
""")
    assert doc["spec"]["application"]["ENABLED"] is False
    with pytest.raises(SystemExit, match="not a string"):
        check(doc)


def test_quoted_no_is_accepted_as_a_string():
    spec = check(yaml.safe_load("""
apiVersion: idp.company/v1
kind: ApplicationValues
spec:
  application:
    ENABLED: "no"
"""))
    assert orc.resolve_application_values(spec, "staging") == {"ENABLED": "no"}


def test_unknown_environment_is_refused():
    with pytest.raises(SystemExit, match="production"):
        check(values_doc(prod={"A": "b"}) | {"spec": {"environments": {"production": {}}}})


def test_wrong_apiversion_or_kind_is_refused():
    with pytest.raises(SystemExit, match="apiVersion"):
        check(values_doc(application={"A": "b"}) | {"apiVersion": "v1"})
    with pytest.raises(SystemExit, match="kind"):
        check(values_doc(application={"A": "b"}) | {"kind": "ConfigMap"})


def test_unknown_top_level_field_is_refused():
    """A typo'd `enviroments:` would otherwise apply to nothing and report nothing."""
    with pytest.raises(SystemExit, match="unknown field"):
        check({"apiVersion": "idp.company/v1", "kind": "ApplicationValues",
               "spec": {"enviroments": {"staging": {}}}})


# ------------------------------------------------------------------------- secretRef shape
SECRET = {"secretRef": {"name": "stripe", "key": "api_key"}}


def test_secret_ref_is_accepted():
    spec = check(values_doc(staging={"STRIPE_KEY": SECRET}))
    assert orc.resolve_application_values(spec, "staging")["STRIPE_KEY"] == SECRET


def test_secret_ref_rejects_an_app_supplied_vault_path():
    """The whole per-app policy rests on the app being unable to name its own prefix."""
    with pytest.raises(SystemExit, match="unknown field"):
        check(values_doc(staging={"K": {"secretRef": {
            "name": "stripe", "key": "api_key", "path": "kv/apps/other-app/prod"}}}))


def test_secret_ref_requires_both_name_and_key():
    with pytest.raises(SystemExit, match="missing"):
        check(values_doc(staging={"K": {"secretRef": {"name": "stripe"}}}))


def test_secret_ref_name_goes_through_the_path_validator():
    with pytest.raises(SystemExit, match="invalid secret name"):
        check(values_doc(staging={"K": {"secretRef": {"name": "../admin", "key": "k"}}}))


def test_a_key_cannot_be_literal_in_one_environment_and_secret_in_another():
    """Otherwise staging renders `env.value` and prod renders `secretKeyRef` from one Score
    file — and whatever staging proved says nothing about prod."""
    with pytest.raises(SystemExit, match="same kind"):
        check(values_doc(staging={"K": "plain"}, prod={"K": SECRET}))


def test_a_key_cannot_be_shared_literal_and_environment_secret():
    with pytest.raises(SystemExit, match="same kind"):
        check(values_doc(application={"K": "plain"}, prod={"K": SECRET}))


# =====================================================================================
# PHASE 1 — environment resource alias discovery
# =====================================================================================
def score_with_resources(resources: dict) -> dict:
    return {"apiVersion": "score.dev/v1b1", "metadata": {"name": "web"},
            "containers": {"main": {"image": "."}}, "resources": resources}


def test_alias_is_discovered_not_assumed_to_be_env():
    """Hardcoding `env` is the same bug as hardcoding the container name `main`: it works
    for everyone who copied the example and no-ops for everyone who did not."""
    doc = score_with_resources({"app-config": {"type": "environment"}})
    assert orc.environment_alias(doc, where="score.yaml") == "app-config"


def test_no_environment_resource_is_fine():
    assert orc.environment_alias(score_with_resources({"db": {"type": "postgres"}}),
                                 where="score.yaml") is None
    assert orc.environment_alias({}, where="score.yaml") is None


def test_two_environment_resources_fail_early():
    """With two, which one supplies a given key is undefined — and score would pick one."""
    doc = score_with_resources({"a": {"type": "environment"}, "b": {"type": "environment"}})
    with pytest.raises(SystemExit, match="at most one"):
        orc.environment_alias(doc, where="score.yaml")


# =====================================================================================
# PHASE 1 — placeholder allowlist
# =====================================================================================
@pytest.mark.parametrize("path,expected", [
    (("containers", "main", "variables", "LOG_LEVEL"), "variables"),
    (("containers", "main", "files", "/etc/a.yaml", "content"), "file"),
    (("containers", "main", "files", 0, "content"), "file"),
    (("containers", "main", "volumes", 0, "source"), "volume-source"),
    (("resources", "route", "params", "host"), "params"),
    (("resources", "route", "params", "nested", "deep"), "params"),
    (("containers", "main", "command", 0), None),
    (("containers", "main", "args", 1), None),
    (("containers", "main", "image"), None),
    (("containers", "main", "livenessProbe", "httpGet", "path"), None),
    (("metadata", "annotations", "x"), None),
])
def test_placeholder_positions(path, expected):
    assert orc.placeholder_position(path) == expected


@pytest.mark.parametrize("where,score", [
    ("command", {"containers": {"main": {"image": ".", "command": ["/app", "--log=${resources.cfg.LOG_LEVEL}"]}}}),
    ("args", {"containers": {"main": {"image": ".", "args": ["${resources.cfg.PORT}"]}}}),
    ("probe", {"containers": {"main": {"image": ".", "livenessProbe": {"httpGet": {"path": "/${resources.cfg.P}"}}}}}),
    ("annotation", {"metadata": {"name": "w", "annotations": {"a": "${resources.cfg.X}"}}}),
])
def test_placeholder_outside_the_allowlist_is_fatal(where, score):
    """score-k8s copies these through verbatim. The pod starts, the app reads the literal
    string '${resources.cfg.LOG_LEVEL}', and nothing anywhere reports a problem."""
    with pytest.raises(SystemExit, match="does not substitute"):
        orc.scan_placeholders(score, where="score.yaml", hard=True)


def test_placeholder_scan_only_warns_while_the_feature_is_off(capsys):
    """An app already deployed with this bug must not have its next deploy fail."""
    score = {"containers": {"main": {"image": ".", "command": ["${resources.cfg.X}"]}}}
    orc.scan_placeholders(score, where="score.yaml", hard=False)
    assert "does not substitute" in capsys.readouterr().err


def test_allowed_positions_pass_the_scan():
    orc.scan_placeholders({
        "containers": {"main": {
            "image": ".",
            "variables": {"L": "${resources.cfg.L}"},
            "files": {"/etc/a": {"content": "x: ${resources.cfg.L}"}},
        }},
        "resources": {"route": {"type": "route", "params": {"host": "${resources.cfg.H}"}}},
    }, where="score.yaml", hard=True)


# =====================================================================================
# PHASE 1 — secret-in-file rules
# =====================================================================================
RESOLVED_WITH_SECRET = {"PRIVATE_KEY": SECRET, "LOG_LEVEL": "debug"}


def file_score(content: str, **extra) -> dict:
    return {"containers": {"main": {"image": ".",
                                    "files": {"/etc/app/key.pem": {"content": content, **extra}}}}}


def test_whole_file_secret_is_allowed():
    orc.check_file_secrets(file_score("${resources.cfg.PRIVATE_KEY}"),
                           RESOLVED_WITH_SECRET, where="score.yaml")


def test_block_scalar_with_trailing_newline_fails_and_names_the_fix():
    """`content: |` keeps the trailing newline, so the file is secret + "\\n" — a mix.
    score-k8s catches it, but says 'mix of secret references and raw content', which does
    not point at the one character that caused it."""
    with pytest.raises(SystemExit, match=r"\|-"):
        orc.check_file_secrets(file_score("${resources.cfg.PRIVATE_KEY}\n"),
                               RESOLVED_WITH_SECRET, where="score.yaml")


def test_secret_mixed_with_literal_text_fails():
    with pytest.raises(SystemExit, match="exactly one reference"):
        orc.check_file_secrets(file_score("username=admin\npassword=${resources.cfg.PRIVATE_KEY}"),
                               RESOLVED_WITH_SECRET, where="score.yaml")


def test_literal_only_file_may_mix_freely():
    """The rule is about secrets. A ConfigMap-backed file is reviewable in git, so there is
    no reason to restrict its shape."""
    orc.check_file_secrets(file_score("level: ${resources.cfg.LOG_LEVEL}\nother: x"),
                           RESOLVED_WITH_SECRET, where="score.yaml")


def test_no_expand_and_binary_content_are_left_alone():
    """Both are verbatim by contract, so a ${...} inside them is data, not a reference."""
    orc.check_file_secrets(file_score("${resources.cfg.PRIVATE_KEY}\n", noExpand=True),
                           RESOLVED_WITH_SECRET, where="score.yaml")
    orc.check_file_secrets(
        {"containers": {"main": {"image": ".", "files": {"/f": {"binaryContent": "AAAA"}}}}},
        RESOLVED_WITH_SECRET, where="score.yaml")


# =====================================================================================
# PHASE 1 — missing and unused keys
# =====================================================================================
def test_a_referenced_key_that_does_not_resolve_is_fatal():
    """Left alone, the container receives an empty value and the failure looks like a bug
    in the application rather than a missing entry in a config file."""
    score = {"containers": {"main": {"variables": {"L": "${resources.cfg.MISSING}"}}}}
    with pytest.raises(SystemExit, match="MISSING"):
        orc.check_referenced_keys(score, "cfg", {"LOG_LEVEL": "debug"}, where="score.yaml")


def test_referenced_keys_are_collected_from_every_allowed_position():
    score = {
        "containers": {"main": {"variables": {"L": "${resources.cfg.A}"},
                                "files": {"/f": {"content": "${resources.cfg.B}"}}}},
        "resources": {"route": {"params": {"host": "${resources.cfg.C}"}}},
    }
    assert orc.check_referenced_keys(
        score, "cfg", {"A": "1", "B": "2", "C": "3"}, where="s") == {"A", "B", "C"}


def test_references_through_another_resource_are_not_our_keys():
    """`${resources.db.host}` comes from the postgres provisioner, not from values."""
    score = {"containers": {"main": {"variables": {"H": "${resources.db.host}"}}}}
    assert orc.check_referenced_keys(score, "cfg", {}, where="s") == set()


# =====================================================================================
# PHASE 1 — integration: one Score, two environments
# =====================================================================================
def values_app(tmp_path: Path, score: dict, values: dict) -> Path:
    app_dir = tmp_path / "app"
    write(app_dir / "score.yaml", score)
    write(app_dir / ".score-values" / "values.yaml", values)
    return app_dir


def render_env(tmp_path: Path, app_dir: Path, env: str, name: str) -> list[dict]:
    """Render into a config-repo-shaped tree, so the prod digest record lands correctly."""
    config = tmp_path / "config"
    args = orc.argparse.Namespace(
        app="payment-api", image="payment-api", tag="sha1", env=env, registry="h.io/p",
        catalog=str(CATALOG), app_dir=str(app_dir), work=str(tmp_path / name),
        out=str(config / env / "manifests.yaml"), kubeconfig=None,
        state_file=str(tmp_path / "state.yaml"), no_state=False, tag_strategy="commit")
    orc.cmd_render(args)
    return orc.load_all(Path(args.work) / "manifests.yaml")


def container_env(docs: list[dict]) -> dict:
    dep = next(d for d in docs if d["kind"] == "Deployment")
    return {e["name"]: e.get("value")
            for e in dep["spec"]["template"]["spec"]["containers"][0]["env"]}


ENV_SCORE = {
    "apiVersion": "score.dev/v1b1",
    "metadata": {"name": "payment-api"},
    "containers": {"app": {"image": ".", "variables": {
        "LOG_LEVEL": "${resources.app-config.LOG_LEVEL}",
        "FEATURE_X": "${resources.app-config.FEATURE_X}"}}},
    "service": {"ports": {"http": {"port": 8080, "targetPort": 8080}}},
    "resources": {"app-config": {"type": "environment"}},
}

ENV_VALUES = values_doc(
    application={"LOG_LEVEL": "info", "FEATURE_X": "false"},
    staging={"LOG_LEVEL": "debug", "FEATURE_X": "true"},
    prod={"PUBLIC_HOST": "payment-api.internal"})


@needs_score_k8s
def test_one_score_renders_different_values_per_environment(tmp_path, values_enabled):
    """The headline gate: LOG_LEVEL=debug in staging and info in prod, from ONE score.yaml
    and one values file, with no branching anywhere in the app."""
    app_dir = values_app(tmp_path, ENV_SCORE, ENV_VALUES)
    assert container_env(render_env(tmp_path, app_dir, "staging", "w1")) == {
        "LOG_LEVEL": "debug", "FEATURE_X": "true"}
    assert container_env(render_env(tmp_path, app_dir, "prod", "w2")) == {
        "LOG_LEVEL": "info", "FEATURE_X": "false"}


@needs_score_k8s
def test_an_app_without_a_values_file_is_untouched(tmp_path):
    """The brownfield promise. Same fixture the legacy tests use, feature flag off."""
    app_dir = tmp_path / "app"
    write(app_dir / "score.yaml", score_spec("nginx", container="web"))
    docs = render_env(tmp_path, app_dir, "staging", "w")
    assert any(d["kind"] == "Deployment" for d in docs)
    assert not (tmp_path / "config" / ".platform" / "prod.values.sha256").exists()


@needs_score_k8s
def test_using_the_feature_while_it_is_off_fails_with_the_actual_fix(tmp_path, monkeypatch):
    """score-k8s would say "not supported by any provisioner. Please implement a custom
    resource provisioner" — which sends the reader off to write one, when the answer is a
    one-line platform config change they cannot guess from that text."""
    data = json.loads(json.dumps(orc.CONFIG.data))
    data["features"]["application_values"] = False
    monkeypatch.setattr(orc, "CONFIG", orc.EnvConfig(data))
    app_dir = values_app(tmp_path, ENV_SCORE, ENV_VALUES)
    with pytest.raises(SystemExit, match="features.application_values"):
        render_env(tmp_path, app_dir, "staging", "w")


def test_a_values_file_nobody_consumes_only_warns(tmp_path, capsys, monkeypatch):
    """No `type: environment` anywhere, so nothing is broken — but an inert config file
    that reports nothing is its own trap."""
    data = json.loads(json.dumps(orc.CONFIG.data))
    data["features"]["application_values"] = False
    monkeypatch.setattr(orc, "CONFIG", orc.EnvConfig(data))
    app_dir = tmp_path / "app"
    write(app_dir / "score.yaml", score_spec("web"))
    write(app_dir / ".score-values" / "values.yaml", ENV_VALUES)
    assert orc.apply_application_values(
        orc.discover(app_dir), app_dir, tmp_path, app="web", env="staging") == []
    assert "features.application_values is off" in capsys.readouterr().err


def test_environment_resource_without_a_values_file_fails(tmp_path, values_enabled):
    app_dir = tmp_path / "app"
    write(app_dir / "score.yaml", ENV_SCORE)
    with pytest.raises(SystemExit, match="no .score-values"):
        orc.apply_application_values(
            orc.discover(app_dir), app_dir, tmp_path, app="web", env="staging")


@needs_score_k8s
def test_literal_file_is_mounted_from_a_configmap(tmp_path, values_enabled):
    """Gate: non-secret file config becomes a ConfigMap without the developer writing one."""
    score = json.loads(json.dumps(ENV_SCORE))
    score["containers"]["app"]["files"] = {"/etc/app/application.yaml": {
        "content": "logLevel: ${resources.app-config.LOG_LEVEL}\nfeatureX: ${resources.app-config.FEATURE_X}"}}
    docs = render_env(tmp_path, values_app(tmp_path, score, ENV_VALUES), "staging", "w")

    cm = next(d for d in docs if d["kind"] == "ConfigMap")
    blob = (cm.get("data") or {}) | {
        k: base64.b64decode(v).decode() for k, v in (cm.get("binaryData") or {}).items()}
    body = "\n".join(blob.values())
    assert "logLevel: debug" in body and "featureX: true" in body

    dep = next(d for d in docs if d["kind"] == "Deployment")
    volumes = dep["spec"]["template"]["spec"]["volumes"]
    assert any(v.get("configMap", {}).get("name") == cm["metadata"]["name"] for v in volumes)
    assert not any(d["kind"] == "Secret" for d in docs), "literal config must not become a Secret"


@needs_score_k8s
def test_a_secret_value_is_refused_while_vault_is_off(
        tmp_path, values_enabled, monkeypatch):
    """Phase 3 wires these to Vault. Until then, refusing beats rendering a workload whose
    variable is simply absent."""
    data = json.loads(json.dumps(orc.CONFIG.data))
    data["features"]["vault_secrets"] = False
    monkeypatch.setattr(orc, "CONFIG", orc.EnvConfig(data))
    app_dir = values_app(tmp_path, ENV_SCORE, values_doc(
        application={"LOG_LEVEL": "info", "FEATURE_X": "false"},
        staging={"STRIPE_KEY": SECRET}))
    with pytest.raises(SystemExit, match="features.vault_secrets is off"):
        render_env(tmp_path, app_dir, "staging", "w")


@needs_score_k8s
def test_values_render_is_deterministic(tmp_path, values_enabled):
    app_dir = values_app(tmp_path, ENV_SCORE, ENV_VALUES)
    first = (tmp_path / "config" / "staging" / "manifests.yaml")
    render_env(tmp_path, app_dir, "staging", "w1")
    one = first.read_bytes()
    render_env(tmp_path, app_dir, "staging", "w2")
    assert first.read_bytes() == one


# =====================================================================================
# PHASE 1 — prod values digest guard
# =====================================================================================
@needs_score_k8s
def test_prod_render_records_a_values_digest(tmp_path, values_enabled):
    app_dir = values_app(tmp_path, ENV_SCORE, ENV_VALUES)
    render_env(tmp_path, app_dir, "prod", "w")
    record = tmp_path / "config" / ".platform" / "prod.values.sha256"
    assert record.is_file()
    assert record.read_text().strip() == orc.values_digest(
        orc.load_application_values(app_dir))


@needs_score_k8s
def test_staging_render_records_nothing(tmp_path, values_enabled):
    """A staging deploy must not move the record a prod promotion is checked against."""
    app_dir = values_app(tmp_path, ENV_SCORE, ENV_VALUES)
    render_env(tmp_path, app_dir, "staging", "w")
    assert not (tmp_path / "config" / ".platform" / "prod.values.sha256").exists()


def promote_args(tmp_path, app_dir, mode, **kw):
    return orc.argparse.Namespace(
        config_dir=str(tmp_path / "config"), app_dir=str(app_dir) if app_dir else None,
        mode=mode, app="payment-api", image="payment-api", tag="v2", **kw)


@needs_score_k8s
@pytest.mark.parametrize("mode", ["tag-only", "from-staging"])
def test_promotion_refuses_when_prod_values_moved(tmp_path, values_enabled, mode):
    """The gate. Both fast modes rewrite image tags in an existing manifest and never run
    the renderer, so an edited prod value would not reach production while the promotion
    reported success."""
    app_dir = values_app(tmp_path, ENV_SCORE, ENV_VALUES)
    render_env(tmp_path, app_dir, "prod", "w")

    changed = json.loads(json.dumps(ENV_VALUES))
    changed["spec"]["environments"]["prod"]["PUBLIC_HOST"] = "moved.internal"
    write(app_dir / ".score-values" / "values.yaml", changed)

    with pytest.raises(SystemExit, match="re-render"):
        orc.cmd_promote(promote_args(tmp_path, app_dir, mode))


@needs_score_k8s
def test_promotion_allows_a_pure_tag_bump(tmp_path, values_enabled):
    """The guard must not cry wolf, or the first thing anyone does is stop trusting it."""
    app_dir = values_app(tmp_path, ENV_SCORE, ENV_VALUES)
    render_env(tmp_path, app_dir, "prod", "w")
    orc.cmd_promote(promote_args(tmp_path, app_dir, "tag-only"))
    manifests = orc.load_all(tmp_path / "config" / "prod" / "manifests.yaml")
    dep = next(d for d in manifests if d["kind"] == "Deployment")
    assert dep["spec"]["template"]["spec"]["containers"][0]["image"].endswith(":v2")


@needs_score_k8s
def test_a_staging_only_edit_does_not_block_promotion(tmp_path, values_enabled):
    app_dir = values_app(tmp_path, ENV_SCORE, ENV_VALUES)
    render_env(tmp_path, app_dir, "prod", "w")
    changed = json.loads(json.dumps(ENV_VALUES))
    changed["spec"]["environments"]["staging"]["LOG_LEVEL"] = "trace"
    write(app_dir / ".score-values" / "values.yaml", changed)
    orc.cmd_promote(promote_args(tmp_path, app_dir, "tag-only"))


def test_promotion_without_a_record_is_unguarded(tmp_path):
    """Every app deployed before this feature existed. No record, nothing to compare."""
    (tmp_path / "config").mkdir()
    orc.guard_prod_values(promote_args(tmp_path, None, "tag-only"))


@needs_score_k8s
def test_promotion_demands_an_app_dir_once_a_record_exists(tmp_path, values_enabled):
    app_dir = values_app(tmp_path, ENV_SCORE, ENV_VALUES)
    render_env(tmp_path, app_dir, "prod", "w")
    with pytest.raises(SystemExit, match="--app-dir"):
        orc.guard_prod_values(promote_args(tmp_path, None, "tag-only"))


# =====================================================================================
# PHASE 2 — Vault foundation: policy scope
# =====================================================================================
# The policy generated here is the ONLY thing standing between one app and another app's
# secrets. Vault enforces it, so a mistake is not caught by anything else in the platform:
# a too-broad prefix reads as a working deploy. These tests pin the prefix.


def vault_config(**overrides) -> orc.EnvConfig:
    """The repo's real vault block with a few keys changed."""
    data = json.loads(json.dumps(orc.CONFIG.data))
    data.setdefault("vault", {}).update(overrides)
    return orc.EnvConfig(data)


@pytest.fixture
def vault_cfg(monkeypatch):
    def apply(**overrides):
        cfg = vault_config(**overrides)
        monkeypatch.setattr(orc, "CONFIG", cfg)
        return cfg
    return apply


def test_policy_grants_one_app_in_one_environment_and_nothing_else():
    """The whole security model in one assertion: the prefix pins app AND environment."""
    policy = orc.vault_policy("payment-api", "staging")
    assert 'path "kv/data/apps/payment-api/staging/*"' in policy
    assert "orders-api" not in policy
    # A staging role that could read prod is the same failure as reading another app:
    # production credentials handed to the environment developers deploy to freely.
    assert "/prod/" not in policy


def test_policy_covers_metadata_because_kv_v2_splits_the_path():
    """Missing metadata makes `kv get`/`list` fail as 'permission denied' — which reads
    like the role is wrong, not like the policy covers only half of a kv-v2 mount."""
    policy = orc.vault_policy("payment-api", "staging")
    assert 'path "kv/metadata/apps/payment-api/staging/*"' in policy


def test_kv_v1_policy_has_no_data_or_metadata_infix(vault_cfg):
    vault_cfg(kv_type="kv-v1")
    policy = orc.vault_policy("payment-api", "staging")
    assert 'path "kv/apps/payment-api/staging/*"' in policy
    assert "/data/" not in policy and "/metadata/" not in policy


def test_read_policy_cannot_write(vault_cfg):
    """VSO only ever reads. A role that can also write turns a compromised operator into
    a way to overwrite every app's credentials."""
    policy = orc.vault_policy("payment-api", "staging")
    assert "create" not in policy and "update" not in policy


def test_write_policy_is_a_separate_named_policy():
    """Humans and the onboarding tool write; the operator does not. Two policies, two
    names, so granting one never implies the other."""
    assert orc.vault_policy_name("payment-api", "staging") != \
        orc.vault_policy_name("payment-api", "staging", write=True)
    write = orc.vault_policy("payment-api", "staging", write=True)
    assert "create" in write and "update" in write
    assert 'path "kv/data/apps/payment-api/staging/*"' in write


def test_policy_follows_a_relocated_mount_and_layout(vault_cfg):
    """Company Vault will not have a mount called `kv` or a prefix called `apps`."""
    vault_cfg(kv_mount="secret/platform",
              path_template="teams/{environment}/{application}/{name}")
    policy = orc.vault_policy("order", "prod")
    assert 'path "secret/platform/data/teams/prod/order/*"' in policy


@pytest.mark.parametrize("template,fragment", [
    ("apps/{environment}/{name}", "{application}"),
    ("apps/{application}/{name}", "{environment}"),
])
def test_path_template_missing_a_scope_is_refused(vault_cfg, template, fragment):
    """Silently generating a policy over a prefix that does not pin the app is the one
    failure this feature must never have, so it is refused at config level."""
    vault_cfg(path_template=template)
    with pytest.raises(SystemExit, match=re.escape(fragment)):
        orc.vault_policy("payment-api", "staging")


def test_path_template_must_end_with_the_app_supplied_segment(vault_cfg):
    vault_cfg(path_template="apps/{application}/{name}/{environment}")
    with pytest.raises(SystemExit, match="must end with"):
        orc.vault_policy_prefix("payment-api", "staging")


def test_unknown_kv_type_is_fatal_rather_than_assumed(vault_cfg):
    vault_cfg(kv_type="kv2")
    with pytest.raises(SystemExit, match="kv_type"):
        orc.vault_policy("payment-api", "staging")


def test_policy_prefix_refuses_an_app_name_that_could_move_it():
    with pytest.raises(SystemExit, match="invalid secret name"):
        orc.vault_policy_prefix("../admin", "staging")


# =====================================================================================
# PHASE 2 — Vault foundation: derived names
# =====================================================================================
def test_role_policy_and_service_account_names_are_derived_from_config(vault_cfg):
    assert orc.vault_role_name("payment-api", "staging") == "idp-payment-api-staging"
    assert orc.vault_service_account("payment-api", "staging") == "idp-payment-api"
    vault_cfg(auth_role_template="vault_{application}_{environment}",
              service_account_template="sa-{application}-{environment}")
    assert orc.vault_role_name("payment-api", "prod") == "vault_payment-api_prod"
    assert orc.vault_service_account("payment-api", "prod") == "sa-payment-api-prod"


def test_service_account_must_still_be_a_kubernetes_name(vault_cfg):
    """Vault tolerates an underscore in a role name; a ServiceAccount does not, and the
    error surfaces at apply time in a namespace nobody is watching."""
    vault_cfg(service_account_template="idp_{application}")
    with pytest.raises(SystemExit, match="Kubernetes object name"):
        orc.vault_service_account("payment-api", "staging")


def test_service_account_is_never_the_namespace_default():
    """The Vault role binds (namespace, serviceAccount). Bound to `default`, every pod in
    the namespace — including one that is not part of this app — can read its secrets."""
    assert orc.vault_service_account("payment-api", "staging") != "default"


def test_unknown_placeholder_in_a_name_template_is_fatal(vault_cfg):
    vault_cfg(auth_role_template="idp-{application}-{tenant}")
    with pytest.raises(SystemExit, match="unknown placeholder"):
        orc.vault_role_name("payment-api", "staging")


# =====================================================================================
# PHASE 2 — Vault foundation: generated manifests
# =====================================================================================
def test_vault_connection_has_no_hardcoded_address(vault_cfg):
    """There is no sensible default for 'where is Vault'. A fallback here is a deploy that
    authenticates against the wrong Vault and reports success."""
    vault_cfg(address="")
    with pytest.raises(SystemExit, match="vault.address"):
        orc.vault_connection_manifest()


def test_vault_connection_carries_the_configured_coordinates(vault_cfg):
    vault_cfg(address="https://vault.corp.internal:8200", skip_tls_verify=False,
              ca_cert_secret="vault-ca", tls_server_name="vault.corp.internal",
              operator_namespace="vso-system", connection_name="corp")
    doc = orc.vault_connection_manifest()
    assert doc["metadata"] == {
        "name": "corp", "namespace": "vso-system",
        "labels": {"app.kubernetes.io/part-of": "idp-platform"}}
    assert doc["spec"] == {
        "address": "https://vault.corp.internal:8200", "skipTLSVerify": False,
        "caCertSecretRef": "vault-ca", "tlsServerName": "vault.corp.internal"}


def test_optional_tls_fields_are_omitted_rather_than_sent_empty(vault_cfg):
    """An empty string is not the same as unset: VSO would look for a Secret named ''."""
    vault_cfg(ca_cert_secret="", tls_server_name="")
    spec = orc.vault_connection_manifest()["spec"]
    assert "caCertSecretRef" not in spec and "tlsServerName" not in spec


def test_auth_global_holds_only_what_is_genuinely_shared(vault_cfg):
    """Role and ServiceAccount must NOT be here. One shared identity for every namespace
    would undo the per-app policy without changing anything visible."""
    vault_cfg(auth_mount="k8s-staging", namespace="", auth_audience="vault")
    spec = orc.vault_auth_global_manifest()["spec"]
    assert spec["defaultAuthMethod"] == "kubernetes"
    assert spec["defaultMount"] == "k8s-staging"
    assert "role" not in json.dumps(spec) and "serviceAccount" not in json.dumps(spec)
    assert "defaultVaultNamespace" not in spec


def test_enterprise_vault_namespace_reaches_both_objects(vault_cfg):
    vault_cfg(namespace="platform")
    assert orc.vault_auth_global_manifest()["spec"]["defaultVaultNamespace"] == "platform"
    auth = next(d for d in orc.vault_auth_manifests("payment-api", "staging")
                if d["kind"] == "VaultAuth")
    assert auth["spec"]["namespace"] == "platform"


def test_vault_auth_uses_a_per_namespace_identity_and_the_shared_global(vault_cfg):
    vault_cfg(operator_namespace="vso-system", auth_global_name="shared")
    sa, auth = orc.vault_auth_manifests("payment-api", "staging")
    assert sa["kind"] == "ServiceAccount"
    assert sa["metadata"]["namespace"] == "payment-api-staging"
    assert auth["metadata"]["name"] == "app-vault"
    assert auth["metadata"]["namespace"] == "payment-api-staging"
    assert auth["spec"]["vaultAuthGlobalRef"] == {"name": "shared", "namespace": "vso-system"}
    assert auth["spec"]["kubernetes"]["role"] == "idp-payment-api-staging"
    assert auth["spec"]["kubernetes"]["serviceAccount"] == sa["metadata"]["name"]


def test_vault_auth_follows_a_custom_namespace_pattern(monkeypatch):
    """A team that is granted namespaces rather than allowed to create them renames every
    namespace. The VaultAuth has to land in the same one as the workload, or the secret
    syncs into a namespace no pod reads from."""
    data = json.loads(json.dumps(orc.CONFIG.data))
    data.setdefault("kubernetes", {})["namespace_pattern"] = "team-x-{app}-{env}"
    monkeypatch.setattr(orc, "CONFIG", orc.EnvConfig(data))
    for doc in orc.vault_auth_manifests("payment-api", "staging"):
        assert doc["metadata"]["namespace"] == "team-x-payment-api-staging"


def test_foundation_manifests_are_deterministic():
    """Applied repeatedly by an operator and by CI. Any churn here is a diff nobody made."""
    assert yaml.safe_dump(orc.vault_foundation_manifests()) == \
        yaml.safe_dump(orc.vault_foundation_manifests())


def test_no_generated_vault_object_carries_a_value():
    """The platform generates references, never values. If a Secret body ever appears in
    this output it is in Git a moment later."""
    docs = orc.vault_foundation_manifests() + orc.vault_auth_manifests("payment-api", "staging")
    for doc in docs:
        assert doc["kind"] != "Secret"
        assert "data" not in doc and "stringData" not in doc


# =====================================================================================
# PHASE 2 — verify identity: read status, never secrets
# =====================================================================================
def test_verify_identity_cannot_read_secrets():
    """Kubernetes RBAC has no verb that shows a Secret's keys but hides its values, so
    `get secrets` IS `read every secret in the namespace`. Verification never needs it."""
    for rule in orc.VERIFY_RULES:
        assert "secrets" not in rule["resources"], rule
        assert "*" not in rule["resources"] and "*" not in rule["apiGroups"], rule


def test_verify_identity_is_read_only():
    for rule in orc.VERIFY_RULES:
        assert set(rule["verbs"]) <= {"get", "list", "watch"}, rule


def test_verify_identity_can_read_vso_status():
    """Gate: auth and sync status must be checkable with the restricted kubeconfig."""
    rule = next(r for r in orc.VERIFY_RULES if r["apiGroups"] == ["secrets.hashicorp.com"])
    assert {"vaultauths", "vaultstaticsecrets"} <= set(rule["resources"])


def test_verify_rbac_binds_only_its_own_namespace():
    sa, role, binding = orc.verify_rbac_manifests("payment-api", "staging")
    assert [d["kind"] for d in (sa, role, binding)] == ["ServiceAccount", "Role", "RoleBinding"]
    assert {d["metadata"]["namespace"] for d in (sa, role, binding)} == {"payment-api-staging"}
    # Role, not ClusterRole: a cluster-wide read of every app's objects is not needed to
    # answer "did MY deploy come up".
    assert binding["roleRef"]["kind"] == "Role"
    assert binding["subjects"] == [{"kind": "ServiceAccount", "name": sa["metadata"]["name"],
                                    "namespace": "payment-api-staging"}]


def test_verify_rbac_names_are_dns_safe_and_stable():
    first = orc.verify_rbac_manifests("payment-api", "staging")[0]["metadata"]["name"]
    again = orc.verify_rbac_manifests("payment-api", "staging")[0]["metadata"]["name"]
    assert first == again and len(first) <= 63 and orc.DNS_LABEL.match(first)


# =====================================================================================
# PHASE 2 — preflight against a real cluster's VSO
# =====================================================================================
# CRDs and controller are two objects that upgrade separately. Skew is silent: the new CR
# is accepted, no event is emitted, and the destination Secret never appears.
def fake_cluster(monkeypatch, *, crds=True, version="1.5.0", foundation=True):
    calls = []

    def fake_kubectl(args, *, kubeconfig=None, **kw):
        calls.append(args)
        if args[:2] == ["get", "crd"]:
            names = "\n".join(
                f"customresourcedefinition.apiextensions.k8s.io/{c}" for c in orc.VSO_CRDS)
            return subprocess.CompletedProcess(args, 0, names if crds else "", "")
        if "deploy" in args and "-o" in args:
            items = {"items": [{"spec": {"template": {"spec": {"containers": [
                {"image": f"hashicorp/vault-secrets-operator:{version}"}]}}}}]} if version else {"items": []}
            return subprocess.CompletedProcess(args, 0, json.dumps(items), "")
        if any(a.startswith("vault") for a in args):
            return subprocess.CompletedProcess(args, 0 if foundation else 1, "", "NotFound")
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(orc, "kubectl", fake_kubectl)
    return calls


def test_preflight_accepts_a_matching_cluster(monkeypatch, vault_cfg):
    vault_cfg(operator_version="1.5.0")
    fake_cluster(monkeypatch)
    orc.check_vault_foundation(None)


def test_preflight_refuses_a_version_skew(monkeypatch, vault_cfg):
    vault_cfg(operator_version="1.5.0")
    fake_cluster(monkeypatch, version="1.4.1")
    with pytest.raises(SystemExit, match="1.4.1"):
        orc.check_vault_foundation(None)


def test_preflight_refuses_missing_crds(monkeypatch, vault_cfg):
    vault_cfg(operator_version="1.5.0")
    fake_cluster(monkeypatch, crds=False)
    with pytest.raises(SystemExit, match="CRDs missing"):
        orc.check_vault_foundation(None)


def test_preflight_refuses_when_the_foundation_was_never_applied(monkeypatch, vault_cfg):
    vault_cfg(operator_version="1.5.0")
    fake_cluster(monkeypatch, foundation=False)
    with pytest.raises(SystemExit, match="vault-foundation"):
        orc.check_vault_foundation(None)


def test_an_empty_pin_skips_the_version_check_but_not_the_rest(monkeypatch, vault_cfg):
    """Same brownfield on-ramp as the other pins: empty means 'not checked', and it is
    still an error for the CRDs or the foundation objects to be missing."""
    vault_cfg(operator_version="")
    fake_cluster(monkeypatch, version="0.9.0")
    orc.check_vault_foundation(None)


def test_require_vault_without_a_cluster_is_rejected(monkeypatch):
    args = argparse.Namespace(require_cluster=False, require_vault=True, kubeconfig=None,
                              require_score_compose=False)
    monkeypatch.setattr(orc, "check_tool_versions", lambda *a, **k: None)
    with pytest.raises(SystemExit, match="--require-cluster"):
        orc.cmd_preflight(args)


# =====================================================================================
# PHASE 2 — the onboarding tool
# =====================================================================================
def test_vault_foundation_prints_and_does_not_touch_the_cluster(monkeypatch, capsys):
    """Default is print. These objects grant access to secrets, so applying them is a
    deliberate act by someone holding cluster-admin, not a side effect of running a tool."""
    def explode(*a, **k):
        raise AssertionError("kubectl must not run without --apply")
    monkeypatch.setattr(orc, "kubectl", explode)
    orc.main(["--env-config", str(CATALOG / "platform.env.yaml"), "vault-foundation"])
    docs = list(yaml.safe_load_all(capsys.readouterr().out))
    assert [d["kind"] for d in docs if d] == ["VaultConnection", "VaultAuthGlobal"]


def test_onboarding_prints_vault_commands_and_uses_no_vault_token(monkeypatch, capsys):
    """CI must never hold a Vault token: that token can read everything the policy allows,
    which makes the split between 'platform generates references' and 'VSO reads values'
    meaningless. The tool prints commands for a Vault administrator instead."""
    monkeypatch.delenv("VAULT_TOKEN", raising=False)
    orc.main(["--env-config", str(CATALOG / "platform.env.yaml"),
              "vault-onboard", "--app", "payment-api", "--env", "staging"])
    out = capsys.readouterr().out
    assert "vault write auth/kubernetes/role/idp-payment-api-staging" in out
    assert "bound_service_account_names=idp-payment-api" in out
    assert "bound_service_account_namespaces=payment-api-staging" in out
    assert "VAULT_TOKEN" not in out


def test_onboarding_can_print_the_policy_for_piping_into_vault(capsys):
    orc.main(["--env-config", str(CATALOG / "platform.env.yaml"),
              "vault-onboard", "--app", "payment-api", "--env", "staging", "--print-policy"])
    assert 'path "kv/data/apps/payment-api/staging/*"' in capsys.readouterr().out


def test_verify_rbac_command_prints_three_objects(capsys):
    orc.main(["--env-config", str(CATALOG / "platform.env.yaml"),
              "verify-rbac", "--app", "payment-api", "--env", "staging"])
    docs = [d for d in yaml.safe_load_all(capsys.readouterr().out) if d]
    assert [d["kind"] for d in docs] == ["ServiceAccount", "Role", "RoleBinding"]


def test_auth_global_qualifies_the_connection_with_its_namespace(vault_cfg):
    """Measured against VSO 1.5.0: an unqualified `vaultConnectionRef` is resolved in the
    namespace of whatever REFERS to it — i.e. each app's namespace — so a bare name makes
    every VaultAuth in the platform fail with `VaultConnection "default" not found`, in a
    controller log nobody is watching rather than at apply time."""
    vault_cfg(operator_namespace="vso-system", connection_name="corp")
    assert orc.vault_auth_global_manifest()["spec"]["vaultConnectionRef"] == "vso-system/corp"


# =====================================================================================
# PHASE 3 — app secrets: bindings and generated VaultStaticSecrets
# =====================================================================================
@pytest.fixture
def secrets_enabled(monkeypatch):
    """The repo's real config with both application_values and vault_secrets on."""
    data = json.loads(json.dumps(orc.CONFIG.data))
    data.setdefault("features", {}).update(
        {"application_values": True, "vault_secrets": True})
    monkeypatch.setattr(orc, "CONFIG", orc.EnvConfig(data))


SECRET_VALUES = values_doc(
    application={"LOG_LEVEL": "info",
                 "STRIPE_KEY": {"secretRef": {"name": "stripe", "key": "api_key"}}},
    staging={"LOG_LEVEL": "debug"})

SECRET_SCORE = {
    "apiVersion": "score.dev/v1b1",
    "metadata": {"name": "payment-api"},
    "containers": {"app": {"image": ".", "variables": {
        "LOG_LEVEL": "${resources.app-config.LOG_LEVEL}",
        "STRIPE_KEY": "${resources.app-config.STRIPE_KEY}"}}},
    "service": {"ports": {"http": {"port": 8080, "targetPort": 8080}}},
    "resources": {"app-config": {"type": "environment"}},
}


def test_a_secret_is_bound_per_workload_not_per_app(secrets_enabled):
    """Two workloads reading the same logical secret get two destination Secrets.

    Not tidiness: the destination Secret is mounted into that workload's pods, so sharing
    one across workloads hands the payment key to the worker that only needed the queue
    password."""
    resolved = {"STRIPE_KEY": {"secretRef": {"name": "stripe", "key": "api_key"}}}
    bindings = orc.secret_bindings("payment-api", "staging", resolved,
                                   {"web": {"STRIPE_KEY"}, "worker": {"STRIPE_KEY"}})
    assert [b["workload"] for b in bindings] == ["web", "worker"]
    assert len({b["destination"] for b in bindings}) == 2


def test_keys_of_one_logical_secret_share_one_binding(secrets_enabled):
    """One VaultStaticSecret per logical secret, so both keys land in the same sync.

    Two CRs on one Vault path sync independently, and there is a window where the app runs
    the new api_key with the old webhook_secret."""
    resolved = {
        "STRIPE_KEY": {"secretRef": {"name": "stripe", "key": "api_key"}},
        "STRIPE_HOOK": {"secretRef": {"name": "stripe", "key": "webhook_secret"}},
    }
    bindings = orc.secret_bindings("payment-api", "staging", resolved,
                                   {"web": {"STRIPE_KEY", "STRIPE_HOOK"}})
    assert len(bindings) == 1
    assert bindings[0]["keys"] == {"STRIPE_KEY": "api_key", "STRIPE_HOOK": "webhook_secret"}


def test_a_secret_no_workload_uses_produces_no_binding(secrets_enabled):
    """No reader, no VaultStaticSecret: otherwise a real secret is pulled into the cluster
    for nobody."""
    resolved = {"STRIPE_KEY": {"secretRef": {"name": "stripe", "key": "api_key"}}}
    assert orc.secret_bindings("payment-api", "staging", resolved, {"web": {"LOG_LEVEL"}}) == []


def test_binding_names_are_stable_dns_safe_and_environment_scoped(secrets_enabled):
    resolved = {"K": {"secretRef": {"name": "stripe", "key": "api_key"}}}
    used = {"web": {"K"}}
    staging = orc.secret_bindings("payment-api", "staging", resolved, used)[0]
    again = orc.secret_bindings("payment-api", "staging", resolved, used)[0]
    prod = orc.secret_bindings("payment-api", "prod", resolved, used)[0]
    assert staging["destination"] == again["destination"]        # deterministic
    assert staging["destination"] != prod["destination"]         # never shared across envs
    assert orc.DNS_LABEL.match(staging["destination"])
    assert len(staging["destination"]) <= 63


def test_generated_cr_reads_only_the_keys_the_workload_asked_for(secrets_enabled):
    """`includes` alone is not enough — see excludeRaw below."""
    binding = orc.secret_bindings(
        "payment-api", "staging",
        {"A": {"secretRef": {"name": "stripe", "key": "api_key"}}},
        {"web": {"A"}})[0]
    spec = orc.vault_static_secret_doc(binding, app="payment-api", env="staging")["spec"]
    assert spec["destination"]["transformation"]["includes"] == ["^api_key$"]


def test_generated_cr_excludes_the_raw_payload(secrets_enabled):
    """Measured on VSO 1.5.0: without excludeRaw the destination Secret also carries `_raw`
    — the ENTIRE Vault secret as JSON. `includes` filters the named keys while `_raw` hands
    over every one of them anyway, so the per-workload filter above becomes decorative."""
    binding = orc.secret_bindings(
        "payment-api", "staging",
        {"A": {"secretRef": {"name": "stripe", "key": "api_key"}}},
        {"web": {"A"}})[0]
    spec = orc.vault_static_secret_doc(binding, app="payment-api", env="staging")["spec"]
    assert spec["destination"]["transformation"]["excludeRaw"] is True


def test_generated_cr_carries_rotation_settings(secrets_enabled):
    """hmacSecretData false makes VSO unable to tell a rotation from a re-read: it either
    ignores rolloutRestartTargets or restarts on every sync."""
    binding = orc.secret_bindings(
        "payment-api", "staging",
        {"A": {"secretRef": {"name": "stripe", "key": "api_key"}}},
        {"web": {"A"}})[0]
    spec = orc.vault_static_secret_doc(binding, app="payment-api", env="staging")["spec"]
    assert spec["hmacSecretData"] is True
    assert spec["rolloutRestartTargets"] == [{"kind": "Deployment", "name": "web"}]
    assert spec["refreshAfter"] == orc.CONFIG.get("vault.refresh_after")


def test_generated_cr_authenticates_through_the_namespace_vault_auth(secrets_enabled):
    """Never the VaultAuthGlobal: that authenticates as an identity shared with every
    other namespace, which undoes the per-app Vault policy."""
    binding = orc.secret_bindings(
        "payment-api", "staging",
        {"A": {"secretRef": {"name": "stripe", "key": "api_key"}}},
        {"web": {"A"}})[0]
    spec = orc.vault_static_secret_doc(binding, app="payment-api", env="staging")["spec"]
    assert spec["vaultAuthRef"] == orc.CONFIG.get("vault.auth_ref")
    assert "vaultAuthGlobalRef" not in spec


def test_generated_cr_path_has_no_mount_prefix(secrets_enabled):
    """VSO takes mount and path as two fields. A path that repeats the mount reads as
    `kv/kv/apps/...` in Vault and fails as 'permission denied', not 'not found'."""
    binding = orc.secret_bindings(
        "payment-api", "staging",
        {"A": {"secretRef": {"name": "stripe", "key": "api_key"}}},
        {"web": {"A"}})[0]
    doc = orc.vault_static_secret_doc(binding, app="payment-api", env="staging")
    assert doc["spec"]["path"] == "apps/payment-api/staging/stripe"
    assert doc["spec"]["mount"] == "kv"
    assert orc.vault_path("payment-api", "staging", "stripe") == "kv/apps/payment-api/staging/stripe"


def test_generated_cr_carries_no_value_only_coordinates(secrets_enabled):
    binding = orc.secret_bindings(
        "payment-api", "staging",
        {"A": {"secretRef": {"name": "stripe", "key": "api_key"}}},
        {"web": {"A"}})[0]
    doc = orc.vault_static_secret_doc(binding, app="payment-api", env="staging")
    assert "data" not in doc and "stringData" not in doc


def test_secret_output_still_refuses_when_the_feature_is_off(tmp_path, monkeypatch):
    """An app that opted in must fail loudly, not deploy with the variable missing."""
    data = json.loads(json.dumps(orc.CONFIG.data))
    data.setdefault("features", {}).update(
        {"application_values": True, "vault_secrets": False})
    monkeypatch.setattr(orc, "CONFIG", orc.EnvConfig(data))
    with pytest.raises(SystemExit, match="features.vault_secrets is off"):
        orc.write_environment_provisioner(
            {"K": {"secretRef": {"name": "stripe", "key": "api_key"}}},
            tmp_path / "p.yaml", app="payment-api", env="staging",
            used_by_workload={"web": {"K"}})


# =====================================================================================
# PHASE 3 — integration: a secretRef becomes a secretKeyRef, never a value
# =====================================================================================
@needs_score_k8s
def test_a_secret_ref_renders_as_a_secret_key_ref(tmp_path, secrets_enabled):
    """The gate: the app writes `secretRef`, the container gets `valueFrom.secretKeyRef`,
    and the value exists in neither the manifest nor anything else this render produced."""
    app_dir = values_app(tmp_path, SECRET_SCORE, SECRET_VALUES)
    docs = render_env(tmp_path, app_dir, "staging", "w")
    dep = next(d for d in docs if d["kind"] == "Deployment")
    env = {e["name"]: e for e in dep["spec"]["template"]["spec"]["containers"][0]["env"]}
    assert env["LOG_LEVEL"]["value"] == "debug"
    assert "value" not in env["STRIPE_KEY"]
    ref = env["STRIPE_KEY"]["valueFrom"]["secretKeyRef"]
    assert ref["key"] == "api_key"

    vss = next(d for d in docs if d["kind"] == "VaultStaticSecret")
    assert vss["spec"]["destination"]["name"] == ref["name"]
    assert vss["spec"]["path"] == "apps/payment-api/staging/stripe"


@needs_score_k8s
def test_the_vault_cr_goes_to_git_and_the_secret_does_not(tmp_path, secrets_enabled):
    """VaultStaticSecret is a reference, so Fleet owns it. The Secret it fills is a runtime
    object owned by VSO and must never appear in the config repo."""
    app_dir = values_app(tmp_path, SECRET_SCORE, SECRET_VALUES)
    render_env(tmp_path, app_dir, "staging", "w")
    committed = orc.load_all(tmp_path / "config" / "staging" / "manifests.yaml")
    kinds = {d["kind"] for d in committed}
    assert "VaultStaticSecret" in kinds
    assert "Secret" not in kinds


@needs_score_k8s
def test_two_renders_with_a_secret_are_byte_identical(tmp_path, secrets_enabled):
    """Names come from a SHA-256 of a stable tuple, not from state or a random suffix. A
    changing name here is a new Secret, a new CR and a pod restart nobody asked for."""
    app_dir = values_app(tmp_path, SECRET_SCORE, SECRET_VALUES)
    render_env(tmp_path, app_dir, "staging", "w1")
    first = (tmp_path / "config" / "staging" / "manifests.yaml").read_text()
    render_env(tmp_path, app_dir, "staging", "w2")
    assert (tmp_path / "config" / "staging" / "manifests.yaml").read_text() == first


@needs_score_k8s
def test_staging_and_prod_read_different_vault_paths(tmp_path, secrets_enabled):
    """One score.yaml, one values file, two environments — and a staging credential must
    never be what production authenticates with."""
    app_dir = values_app(tmp_path, SECRET_SCORE, SECRET_VALUES)
    paths = {}
    for env, work in (("staging", "w1"), ("prod", "w2")):
        docs = render_env(tmp_path, app_dir, env, work)
        paths[env] = next(d for d in docs if d["kind"] == "VaultStaticSecret")["spec"]["path"]
    assert paths == {"staging": "apps/payment-api/staging/stripe",
                     "prod": "apps/payment-api/prod/stripe"}


@needs_score_k8s
def test_only_the_workload_that_asked_gets_the_secret(tmp_path, secrets_enabled):
    """Two workloads, one secret, one consumer. The other workload must not end up with a
    reference to a Secret it never asked for."""
    # Both in subdirectories: a score.yaml at the root wins over subdirs, so a root file
    # would hide the worker entirely and the test would pass for the wrong reason.
    write(tmp_path / "app" / "api" / "score.yaml", SECRET_SCORE)
    write(tmp_path / "app" / "worker" / "score.yaml", {
        "apiVersion": "score.dev/v1b1",
        "metadata": {"name": "worker"},
        "containers": {"app": {"image": ".", "variables": {
            "LOG_LEVEL": "${resources.app-config.LOG_LEVEL}"}}},
        "resources": {"app-config": {"type": "environment"}},
    })
    write(tmp_path / "app" / ".score-values" / "values.yaml", SECRET_VALUES)
    docs = render_env(tmp_path, tmp_path / "app", "staging", "w")

    crs = [d for d in docs if d["kind"] == "VaultStaticSecret"]
    assert [c["metadata"]["labels"]["idp.platform/workload"] for c in crs] == ["payment-api"]
    worker = next(d for d in docs if d["kind"] == "Deployment"
                  and d["metadata"]["name"] == "worker")
    assert "secretKeyRef" not in json.dumps(worker)


# =====================================================================================
# PHASE 3 — verify waits for the secret before blaming the rollout
# =====================================================================================
def verify_args(**kw):
    base = dict(app="payment-api", env="staging", kubeconfig=None, timeout=1, manifests=None)
    base.update(kw)
    return argparse.Namespace(**base)


VSS_DOC = {
    "kind": "VaultStaticSecret",
    "metadata": {"name": "idp-x", "annotations": {
        "idp.platform/logical-secret": "stripe",
        "idp.platform/vault-path": "apps/payment-api/staging/stripe"},
        "labels": {"idp.platform/application": "payment-api",
                   "idp.platform/environment": "staging",
                   "idp.platform/workload": "web"}},
}


def vss_cluster(monkeypatch, payload, returncode=0):
    def fake_kubectl(args, *, kubeconfig=None, **kw):
        return subprocess.CompletedProcess(args, returncode, json.dumps(payload), "NotFound")
    monkeypatch.setattr(orc, "kubectl", fake_kubectl)


def test_verify_accepts_a_synced_secret(monkeypatch):
    vss_cluster(monkeypatch, {"status": {"conditions": [
        {"status": "True", "reason": "SecretSynced", "message": "Secret synced"}]}})
    ok, _ = orc.vault_secret_status(VSS_DOC, "payment-api-staging", verify_args())
    assert ok


def test_verify_reports_the_vault_path_and_reason_but_no_value(monkeypatch):
    """The diagnostic has to be enough to act on — which app, which workload, which Vault
    path, which reason — while never containing the thing it is protecting."""
    vss_cluster(monkeypatch, {"status": {"conditions": [
        {"status": "False", "reason": "VaultClientError",
         "message": "Error making API request. Code: 403. permission denied"}]}})
    ok, msg = orc.vault_secret_status(VSS_DOC, "payment-api-staging", verify_args())
    assert not ok
    assert "payment-api/staging" in msg and "[web]" in msg
    assert "secret=stripe" in msg
    assert "kv/apps/payment-api/staging/stripe" in msg
    assert "VaultClientError" in msg and "permission denied" in msg


def test_verify_says_so_when_the_cr_never_reached_the_cluster(monkeypatch):
    """Distinct from 'not synced': this one means Fleet has not applied the render yet, and
    looking at Vault policy would be a waste of time."""
    vss_cluster(monkeypatch, {}, returncode=1)
    ok, msg = orc.vault_secret_status(VSS_DOC, "payment-api-staging", verify_args())
    assert not ok and "chưa có trên cụm" in msg


def test_verify_fails_with_diagnostics_when_a_secret_never_syncs(monkeypatch):
    monkeypatch.setattr(orc, "CONFIG", orc.EnvConfig(
        {"vault": {"initial_sync_timeout_seconds": 0}}))
    vss_cluster(monkeypatch, {"status": {"conditions": [
        {"status": "False", "reason": "VaultClientError", "message": "permission denied"}]}})
    with pytest.raises(SystemExit, match="CreateContainerConfigError"):
        orc.wait_for_vault_secrets([VSS_DOC], "payment-api-staging", verify_args())


def test_verify_is_inert_for_an_app_with_no_secrets(monkeypatch):
    """Every app deployed before this feature existed renders no VaultStaticSecret."""
    def explode(*a, **k):
        raise AssertionError("must not query the cluster when there is nothing to wait for")
    monkeypatch.setattr(orc, "kubectl", explode)
    orc.wait_for_vault_secrets([{"kind": "Deployment"}], "ns", verify_args())


# =====================================================================================
# PHASE 3 — writing a secret: `secret-set`
# =====================================================================================
def test_secret_set_has_no_value_flag():
    """A value in argv is in the shell history and in `ps` for every other user on the box.
    Neither can be un-leaked, so the flag does not exist."""
    with pytest.raises(SystemExit):
        orc.main(["--env-config", str(CATALOG / "platform.env.yaml"), "secret-set",
                  "--app", "payment-api", "--env", "staging", "--name", "stripe",
                  "--key", "api_key", "--value", "sk_live_1"])


def test_secret_set_refuses_without_a_token(monkeypatch):
    monkeypatch.delenv("VAULT_TOKEN", raising=False)
    monkeypatch.setenv("VAULT_ADDR", "http://vault.test:8200")
    with pytest.raises(SystemExit, match="VAULT_TOKEN"):
        orc.cmd_secret_set(argparse.Namespace(
            app="payment-api", env="staging", name="stripe", key="api_key",
            stdin=True, replace=False))


def test_secret_set_writes_where_the_app_will_read(monkeypatch, capsys):
    """The one pairing that matters: written path must equal derived read path. Getting it
    wrong by hand is the most common source of a 403 against a path that looks right."""
    sent = {}

    def fake_urlopen(request, timeout=None):
        sent["url"] = request.full_url
        sent["method"] = request.get_method()
        sent["body"] = json.loads(request.data.decode())
        sent["headers"] = {k.lower(): v for k, v in request.header_items()}

        class R:
            def read(self): return b""
            def __enter__(self): return self
            def __exit__(self, *a): return False
        return R()

    monkeypatch.setenv("VAULT_ADDR", "http://vault.test:8200")
    monkeypatch.setenv("VAULT_TOKEN", "s.token")
    monkeypatch.setattr(orc.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(orc.sys, "stdin", type("S", (), {"read": staticmethod(lambda: "sk_live_1\n")})())
    orc.cmd_secret_set(argparse.Namespace(
        app="payment-api", env="staging", name="stripe", key="api_key",
        stdin=True, replace=False))

    assert sent["url"] == "http://vault.test:8200/v1/kv/data/apps/payment-api/staging/stripe"
    assert orc.vault_path("payment-api", "staging", "stripe") == "kv/apps/payment-api/staging/stripe"
    assert sent["body"] == {"data": {"api_key": "sk_live_1"}}
    # A patch, not a put: writing one key must not delete the others in the same secret.
    assert sent["method"] == "PATCH"
    # Neither the token nor the value may be logged.
    assert "sk_live_1" not in capsys.readouterr().err


def test_secret_set_replaces_only_when_asked(monkeypatch):
    seen = {}

    def fake_urlopen(request, timeout=None):
        seen["method"] = request.get_method()

        class R:
            def read(self): return b""
            def __enter__(self): return self
            def __exit__(self, *a): return False
        return R()

    monkeypatch.setenv("VAULT_ADDR", "http://vault.test:8200")
    monkeypatch.setenv("VAULT_TOKEN", "s.token")
    monkeypatch.setattr(orc.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(orc.sys, "stdin", type("S", (), {"read": staticmethod(lambda: "v")})())
    orc.cmd_secret_set(argparse.Namespace(
        app="payment-api", env="staging", name="stripe", key="api_key",
        stdin=True, replace=True))
    assert seen["method"] == "POST"


def test_secret_set_refuses_a_name_that_could_move_the_path(monkeypatch):
    monkeypatch.setenv("VAULT_ADDR", "http://vault.test:8200")
    monkeypatch.setenv("VAULT_TOKEN", "s.token")
    with pytest.raises(SystemExit, match="invalid secret name"):
        orc.cmd_secret_set(argparse.Namespace(
            app="payment-api", env="staging", name="../../root", key="api_key",
            stdin=True, replace=False))


def test_secret_set_keeps_a_trailing_space_in_the_value(monkeypatch):
    """Only the newline the shell adds is stripped. A secret may legitimately end in
    whitespace, and silently trimming it produces an auth failure nobody can explain."""
    monkeypatch.setattr(orc.sys, "stdin",
                        type("S", (), {"read": staticmethod(lambda: "sk_live_1 \n")})())
    assert orc.read_secret_value(argparse.Namespace(stdin=True, key="api_key")) == "sk_live_1 "


# =====================================================================================
# PHASE 4 — postgres class `application`: profile per environment
# =====================================================================================
# Same contract in both environments, different capacity. The failure this guards against
# is subtle: if staging and prod diverge in anything but capacity — a different major
# version, a different auth flow — then staging has stopped being evidence about prod,
# which is the only reason staging exists.
@pytest.fixture
def postgres_enabled(monkeypatch):
    data = json.loads(json.dumps(orc.CONFIG.data))
    data.setdefault("features", {}).update(
        {"application_values": True, "vault_secrets": True, "postgres_application": True})
    monkeypatch.setattr(orc, "CONFIG", orc.EnvConfig(data))
    return data


PG_SCORE = {
    "apiVersion": "score.dev/v1b1",
    "metadata": {"name": "api"},
    "containers": {"main": {"image": ".", "variables": {
        "PGHOST": "${resources.db.host}",
        "PGUSER": "${resources.db.username}",
        "PGPASSWORD": "${resources.db.password}"}}},
    "service": {"ports": {"http": {"port": 8080, "targetPort": 8080}}},
    "resources": {"db": {"type": "postgres", "class": "application"}},
}


def test_profile_of_the_active_environment_is_exposed_to_the_catalog():
    """A provisioner cannot name the environment it renders for — one catalog serves both —
    so the active profile is flattened under one stable prefix instead."""
    staging = orc.CONFIG.for_env("staging")
    prod = orc.CONFIG.for_env("prod")
    assert staging["computed.database.instances"] == 1
    assert prod["computed.database.instances"] == 3
    assert staging["computed.database.storage"] == "10Gi"
    assert prod["computed.database.storage"] == "100Gi"
    assert staging["computed.database.backup.retention_days"] == 3
    assert prod["computed.database.backup.retention_days"] == 30


def test_engine_version_is_identical_across_environments():
    """The one profile field that must NOT differ. A prod-only major version means every
    staging test ran against a different database than the one it was meant to prove."""
    assert (orc.CONFIG.for_env("staging")["computed.database.engine_version"]
            == orc.CONFIG.for_env("prod")["computed.database.engine_version"])


def test_database_image_is_repository_from_config_plus_version_from_profile():
    """Registry is infrastructure, major version is a platform decision; neither is
    hardcoded, and they are combined in exactly one place."""
    table = orc.CONFIG.for_env("staging")
    assert table["computed.database.image"] == \
        f"{orc.CONFIG.get('database.image_repository')}:{table['computed.database.engine_version']}"


def test_database_image_is_empty_when_the_repository_is_unset(monkeypatch):
    """An empty repository must not silently produce ':17', which resolves to a public
    image on whatever registry the node happens to default to."""
    data = json.loads(json.dumps(orc.CONFIG.data))
    data["database"]["image_repository"] = ""
    monkeypatch.setattr(orc, "CONFIG", orc.EnvConfig(data))
    assert orc.CONFIG.for_env("staging")["computed.database.image"] == ""


def test_database_storage_class_falls_back_to_the_cluster_default(monkeypatch):
    data = json.loads(json.dumps(orc.CONFIG.data))
    data["database"]["storage_class"] = ""
    monkeypatch.setattr(orc, "CONFIG", orc.EnvConfig(data))
    assert orc.CONFIG.for_env("staging")["computed.database.storage_class"] == \
        orc.CONFIG.get("kubernetes.storage_class")
    data["database"]["storage_class"] = "fast-ssd"
    monkeypatch.setattr(orc, "CONFIG", orc.EnvConfig(data))
    assert orc.CONFIG.for_env("staging")["computed.database.storage_class"] == "fast-ssd"


# ---------------------------------------------------------------- the demo-grade guard
def pg_service(tmp_path, klass=None, workload="api"):
    resource = {"type": "postgres"}
    if klass:
        resource["class"] = klass
    doc = {"apiVersion": "score.dev/v1b1", "metadata": {"name": workload},
           "containers": {"main": {"image": "."}}, "resources": {"db": resource}}
    path = tmp_path / f"{workload}.yaml"
    write(path, doc)
    return [(orc.Service(path=path, workload=workload, container="main"), doc)]


@pytest.mark.parametrize("klass", [None, "development"])
def test_the_demo_database_is_refused_in_prod(tmp_path, postgres_enabled, klass):
    """Single replica, 1Gi volume, no HA, no backup, password in render state. Nothing
    about a running deploy distinguishes it from a real database until data is lost."""
    with pytest.raises(SystemExit, match="refused in prod"):
        orc.check_database_classes(pg_service(tmp_path, klass), "prod")


@pytest.mark.parametrize("klass", [None, "development"])
def test_the_demo_database_is_still_allowed_in_staging(tmp_path, postgres_enabled, klass):
    orc.check_database_classes(pg_service(tmp_path, klass), "staging")


@pytest.mark.parametrize("klass", [None, "development"])
def test_the_guard_is_inert_until_the_platform_adopts_the_capability(
        tmp_path, klass, monkeypatch):
    """The brownfield promise: apps deployed before this existed keep rendering prod
    exactly as they always have, with features.postgres_application off."""
    data = json.loads(json.dumps(orc.CONFIG.data))
    data["features"]["postgres_application"] = False
    monkeypatch.setattr(orc, "CONFIG", orc.EnvConfig(data))
    orc.check_database_classes(pg_service(tmp_path, klass), "prod")


def test_prod_without_a_backup_target_is_refused(tmp_path, postgres_enabled):
    """Fail closed. A production database nobody can restore is not a database that is
    running; it is one that has not failed yet."""
    with pytest.raises(SystemExit, match="backup.object_store_url"):
        orc.check_database_classes(pg_service(tmp_path, "application"), "prod")


def test_prod_with_a_backup_target_is_allowed(tmp_path, monkeypatch, postgres_enabled):
    data = json.loads(json.dumps(orc.CONFIG.data))
    data["database"]["backup"]["object_store_url"] = "s3://backups/idp"
    monkeypatch.setattr(orc, "CONFIG", orc.EnvConfig(data))
    orc.check_database_classes(pg_service(tmp_path, "application"), "prod")


def test_staging_needs_no_backup_target(tmp_path, postgres_enabled):
    orc.check_database_classes(pg_service(tmp_path, "application"), "staging")


# --------------------------------------------------------------- verify waits for Ready
CLUSTER_DOC = {"apiVersion": "postgresql.cnpg.io/v1", "kind": "Cluster",
               "metadata": {"name": "pg-api-1234"}}


def cluster_cluster(monkeypatch, payload, returncode=0):
    def fake_kubectl(args, *, kubeconfig=None, **kw):
        return subprocess.CompletedProcess(args, returncode, json.dumps(payload), "")
    monkeypatch.setattr(orc, "kubectl", fake_kubectl)


def test_verify_accepts_a_ready_cluster(monkeypatch):
    cluster_cluster(monkeypatch, {"spec": {"instances": 3}, "status": {
        "readyInstances": 3, "phase": "Cluster in healthy state",
        "conditions": [{"type": "Ready", "status": "True"}]}})
    orc.wait_for_databases([CLUSTER_DOC], "ns", verify_args())


def test_verify_does_not_mistake_running_pods_for_a_ready_cluster(monkeypatch):
    """A three-replica cluster has pods up long before the replicas have joined, and an app
    that connects then gets 'the database system is starting up' — which reads like a
    config error, not like a database that is still coming up."""
    monkeypatch.setattr(orc, "CONFIG", orc.EnvConfig({"database": {"ready_timeout_seconds": 0}}))
    cluster_cluster(monkeypatch, {"spec": {"instances": 3}, "status": {
        "readyInstances": 1, "phase": "Setting up primary",
        "conditions": [{"type": "Ready", "status": "False", "reason": "Creating"}]}})
    with pytest.raises(SystemExit, match="chưa Ready"):
        orc.wait_for_databases([CLUSTER_DOC], "ns", verify_args())


def test_verify_is_inert_for_an_app_with_no_database(monkeypatch):
    def explode(*a, **k):
        raise AssertionError("must not query the cluster when there is no database")
    monkeypatch.setattr(orc, "kubectl", explode)
    orc.wait_for_databases([{"kind": "Deployment"}], "ns", verify_args())


def test_verify_ignores_a_cluster_from_another_api(monkeypatch):
    """`Cluster` is a popular kind. Matching on kind alone would make verify wait forever
    on someone else's CR."""
    def explode(*a, **k):
        raise AssertionError("must not query a Cluster from an unrelated API group")
    monkeypatch.setattr(orc, "kubectl", explode)
    orc.wait_for_databases([{"apiVersion": "cluster.x-k8s.io/v1beta1", "kind": "Cluster",
                             "metadata": {"name": "other"}}], "ns", verify_args())


# ------------------------------------------------------- generated credentials go to Vault
def test_generated_password_is_never_returned_to_a_caller_twice_the_same():
    """Platform-owned credentials nobody should see, type or paste."""
    args = argparse.Namespace(generate=True, stdin=False, key="password")
    first, second = orc.read_secret_value(args), orc.read_secret_value(args)
    assert first != second
    assert len(first) >= 32 and first.isalnum()


# =====================================================================================
# PHASE 4 — integration: one Score, two profiles, no credential in state
# =====================================================================================
@needs_score_k8s
def test_application_class_renders_a_managed_cluster(tmp_path, postgres_enabled):
    app_dir = tmp_path / "app"
    write(app_dir / "score.yaml", PG_SCORE)
    docs = render_env(tmp_path, app_dir, "staging", "w")
    cluster = next(d for d in docs if d["kind"] == "Cluster")
    assert cluster["apiVersion"] == "postgresql.cnpg.io/v1"
    assert cluster["spec"]["instances"] == 1
    assert cluster["spec"]["storage"]["size"] == "10Gi"
    # The app's user is created FROM the Vault-backed Secret, so there is exactly one
    # credential and no second copy for the operator.
    vss = next(d for d in docs if d["kind"] == "VaultStaticSecret")
    assert cluster["spec"]["bootstrap"]["initdb"]["secret"]["name"] == \
        vss["spec"]["destination"]["name"]
    assert vss["spec"]["destination"]["type"] == "kubernetes.io/basic-auth"
    assert vss["spec"]["path"] == "apps/payment-api/staging/database"


@needs_score_k8s
def test_the_database_password_never_reaches_git_or_state(tmp_path, postgres_enabled):
    """The defect the old provisioner has by construction: it generates the password during
    render, so the password IS the render state. Here the platform never sees one."""
    app_dir = tmp_path / "app"
    write(app_dir / "score.yaml", PG_SCORE)
    docs = render_env(tmp_path, app_dir, "staging", "w")
    dep = next(d for d in docs if d["kind"] == "Deployment")
    env = {e["name"]: e for e in dep["spec"]["template"]["spec"]["containers"][0]["env"]}
    assert "value" not in env["PGPASSWORD"]
    assert env["PGPASSWORD"]["valueFrom"]["secretKeyRef"]["key"] == "password"

    state = yaml.safe_load((tmp_path / "state.yaml").read_text())
    stored = next(v["state"] for k, v in state["resources"].items() if "postgres" in k)
    assert set(stored) == {"cluster", "database", "username"}, stored


@needs_score_k8s
def test_same_contract_different_profile_across_environments(tmp_path, monkeypatch,
                                                             postgres_enabled):
    """The headline gate: identical everything except capacity, HA and retention."""
    data = json.loads(json.dumps(orc.CONFIG.data))
    data["database"]["backup"]["object_store_url"] = "s3://backups/idp"
    monkeypatch.setattr(orc, "CONFIG", orc.EnvConfig(data))
    app_dir = tmp_path / "app"
    write(app_dir / "score.yaml", PG_SCORE)
    staging = next(d for d in render_env(tmp_path, app_dir, "staging", "w1")
                   if d["kind"] == "Cluster")
    prod = next(d for d in render_env(tmp_path, app_dir, "prod", "w2")
                if d["kind"] == "Cluster")

    assert staging["spec"]["imageName"] == prod["spec"]["imageName"]
    assert staging["spec"]["bootstrap"] == prod["spec"]["bootstrap"]
    assert staging["spec"]["enableSuperuserAccess"] == prod["spec"]["enableSuperuserAccess"]

    assert (staging["spec"]["instances"], prod["spec"]["instances"]) == (1, 3)
    assert (staging["spec"]["storage"]["size"], prod["spec"]["storage"]["size"]) == \
        ("10Gi", "100Gi")
    # Same backup MECHANISM, different retention — the profile difference, stated exactly.
    assert staging["spec"]["backup"]["barmanObjectStore"] == \
        prod["spec"]["backup"]["barmanObjectStore"]
    assert (staging["spec"]["backup"]["retentionPolicy"],
            prod["spec"]["backup"]["retentionPolicy"]) == ("3d", "30d")


@needs_score_k8s
def test_two_renders_of_a_database_are_byte_identical(tmp_path, postgres_enabled):
    app_dir = tmp_path / "app"
    write(app_dir / "score.yaml", PG_SCORE)
    render_env(tmp_path, app_dir, "staging", "w1")
    first = (tmp_path / "config" / "staging" / "manifests.yaml").read_text()
    render_env(tmp_path, app_dir, "staging", "w2")
    assert (tmp_path / "config" / "staging" / "manifests.yaml").read_text() == first


@needs_score_k8s
def test_the_legacy_postgres_class_still_renders_a_stateful_set(tmp_path):
    """Compatibility, stated as a test: every app using `type: postgres` today keeps the
    behaviour it has, feature flag off."""
    app_dir = tmp_path / "app"
    write(app_dir / "score.yaml", {
        "apiVersion": "score.dev/v1b1", "metadata": {"name": "api"},
        "containers": {"main": {"image": ".", "variables": {
            "PGHOST": "${resources.db.host}"}}},
        "resources": {"db": {"type": "postgres"}}})
    kinds = {d["kind"] for d in render_env(tmp_path, app_dir, "staging", "w")}
    assert "StatefulSet" in kinds and "Cluster" not in kinds


def test_a_configured_zero_timeout_means_zero(monkeypatch):
    """`int(CONFIG.get(k) or default)` reads naturally and is wrong: 0 is falsy, so a
    timeout set to zero silently becomes the default, and the symptom is a command that
    hangs for ten minutes instead of failing immediately."""
    monkeypatch.setattr(orc, "CONFIG", orc.EnvConfig(
        {"database": {"ready_timeout_seconds": 0}}))
    assert orc.config_int("database.ready_timeout_seconds", 600) == 0
    assert orc.config_int("database.missing_key", 600) == 600
    monkeypatch.setattr(orc, "CONFIG", orc.EnvConfig(
        {"database": {"ready_timeout_seconds": ""}}))
    assert orc.config_int("database.ready_timeout_seconds", 600) == 600


@needs_score_k8s
def test_no_object_store_means_no_backup_block_at_all(tmp_path, postgres_enabled):
    """Not an empty `backup:` — CNPG rejects a partial one at cluster level. Either a real
    backup target, or the section is absent and prod rendering is refused earlier."""
    app_dir = tmp_path / "app"
    write(app_dir / "score.yaml", PG_SCORE)
    cluster = next(d for d in render_env(tmp_path, app_dir, "staging", "w")
                   if d["kind"] == "Cluster")
    assert "backup" not in cluster["spec"]


@needs_score_k8s
def test_a_non_aws_object_store_gets_its_endpoint_into_the_cluster(tmp_path, monkeypatch,
                                                                   postgres_enabled):
    """Đo được trên harness: thiếu `endpointURL`, barman gọi thẳng s3.amazonaws.com và
    WAL archiving hỏng trong im lặng — Cluster vẫn Ready, database vẫn phục vụ, và không
    có gì để phục hồi. Mọi cài đặt on-prem (MinIO/Ceph) đều rơi vào trường hợp này."""
    data = json.loads(json.dumps(orc.CONFIG.data))
    data["database"]["backup"].update(
        {"object_store_url": "s3://idp-backup/",
         "endpoint_url": "http://minio.object-store.svc.cluster.local:9000",
         "credentials_secret": "backup-object-store"})
    monkeypatch.setattr(orc, "CONFIG", orc.EnvConfig(data))
    app_dir = tmp_path / "app"
    write(app_dir / "score.yaml", PG_SCORE)
    cluster = next(d for d in render_env(tmp_path, app_dir, "staging", "w")
                   if d["kind"] == "Cluster")
    store = cluster["spec"]["backup"]["barmanObjectStore"]
    assert store["endpointURL"] == "http://minio.object-store.svc.cluster.local:9000"
    assert store["destinationPath"] == "s3://idp-backup/"
    assert store["s3Credentials"]["accessKeyId"]["name"] == "backup-object-store"


@needs_score_k8s
def test_an_aws_object_store_carries_no_endpoint_override(tmp_path, monkeypatch,
                                                          postgres_enabled):
    """Rỗng phải nghĩa là "AWS S3", không phải một endpointURL rỗng — CNPG sẽ cố gọi
    chuỗi rỗng đó."""
    data = json.loads(json.dumps(orc.CONFIG.data))
    data["database"]["backup"]["object_store_url"] = "s3://backups/idp"
    monkeypatch.setattr(orc, "CONFIG", orc.EnvConfig(data))
    app_dir = tmp_path / "app"
    write(app_dir / "score.yaml", PG_SCORE)
    cluster = next(d for d in render_env(tmp_path, app_dir, "staging", "w")
                   if d["kind"] == "Cluster")
    assert "endpointURL" not in cluster["spec"]["backup"]["barmanObjectStore"]


@needs_score_k8s
def test_resource_quantities_render_as_strings_not_numbers(tmp_path, monkeypatch,
                                                           postgres_enabled):
    """Quantity của Kubernetes là CHUỖI. Profile prod đặt cpu "1", và nếu manifest ghi số
    1 thì API server lưu lại thành "1" — desired và live khác nhau mãi mãi, Fleet báo
    bundle `Modified` không bao giờ hết. Đo được trên cụm: bundle prod đứng ở 0/1 với
    `modified {"spec":{"resources":{"requests":{"cpu":1}}}}` trong khi app chạy đúng.
    Staging không bao giờ lộ ra vì `250m` không thể là số."""
    data = json.loads(json.dumps(orc.CONFIG.data))
    data["database"]["backup"]["object_store_url"] = "s3://backups/idp"
    monkeypatch.setattr(orc, "CONFIG", orc.EnvConfig(data))
    app_dir = tmp_path / "app"
    write(app_dir / "score.yaml", PG_SCORE)
    for env in ("staging", "prod"):
        cluster = next(d for d in render_env(tmp_path, app_dir, env, f"w-{env}")
                       if d["kind"] == "Cluster")
        requests = cluster["spec"]["resources"]["requests"]
        for key, value in requests.items():
            assert isinstance(value, str), f"{env}.{key} là {type(value).__name__}: {value!r}"
        assert isinstance(cluster["spec"]["storage"]["size"], str)


@needs_score_k8s
def test_an_object_store_also_gets_a_base_backup_schedule(tmp_path, monkeypatch,
                                                          postgres_enabled):
    """Lỗi thật thứ mười ba, đo trên cụm sống.

    `barmanObjectStore` MỘT MÌNH chỉ bật WAL archiving. CNPG không tự chụp lấy một base
    backup nào, và WAL không có base thì phục hồi được ĐÚNG KHÔNG GÌ CẢ. Đo được:
    Cluster `Ready`, condition `ContinuousArchiving=True` kèm thông điệp "Continuous
    archiving is working", WAL nằm thật trong bucket MinIO — mà một Cluster dựng bằng
    `bootstrap.recovery` từ chính kho đó chết ngay với `no target backup found`.

    Tức là guard `object_store_url` của Phase 4 đang bảo vệ một thứ không tồn tại."""
    data = json.loads(json.dumps(orc.CONFIG.data))
    data["database"]["backup"].update(
        {"object_store_url": "s3://idp-backup/",
         "endpoint_url": "http://minio.object-store.svc.cluster.local:9000",
         "credentials_secret": "backup-object-store"})
    monkeypatch.setattr(orc, "CONFIG", orc.EnvConfig(data))
    app_dir = tmp_path / "app"
    write(app_dir / "score.yaml", PG_SCORE)
    docs = render_env(tmp_path, app_dir, "staging", "w")
    cluster = next(d for d in docs if d["kind"] == "Cluster")
    sched = next((d for d in docs if d["kind"] == "ScheduledBackup"), None)
    assert sched, "có kho object nhưng không có ScheduledBackup — WAL sẽ không phục hồi được"
    assert sched["spec"]["cluster"]["name"] == cluster["metadata"]["name"]
    # `immediate` là phần quan trọng nhất: không có nó, mọi database mới sinh ra đều có
    # một cửa sổ dài tới một chu kỳ cron trong đó nó chạy, nhận ghi và KHÔNG phục hồi
    # được — đúng vào ngày người ta hay nhập dữ liệu khởi tạo nhất.
    assert sched["spec"]["immediate"] is True
    # Cron của CNPG có SÁU trường (giây đứng đầu). Năm trường kiểu Unix vẫn là YAML hợp lệ
    # và vẫn được CNPG nhận, nhưng bị đọc lệch một bậc: "0 2 * * *" thành "mỗi giờ".
    assert len(str(sched["spec"]["schedule"]).split()) == 6, sched["spec"]["schedule"]


def _legacy_pg_state(tmp_path: Path, uid: str) -> Path:
    p = tmp_path / "state.yaml"
    write(p, {"resources": {uid: {"state": {
        "service": "pg-api-54f63de0", "database": "db-haKaonqu",
        "username": "user-IUvGqfQK", "password": "cu-the"}}}})
    return p


def _pg_app(tmp_path: Path, klass: str | None) -> list:
    doc = copy.deepcopy(PG_SCORE)
    doc["resources"]["db"] = {"type": "postgres"}
    if klass:
        doc["resources"]["db"]["class"] = klass
    app_dir = tmp_path / "app"
    write(app_dir / "score.yaml", doc)
    return orc.discover(app_dir)


@pytest.mark.parametrize("old_class", ["default", "development"])
def test_switching_a_postgres_class_over_live_data_is_refused(tmp_path, old_class):
    """Lỗi thật thứ mười lăm. Đổi class là một RESOURCE KHÁC với score-k8s, nên nó dựng
    một database rỗng bên cạnh cái đang có dữ liệu, đổi cả host/database/user của app, và
    để dữ liệu cũ lại trên một PVC không còn ai trỏ tới — trong khi mọi thứ báo xanh."""
    state = _legacy_pg_state(tmp_path, f"postgres.{old_class}#api.db")
    services = _pg_app(tmp_path, "application")
    with pytest.raises(SystemExit, match="KHÔNG di chuyển dữ liệu"):
        orc.check_postgres_class_migration(services, state, accepted=False)


def test_the_class_switch_is_allowed_once_it_is_acknowledged(tmp_path):
    """Một app thật sự chưa có gì đáng giữ vẫn phải đi tiếp được — nhưng bằng một câu nói
    thẳng, không phải bằng im lặng."""
    state = _legacy_pg_state(tmp_path, "postgres.default#api.db")
    services = _pg_app(tmp_path, "application")
    orc.check_postgres_class_migration(services, state, accepted=True)


def test_an_already_migrated_resource_stops_warning(tmp_path):
    """Sau khi đã chuyển xong, state có CẢ HAI khoá. Cằn nhằn mãi thì lần render nào cũng
    phải truyền cờ, và một cờ luôn phải truyền là một cờ không còn ai đọc."""
    state = tmp_path / "state.yaml"
    write(state, {"resources": {
        "postgres.default#api.db": {"state": {"service": "pg-api-54f63de0"}},
        "postgres.application#api.db": {"state": {"cluster": "pg-api-be0342e7"}}}})
    orc.check_postgres_class_migration(_pg_app(tmp_path, "application"), state,
                                       accepted=False)


def test_a_brand_new_application_database_is_not_a_migration(tmp_path):
    """App mới toanh: state rỗng, không có gì để mất, không được hỏi gì cả."""
    state = tmp_path / "state.yaml"
    write(state, {"resources": {}})
    orc.check_postgres_class_migration(_pg_app(tmp_path, "application"), state,
                                       accepted=False)


def test_staying_on_the_old_class_is_never_blocked(tmp_path):
    """Lời hứa brownfield: app không đổi gì thì không bao giờ gặp guard này."""
    state = _legacy_pg_state(tmp_path, "postgres.default#api.db")
    orc.check_postgres_class_migration(_pg_app(tmp_path, None), state, accepted=False)


@needs_score_k8s
def test_the_app_role_password_is_reconciled_from_the_same_secret(tmp_path, postgres_enabled):
    """Lỗi thật thứ mười bốn: xoay vòng credential database sinh ra một Secret mà chính
    database TỪ CHỐI.

    `bootstrap.initdb.secret` chỉ được đọc một lần, lúc khởi tạo. Đo trên harness: ghi
    mật khẩu mới vào Vault -> VSO đồng bộ ra Secret -> `psql` bằng đúng giá trị trong
    Secret trả về `password authentication failed`. App đang chạy không hỏng ngay vì nó
    giữ mật khẩu cũ trong env của pod; nó hỏng ở lần restart kế tiếp, vì một lý do không
    liên quan, nhiều ngày sau.

    `managed.roles` là thứ làm cho xoay vòng có hiệu lực thật."""
    app_dir = tmp_path / "app"
    write(app_dir / "score.yaml", PG_SCORE)
    cluster = next(d for d in render_env(tmp_path, app_dir, "staging", "w")
                   if d["kind"] == "Cluster")
    roles = cluster["spec"]["managed"]["roles"]
    assert len(roles) == 1, roles
    role = roles[0]
    # Cùng một Secret mà initdb dùng, và cùng role mà app đăng nhập — nếu ba thứ này lệch
    # nhau thì xoay vòng lại im lặng hỏng theo một kiểu khác.
    assert role["name"] == cluster["spec"]["bootstrap"]["initdb"]["owner"]
    assert role["passwordSecret"]["name"] == \
        cluster["spec"]["bootstrap"]["initdb"]["secret"]["name"]
    assert role["login"] is True


@needs_score_k8s
def test_no_object_store_means_no_scheduled_backup_either(tmp_path, postgres_enabled):
    """Đối xứng với khối `backup` của Cluster: không có kho thì không sinh ScheduledBackup
    trỏ vào hư không."""
    app_dir = tmp_path / "app"
    write(app_dir / "score.yaml", PG_SCORE)
    docs = render_env(tmp_path, app_dir, "staging", "w")
    assert not [d for d in docs if d["kind"] == "ScheduledBackup"]


def test_an_object_store_without_a_schedule_is_refused(tmp_path, monkeypatch,
                                                       postgres_enabled):
    """Trường hợp nguy hiểm nhất, vì nó trông giống hệt một cấu hình đầy đủ: kho object
    có thật, WAL chảy thật, cụm báo `ContinuousArchiving=True` — và không phục hồi được
    gì. Chặn ở render, vì phát hiện lúc cần phục hồi là quá muộn theo đúng nghĩa đen."""
    data = json.loads(json.dumps(orc.CONFIG.data))
    data["database"]["backup"]["object_store_url"] = "s3://idp-backup/"
    data["database"]["backup"]["schedule"] = ""
    monkeypatch.setattr(orc, "CONFIG", orc.EnvConfig(data))
    app_dir = tmp_path / "app"
    write(app_dir / "score.yaml", PG_SCORE)
    scores = [(s, yaml.safe_load(s.path.read_text())) for s in orc.discover(app_dir)]
    with pytest.raises(SystemExit, match="base backup"):
        orc.check_database_classes(scores, "staging")


def test_the_environment_profile_can_override_the_backup_schedule(monkeypatch):
    """Prod thường phải chụp trong cửa sổ bảo trì do DBA chốt, staging thì lúc nào cũng
    được — nên profile ghi đè được mặc định chung, giống retention."""
    data = json.loads(json.dumps(orc.CONFIG.data))
    data["database"]["backup"]["schedule"] = "0 0 2 * * *"
    data["database_profiles"]["prod"]["application"]["backup"]["schedule"] = "0 30 3 * * 0"
    cfg = orc.EnvConfig(data)
    assert cfg.for_env("staging")["computed.database.backup.schedule"] == "0 0 2 * * *"
    assert cfg.for_env("prod")["computed.database.backup.schedule"] == "0 30 3 * * 0"


def test_verify_waits_for_a_first_recoverability_point(monkeypatch):
    """`Ready` và `ContinuousArchiving=True` đều KHÔNG nói gì về việc có phục hồi được
    hay không — cả hai đều True trên một cụm mà `bootstrap.recovery` fail. Trường duy
    nhất phân biệt là `firstRecoverabilityPoint`, nên `verify` khẳng định đúng trường đó.
    """
    monkeypatch.setattr(orc, "CONFIG", orc.EnvConfig(
        {"database": {"backup": {"first_backup_timeout_seconds": 1}}}))
    cluster = {"kind": "Cluster", "apiVersion": "postgresql.cnpg.io/v1",
               "metadata": {"name": "pg-api"},
               "spec": {"backup": {"barmanObjectStore": {"destinationPath": "s3://b/"}}}}
    args = orc.argparse.Namespace(app="a", env="staging", kubeconfig=None)

    status = {"conditions": [{"type": "Ready", "status": "True"}]}
    monkeypatch.setattr(orc, "kubectl", lambda *a, **k: orc.subprocess.CompletedProcess(
        [], 0, json.dumps({"status": status}), ""))
    with pytest.raises(SystemExit, match="firstRecoverabilityPoint|base backup"):
        orc.wait_for_recoverability([cluster], "ns", args)

    status["firstRecoverabilityPoint"] = "2026-08-11T05:32:52Z"
    orc.wait_for_recoverability([cluster], "ns", args)  # không còn raise


def test_verify_does_not_wait_for_backups_that_were_never_configured(monkeypatch):
    """Một Cluster staging không khai kho object thì không có gì để chờ; chờ nó là treo
    trọn timeout một cách vô ích."""
    called = []
    monkeypatch.setattr(orc, "kubectl", lambda *a, **k: called.append(a))
    args = orc.argparse.Namespace(app="a", env="staging", kubeconfig=None)
    orc.wait_for_recoverability(
        [{"kind": "Cluster", "metadata": {"name": "pg-api"}, "spec": {}}], "ns", args)
    assert not called


def test_rotation_refuses_a_cluster_rendered_before_managed_roles(monkeypatch):
    """Một Cluster dựng bằng catalog cũ không có `managed.roles`, nên CNPG sẽ không bao
    giờ đổi mật khẩu role. Ghi mật khẩu mới vào Vault lúc đó chỉ tạo ra một Secret mà
    database từ chối — im lặng, vì pod cũ vẫn chạy. Nên lệnh DỪNG trước khi ghi gì."""
    monkeypatch.setattr(orc, "CONFIG", orc.EnvConfig({}))
    monkeypatch.setattr(orc, "kubectl", lambda *a, **k: orc.subprocess.CompletedProcess(
        [], 0, json.dumps({"items": [{"metadata": {"name": "pg-api"}, "spec": {}}]}), ""))
    written = []
    monkeypatch.setattr(orc, "cmd_secret_set", lambda a: written.append(a))
    args = orc.argparse.Namespace(app="a", env="staging", kubeconfig=None)
    with pytest.raises(SystemExit, match="managed.roles"):
        orc.cmd_rotate_db_credential(args)
    assert not written, "đã ghi vào Vault dù biết database sẽ không nhận"


def test_rotation_runs_vault_then_vso_then_cnpg_then_pods(monkeypatch):
    """Thứ tự là toàn bộ giá trị của lệnh này. Restart pod TRƯỚC khi role đổi thì pod
    nhận mật khẩu mới trong khi database vẫn dùng mật khẩu cũ — tự tạo sự cố."""
    monkeypatch.setattr(orc, "CONFIG", orc.EnvConfig({}))
    monkeypatch.setattr(orc, "time", type("T", (), {
        "time": staticmethod(lambda: 0.0), "sleep": staticmethod(lambda s: None)})())
    steps: list[str] = []
    state = {"rv": "1"}

    def fake_kubectl(argv, **kwargs):
        joined = " ".join(argv)
        if argv[:2] == ["get", "cluster.postgresql.cnpg.io"] and "-o" in argv and "json" in argv:
            return orc.subprocess.CompletedProcess([], 0, json.dumps({"items": [{
                "metadata": {"name": "pg-api"},
                "spec": {"managed": {"roles": [
                    {"name": "app_api", "passwordSecret": {"name": "cred"}}]}}}]}), "")
        if argv[0] == "get" and argv[1] == "secret":
            return orc.subprocess.CompletedProcess([], 0, state["rv"], "")
        if argv[0] == "annotate":
            steps.append("cnpg-nudge")
            return orc.subprocess.CompletedProcess([], 0, "", "")
        if argv[:2] == ["get", "cluster.postgresql.cnpg.io"]:
            return orc.subprocess.CompletedProcess([], 0, state["rv"], "")
        if argv[:2] == ["get", "deploy"]:
            return orc.subprocess.CompletedProcess([], 0, json.dumps({"items": [{
                "metadata": {"name": "api"},
                "spec": {"template": {"spec": {"containers": [
                    {"env": [{"valueFrom": {"secretKeyRef": {"name": "cred"}}}]}]}}}}]}), "")
        if argv[0] == "rollout" and argv[1] == "restart":
            steps.append("restart")
        return orc.subprocess.CompletedProcess([], 0, "", "")

    def fake_secret_set(a):
        steps.append("vault-write")
        state["rv"] = "2"          # VSO đồng bộ ngay sau đó

    monkeypatch.setattr(orc, "kubectl", fake_kubectl)
    monkeypatch.setattr(orc, "cmd_secret_set", fake_secret_set)
    orc.cmd_rotate_db_credential(
        orc.argparse.Namespace(app="a", env="staging", kubeconfig=None))
    assert steps == ["vault-write", "cnpg-nudge", "restart"], steps


def test_rotation_only_restarts_workloads_that_use_the_credential(monkeypatch):
    """App hai workload: chỉ workload nào thật sự đọc Secret database mới bị restart.
    Đo trên cụm: `api` restart đúng một lần, `worker` giữ nguyên generation."""
    ns_deploys = {"items": [
        {"metadata": {"name": "api"}, "spec": {"template": {"spec": {"containers": [
            {"env": [{"valueFrom": {"secretKeyRef": {"name": "cred"}}}]}]}}}},
        {"metadata": {"name": "worker"}, "spec": {"template": {"spec": {"containers": [
            {"env": [{"valueFrom": {"secretKeyRef": {"name": "stripe"}}}]}]}}}}]}
    monkeypatch.setattr(orc, "kubectl", lambda *a, **k: orc.subprocess.CompletedProcess(
        [], 0, json.dumps(ns_deploys), ""))
    assert orc._consumers_of_secret("ns", "cred", None) == ["api"]


def _apply_secrets_args(tmp_path, **over):
    base = dict(app="banggia", env="staging", secrets=str(tmp_path / "none.yaml"),
                harbor_host="ghcr.io", harbor_user=None, harbor_pass=None, kubeconfig=None)
    base.update(over)
    return orc.argparse.Namespace(**base)


def test_a_registry_secret_is_never_created_with_missing_credentials(tmp_path, monkeypatch,
                                                                     capsys):
    """Lỗi thật thứ mười sáu, đo trên cụm.

    Thiếu REGISTRY_USER/REGISTRY_PASS mà vẫn `create secret docker-registry` thì Python
    nội suy `None` thành CHUỖI "None". Cụm nhận một credential TRÔNG NHƯ đã cấu hình đầy
    đủ: `kubectl get secret` thấy registry-pull, đúng kiểu dockerconfigjson, có dữ liệu.
    Ảnh thì không kéo được, và lỗi là `403 Forbidden` từ registry — không một chữ nào
    nhắc tới biến môi trường còn thiếu. Và vì đây là create-if-missing, cái Secret hỏng đó
    sống mãi cho tới khi có người xoá tay."""
    calls = []
    monkeypatch.setattr(orc, "kubectl", lambda argv, **k: calls.append(argv) or
                        orc.subprocess.CompletedProcess([], 0, "namespace/x", ""))
    orc.cmd_apply_secrets(_apply_secrets_args(tmp_path))
    created = [c for c in calls if c[:3] == ["create", "secret", "docker-registry"]]
    assert not created, "đã tạo pull secret với credential rỗng"
    err = capsys.readouterr().err
    assert "REGISTRY_USER" in err and "REGISTRY_PASS" in err


def test_a_registry_secret_is_created_when_credentials_are_present(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(orc, "kubectl", lambda argv, **k: calls.append(argv) or
                        orc.subprocess.CompletedProcess([], 0, "namespace/x", ""))
    orc.cmd_apply_secrets(_apply_secrets_args(tmp_path, harbor_user="u", harbor_pass="p"))
    created = [c for c in calls if c[:3] == ["create", "secret", "docker-registry"]]
    assert len(created) == 1, calls
    assert "--docker-username=u" in created[0]


def _backup_cfg(**over):
    data = {"database": {"backup": {"object_store_url": "s3://idp-backup/",
                                    "credentials_secret": "backup-object-store"}}}
    data["database"]["backup"].update(over)
    return orc.EnvConfig(data)


def test_backup_credentials_are_created_in_the_app_namespace(monkeypatch):
    """Lỗi thật thứ mười bảy: provisioner sinh `barmanObjectStore` tham chiếu một Secret
    mà KHÔNG ai tạo trong namespace onboarding vừa dựng — và CNPG không đọc chéo
    namespace. Đo trên cụm: ScheduledBackup chạy ngay như thiết kế, Backup đứng ở
    `walArchivingFailing`, firstRecoverabilityPoint không bao giờ xuất hiện, database vẫn
    Ready và vẫn phục vụ."""
    monkeypatch.setattr(orc, "CONFIG", _backup_cfg())
    calls = []

    def fake_kubectl(argv, **k):
        calls.append(argv)
        rc = 1 if argv[:2] == ["get", "secret"] else 0   # chưa tồn tại
        return orc.subprocess.CompletedProcess([], rc, "", "")

    monkeypatch.setattr(orc, "kubectl", fake_kubectl)
    orc.ensure_backup_credentials("sinhvien-staging", orc.argparse.Namespace(
        kubeconfig=None, backup_key_id="k", backup_secret_key="s"))
    created = [c for c in calls if c[:3] == ["create", "secret", "generic"]]
    assert len(created) == 1, calls
    assert "backup-object-store" in created[0] and "-n" in created[0]
    assert "sinhvien-staging" in created[0]


def test_backup_credentials_are_never_created_empty(monkeypatch, capsys):
    """Cùng kỷ luật với registry-pull: một credential sai còn tệ hơn không có, vì
    create-if-missing khiến nó sống mãi."""
    monkeypatch.setattr(orc, "CONFIG", _backup_cfg())
    calls = []
    monkeypatch.setattr(orc, "kubectl", lambda argv, **k: calls.append(argv) or
                        orc.subprocess.CompletedProcess([], 1, "", ""))
    monkeypatch.delenv("BACKUP_ACCESS_KEY_ID", raising=False)
    monkeypatch.delenv("BACKUP_ACCESS_SECRET_KEY", raising=False)
    orc.ensure_backup_credentials("sinhvien-staging", orc.argparse.Namespace(
        kubeconfig=None, backup_key_id=None, backup_secret_key=None))
    assert not [c for c in calls if c[:3] == ["create", "secret", "generic"]]
    err = capsys.readouterr().err
    assert "BACKUP_ACCESS_KEY_ID" in err and "phục hồi" in err


def test_no_object_store_means_no_backup_secret(monkeypatch):
    """Không cấu hình kho object thì không có gì để tạo — và không được cằn nhằn."""
    monkeypatch.setattr(orc, "CONFIG", _backup_cfg(object_store_url=""))
    calls = []
    monkeypatch.setattr(orc, "kubectl", lambda argv, **k: calls.append(argv) or
                        orc.subprocess.CompletedProcess([], 1, "", ""))
    orc.ensure_backup_credentials("x-staging", orc.argparse.Namespace(
        kubeconfig=None, backup_key_id="k", backup_secret_key="s"))
    assert not calls


def test_an_existing_backup_secret_is_left_alone(monkeypatch):
    """Create-if-missing: không bao giờ ghi đè một credential đang chạy."""
    monkeypatch.setattr(orc, "CONFIG", _backup_cfg())
    calls = []
    monkeypatch.setattr(orc, "kubectl", lambda argv, **k: calls.append(argv) or
                        orc.subprocess.CompletedProcess([], 0, "secret/x", ""))
    orc.ensure_backup_credentials("x-staging", orc.argparse.Namespace(
        kubeconfig=None, backup_key_id="k", backup_secret_key="s"))
    assert not [c for c in calls if c[0] == "create"]


def test_every_postgres_provisioner_declares_its_class():
    """A provisioner without `class` matches EVERY class, and when several match, the one
    score-k8s happens to load last wins — an order that depends on temp filenames it
    generates itself. Measured on 0.15.0: the same input rendered `class: application` as
    the demo StatefulSet on some runs and as a managed Cluster on others, with nothing
    changed in between. For a database that means the data is in two different places.

    So: every postgres provisioner names its class, AND no two postgres provisioners that
    are handed to score-k8s TOGETHER share a class.

    The catalog now ships two `class: application` files (CNPG and StatefulSet), but they
    are mutually exclusive by construction — select_provisioner_files hands exactly one to
    a render, chosen by database.backend. So the no-duplicate-class invariant is asserted
    over the SELECTED set of each backend, not the raw glob; that is a stronger check,
    since it is precisely the set score-k8s actually sees."""
    # Every postgres provisioner in the whole catalog still declares a class.
    for path in (CATALOG / "provisioners").glob("*.provisioners.yaml"):
        for e in (yaml.safe_load(path.read_text()) or []):
            if e.get("type") == "postgres":
                assert e.get("class"), f"{e['uri']} does not declare a class"

    # For EACH backend, the set score-k8s is handed has no two postgres provisioners
    # sharing a class.
    for backend in orc.DATABASE_BACKENDS:
        orig = orc.CONFIG
        try:
            orc.CONFIG = orc.EnvConfig({"database": {"backend": backend}})
            selected = orc.select_provisioner_files(CATALOG)
        finally:
            orc.CONFIG = orig
        classes = [e["class"] for p in selected
                   for e in (yaml.safe_load(p.read_text()) or [])
                   if e.get("type") == "postgres"]
        assert classes, f"backend {backend}: no postgres provisioner selected"
        assert len(classes) == len(set(classes)), \
            f"backend {backend}: two provisioners share a class: {classes}"
        assert "application" in classes, f"backend {backend}: no class:application served"


@needs_score_k8s
def test_the_development_class_renders_the_demo_database(tmp_path):
    """`class: development` is the same demo database under its real name."""
    app_dir = tmp_path / "app"
    write(app_dir / "score.yaml", {
        "apiVersion": "score.dev/v1b1", "metadata": {"name": "api"},
        "containers": {"main": {"image": ".", "variables": {
            "PGHOST": "${resources.db.host}"}}},
        "resources": {"db": {"type": "postgres", "class": "development"}}})
    kinds = {d["kind"] for d in render_env(tmp_path, app_dir, "staging", "w")}
    assert "StatefulSet" in kinds and "Cluster" not in kinds


# =====================================================================================
# PHASE 5 — stack catalog: the catalog itself must stay coherent
# =====================================================================================
# These are static checks over templates/stacks/. They are cheap and they catch the class
# of mistake that only shows up as a broken app repo three steps later: a stack naming a
# component that does not exist, a workload with no container name, a token nobody fills.
STACK_IDS = ["node-fullstack", "node-api", "node-worker", "static-frontend"]


def test_every_published_stack_loads_and_resolves():
    """Filename, metadata.id and every referenced component/capability must line up."""
    ids = [d["metadata"]["id"] for d in orc.list_stacks(CATALOG)]
    assert sorted(ids) == sorted(STACK_IDS), ids
    for stack_id in STACK_IDS:
        stack = orc.load_stack(CATALOG, stack_id)
        assert stack["metadata"]["version"], stack_id
        components = orc.stack_components(CATALOG, stack)
        assert components, stack_id
        for capability in (stack["spec"].get("capabilities") or []):
            orc.load_capability(CATALOG, capability)


def test_every_workload_component_names_its_container():
    """`--build` addresses containers, not workloads. A component missing this renders an
    app whose compose file still says `image: .`, and that only fails inside docker."""
    for stack_id in STACK_IDS:
        for component in orc.stack_components(CATALOG, orc.load_stack(CATALOG, stack_id)):
            if orc._is_workload(component):
                assert component.get("container"), (stack_id, component["id"])


def test_node_fullstack_is_the_sum_of_its_parts():
    """Section 9.3: node-fullstack = static-frontend + node-api + postgres. If this ever
    becomes a standalone copy, fixing node-api stops fixing node-fullstack."""
    stack = orc.load_stack(CATALOG, "node-fullstack")
    ids = {c["id"] for c in orc.stack_components(CATALOG, stack)}
    assert {"static-frontend", "node-api"} <= ids
    assert "database" in stack["spec"]["capabilities"]
    assert stack["spec"]["tagStrategy"] == "commit"


def test_the_golden_path_routes_api_deeper_than_the_frontend():
    """Same-origin routing only works if the API path is a strict prefix extension of the
    frontend's. `/` for the frontend and `/api` for the API is the whole contract."""
    components = orc.stack_components(CATALOG, orc.load_stack(CATALOG, "node-fullstack"))
    paths = {c["id"]: c.get("routePath") for c in components if orc._is_workload(c)}
    assert paths["static-frontend"] == "/"
    assert paths["node-api"] == "/api"
    assert len(paths["node-api"]) > len(paths["static-frontend"])


# ------------------------------------------------- the compose catalog, same class rule
def test_local_postgres_provisioner_declares_its_class():
    """The Phase 4 lesson, re-applied to score-compose: a provisioner with no `class`
    matches EVERY class, and score-compose ships a classless `postgres` of its own. Without
    an explicit class here, `class: application` would be served by whichever loaded last."""
    path = CATALOG / "templates" / "score-compose" / "postgres-application.provisioners.yaml"
    entries = [e for e in yaml.safe_load(path.read_text()) if e.get("type") == "postgres"]
    assert entries, "no postgres provisioner in the compose catalog"
    for entry in entries:
        assert entry.get("class") == "application", entry["uri"]


def test_local_and_cluster_postgres_agree_on_the_output_contract():
    """An app must not be able to tell local from staging. Same output keys, same naming."""
    local = yaml.safe_load(
        (CATALOG / "templates" / "score-compose"
         / "postgres-application.provisioners.yaml").read_text())[0]
    cluster = yaml.safe_load(
        (CATALOG / "provisioners" / "postgres-application.provisioners.yaml").read_text())[0]
    assert set(local["expected_outputs"]) == set(cluster["expected_outputs"])
    # Both derive database and username the same way, so a migration that hardcodes the
    # schema owner locally still applies on the cluster.
    for provisioner in (local, cluster):
        assert 'print "app_" (.SourceWorkload | replace "-" "_")' in provisioner["state"]


def test_the_local_route_provisioner_ranks_by_path_not_by_workload_name():
    """The measured bug: score-compose's default route provisioner keys its shared map by
    `.Uid`, so nginx emits locations in WORKLOAD-NAME order — and nginx takes the first
    matching regex. Name the frontend so it sorts first and `^/` swallows every `/api`
    request. Gateway API on the cluster ranks by prefix length instead, so the default
    makes local and staging disagree. Our key carries the rank, so names cannot matter."""
    path = CATALOG / "templates" / "score-compose" / "route.provisioners.yaml"
    shared = yaml.safe_load(path.read_text())[0]["shared"]
    assert "sub 999 (len .Params.path)" in shared
    assert "dict $rank $inner" in shared, "the shared map must be keyed by rank, not .Uid"


def test_the_local_route_provisioner_pins_a_short_dns_ttl():
    """Also measured: without `valid=`, nginx caches the workload's IP for Docker's TTL, so
    every `docker compose up --build` leaves the API 502 while its container runs fine."""
    text = (CATALOG / "templates" / "score-compose" / "route.provisioners.yaml").read_text()
    assert re.search(r"resolver 127\.0\.0\.11 valid=\d+s", text)


def test_local_postgres_major_version_must_match_the_staging_profile(monkeypatch):
    """`make dev` claims to rehearse staging. Different major versions make that false."""
    orc.check_local_postgres_image()          # the repo's own config must be consistent

    data = json.loads(json.dumps(orc.CONFIG.data))
    data["images"]["postgres"] = "registry.example/postgres:16-alpine"
    monkeypatch.setattr(orc, "CONFIG", orc.EnvConfig(data))
    with pytest.raises(SystemExit, match="different PostgreSQL major version"):
        orc.check_local_postgres_image()


# =====================================================================================
# PHASE 5 — the generator
# =====================================================================================
@pytest.fixture
def stack_enabled(monkeypatch):
    data = json.loads(json.dumps(orc.CONFIG.data))
    data.setdefault("features", {}).update(
        {"application_values": True, "vault_secrets": True,
         "postgres_application": True, "stack_onboarding": True})
    monkeypatch.setattr(orc, "CONFIG", orc.EnvConfig(data))
    return data


def new_app(tmp_path: Path, stack_id: str = "node-fullstack", app: str = "shopdemo",
            **kw) -> Path:
    dest = tmp_path / app
    orc.generate_stack(CATALOG, stack_id, app, dest, owner="team-x", **kw)
    return dest


def test_generated_repo_has_the_layout_section_10_1_describes(tmp_path, stack_enabled):
    dest = new_app(tmp_path)
    for rel in ("frontend/Dockerfile", "frontend/score.yaml", "backend/Dockerfile",
                "backend/score.yaml", ".idp/stack.yaml", ".score-values/values.yaml",
                "platform.lock", "Makefile", ".env.example", "README.md"):
        assert (dest / rel).is_file(), rel


def test_generated_files_carry_no_unresolved_tokens(tmp_path, stack_enabled):
    """A `__TOKEN__` that survives into an app repo is a defect nobody notices until a
    developer reads their own Dockerfile. The generator refuses; this proves it."""
    dest = new_app(tmp_path)
    for path in dest.rglob("*"):
        if path.is_file():
            assert not orc.STACK_TOKEN.search(path.read_text()), path


def test_an_unfillable_token_stops_the_generator():
    with pytest.raises(SystemExit, match="unresolved template token"):
        orc._substitute("port: __NOT_A_REAL_TOKEN__", {"__APP__": "x"}, where="t")


def test_generated_values_and_stack_files_pass_their_own_validators(tmp_path, stack_enabled):
    """The generator must not emit something the renderer will later reject."""
    dest = new_app(tmp_path)
    spec = orc.load_application_values(dest)
    assert set(spec["environments"]) == {"staging", "prod"}
    instance = orc.load_stack_instance(dest)
    assert instance["stack"] == {"id": "node-fullstack", "version": "1.0.0"}
    assert instance["tagStrategy"] == "commit"


def test_generated_score_files_are_discoverable_and_placeholder_clean(tmp_path,
                                                                     stack_enabled):
    dest = new_app(tmp_path)
    services = orc.discover(dest)
    assert {s.workload for s in services} == {"backend", "frontend"}
    for service in services:
        doc = yaml.safe_load(service.path.read_text())
        orc.scan_placeholders(doc, where=str(service.path), hard=True)


def test_env_example_keys_cannot_drift_from_the_values_file(tmp_path, stack_enabled):
    """score-compose's environment provisioner turns a MISSING variable into an empty
    string, not an error, so a drift here starts containers with blank configuration."""
    dest = new_app(tmp_path)
    spec = orc.load_application_values(dest)
    expected = set(orc.resolve_application_values(spec, "staging"))
    found = {line.split("=", 1)[0]
             for line in (dest / ".env.example").read_text().splitlines()
             if line.strip() and not line.startswith("#")}
    assert found == expected


def test_local_values_override_staging_for_the_developer_machine(tmp_path, stack_enabled):
    """Local is not a third environment; it is staging with the host name swapped, so a
    browser resolves it without anyone editing /etc/hosts."""
    dest = new_app(tmp_path)
    env_example = dict(
        line.split("=", 1) for line in (dest / ".env.example").read_text().splitlines()
        if line.strip() and not line.startswith("#"))
    assert env_example["PUBLIC_HOST"] == "shopdemo.localhost"
    values = orc.resolve_application_values(orc.load_application_values(dest), "staging")
    assert values["PUBLIC_HOST"].endswith(orc.CONFIG.get("environments.staging.domain"))


def test_rerunning_the_generator_creates_no_duplicates(tmp_path, stack_enabled):
    """The onboarding workflow retries. A retry must not overwrite a developer's work."""
    dest = new_app(tmp_path)
    (dest / "backend" / "src" / "index.js").write_text("// mã của đội ứng dụng\n")
    again = orc.generate_stack(CATALOG, "node-fullstack", "shopdemo", dest)
    assert again["created"] == []
    assert "backend/src/index.js" in again["skipped"]
    assert (dest / "backend" / "src" / "index.js").read_text() == "// mã của đội ứng dụng\n"

    forced = orc.generate_stack(CATALOG, "node-fullstack", "shopdemo", dest, force=True)
    assert forced["skipped"] == []
    assert "mã của đội ứng dụng" not in (dest / "backend" / "src" / "index.js").read_text()


@pytest.mark.parametrize("name", ["Shop", "shop_demo", "-shop", "shop-", "s" * 41, ""])
def test_bad_application_names_are_refused(name):
    """The name becomes a namespace prefix, an image name and an npm scope at once."""
    with pytest.raises(SystemExit, match="invalid application name"):
        orc.validate_app_name(name)


def test_the_worker_stack_has_no_route_and_no_service(tmp_path, stack_enabled):
    """The one shape difference between `worker` and `web-api`. Everything else — config,
    credentials, render path, verify — goes down the same road."""
    dest = new_app(tmp_path, "node-worker", "batchdemo")
    doc = yaml.safe_load((dest / "worker" / "score.yaml").read_text())
    assert "service" not in doc
    assert "route" not in doc["resources"]
    assert "db" in doc["resources"]


def test_a_stack_without_the_database_capability_gets_no_db_resource(tmp_path,
                                                                    stack_enabled):
    """Capabilities are composed in, not commented out: an unwanted one leaves no trace."""
    dest = new_app(tmp_path, "static-frontend", "uidemo")
    doc = yaml.safe_load((dest / "frontend" / "score.yaml").read_text())
    assert set(doc["resources"]) == {"config", "route"}
    assert "PGHOST" not in (dest / "frontend" / "score.yaml").read_text()


def test_generate_steps_use_one_call_per_score_file(tmp_path, stack_enabled):
    """score-compose refuses `--build` when several score files are passed at once, so the
    Makefile must call it once per workload — and address the CONTAINER, not the workload."""
    components = orc.stack_components(CATALOG, orc.load_stack(CATALOG, "node-fullstack"))
    steps = orc.stack_generate_steps(components)
    assert steps.count("score-compose generate") == 2
    assert "--build 'api=" in steps and "--build 'web=" in steps
    assert "backend/score.yaml --build" in steps


def test_dockerfiles_build_from_the_repo_root_so_they_see_the_shared_package(tmp_path,
                                                                            stack_enabled):
    """A monorepo workload importing `shared/` cannot build with its own directory as
    context; npm fails to resolve the workspace and the error names neither."""
    dest = new_app(tmp_path)
    steps = orc.stack_generate_steps(
        orc.stack_components(CATALOG, orc.load_stack(CATALOG, "node-fullstack")))
    assert '"context":"."' in steps
    for rel in ("backend/Dockerfile", "frontend/Dockerfile"):
        text = (dest / rel).read_text()
        assert "COPY shared/package.json shared/" in text
        assert "COPY shared/ ./shared/" in text


def test_the_vendored_compose_catalog_carries_no_placeholders(tmp_path, stack_enabled):
    """`make dev` needs docker and score-compose, nothing else — so the provisioners the
    app repo receives must already be resolved, never the platform's config file."""
    dest = new_app(tmp_path)
    vendored = sorted((dest / ".idp" / "score-compose").glob("*.provisioners.yaml"))
    assert len(vendored) == 2
    for path in vendored:
        assert "%%" not in path.read_text(), path


# ------------------------------------------------------------------- tag strategy
def test_an_app_without_a_stack_file_keeps_the_historical_default(tmp_path):
    """The brownfield promise: every app deployed before Phase 5 renders exactly as before."""
    assert orc.resolve_tag_strategy(tmp_path, "") == "content"
    assert orc.resolve_tag_strategy(None, "") == "content"


def test_the_stack_file_supplies_the_strategy_once_the_flag_is_on(tmp_path, stack_enabled):
    dest = new_app(tmp_path)
    assert orc.resolve_tag_strategy(dest, "") == "commit"


def test_the_declared_strategy_is_inert_and_loud_while_the_flag_is_off(
        tmp_path, capsys, values_enabled, monkeypatch):
    """Opt-in means opt-in. But silently ignoring it would ship stale images from a
    monorepo, so it says so."""
    data = json.loads(json.dumps(orc.CONFIG.data))
    data["features"]["stack_onboarding"] = False
    monkeypatch.setattr(orc, "CONFIG", orc.EnvConfig(data))
    dest = new_app(tmp_path)
    assert orc.resolve_tag_strategy(dest, "") == "content"
    assert "features.stack_onboarding is off" in capsys.readouterr().err


def test_an_explicit_flag_still_wins(tmp_path, stack_enabled):
    dest = new_app(tmp_path)
    assert orc.resolve_tag_strategy(dest, "content") == "content"


def test_a_broken_stack_file_does_not_take_down_a_deploy(tmp_path):
    """`.idp/stack.yaml` is not on the deploy critical path. A malformed one is reported by
    stack-validate; it must not stop a render that never needed it."""
    (tmp_path / ".idp").mkdir()
    (tmp_path / ".idp" / "stack.yaml").write_text("this: is: not: valid\n")
    assert orc.resolve_tag_strategy(tmp_path, "") == "content"


# ------------------------------------------------------------------- validate / upgrade
def test_stack_validate_catches_a_renamed_workload(tmp_path, stack_enabled):
    """Renaming metadata.name renames the image and the Deployment, so the old workload
    keeps running beside the new one instead of being replaced."""
    dest = new_app(tmp_path)
    doc = yaml.safe_load((dest / "backend" / "score.yaml").read_text())
    doc["metadata"]["name"] = "backend-v2"
    write(dest / "backend" / "score.yaml", doc)
    args = orc.argparse.Namespace(app_dir=str(dest), catalog=str(CATALOG))
    with pytest.raises(SystemExit, match="metadata.name"):
        orc.cmd_stack_validate(args)


def test_stack_validate_reports_a_capability_whose_flag_is_off(
        tmp_path, values_enabled, monkeypatch):
    data = json.loads(json.dumps(orc.CONFIG.data))
    data["features"]["postgres_application"] = False
    monkeypatch.setattr(orc, "CONFIG", orc.EnvConfig(data))
    dest = new_app(tmp_path)
    args = orc.argparse.Namespace(app_dir=str(dest), catalog=str(CATALOG))
    with pytest.raises(SystemExit, match="features.postgres_application"):
        orc.cmd_stack_validate(args)


def test_stack_upgrade_reports_no_change_for_a_freshly_generated_app(tmp_path, capsys,
                                                                    stack_enabled):
    dest = new_app(tmp_path)
    orc.cmd_stack_upgrade(orc.argparse.Namespace(
        app_dir=str(dest), catalog=str(CATALOG), app="", write=False, all=False, work=""))
    assert "không có thay đổi" in capsys.readouterr().err


def test_stack_upgrade_diffs_platform_owned_files_without_touching_the_repo(tmp_path,
                                                                           capsys,
                                                                           stack_enabled):
    """Section 9.4: an upgrade is a pull request a human reads, not an overwrite."""
    dest = new_app(tmp_path)
    makefile = dest / "Makefile"
    makefile.write_text(makefile.read_text().replace("ROUTER_PORT ?= 8080",
                                                     "ROUTER_PORT ?= 9999"))
    args = orc.argparse.Namespace(app_dir=str(dest), catalog=str(CATALOG), app="",
                                  write=False, all=False, work="")
    orc.cmd_stack_upgrade(args)
    assert "ROUTER_PORT ?= 9999" in makefile.read_text(), "upgrade must not write by default"
    assert "--- a/Makefile" in capsys.readouterr().out

    args.write = True
    orc.cmd_stack_upgrade(args)
    assert "ROUTER_PORT ?= 8080" in makefile.read_text()


def test_stack_upgrade_leaves_application_code_alone_by_default(tmp_path, stack_enabled):
    dest = new_app(tmp_path)
    mine = dest / "backend" / "src" / "index.js"
    mine.write_text("// mã của tôi\n")
    orc.cmd_stack_upgrade(orc.argparse.Namespace(
        app_dir=str(dest), catalog=str(CATALOG), app="", write=True, all=False, work=""))
    assert mine.read_text() == "// mã của tôi\n"


# =====================================================================================
# PHASE 5 — integration: the generated app renders to the cluster shape it promises
# =====================================================================================
def render_stack_app(tmp_path: Path, app_dir: Path, app: str, env: str, name: str) -> list[dict]:
    args = orc.argparse.Namespace(
        app=app, image=app, tag="deadbeef", env=env, registry="h.io/p",
        catalog=str(CATALOG), app_dir=str(app_dir), work=str(tmp_path / name),
        out=str(tmp_path / "config" / env / "manifests.yaml"), kubeconfig=None,
        state_file=str(tmp_path / f"state-{name}.yaml"), no_state=False, tag_strategy="")
    orc.cmd_render(args)
    return orc.load_all(Path(args.work) / "manifests.yaml")


@needs_score_k8s
def test_the_golden_path_renders_same_origin_routes(tmp_path, stack_enabled):
    """The headline gate, on the cluster side: one hostname, two paths, and `/api` more
    specific than `/`. Gateway API ranks PathPrefix by length, so this is what makes the
    browser treat the API as same-origin and skip CORS entirely."""
    dest = new_app(tmp_path)
    docs = render_stack_app(tmp_path, dest, "shopdemo", "staging", "w")
    routes = [d for d in docs if d["kind"] == "HTTPRoute"]
    assert len(routes) == 2
    assert len({tuple(r["spec"]["hostnames"]) for r in routes}) == 1, "must share one origin"
    by_path = {r["spec"]["rules"][0]["matches"][0]["path"]["value"]:
               r["spec"]["rules"][0]["backendRefs"][0] for r in routes}
    assert set(by_path) == {"/", "/api"}
    assert by_path["/api"]["name"] == "backend" and by_path["/api"]["port"] == 8080
    assert by_path["/"]["name"] == "frontend" and by_path["/"]["port"] == 80
    for route in routes:
        assert route["spec"]["rules"][0]["matches"][0]["path"]["type"] == "PathPrefix"


@needs_score_k8s
def test_the_monorepo_ships_one_tag_for_every_workload(tmp_path, stack_enabled):
    """`tagStrategy: commit` from .idp/stack.yaml, with no --tag-strategy on the command
    line. Under `content` the two workloads would carry different directory hashes and a
    change to shared/ would retag neither."""
    dest = new_app(tmp_path)
    docs = render_stack_app(tmp_path, dest, "shopdemo", "staging", "w")
    images = {c["image"] for d in docs if d["kind"] == "Deployment"
              for c in d["spec"]["template"]["spec"]["containers"]}
    assert images == {"h.io/p/shopdemo-backend:deadbeef", "h.io/p/shopdemo-frontend:deadbeef"}


@needs_score_k8s
def test_the_generated_app_gets_a_managed_database_and_no_plaintext_password(tmp_path,
                                                                            stack_enabled):
    dest = new_app(tmp_path)
    docs = render_stack_app(tmp_path, dest, "shopdemo", "staging", "w")
    assert any(d["kind"] == "Cluster" for d in docs)
    backend = next(d for d in docs if d["kind"] == "Deployment"
                   and d["metadata"]["name"] == "backend")
    env = {e["name"]: e for e in backend["spec"]["template"]["spec"]["containers"][0]["env"]}
    assert "value" not in env["PGPASSWORD"]
    assert env["PGPASSWORD"]["valueFrom"]["secretKeyRef"]["key"] == "password"
    assert env["LOG_LEVEL"]["value"] == "info"


@needs_score_k8s
def test_the_frontend_receives_no_api_address_at_runtime(tmp_path, stack_enabled):
    """Section 10.2: the bundle is built, shipped to a browser, and cannot read container
    environment variables. If a stack ever starts injecting one, the same-origin contract
    has been abandoned without anyone deciding to."""
    dest = new_app(tmp_path)
    docs = render_stack_app(tmp_path, dest, "shopdemo", "staging", "w")
    frontend = next(d for d in docs if d["kind"] == "Deployment"
                    and d["metadata"]["name"] == "frontend")
    container = frontend["spec"]["template"]["spec"]["containers"][0]
    for entry in container.get("env") or []:
        assert "api" not in (entry.get("value") or "").lower(), entry


# =====================================================================================
# PHASE 5 — integration with the pinned score-compose binary
# =====================================================================================
HAS_SCORE_COMPOSE = shutil.which("score-compose") is not None
HAS_MAKE = shutil.which("make") is not None
needs_score_compose = pytest.mark.skipif(
    not (HAS_SCORE_COMPOSE and HAS_MAKE),
    reason="score-compose or make not installed")


def make_generate(app_dir: Path) -> dict:
    """Run the app's OWN `make generate` — the same recipe a developer runs.

    Driving the generated Makefile rather than reimplementing its steps is the point: the
    recipe, the vendored provisioners and the pinned binary are exactly what can drift apart,
    so the test has to exercise all three together. Only `docker compose up` is left out.
    """
    (app_dir / ".env").write_text((app_dir / ".env.example").read_text())
    proc = subprocess.run(["make", "generate"], cwd=app_dir,
                          capture_output=True, text=True)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    return yaml.safe_load((app_dir / "compose.yaml").read_text())


def nginx_conf(app_dir: Path) -> str:
    found = list((app_dir / ".score-compose" / "mounts").glob("routing-*/nginx.conf"))
    assert len(found) == 1, found
    return found[0].read_text()


@needs_score_compose
def test_make_dev_generates_a_whole_compose_project_from_score_alone(tmp_path,
                                                                    stack_enabled):
    """Gate: `make dev` runs from Score with no hand-written compose file anywhere."""
    dest = new_app(tmp_path)
    assert not (dest / "compose.yaml").exists(), "a compose file must never be committed"
    project = make_generate(dest)

    services = project["services"]
    # Both workloads build from the repo root, so both see shared/.
    for name, container in (("backend-api", "backend"), ("frontend-web", "frontend")):
        assert services[name]["build"]["context"] == "."
        assert services[name]["hostname"] == container
    assert any(s.get("image", "").startswith(str(orc.CONFIG.get("images.postgres")))
               for s in services.values()), "no local postgres provisioned"
    # The API reads its database through the same variable names it uses on the cluster.
    env = services["backend-api"]["environment"]
    assert env["PGDATABASE"] == "app_backend" and env["PGUSER"] == "app_backend"
    assert env["PGPASSWORD"], "local password must be generated, not blank"


@needs_score_compose
def test_local_routing_puts_api_first_whatever_the_workloads_are_called(tmp_path,
                                                                       stack_enabled):
    """The measured regression, end to end through the real binary.

    nginx takes the FIRST matching regex location, so if `^/` is emitted before `^/api/`
    the frontend answers every API call. Naming the frontend `app-ui` and the backend
    `orders` is the ordering that broke score-compose's own route provisioner.
    """
    dest = new_app(tmp_path)
    for directory, old, new in (("frontend", "frontend", "app-ui"),
                                ("backend", "backend", "orders")):
        score = dest / directory / "score.yaml"
        doc = yaml.safe_load(score.read_text())
        doc["metadata"]["name"] = new
        write(score, doc)
    make_generate(dest)

    conf = nginx_conf(dest)
    api = conf.index("location ~ ^/api/")
    catch_all = conf.index("location ~ ^/\n") if "location ~ ^/\n" in conf \
        else conf.index("location ~ ^/ ")
    assert api < catch_all, "the /api locations must precede the frontend catch-all"
    assert "orders:8080" in conf and "app-ui:80" in conf


@needs_score_compose
def test_the_local_router_re_resolves_dns_so_a_rebuild_is_not_a_502(tmp_path,
                                                                   stack_enabled):
    """Rebuilding a workload gives its container a new IP. Without a short resolver TTL
    nginx keeps the old one for ten minutes and answers 502 while the target runs fine."""
    dest = new_app(tmp_path)
    make_generate(dest)
    conf = nginx_conf(dest)
    assert re.search(r"resolver 127\.0\.0\.11 valid=\d+s", conf)
    # proxy_pass through a VARIABLE is what makes nginx consult the resolver at all.
    assert "set $backend" in conf and "proxy_pass http://$backend;" in conf


# ------------------------------------------------- CI and renderer must agree on tags
@needs_score_k8s
def test_image_plan_and_render_pin_the_same_images(tmp_path, capsys, stack_enabled):
    """The mismatch this guards is invisible until a pod dies: an app's CI builds what
    `image-plan` says, the orchestrator renders what `plan_images` says, and if the two
    disagree Fleet applies a manifest referencing an image nobody pushed."""
    dest = new_app(tmp_path)
    orc.cmd_image_plan(orc.argparse.Namespace(
        app="shopdemo", image="shopdemo", tag="deadbeef", registry="h.io/p",
        app_dir=str(dest), tag_strategy=""))
    planned = json.loads(capsys.readouterr().out)
    docs = render_stack_app(tmp_path, dest, "shopdemo", "staging", "w")
    rendered = {d["metadata"]["name"]: d["spec"]["template"]["spec"]["containers"][0]["image"]
                for d in docs if d["kind"] == "Deployment"}
    assert planned == rendered


def test_the_shipped_ci_templates_let_the_platform_choose_the_strategy():
    """A template that hardcodes `--tag-strategy` silently overrules `.idp/stack.yaml`.
    Worse, `image-plan` without --env-config cannot see features.stack_onboarding, so CI
    would compute `content` while the orchestrator computes `commit` — two different tags
    for one commit."""
    for name in ("app-ci-mot-service.yaml", "app-ci-nhieu-service.yaml"):
        text = (CATALOG / "templates" / name).read_text()
        # Comments may DISCUSS the flag; what matters is that no command passes it.
        code = "\n".join(line for line in text.splitlines()
                         if not line.lstrip().startswith("#"))
        assert "--tag-strategy" not in code, name
        assert 'tag_strategy:"' not in code, name
        assert "image-plan" in code, name
        plan_call = code[code.index("idpctl"):code.index("image-plan")]
        assert "--env-config" in plan_call, name


def test_the_deploy_workflow_defers_to_the_app_when_the_payload_is_silent():
    text = (CATALOG / ".github" / "workflows" / "deploy.yaml").read_text()
    assert "client_payload.tag_strategy || ''" in text
    assert "client_payload.tag_strategy || 'content'" not in text

# =====================================================================================
# COMPANY-PROFILE COMPATIBILITY — one source tree, two profiles.
#
# platform.env.yaml is the harness (github.com + GHCR + CNPG + HTTP Traefik).
# platform.env.company.yaml is the company shape (GHES + Harbor/HTTPS + StatefulSet
# postgres on rook-ceph-block + Traefik with an HTTPS websecure listener). These tests
# prove the SAME renderer serves both: the difference is only coordinates and capability,
# never a code fork. A "feature off" profile must not grow a prerequisite either.
# =====================================================================================
HARNESS_PROFILE = CATALOG / "platform.env.yaml"
COMPANY_PROFILE = CATALOG / "platform.env.company.yaml"

# The company's real coordinates. This list is a DENYLIST: it exists so the hardcode-scan
# below can prove none of them leaked into shared source. It is the one place they may be
# written down in the test suite.
COMPANY_COORDINATES = (
    "harbor.stg.exampledevops.com", "stg.exampledevops.com", "exampledevops",
    "rook-ceph-block", "traefik-gateway", "example-org",
)


def profile_config(profile: Path, **features) -> orc.EnvConfig:
    """Load a profile, optionally flipping feature flags on for a render test."""
    data = yaml.safe_load(profile.read_text()) or {}
    if features:
        data.setdefault("features", {}).update(features)
    return orc.EnvConfig(data)


PG_APPLICATION_SCORE = {
    "apiVersion": "score.dev/v1b1",
    "metadata": {"name": "api"},
    "containers": {"main": {"image": "."}},
    "service": {"ports": {"http": {"port": 8080, "targetPort": 8080}}},
    "resources": {"db": {"type": "postgres", "class": "application"}},
}

ROUTE_SCORE = {
    "apiVersion": "score.dev/v1b1",
    "metadata": {"name": "api"},
    "containers": {"main": {"image": "."}},
    "service": {"ports": {"http": {"port": 8080, "targetPort": 8080}}},
    "resources": {"web": {"type": "route",
                          "params": {"host": "api.example.test", "port": 8080, "path": "/"}}},
}


def render_docs(tmp_path: Path, score: dict, *, env="staging", app="api",
                registry="reg.example.test/idp", name="run") -> list[dict]:
    """Render one score app with whatever orc.CONFIG is currently set, return manifests."""
    app_dir = tmp_path / f"app-{name}"
    app_dir.mkdir(parents=True, exist_ok=True)
    write(app_dir / "score.yaml", score)
    args = orc.argparse.Namespace(
        app=app, image=app, tag="sha1", env=env, registry=registry,
        catalog=str(CATALOG), app_dir=str(app_dir),
        work=str(tmp_path / name), out=str(tmp_path / name / "out.yaml"),
        kubeconfig=None, state_file=str(tmp_path / f"{name}-state.yaml"), no_state=False,
    )
    orc.cmd_render(args)
    return orc.load_all(Path(args.work) / "manifests.yaml")


# ----------------------------------------------------------------- config: both profiles
@pytest.mark.parametrize("profile", [HARNESS_PROFILE, COMPANY_PROFILE])
def test_both_profiles_load_and_expose_required_keys(profile):
    cfg = orc.EnvConfig.load(str(profile))
    for key in ("kubernetes.namespace_pattern", "kubernetes.state_namespace",
                "ingress.gateway_name", "ingress.gateway_namespace",
                "ingress.section_name", "ingress.route_scheme",
                "registry.pull_secret", "database.backend"):
        assert cfg.get(key) is not None, f"{key} missing from {profile.name}"


def test_new_keys_have_backward_compatible_defaults():
    """An install with NO config file (empty) keeps the historical behaviour: CNPG backend,
    no sectionName, http scheme."""
    empty = orc.EnvConfig({})
    assert empty.get("database.backend") == "cnpg"
    assert empty.get("ingress.section_name") == ""
    assert empty.get("ingress.route_scheme") == "http"


def test_database_backend_enum(monkeypatch):
    monkeypatch.setattr(orc, "CONFIG", orc.EnvConfig({}))
    assert orc.database_backend() == "cnpg"
    monkeypatch.setattr(orc, "CONFIG", orc.EnvConfig({"database": {"backend": "statefulset"}}))
    assert orc.database_backend() == "statefulset"
    monkeypatch.setattr(orc, "CONFIG", orc.EnvConfig({"database": {"backend": "statefull"}}))
    with pytest.raises(SystemExit, match="statefull"):
        orc.database_backend()


def test_profiles_choose_their_backend():
    assert orc.EnvConfig.load(str(HARNESS_PROFILE)).get("database.backend") == "cnpg"
    assert orc.EnvConfig.load(str(COMPANY_PROFILE)).get("database.backend") == "statefulset"


def test_route_scheme_and_section_per_profile():
    harness = orc.EnvConfig.load(str(HARNESS_PROFILE))
    company = orc.EnvConfig.load(str(COMPANY_PROFILE))
    assert (harness.get("ingress.route_scheme"), harness.get("ingress.section_name")) == ("http", "")
    assert company.get("ingress.route_scheme") == "https"
    assert company.get("ingress.section_name") == "websecure"


# --------------------------------------------------------- provisioner backend selection
def test_backend_selects_exactly_one_postgres_application_file(monkeypatch):
    cases = [("cnpg", "postgres-application.provisioners.yaml",
              "postgres-application-statefulset.provisioners.yaml"),
             ("statefulset", "postgres-application-statefulset.provisioners.yaml",
              "postgres-application.provisioners.yaml")]
    for backend, want, drop in cases:
        monkeypatch.setattr(orc, "CONFIG", orc.EnvConfig({"database": {"backend": backend}}))
        names = [p.name for p in orc.select_provisioner_files(CATALOG)]
        assert want in names and drop not in names, backend
        # every OTHER provisioner is always present regardless of backend
        assert "local.provisioners.yaml" in names and "secret.provisioners.yaml" in names


# ----------------------------------------------------------------- render matrix (real)
@needs_score_k8s
def test_statefulset_backend_renders_a_real_statefulset_from_config(tmp_path, monkeypatch):
    cfg = profile_config(COMPANY_PROFILE, postgres_application=True, vault_secrets=True)
    monkeypatch.setattr(orc, "CONFIG", cfg)
    docs = render_docs(tmp_path, PG_APPLICATION_SCORE, name="sts")
    kinds = {d["kind"] for d in docs}
    assert "StatefulSet" in kinds
    # backend=statefulset must NOT emit any CNPG object
    assert not any(str(d.get("apiVersion", "")).startswith("postgresql.cnpg.io") for d in docs)

    sts = next(d for d in docs if d["kind"] == "StatefulSet")
    vct = sts["spec"]["volumeClaimTemplates"][0]
    # StorageClass + size come from config/profile, never a literal in the catalog
    assert vct["spec"]["storageClassName"] == cfg.get("kubernetes.storage_class")
    assert vct["spec"]["resources"]["requests"]["storage"] == \
        cfg.get("database_profiles.staging.application.storage")
    # image = repository(config):engine_version(profile)
    want_image = f'{cfg.get("database.image_repository")}:' \
                 f'{cfg.get("database_profiles.staging.application.engine_version")}'
    container = sts["spec"]["template"]["spec"]["containers"][0]
    assert container["image"] == want_image
    # imagePullSecret injected from config (private registry pull)
    assert sts["spec"]["template"]["spec"]["imagePullSecrets"] == \
        [{"name": cfg.get("registry.pull_secret")}]
    # password is NEVER a literal: it comes from the VSO-synced Secret via secretKeyRef
    pw = next(e for e in container["env"] if e["name"] == "POSTGRES_PASSWORD")
    assert "value" not in pw and pw["valueFrom"]["secretKeyRef"]["key"] == "password"
    # credential is a VaultStaticSecret (reference), and no core Secret carries data
    assert any(d["kind"] == "VaultStaticSecret" for d in docs)
    assert not any(d["kind"] == "Secret" and d.get("data") for d in docs)
    # app-facing host is the SAME `<cluster>-rw` Service name CNPG would create
    assert any(d["kind"] == "Service" and d["metadata"]["name"].endswith("-rw") for d in docs)
    # single instance — no accidental HA/split-brain
    assert sts["spec"]["replicas"] == 1


@needs_score_k8s
def test_cnpg_backend_is_unchanged_by_the_new_key(tmp_path, monkeypatch):
    cfg = profile_config(HARNESS_PROFILE, postgres_application=True, vault_secrets=True)
    assert cfg.get("database.backend") == "cnpg"
    monkeypatch.setattr(orc, "CONFIG", cfg)
    docs = render_docs(tmp_path, PG_APPLICATION_SCORE, name="cnpg")
    assert any(str(d.get("apiVersion", "")).startswith("postgresql.cnpg.io")
               and d["kind"] == "Cluster" for d in docs)
    assert not any(d["kind"] == "StatefulSet" for d in docs)


@needs_score_k8s
def test_statefulset_prod_is_refused_no_backup_no_ha(tmp_path, monkeypatch):
    cfg = profile_config(COMPANY_PROFILE, postgres_application=True, vault_secrets=True)
    monkeypatch.setattr(orc, "CONFIG", cfg)
    with pytest.raises(SystemExit, match="statefulset"):
        render_docs(tmp_path, PG_APPLICATION_SCORE, env="prod", name="stsprod")


# ------------------------------------------------------------------ gateway sectionName
@needs_score_k8s
def test_route_sectionName_is_config_driven(tmp_path, monkeypatch):
    # empty section_name (harness) -> no sectionName written, old manifest shape
    monkeypatch.setattr(orc, "CONFIG", orc.EnvConfig.load(str(HARNESS_PROFILE)))
    docs = render_docs(tmp_path, ROUTE_SCORE, name="route-h")
    route = next(d for d in docs if d["kind"] == "HTTPRoute")
    assert "sectionName" not in route["spec"]["parentRefs"][0]

    # company -> attaches to the HTTPS websecure listener
    monkeypatch.setattr(orc, "CONFIG", orc.EnvConfig.load(str(COMPANY_PROFILE)))
    docs = render_docs(tmp_path, ROUTE_SCORE, name="route-c")
    route = next(d for d in docs if d["kind"] == "HTTPRoute")
    pref = route["spec"]["parentRefs"][0]
    assert pref["name"] == "traefik-gateway" and pref["sectionName"] == "websecure"


# --------------------------------------------------------------------------- doctor matrix
def make_probe(present):
    """Fake read-only cluster probe: a resource named in `present` exists, else NotFound."""
    def probe(args):
        # args is ["get", <kind>, <name>, ...tail..., "-o", "name"]
        name = args[2]
        if name in present:
            return (0, name + "\n", "")
        return (1, "", "Error from server (NotFound): namespaces \"x\" not found")
    return probe


def _levels(results, capability):
    return [r["level"] for r in results if r["capability"] == capability]


def test_doctor_skips_database_and_vault_when_features_off(monkeypatch):
    monkeypatch.setattr(orc, "CONFIG", profile_config(COMPANY_PROFILE))  # all flags off
    res = orc.run_doctor_checks(make_probe(set()))
    caps = {r["capability"] for r in res}
    assert not any(c.startswith("database.") for c in caps)  # no DB prerequisite at all
    assert "vault.vso" not in caps
    assert any(r["capability"] == "database" and r["level"] == "SKIP" for r in res)
    assert any(r["capability"] == "vault" and r["level"] == "SKIP" for r in res)


def test_doctor_cnpg_backend_checks_cnpg_crd(monkeypatch):
    cfg = profile_config(HARNESS_PROFILE, postgres_application=True)
    monkeypatch.setattr(orc, "CONFIG", cfg)
    # storageclass present, CNPG CRD absent -> blocker on cnpg
    res = orc.run_doctor_checks(make_probe({cfg.get("kubernetes.storage_class")}))
    assert "FAIL" in _levels(res, "database.cnpg")
    # CNPG CRD present -> ok
    res = orc.run_doctor_checks(make_probe(
        {cfg.get("kubernetes.storage_class"), "clusters.postgresql.cnpg.io"}))
    assert _levels(res, "database.cnpg") == ["OK"]


def test_doctor_statefulset_backend_never_checks_cnpg(monkeypatch):
    cfg = profile_config(COMPANY_PROFILE, postgres_application=True)
    monkeypatch.setattr(orc, "CONFIG", cfg)
    res = orc.run_doctor_checks(make_probe({cfg.get("kubernetes.storage_class")}))
    assert not any(r["capability"] == "database.cnpg" for r in res)
    assert "OK" in _levels(res, "database.storage")   # rook-ceph-block resolved + present
    assert "OK" in _levels(res, "database.backend")   # states it skips CNPG on purpose


def test_doctor_missing_storageclass_and_gateway_are_blockers(monkeypatch):
    cfg = profile_config(COMPANY_PROFILE, postgres_application=True)
    monkeypatch.setattr(orc, "CONFIG", cfg)
    res = orc.run_doctor_checks(make_probe(set()))   # nothing exists
    assert "FAIL" in _levels(res, "database.storage")
    assert "FAIL" in _levels(res, "gateway")


def test_doctor_vault_on_requires_vso(monkeypatch):
    cfg = profile_config(HARNESS_PROFILE, vault_secrets=True)
    monkeypatch.setattr(orc, "CONFIG", cfg)
    res = orc.run_doctor_checks(make_probe(set()))   # VSO CRD absent
    assert "FAIL" in _levels(res, "vault.vso")


def test_doctor_config_only_mode_never_false_fails_cluster_facts(monkeypatch):
    monkeypatch.setattr(orc, "CONFIG", orc.EnvConfig.load(str(COMPANY_PROFILE)))
    res = orc.run_doctor_checks(probe=None)   # no cluster
    # a can't-check is a WARN, never a FAIL — no green-on-unknown, no false-red either
    assert "WARN" in _levels(res, "gateway")
    assert not any(r["level"] == "FAIL" for r in res)


# --------------------------------------------------------------------------- hardcode scan
def _noncomment(text: str) -> str:
    return "\n".join(l for l in text.splitlines() if not l.lstrip().startswith("#"))


def test_no_company_coordinates_leak_into_shared_source():
    """Company endpoints/identifiers live ONLY in platform.env.company.yaml. They must not
    appear in the renderer, the catalog, or the gap register."""
    targets = sorted((CATALOG / "engine").glob("*.py"))
    targets += sorted((CATALOG / "provisioners").glob("*.yaml"))
    targets += sorted((CATALOG / "patches").glob("*.tpl"))
    gap = CATALOG / "GAP-REGISTER.md"
    if gap.exists():
        targets.append(gap)
    for f in targets:
        text = f.read_text()
        for lit in COMPANY_COORDINATES:
            assert lit not in text, f"company coordinate {lit!r} leaked into {f.name}"


def test_catalog_uses_placeholders_not_literal_infrastructure():
    """Provisioners and patches carry SHAPE; every infra coordinate is a %%placeholder%%.
    A literal registry host or storage backend here is the exact leak this scan blocks."""
    banned = ("ghcr.io", "mirror.gcr.io", "harbor.", "rook-ceph", "cloudnative-pg/")
    for f in sorted((CATALOG / "provisioners").glob("*.yaml")) + \
            sorted((CATALOG / "patches").glob("*.tpl")):
        body = _noncomment(f.read_text())
        for lit in banned:
            assert lit not in body, f"{lit!r} hardcoded (non-comment) in {f.name}"


def test_user_facing_github_host_is_not_hardcoded():
    """The only github.com literal in the renderer is the documented fallback default in
    git_server_url; every other user-facing URL derives from GITHUB_SERVER_URL."""
    src = []
    for path in sorted((CATALOG / "engine").glob("*.py")):
        src.extend(path.read_text().splitlines())
    offenders = []
    for i, line in enumerate(src, 1):
        if "github.com" not in line:
            continue
        # allow: comments, docstring prose, and the single fallback expression
        stripped = line.strip()
        if stripped.startswith("#") or "GITHUB_SERVER_URL" in line:
            continue
        if '"https://github.com"' in line or "`https://github.com`" in line:
            continue
        if "github.com" in line and ("Hardcoding" in line or "a github.com literal" in line):
            continue  # git_server_url docstring
        offenders.append((i, stripped))
    assert not offenders, f"hardcoded github.com outside allowlist: {offenders}"


# ============================================================================
# deploy-check / pre-gitops — đường điều phối TRƯỚC-GitOps dùng chung
# (engine/pipeline.py). Không cần cụm: mọi lệnh cụm/Vault đều được mock.
# ============================================================================
def _score_git_app(tmp_path: Path):
    """Một app một-workload là kho git sạch. Trả (repo, sha đầy đủ của HEAD)."""
    repo = tmp_path / "app"
    write(repo / "score.yaml", score_spec("web", "main"))
    git(repo, "init", "-q")
    git(repo, "config", "user.email", "t@t")
    git(repo, "config", "user.name", "t")
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "one")
    return repo, git(repo, "rev-parse", "HEAD")


def _cp(rc=0, out="ok", err=""):
    return subprocess.CompletedProcess([], rc, stdout=out, stderr=err)


# ---- chẩn đoán phân tầng ----------------------------------------------------
def test_diagnostic_has_the_contract_shape():
    err = orc.DeployCheckError("render manifest", "score.yaml sai",
                               orc.LAYER_SOURCE, "score.yaml", "idpctl render")
    out = orc.format_diagnostic(err, run_id="r1", cleanup="thành công")
    assert "[FAIL] render manifest" in out
    assert "Nguyên nhân: score.yaml sai" in out
    assert "Tầng lỗi: SOURCE" in out
    assert "Sửa tại: score.yaml" in out
    assert "Kiểm tra tiếp: idpctl render" in out
    assert "Run ID: r1" in out
    assert "Cleanup: thành công" in out


def test_diagnostic_layer_must_be_a_known_layer():
    with pytest.raises(ValueError):
        orc.DeployCheckError("x", "y", "BOGUS", "z")


def test_diagnostic_omits_runid_and_cleanup_for_pre_gitops():
    """pre-gitops trong CI không tạo namespace kiểm tra, nên không có hai dòng đó."""
    err = orc.DeployCheckError("dry-run", "boom", orc.LAYER_KUBERNETES, "manifest")
    out = orc.format_diagnostic(err)
    assert "Run ID" not in out and "Cleanup" not in out


# ---- platform.lock ----------------------------------------------------------
def test_read_platform_lock_skips_comments(tmp_path):
    (tmp_path / "platform.lock").write_text("# ghi chú\n\ncatalog/v1\n")
    assert orc.read_platform_lock(tmp_path) == "catalog/v1"


def test_read_platform_lock_defaults_to_main(tmp_path):
    assert orc.read_platform_lock(tmp_path) == "main"


# ---- bước 4: xác nhận source + SHA -----------------------------------------
def test_deploy_check_rejects_non_git_source(tmp_path):
    d = tmp_path / "nogit"
    d.mkdir()
    with pytest.raises(orc.DeployCheckError) as e:
        orc._resolve_and_validate_sha(d, "abc", build=False)
    assert e.value.layer == orc.LAYER_SOURCE
    assert "không phải kho Git" in e.value.cause


def test_deploy_check_rejects_unknown_sha(tmp_path):
    repo, sha = _score_git_app(tmp_path)
    with pytest.raises(orc.DeployCheckError) as e:
        orc._resolve_and_validate_sha(repo, "0" * 40, build=False)
    assert e.value.layer == orc.LAYER_SOURCE
    assert "không có trong kho" in e.value.cause


def test_deploy_check_rejects_head_mismatch(tmp_path, repo_with_two_commits):
    repo, old, new = repo_with_two_commits
    with pytest.raises(orc.DeployCheckError) as e:
        orc._resolve_and_validate_sha(repo, old, build=False)  # old != HEAD(new)
    assert "khác --sha" in e.value.cause


def test_deploy_check_rejects_uncommitted_changes(tmp_path):
    """Ảnh theo SHA KHÔNG chứa thay đổi chưa commit — báo xanh trên nó là nói dối."""
    repo, sha = _score_git_app(tmp_path)
    (repo / "score.yaml").write_text("dirty: true\n")  # sửa file đã track
    with pytest.raises(orc.DeployCheckError) as e:
        orc._resolve_and_validate_sha(repo, sha, build=False)
    assert e.value.layer == orc.LAYER_SOURCE
    assert "CHƯA COMMIT" in e.value.cause


def test_deploy_check_build_allows_uncommitted(tmp_path):
    repo, sha = _score_git_app(tmp_path)
    (repo / "extra.txt").write_text("x")  # cây bẩn
    assert orc._resolve_and_validate_sha(repo, sha, build=True) == sha


def test_source_and_image_correspond_to_the_same_sha(tmp_path):
    """Ảnh (commit strategy) mang ĐÚNG SHA vừa xác nhận từ source — không lệch phiên bản."""
    repo, sha = _score_git_app(tmp_path)
    resolved = orc._resolve_and_validate_sha(repo, sha, build=False)
    services = orc.discover(repo)
    plan = orc.plan_images(services, "r.io/p", "web", resolved, repo, "commit")
    assert plan["web"] == f"r.io/p/web:{resolved}"


# ---- kiểm GitHub (chỉ thứ local xác minh được) -----------------------------
def test_github_checks_requires_login(monkeypatch):
    def fake_run(argv, **kw):
        if argv[:3] == ["gh", "auth", "status"]:
            return _cp(rc=1, out="", err="not logged in")
        return _cp()
    monkeypatch.setattr(orc, "run", fake_run)
    with pytest.raises(orc.DeployCheckError) as e:
        orc.github_checks("web", "staging", "s")
    assert e.value.layer == orc.LAYER_GITHUB
    assert "chưa đăng nhập" in e.value.cause


def test_github_checks_fails_when_app_repo_missing(monkeypatch):
    def fake_run(argv, **kw):
        if argv[:3] == ["gh", "auth", "status"]:
            return _cp()
        if argv[:2] == ["gh", "api"] and "repos/" in argv[2]:
            return _cp(rc=1, out="", err="gh: Not Found (HTTP 404)")
        return _cp()
    monkeypatch.setattr(orc, "run", fake_run)
    with pytest.raises(orc.DeployCheckError) as e:
        orc.github_checks("web", "staging", "s")
    assert e.value.layer == orc.LAYER_GITHUB
    assert "kho ứng dụng" in e.value.stage


def test_github_checks_fails_when_commit_not_pushed(monkeypatch):
    def fake_run(argv, **kw):
        if argv[:3] == ["gh", "auth", "status"]:
            return _cp()
        if argv[:2] == ["gh", "api"]:
            path = argv[2]
            if "/commits/" in path:
                return _cp(rc=1, out="", err="404 Not Found")
            return _cp(rc=0, out="pr3s3nt/web")  # repos tồn tại
        return _cp()
    monkeypatch.setattr(orc, "run", fake_run)
    with pytest.raises(orc.DeployCheckError) as e:
        orc.github_checks("web", "staging", "deadbeef", check_pushed=True)
    assert "commit trên GitHub" in e.value.stage


def test_github_checks_does_not_assert_actions_secrets(monkeypatch, capsys):
    """Giá trị Actions Secrets chỉ CI đọc được — phải BÁO 'chưa xác minh', không im lặng OK."""
    def fake_run(argv, **kw):
        return _cp()  # mọi gh đều OK
    monkeypatch.setattr(orc, "run", fake_run)
    orc.github_checks("web", "staging", "s", check_pushed=False)
    err = capsys.readouterr().err
    assert "Actions Secrets" in err and "chưa xác minh" in err


# ---- run_pre_gitops: chạy đủ 12 bước, mock cụm/render/Vault/secret ----------
def _happy_pipeline(monkeypatch):
    import shutil as _sh
    monkeypatch.setattr(_sh, "which", lambda t: "/usr/bin/" + t)
    monkeypatch.setattr(orc, "check_tool_versions", lambda *a, **k: None)
    monkeypatch.setattr(orc, "kubectl",
                        lambda args, **kw: _cp(rc=0, out="ok"))
    monkeypatch.setattr(orc, "cmd_vault_auto_setup", lambda a: None)
    monkeypatch.setattr(orc, "cmd_apply_secrets", lambda a: None)


def _render_writing(out_text="", secrets_text=""):
    def fake_render(args):
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(out_text or "kind: Deployment\n")
        (Path(args.work) / "secrets.yaml").parent.mkdir(parents=True, exist_ok=True)
        (Path(args.work) / "secrets.yaml").write_text(secrets_text)
    return fake_render


def test_run_pre_gitops_success_records_every_stage(tmp_path, monkeypatch):
    repo, sha = _score_git_app(tmp_path)
    _happy_pipeline(monkeypatch)
    monkeypatch.setattr(orc, "cmd_render", _render_writing())
    stages = []
    params = orc.PipelineParams(
        app="web", env="staging", app_dir=repo, sha=sha, registry="r.io/p",
        work=tmp_path / "w", out=tmp_path / "w" / "manifests.yaml", catalog=CATALOG)
    result = orc.run_pre_gitops(
        params, record=lambda s, st, **k: stages.append((s, st)))
    assert result.catalog_ref == "main"
    assert result.image_plan["web"].startswith("r.io/p/")
    assert result.target_namespace == "web-staging"
    assert ("render", "success") in stages
    assert ("apply_secrets", "success") in stages
    assert ("dry_run", "success") in stages


def test_run_pre_gitops_translates_render_error_to_source(tmp_path, monkeypatch):
    repo, sha = _score_git_app(tmp_path)
    _happy_pipeline(monkeypatch)
    def boom(args):
        raise SystemExit("score.yaml: no containers defined")
    monkeypatch.setattr(orc, "cmd_render", boom)
    params = orc.PipelineParams(
        app="web", env="staging", app_dir=repo, sha=sha, registry="r.io/p",
        work=tmp_path / "w", out=tmp_path / "w" / "m.yaml", catalog=CATALOG)
    with pytest.raises(orc.DeployCheckError) as e:
        orc.run_pre_gitops(params)
    assert e.value.layer == orc.LAYER_SOURCE
    assert e.value.stage == "render manifest"


def test_run_pre_gitops_translates_vault_error(tmp_path, monkeypatch):
    repo, sha = _score_git_app(tmp_path)
    _happy_pipeline(monkeypatch)
    monkeypatch.setattr(orc, "cmd_render", _render_writing())
    def boom(a):
        raise SystemExit("Vault từ chối PUT policy")
    monkeypatch.setattr(orc, "cmd_vault_auto_setup", boom)
    params = orc.PipelineParams(
        app="web", env="staging", app_dir=repo, sha=sha, registry="r.io/p",
        work=tmp_path / "w", out=tmp_path / "w" / "m.yaml", catalog=CATALOG)
    with pytest.raises(orc.DeployCheckError) as e:
        orc.run_pre_gitops(params)
    assert e.value.layer == orc.LAYER_VAULT


def test_dry_run_failure_is_a_kubernetes_layer_error(tmp_path, monkeypatch):
    repo, sha = _score_git_app(tmp_path)
    monkeypatch.setattr(orc, "cmd_render", _render_writing())
    m = tmp_path / "m.yaml"
    m.write_text("kind: Deployment\n")
    def fake_kubectl(args, **kw):
        if args[:1] == ["apply"] and "--dry-run=server" in args:
            return _cp(rc=1, out="", err='no matches for kind "Cluster"')
        return _cp()
    monkeypatch.setattr(orc, "kubectl", fake_kubectl)
    with pytest.raises(orc.DeployCheckError) as e:
        orc.server_side_dry_run(m, "web-staging", None)
    assert e.value.layer == orc.LAYER_KUBERNETES
    assert "dry-run" in e.value.stage


# ---- chờ rollout: timeout -> KUBERNETES ------------------------------------
def test_ephemeral_rollout_timeout_is_layered(tmp_path, monkeypatch):
    docs = [deploy_doc("web", "r.io/web:v2")]
    # cụm chạy ảnh KHÁC -> không bao giờ khớp -> timeout
    monkeypatch.setattr(orc, "kubectl",
                        fake_kubectl_returning({"web": live("r.io/web:cu")}))
    with pytest.raises(orc.DeployCheckError) as e:
        orc._wait_rollout(docs, "web-check-x", None, timeout=0)
    assert e.value.layer == orc.LAYER_KUBERNETES
    assert "Deployment" in e.value.stage


def test_ephemeral_database_wait_failure_is_database_layer(tmp_path, monkeypatch):
    """SystemExit của wait_for_databases phải thành DeployCheckError tầng DATABASE."""
    def boom(docs, ns, args):
        raise SystemExit("cơ sở dữ liệu chưa Ready sau 600s")
    with pytest.raises(orc.DeployCheckError) as e:
        orc._run_wait(boom, [], "ns", None, stage="chờ database sẵn sàng",
                      layer=orc.LAYER_DATABASE, fix_at="CNPG")
    assert e.value.layer == orc.LAYER_DATABASE


# ---- cleanup: chỉ xoá của mình, không đụng tài nguyên có sẵn ----------------
def test_cleanup_refuses_a_namespace_it_does_not_own(monkeypatch):
    cr = orc.CheckRun(app="a", env="staging", sha="s", run_id="r1",
                      kubeconfig=None, namespace="a-check-r1")
    deleted = []
    def fake_kubectl(args, **kw):
        if args[:2] == ["get", "namespace"] and "json" in args:
            return _cp(out=json.dumps(
                {"metadata": {"labels": {orc.CHECK_RUN_LABEL: "SOMEONE-ELSE"}}}))
        if args[:2] == ["get", "namespace"]:
            return _cp(out="namespace/a-check-r1")
        if args[:1] == ["delete"]:
            deleted.append(args)
            return _cp()
        return _cp()
    monkeypatch.setattr(orc, "kubectl", fake_kubectl)
    ok, leftovers = cr.cleanup()
    assert ok is False
    assert any("TỪ CHỐI" in l for l in leftovers)
    assert deleted == []  # KHÔNG xoá tài nguyên có sẵn từ trước


def test_cleanup_deletes_a_namespace_it_owns(monkeypatch):
    cr = orc.CheckRun(app="a", env="staging", sha="s", run_id="r1",
                      kubeconfig=None, namespace="a-check-r1")
    deleted = []
    def fake_kubectl(args, **kw):
        if args[:2] == ["get", "namespace"] and "json" in args:
            return _cp(out=json.dumps(
                {"metadata": {"labels": {orc.CHECK_RUN_LABEL: "r1"}}}))
        if args[:2] == ["get", "namespace"]:
            return _cp(out="namespace/a-check-r1")
        if args[:1] == ["delete"]:
            deleted.append(args)
            return _cp()
        return _cp()
    monkeypatch.setattr(orc, "kubectl", fake_kubectl)
    ok, leftovers = cr.cleanup()
    assert ok is True and leftovers == []
    assert any(a[:2] == ["delete", "namespace"] for a in deleted)


def test_cleanup_reports_leftovers_when_delete_fails(monkeypatch):
    cr = orc.CheckRun(app="a", env="staging", sha="s", run_id="r1",
                      kubeconfig=None, namespace="a-check-r1")
    def fake_kubectl(args, **kw):
        if args[:2] == ["get", "namespace"] and "json" in args:
            return _cp(out=json.dumps({"metadata": {"labels": {orc.CHECK_RUN_LABEL: "r1"}}}))
        if args[:2] == ["get", "namespace"]:
            return _cp(out="namespace/a-check-r1")
        if args[:1] == ["delete"]:
            return _cp(rc=1, err="Forbidden")
        return _cp()
    monkeypatch.setattr(orc, "kubectl", fake_kubectl)
    ok, leftovers = cr.cleanup()
    assert ok is False and leftovers


# ---- deploy-check chạy cleanup DÙ pipeline ném lỗi (finally) ----------------
def test_deploy_check_cleans_up_even_on_failure(tmp_path, monkeypatch):
    repo, sha = _score_git_app(tmp_path)
    monkeypatch.setattr(orc, "github_checks", lambda *a, **k: None)
    def boom(*a, **k):
        raise orc.DeployCheckError("render manifest", "boom",
                                   orc.LAYER_PLATFORM, "x")
    monkeypatch.setattr(orc, "run_pre_gitops", boom)
    monkeypatch.setattr(orc, "_create_labeled_namespace", lambda *a, **k: None)
    called = {}
    def fake_cleanup(self):
        called["yes"] = True
        return True, []
    monkeypatch.setattr(orc.CheckRun, "cleanup", fake_cleanup)
    args = argparse.Namespace(
        app="web", app_dir=str(repo), sha=sha, env="staging", kubeconfig=None,
        registry="r.io/p", image=None, catalog=str(CATALOG), tag_strategy="",
        build=False, timeout=1, work=str(tmp_path / "w"))
    with pytest.raises(SystemExit):
        orc.cmd_deploy_check(args)
    assert called.get("yes") is True


def test_deploy_check_refuses_prod(tmp_path):
    args = argparse.Namespace(
        app="web", app_dir=str(tmp_path), sha="s", env="prod", kubeconfig=None,
        registry="r.io/p", image=None, catalog=str(CATALOG), tag_strategy="",
        build=False, timeout=1, work=None)
    with pytest.raises(SystemExit, match="chỉ hỗ trợ staging"):
        orc.cmd_deploy_check(args)


# ---- deploy-check và deploy.yaml (pre-gitops) dùng CÙNG một logic -----------
def test_deploy_check_and_deploy_yaml_share_the_pipeline(tmp_path, monkeypatch):
    repo, sha = _score_git_app(tmp_path)
    calls = []
    def spy(params, record=None):
        calls.append(params)
        out = Path(params.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text("kind: Deployment\n")
        return orc.PipelineResult(manifests=out,
                                  secrets=Path(params.work) / "secrets.yaml",
                                  target_namespace=params.target_namespace or "x")
    monkeypatch.setattr(orc, "run_pre_gitops", spy)
    monkeypatch.setattr(orc, "github_checks", lambda *a, **k: None)
    monkeypatch.setattr(orc, "_ephemeral_deploy", lambda *a, **k: None)
    monkeypatch.setattr(orc, "_create_labeled_namespace", lambda *a, **k: None)
    monkeypatch.setattr(orc, "kubectl", lambda args, **kw: _cp(rc=1, err="NotFound"))

    # deploy.yaml -> idpctl pre-gitops
    pg = argparse.Namespace(
        app="web", image=None, tag=sha, registry="r.io/p", tag_strategy="",
        accept_empty_database=False, catalog=str(CATALOG), app_dir=str(repo),
        work=str(tmp_path / "w1"), kubeconfig=None, state_file=None, no_state=False,
        env="staging", out=str(tmp_path / "w1" / "m.yaml"),
        harbor_host=None, harbor_user=None, harbor_pass=None,
        backup_key_id=None, backup_secret_key=None, skip_dry_run=False)
    orc.cmd_pre_gitops(pg)

    # deploy-check
    dc = argparse.Namespace(
        app="web", app_dir=str(repo), sha=sha, env="staging", kubeconfig=None,
        registry="r.io/p", image=None, catalog=str(CATALOG), tag_strategy="",
        build=False, timeout=1, work=str(tmp_path / "w2"))
    orc.cmd_deploy_check(dc)

    assert len(calls) == 2, "cả pre-gitops và deploy-check phải gọi run_pre_gitops"
    # deploy-check nhắm namespace kiểm tra riêng, không phải namespace chính thức
    assert "check" in calls[1].target_namespace
    assert calls[1].do_vault is False and calls[1].do_secrets is False


def test_keys_for_vss_gathers_referenced_keys():
    """Secret thử phải đủ đúng các key mà Deployment tham chiếu, nếu không pod kẹt
    CreateContainerConfigError — một lỗi GIẢ do bản kiểm tự gây ra."""
    vss = {"kind": "VaultStaticSecret",
           "metadata": {"name": "stripe"},
           "spec": {"destination": {"name": "stripe-secret"}}}
    dep = {"kind": "Deployment", "metadata": {"name": "web"},
           "spec": {"template": {"spec": {"containers": [{
               "name": "main",
               "env": [
                   {"name": "API_KEY", "valueFrom": {"secretKeyRef": {
                       "name": "stripe-secret", "key": "api_key"}}},
                   {"name": "WEBHOOK", "valueFrom": {"secretKeyRef": {
                       "name": "stripe-secret", "key": "webhook"}}},
                   {"name": "OTHER", "valueFrom": {"secretKeyRef": {
                       "name": "some-other", "key": "nope"}}},
               ]}]}}}}
    assert orc._keys_for_vss(vss, [vss, dep]) == ["api_key", "webhook"]


def test_keys_for_vss_reads_transformation_includes():
    """Credential DB (CNPG) đọc THẲNG Secret basic-auth qua `includes: [^username$,^password$]`,
    KHÔNG qua secretKeyRef của Deployment. Bỏ qua includes = secret thử thiếu key = initdb chết
    (CreateContainerConfigError) — đúng lỗi thật gặp khi chạy student-manager trên cụm."""
    vss = {"kind": "VaultStaticSecret",
           "metadata": {"name": "pg-cred"},
           "spec": {"destination": {"name": "pg-cred", "type": "kubernetes.io/basic-auth",
                                    "transformation": {"excludeRaw": True,
                                                       "includes": ["^username$", "^password$"]}}}}
    assert orc._keys_for_vss(vss, [vss]) == ["password", "username"]


def test_db_owner_for_secret_from_cnpg_cluster():
    """username của secret thử phải = tên role CNPG (owner), nếu không CNPG initdb tạo role
    sai tên và app auth thất bại (InvalidPasswordError) — lỗi thật gặp trên cụm."""
    cluster = {"kind": "Cluster", "apiVersion": "postgresql.cnpg.io/v1",
               "metadata": {"name": "pg-backend"},
               "spec": {"bootstrap": {"initdb": {"owner": "app_backend",
                                                 "secret": {"name": "pg-backend-cred"}}},
                        "managed": {"roles": [{"name": "app_backend",
                                               "passwordSecret": {"name": "pg-backend-cred"}}]}}}
    assert orc._db_owner_for_secret("pg-backend-cred", [cluster]) == "app_backend"
    assert orc._db_owner_for_secret("other-secret", [cluster]) is None
