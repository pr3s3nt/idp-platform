"""Tests for orchestrate.py.

Run from the idp repo root:  python -m pytest test_orchestrate.py -v

The state-stability tests shell out to a real score-k8s, so they need it on PATH along
with the catalog in this repo (provisioners/ + patches/).
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

import orchestrate as orc

CATALOG = Path(__file__).parent
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
    orc.rewrite_images(services, "r.io/p", "shop", "sha1")
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


def test_commit_refuses_out_of_order_deploy(tmp_path, repo_with_two_commits):
    """The ordering bug: build durations differ, so an older commit can dispatch second.
    Without this guard the config repo silently regresses to the older SHA."""
    app, old, new = repo_with_two_commits
    config = make_config_repo(tmp_path)
    record = config / orc.SHA_RECORD_DIR / "staging.sha"
    record.parent.mkdir(parents=True)
    record.write_text(new + "\n")

    args = orc.argparse.Namespace(
        config_dir=str(config), app="a", env="staging", sha=old,
        app_dir=str(app), catalog_ref=None,
    )
    with pytest.raises(SystemExit, match="ancestor of the already-deployed"):
        orc.cmd_commit(args)


def test_commit_allows_newer_deploy(tmp_path, repo_with_two_commits):
    app, old, new = repo_with_two_commits
    config = make_config_repo(tmp_path)
    record = config / orc.SHA_RECORD_DIR / "staging.sha"
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
