#!/usr/bin/env python3
"""
IDP deploy orchestrator.

Invoked by .github/workflows/orchestrator.yaml, one subcommand per step. Everything
arrives as an explicit flag: this script NEVER reads GITHUB_* environment variables, so
any failed deploy can be replayed by hand on the runner. That is the whole point.

  ./orchestrate.py preflight  --require-cluster
  ./orchestrate.py render     --app sample-nginx --image nginx --tag <sha> --env staging \
                              --catalog ./catalog --app-dir ./app --work ./work-staging \
                              --out ./config/staging/manifests.yaml
  ./orchestrate.py apply-secrets --app sample-nginx --env staging --secrets ./work-staging/secrets.yaml
  ./orchestrate.py commit     --config-dir ./config --app sample-nginx --env staging \
                              --sha <sha> --app-dir ./app
  ./orchestrate.py promote    --app sample-nginx --image nginx --tag v1.2.3 --mode tag-only \
                              --config-dir ./config

NOTE ON NAMING: this file is deliberately not called platform.py — that would shadow the
Python stdlib `platform` module for anything running in this directory.
"""
from __future__ import annotations

import argparse
import base64
import json
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import yaml

# Namespace holding one Secret per (app, env) with that project's score-k8s state.
STATE_NAMESPACE = "cluster-state"
# Image pull secret injected by the patch templates; created here if missing.
PULL_SECRET = "registry-pull"
# Where the deployed SHA is recorded in the config repo. Kept at the repo root, OUTSIDE
# staging/ and prod/, so Fleet never tries to parse it as a manifest.
SHA_RECORD_DIR = ".platform"


# --------------------------------------------------------------------------------------
# plumbing
# --------------------------------------------------------------------------------------
# Logs go to stderr so stdout carries DATA only. `image-plan` prints JSON that an app's CI
# parses; with the transcript on stdout too, that JSON is unparseable. Actions captures both
# streams identically, so nothing is lost in the run log.
def log(msg: str) -> None:
    print(f"==> {msg}", file=sys.stderr, flush=True)


def warn(msg: str) -> None:
    # ::warning:: renders as an annotation in the Actions UI and is harmless elsewhere.
    print(f"::warning::{msg}", file=sys.stderr, flush=True)


def run(
    argv: list[str],
    *,
    check: bool = True,
    capture: bool = False,
    cwd: Path | None = None,
    stdin: str | None = None,
) -> subprocess.CompletedProcess:
    """Run a command, always logging the full argv.

    Bash gives you `set -x` for free; in Python you have to be deliberate about it. Every
    external call is logged so a failed run reads like a transcript you can replay.
    """
    log(f"$ {' '.join(argv)}" + (f"   (cwd={cwd})" if cwd else ""))
    return subprocess.run(
        argv,
        check=check,
        cwd=cwd,
        input=stdin,
        text=True,
        capture_output=capture,
    )


def kubectl(args: list[str], *, kubeconfig: str | None = None, **kw) -> subprocess.CompletedProcess:
    argv = ["kubectl"]
    if kubeconfig:
        argv += ["--kubeconfig", kubeconfig]
    return run(argv + args, **kw)


def load_all(path: Path) -> list[dict]:
    with path.open() as fh:
        return [d for d in yaml.safe_load_all(fh) if d]


def dump_all(docs: list[dict], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w") as fh:
        yaml.safe_dump_all(docs, fh, sort_keys=False, default_flow_style=False)


# --------------------------------------------------------------------------------------
# discovery
# --------------------------------------------------------------------------------------
@dataclass
class Service:
    path: Path       # the score file
    workload: str    # metadata.name
    container: str   # first container key — 'web', 'main', 'frontend', ...


def discover(app_dir: Path) -> list[Service]:
    """Find every score file in an app repo. Three supported layouts, in precedence order:

    1. a single score.yaml at the repo root            (sample-nginx)
    2. flat score-*.yaml / *.score.yaml in the root    (OnlineBoutique)
    3. one score.yaml per first-level directory        (multi-service monorepo)
    """
    root = app_dir / "score.yaml"
    if root.is_file():
        found = [root]
    else:
        found = sorted(
            {p for pat in ("score-*.yaml", "*.score.yaml") for p in app_dir.glob(pat)}
        )
        if not found:
            found = sorted(app_dir.glob("*/score.yaml"))

    services = []
    for path in found:
        spec = yaml.safe_load(path.read_text())
        containers = spec.get("containers") or {}
        if not containers:
            raise SystemExit(f"{path}: no containers defined")
        name = (spec.get("metadata") or {}).get("name")
        if not name:
            raise SystemExit(f"{path}: metadata.name is required")
        # First key, matching score-k8s's own ordering. Hardcoding 'main' here would make
        # the image rewrite silently no-op on workloads that name their container anything else.
        services.append(Service(path=path, workload=name, container=next(iter(containers))))

    if not services:
        raise SystemExit(
            f"no score file found under {app_dir} "
            "(looked for score.yaml, score-*.yaml, *.score.yaml, */score.yaml)"
        )
    log(f"discovered {len(services)} service(s): " + ", ".join(s.workload for s in services))
    return services


def image_ref(registry: str, image: str, service: Service, tag: str, *, multi: bool) -> str:
    """<registry>/<image>:<tag> for a single-workload app, <registry>/<image>-<workload>:<tag>
    when the repo holds several. Derived from metadata.name, never the directory name."""
    name = f"{image}-{service.workload}" if multi else image
    return f"{registry}/{name}:{tag}"


def service_dir(app_dir: Path, service: Service) -> str:
    """The service's directory relative to the app checkout; '.' for a root score.yaml."""
    return str(service.path.parent.relative_to(app_dir))


def content_tag(app_dir: Path, rel: str) -> str | None:
    """Git's hash of that directory's CONTENT, or None if it cannot be determined.

    Not the commit SHA: git already stores a hash per directory tree, and it only changes
    when something inside that directory changes. Two commits that leave `frontend/`
    untouched produce the same hash for it.
    """
    target = "HEAD^{tree}" if rel == "." else f"HEAD:{rel}"
    cp = run(["git", "rev-parse", target], cwd=app_dir, check=False, capture=True)
    if cp.returncode != 0:
        return None
    value = cp.stdout.strip()
    return value or None


def plan_images(
    services: list[Service], registry: str, image: str, tag: str,
    app_dir: Path, strategy: str,
) -> dict[str, str]:
    """workload -> the full image reference this render will pin.

    THE ONE PLACE that decides image names. An app's CI asks for this plan (via the
    `image-plan` subcommand) so it builds exactly the tags the renderer is going to
    reference — if the two ever disagreed, Fleet would apply a manifest pointing at an
    image nobody pushed.

    Strategies:
      commit   every workload tagged with the repo's commit SHA. Simple, and correct for a
               single-workload repo.
      content  each workload tagged with the hash of ITS OWN directory. In a repo holding
               many services this is what stops one service's commit from re-tagging — and
               therefore restarting — the other ten. Measured on the 11-service boutique:
               a commit touching only .github/ still rolled all 11 Deployments.
    """
    multi = len(services) > 1
    plan: dict[str, str] = {}
    for svc in services:
        svc_tag = tag
        if strategy == "content":
            rel = service_dir(app_dir, svc)
            found = content_tag(app_dir, rel)
            if found:
                svc_tag = found
            else:
                warn(
                    f"cannot read a content hash for {svc.workload} ({rel}) — the app dir is "
                    f"probably not a git checkout. Falling back to {tag}."
                )
        plan[svc.workload] = image_ref(registry, image, svc, svc_tag, multi=multi)
    return plan


def rewrite_images(services: list[Service], plan: dict[str, str]) -> None:
    """Pin each workload's container image in place, following `plan`.

    We rewrite the score files rather than pass --override-property because that flag only
    works when a SINGLE score file is given (see `score-k8s generate --help`), and
    multi-workload apps must be generated in one invocation so that cross-workload
    ${resources.x.name} references resolve against a shared project state.

    This mutates app_dir, which is expected to be a disposable checkout.
    """
    for svc in services:
        ref = plan[svc.workload]
        spec = yaml.safe_load(svc.path.read_text())
        spec["containers"][svc.container]["image"] = ref
        svc.path.write_text(yaml.safe_dump(spec, sort_keys=False))
        log(f"pinned {svc.workload}.{svc.container} -> {ref}")


# --------------------------------------------------------------------------------------
# state persistence
# --------------------------------------------------------------------------------------
# score-k8s keeps resource identity (the guid every provisioner derives its resource names
# from) and generated secrets in .score-k8s/state.yaml. There is no --state-dir flag, so it
# always lives in the working directory. Discarding it between runs means new resource names
# and a NEW RANDOM POSTGRES PASSWORD on every deploy, which orphans the old PVC and abandons
# the data. So it has to be carried across runs.
#
# It cannot go in git: `score-k8s init --help` warns it holds raw secrets, and the Postgres
# password really is in there in plaintext. A runner-local cache is also wrong the moment a
# second runner picks up the job. So the cluster holds it.
class StateStore:
    def pull(self, dest: Path) -> bool:
        raise NotImplementedError

    def push(self, src: Path) -> None:
        raise NotImplementedError


class NullStateStore(StateStore):
    """No persistence — reproduces the data-loss bug. Only for demonstrating it in tests."""

    def pull(self, dest: Path) -> bool:
        warn("state persistence DISABLED: resource names and generated passwords will churn")
        return False

    def push(self, src: Path) -> None:
        pass


class FileStateStore(StateStore):
    """State in a local file. Used by tests and by hand-replay on a runner."""

    def __init__(self, path: Path):
        self.path = path

    def pull(self, dest: Path) -> bool:
        if not self.path.is_file():
            log(f"no prior state at {self.path} -> first deploy")
            return False
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(self.path, dest)
        log(f"restored state from {self.path}")
        return True

    def push(self, src: Path) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, self.path)
        log(f"saved state to {self.path}")


class StateConflict(SystemExit):
    """Another run wrote this (app, env) state while we were rendering."""


class SecretStateStore(StateStore):
    """State in a cluster Secret, one per (app, env). The production path.

    Writes are OPTIMISTICALLY LOCKED. `kubectl apply` is last-write-wins, so two renders of
    the same (app, env) overlapping would silently discard one side's state — and that state
    is exactly the resource GUIDs and generated Postgres password, so losing it renames the
    StatefulSet and orphans the PVC. That is the failure this whole class exists to prevent,
    so it must not be reintroduced by a race.

    The read captures the Secret's resourceVersion and the write sends it back as a
    precondition: `replace` is rejected by the API server if anyone else wrote in between.
    A first write uses `create`, where AlreadyExists carries the same meaning.

    Runs of one app are normally serialized by the workflow's concurrency group, but that is
    a convention one workflow edit away from being wrong, and the runner is also meant to be
    used for hand-replay of a failed step. The precondition does not depend on either.
    """

    def __init__(self, app: str, env: str, kubeconfig: str | None):
        self.name = f"{app}-{env}-score-state"
        self.kubeconfig = kubeconfig
        # None means "we did not observe an existing Secret", which selects `create`.
        self.resource_version: str | None = None

    def pull(self, dest: Path) -> bool:
        cp = kubectl(
            ["get", "secret", self.name, "-n", STATE_NAMESPACE, "-o", "json"],
            kubeconfig=self.kubeconfig, check=False, capture=True,
        )
        if cp.returncode != 0:
            if "NotFound" in (cp.stderr or ""):
                log(f"no prior state Secret {self.name} -> first deploy")
                return False
            raise SystemExit(f"reading state Secret {self.name} failed: {cp.stderr.strip()}")

        obj = json.loads(cp.stdout)
        # Captured even when the payload is empty: the Secret exists, so our write is still
        # a checked replace rather than a create.
        self.resource_version = (obj.get("metadata") or {}).get("resourceVersion")
        payload = (obj.get("data") or {}).get("state.yaml")
        if not payload:
            log(f"state Secret {self.name} carries no state.yaml -> treating as first deploy")
            return False

        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(base64.b64decode(payload))
        log(f"restored state from Secret {STATE_NAMESPACE}/{self.name}"
            f" (resourceVersion {self.resource_version})")
        return True

    def push(self, src: Path) -> None:
        ensure_namespace(STATE_NAMESPACE, self.kubeconfig)
        body = {
            "apiVersion": "v1",
            "kind": "Secret",
            "type": "Opaque",
            "metadata": {"name": self.name, "namespace": STATE_NAMESPACE},
            "data": {"state.yaml": base64.b64encode(src.read_bytes()).decode()},
        }
        if self.resource_version is None:
            verb, expected = "create", "no Secret existed when this render started"
        else:
            body["metadata"]["resourceVersion"] = self.resource_version
            verb, expected = "replace", f"resourceVersion {self.resource_version}"

        cp = kubectl([verb, "-f", "-"], kubeconfig=self.kubeconfig,
                     stdin=yaml.safe_dump(body), check=False, capture=True)
        if cp.returncode == 0:
            log(f"saved state to Secret {STATE_NAMESPACE}/{self.name} ({verb})")
            return

        err = (cp.stderr or "") + (cp.stdout or "")
        if any(s in err for s in ("AlreadyExists", "the object has been modified",
                                  "Operation cannot be fulfilled", "Conflict")):
            raise StateConflict(
                f"state Secret {STATE_NAMESPACE}/{self.name} changed while this render was "
                f"running (expected {expected}). Another deploy of {self.name} overlapped "
                "this one. Nothing was written — re-run this deploy so it renders from the "
                "current state instead of overwriting it."
            )
        raise SystemExit(f"writing state Secret {self.name} failed: {err.strip()}")


def make_state_store(args) -> StateStore:
    if getattr(args, "no_state", False):
        return NullStateStore()
    if getattr(args, "state_file", None):
        return FileStateStore(Path(args.state_file))
    return SecretStateStore(args.app, args.env, args.kubeconfig)


# --------------------------------------------------------------------------------------
# render
# --------------------------------------------------------------------------------------
def strip_managed_by(docs: list[dict]) -> int:
    """Drop the top-level app.kubernetes.io/managed-by label from every manifest.

    score-k8s stamps `managed-by: score-k8s`. Fleet deploys a Bundle as a Helm release and
    Helm overwrites that same label with `Helm` on whatever it applies, so a manifest that
    carries the label in git can NEVER match the cluster: the Bundle sits at Modified and
    never reaches Ready. Leaving the label out makes git agree with reality — Fleet ignores
    labels that are present live but absent from the desired state.

    Only the top-level metadata is touched. Helm does not rewrite pod template labels, so
    those still match, and nothing selects on managed-by (selectors use name/instance).

    The alternative — per-resource `diff.comparePatches` in fleet.yaml — cannot work in
    general: provisioner-generated resources are named with a GUID (redis-cart-d2eaf96b),
    so their names are not knowable when the config repo is written.
    """
    label = "app.kubernetes.io/managed-by"
    stripped = 0
    for doc in docs:
        labels = (doc.get("metadata") or {}).get("labels") or {}
        if labels.pop(label, None) is not None:
            stripped += 1
    return stripped


def sort_manifests(docs: list[dict]) -> list[dict]:
    """Deterministic order, so a config repo diff shows what actually changed.

    score-k8s does not promise a stable document order between runs. Two renders of the same
    app came out with the workloads in different positions, turning a 22-line change into a
    304-line diff — which makes the config repo's whole point (reviewing what a deploy did)
    useless. Order carries no meaning for these resources, so imposing one costs nothing.
    """
    def key(doc: dict) -> tuple[str, str, str]:
        meta = doc.get("metadata") or {}
        return (doc.get("kind", ""), meta.get("namespace", "") or "", meta.get("name", ""))

    return sorted(docs, key=key)


def split_manifests(manifests: Path, work: Path) -> tuple[Path, Path]:
    """Partition generated manifests into secrets (cluster-only) and everything else (git)."""
    docs = load_all(manifests)
    secrets = [d for d in docs if d.get("kind") == "Secret"]
    public = sort_manifests([d for d in docs if d.get("kind") != "Secret"])
    n = strip_managed_by(public)
    if n:
        log(f"stripped managed-by label from {n} manifest(s) so Fleet sees no false drift")
    sec_path, pub_path = work / "secrets.yaml", work / "app.yaml"
    dump_all(secrets, sec_path)
    dump_all(public, pub_path)
    log(f"split: {len(secrets)} secret(s) -> cluster, {len(public)} manifest(s) -> config repo")
    return sec_path, pub_path


def cmd_render(args) -> None:
    work, catalog, app_dir = Path(args.work), Path(args.catalog), Path(args.app_dir)
    if work.exists():
        shutil.rmtree(work)
    work.mkdir(parents=True)

    store = make_state_store(args)
    store.pull(work / ".score-k8s" / "state.yaml")

    services = discover(app_dir)
    plan = plan_images(services, args.registry, args.image, args.tag, app_dir,
                       getattr(args, "tag_strategy", "commit"))
    rewrite_images(services, plan)

    provisioners = sorted(catalog.glob("provisioners/*.provisioners.yaml"))
    if not provisioners:
        raise SystemExit(f"no provisioners found under {catalog}/provisioners")
    patch = catalog / "patches" / f"{args.env}.tpl"
    if not patch.is_file():
        raise SystemExit(f"missing patch template {patch}")

    init = ["score-k8s", "init", "--no-sample"]
    for p in provisioners:
        init += ["--provisioners", str(p.resolve())]
    init += ["--patch-templates", str(patch.resolve())]
    run(init, cwd=work)

    # One invocation for every workload: cross-workload ${resources.x.name} references only
    # resolve when all workloads share a single project state.
    run(
        ["score-k8s", "generate"]
        + [str(s.path.resolve()) for s in services]
        + ["--output", "manifests.yaml"],
        cwd=work,
    )

    _, public = split_manifests(work / "manifests.yaml", work)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(public, out)
    log(f"wrote {out}")

    store.push(work / ".score-k8s" / "state.yaml")


# --------------------------------------------------------------------------------------
# secrets
# --------------------------------------------------------------------------------------
def _tolerate_exists(cp: subprocess.CompletedProcess, what: str) -> None:
    """Treat AlreadyExists as success; re-raise everything else.

    The bash this replaces used `2>/dev/null || echo "already exists"`, which also swallowed
    auth failures, unreachable clusters and typos — reporting a green deploy with no secret
    in the cluster.
    """
    if cp.returncode == 0:
        log(f"created {what}")
        return
    err = (cp.stderr or "") + (cp.stdout or "")
    if "AlreadyExists" in err or "already exists" in err:
        log(f"{what} already exists -> left as is")
        return
    raise SystemExit(f"creating {what} failed: {err.strip()}")


def ensure_namespace(ns: str, kubeconfig: str | None) -> None:
    _tolerate_exists(
        kubectl(["create", "namespace", ns], kubeconfig=kubeconfig, check=False, capture=True),
        f"namespace {ns}",
    )


def cmd_apply_secrets(args) -> None:
    ns = f"{args.app}-{args.env}"
    ensure_namespace(ns, args.kubeconfig)

    if args.harbor_host:
        _tolerate_exists(
            kubectl(
                ["create", "secret", "docker-registry", PULL_SECRET, "-n", ns,
                 f"--docker-server={args.harbor_host}",
                 f"--docker-username={args.harbor_user}",
                 f"--docker-password={args.harbor_pass}"],
                kubeconfig=args.kubeconfig, check=False, capture=True,
            ),
            f"{PULL_SECRET} in {ns}",
        )
    else:
        warn(f"no --harbor-host given: skipping {PULL_SECRET} in {ns}")

    secrets = Path(args.secrets)
    if not secrets.is_file() or not secrets.stat().st_size:
        log(f"no generated secrets for {ns} -> nothing to apply")
        return
    # create-if-missing, deliberately not apply: never clobber a live credential.
    _tolerate_exists(
        kubectl(["create", "-n", ns, "-f", str(secrets)],
                kubeconfig=args.kubeconfig, check=False, capture=True),
        f"generated secrets in {ns}",
    )


# --------------------------------------------------------------------------------------
# git
# --------------------------------------------------------------------------------------
def is_ancestor(app_dir: Path, maybe_ancestor: str, descendant: str) -> bool | None:
    """True/False, or None when git cannot tell (shallow clone, unknown commit)."""
    cp = run(["git", "merge-base", "--is-ancestor", maybe_ancestor, descendant],
             cwd=app_dir, check=False, capture=True)
    if cp.returncode == 0:
        return True
    if cp.returncode == 1:
        return False
    return None


class OutOfOrder(SystemExit):
    """The commit being deployed is older than what is already deployed."""


def guard_ordering(deployed: str, sha: str, app_dir: Path | None, env: str) -> None:
    """Refuse to move `env` backwards.

    Build durations differ, so a later commit can dispatch BEFORE an earlier one: the
    concurrency group serializes runs but does not reorder them. Without this, the older
    render simply wins and the environment silently regresses.

    `deployed` is whatever the config repo currently records — read it from the version we
    are actually about to write on top of, not from a checkout taken minutes ago.
    """
    if not deployed or not app_dir:
        return
    if deployed == sha:
        log(f"{env} already at {sha}")
        return
    anc = is_ancestor(app_dir, sha, deployed)
    if anc is True:
        raise OutOfOrder(
            f"refusing to deploy {sha} to {env}: it is an ancestor of the already-deployed "
            f"{deployed} (out-of-order dispatch)"
        )
    if anc is None:
        warn(
            f"cannot determine ancestry between {sha} and {deployed} — the app checkout is "
            "probably shallow (needs fetch-depth: 0), or the ref is a tag that was never "
            "fetched. Proceeding."
        )


def upstream_record(config: Path, env: str) -> str:
    """The deploy record as it exists on the remote RIGHT NOW ('' if absent).

    Read after a fetch, so it reflects writers that landed since this job cloned the repo.
    """
    branch = run(["git", "rev-parse", "--abbrev-ref", "HEAD"],
                 cwd=config, capture=True).stdout.strip()
    cp = run(["git", "show", f"origin/{branch}:{SHA_RECORD_DIR}/{env}.sha"],
             cwd=config, check=False, capture=True)
    return cp.stdout.strip() if cp.returncode == 0 else ""


def cmd_commit(args) -> None:
    config, app_dir = Path(args.config_dir), Path(args.app_dir) if args.app_dir else None
    record = config / SHA_RECORD_DIR / f"{args.env}.sha"

    if record.is_file():
        guard_ordering(record.read_text().strip(), args.sha, app_dir, args.env)

    record.parent.mkdir(parents=True, exist_ok=True)
    record.write_text(args.sha + "\n")

    run(["git", "config", "user.name", "platform-orchestrator"], cwd=config)
    run(["git", "config", "user.email", "ci-bot@users.noreply.github.com"], cwd=config)
    run(["git", "add", "."], cwd=config)
    if run(["git", "diff", "--cached", "--quiet"], cwd=config, check=False).returncode == 0:
        log("no manifest changes")
        return

    msg = f"deploy({args.app}): {args.env} {args.sha}"
    if args.catalog_ref:
        msg += f" (catalog: {args.catalog_ref})"
    run(["git", "commit", "-m", msg], cwd=config)

    for attempt in (1, 2, 3):
        if run(["git", "push"], cwd=config, check=False).returncode == 0:
            log("pushed")
            return
        if attempt == 3:
            raise SystemExit("push failed after 3 attempts")
        warn(f"push rejected (new commits upstream) -> pull --rebase, retry ({attempt}/3)")

        # RE-CHECK BEFORE REBASING. Somebody landed commits since this job cloned, and a
        # rebase replays OUR commit on top of theirs — including our deploy record. If what
        # they pushed is newer than what we are holding, rebasing would quietly roll the
        # environment back. The guard only ran against the stale clone, so run it again
        # against what is actually on the remote now.
        run(["git", "fetch", "origin"], cwd=config)
        guard_ordering(upstream_record(config, args.env), args.sha, app_dir, args.env)

        rebase = run(["git", "pull", "--rebase"], cwd=config, check=False, capture=True)
        if rebase.returncode != 0:
            # Usually a genuine conflict, or a branch with no upstream. Either way the
            # retry loop cannot make progress, so fail with the reason rather than a traceback.
            run(["git", "rebase", "--abort"], cwd=config, check=False)
            raise SystemExit(
                "cannot rebase onto the config repo: "
                f"{(rebase.stderr or rebase.stdout or '').strip()}"
            )


# --------------------------------------------------------------------------------------
# promote
# --------------------------------------------------------------------------------------
def replace_tag(ref: str, tag: str) -> str:
    """Swap the tag, treating ':' as a separator only after the last '/' so that a registry
    port (harbor:5000/x/y) is never mistaken for a tag."""
    slash, colon = ref.rfind("/"), ref.rfind(":")
    return (ref[:colon] if colon > slash else ref) + ":" + tag


def retag(path: Path, image: str, tag: str) -> int:
    """Retag only this app's own images; datastore images (postgres:17-alpine) stay put.
    Matches <image>: for a single-workload app and <image>- for multi-workload."""
    docs = load_all(path)
    pattern = re.compile(rf"/{re.escape(image)}[:-]")
    changed = 0
    for doc in docs:
        if doc.get("kind") != "Deployment":
            continue
        spec = doc.get("spec", {}).get("template", {}).get("spec", {})
        for container in spec.get("containers", []) or []:
            ref = container.get("image", "")
            if pattern.search(ref):
                container["image"] = replace_tag(ref, tag)
                changed += 1
    dump_all(docs, path)
    log(f"retagged {changed} container image(s) in {path}")
    return changed


def workload_images(path: Path, image: str) -> dict[tuple[str, str], str]:
    """{(deployment name, container name): image ref} for this app's own containers.

    Datastore images (a provisioner's postgres/redis) are skipped: they are decided by the
    catalog, not by what the app built, so promoting must not touch them.
    """
    pattern = re.compile(rf"/{re.escape(image)}[:-]")
    found = {}
    for doc in load_all(path):
        if doc.get("kind") != "Deployment":
            continue
        name = doc["metadata"]["name"]
        for container in doc.get("spec", {}).get("template", {}).get("spec", {}).get("containers", []) or []:
            ref = container.get("image", "")
            if pattern.search(ref):
                found[(name, container.get("name", ""))] = ref
    return found


def copy_images(src: Path, dst: Path, image: str) -> int:
    """Make dst run exactly the images src is running. Returns how many changed.

    This is what promoting a MULTI-WORKLOAD app means once each service carries its own
    content-derived tag: there is no single "version" to move prod to, there is a SET of
    eleven image references, and prod should run precisely the set staging was verified on.
    """
    wanted = workload_images(src, image)
    docs = load_all(dst)
    changed = 0
    for doc in docs:
        if doc.get("kind") != "Deployment":
            continue
        name = doc["metadata"]["name"]
        for container in doc.get("spec", {}).get("template", {}).get("spec", {}).get("containers", []) or []:
            ref = wanted.get((name, container.get("name", "")))
            if ref and container.get("image") != ref:
                container["image"] = ref
                changed += 1
    dump_all(docs, dst)
    log(f"copied {changed} image(s) from {src} into {dst}")
    return changed


def cmd_promote(args) -> None:
    config = Path(args.config_dir)
    target = config / "prod" / "manifests.yaml"

    if args.mode == "from-staging":
        # Prod runs exactly what staging runs. The only mode that is correct when each
        # service has its own tag, because then "promote to version X" is not a single value.
        source = config / "staging" / "manifests.yaml"
        if not source.is_file():
            raise SystemExit(f"{source} missing — nothing has been deployed to staging yet")
        if not target.is_file():
            raise SystemExit(f"{target} missing — run --mode re-render first")
        if not copy_images(source, target, args.image):
            log("prod already runs the same images as staging")
        return

    if args.mode == "tag-only":
        # Every workload moved to ONE tag. Correct for a single-workload app; for a repo of
        # many services use from-staging instead, or this will point them all at a tag only
        # one of them actually has.
        if not target.is_file():
            raise SystemExit(f"{target} missing — run --mode re-render first")
        if not retag(target, args.image, args.tag):
            warn(f"no image matching /{args.image} found in {target}")
        return

    for flag in ("catalog", "app_dir", "work"):
        if not getattr(args, flag, None):
            raise SystemExit(f"--{flag.replace('_', '-')} is required for --mode re-render")
    render_args = argparse.Namespace(**vars(args))
    render_args.env = "prod"
    render_args.out = str(target)
    cmd_render(render_args)


# --------------------------------------------------------------------------------------
# preflight
# --------------------------------------------------------------------------------------
def cmd_image_plan(args) -> None:
    """Print {workload: image ref} as JSON, for an app's CI to build against.

    The naming rule lives here and nowhere else. An app's CI asks what to build instead of
    reimplementing the rule, because a mismatch between the two is invisible until Fleet
    applies a manifest referencing an image that was never pushed.
    """
    app_dir = Path(args.app_dir)
    services = discover(app_dir)
    plan = plan_images(services, args.registry, args.image, args.tag, app_dir, args.tag_strategy)
    print(json.dumps(plan, indent=2, sort_keys=True))


def cmd_preflight(args) -> None:
    missing = [t for t in ("score-k8s", "kubectl", "git") if not shutil.which(t)]
    if missing:
        raise SystemExit(
            f"runner is missing required tool(s): {', '.join(missing)}. "
            "Check the job landed on a correctly-labelled runner."
        )
    for tool in ("score-k8s", "kubectl", "git"):
        log(f"found {tool} at {shutil.which(tool)}")
    log(f"python {sys.version.split()[0]}, pyyaml {yaml.__version__}")

    if args.require_cluster:
        cp = kubectl(["version", "--output=json"], kubeconfig=args.kubeconfig,
                     check=False, capture=True)
        if cp.returncode != 0:
            raise SystemExit(f"cluster unreachable: {(cp.stderr or '').strip()}")
        log("cluster reachable")
    log("preflight OK")


# --------------------------------------------------------------------------------------
# cli
# --------------------------------------------------------------------------------------
def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    def add_state_flags(p):
        p.add_argument("--state-file", help="persist state in this file instead of a cluster Secret")
        p.add_argument("--no-state", action="store_true",
                       help="disable state persistence (reproduces the churn bug; tests only)")

    def add_render_flags(p, *, paths_required: bool):
        p.add_argument("--app", required=True)
        p.add_argument("--image", help="Harbor image name; defaults to --app")
        p.add_argument("--tag", required=True, help="image tag, normally the commit SHA")
        p.add_argument("--registry", default="registry.staging.internal.dev/platform-images")
        p.add_argument(
            "--tag-strategy", choices=("commit", "content"), default="commit",
            help="commit: every workload gets --tag. content: each workload gets the hash of "
                 "its own directory, so one service's commit does not re-tag the others",
        )
        # Optional for `promote --mode tag-only`, which rewrites an existing manifest and
        # needs no catalog, app checkout or scratch dir.
        p.add_argument("--catalog", required=paths_required, help="checkout of the idp catalog")
        p.add_argument("--app-dir", required=paths_required, help="checkout of the app repo")
        p.add_argument("--work", required=paths_required, help="scratch dir for this render")
        p.add_argument("--kubeconfig")
        add_state_flags(p)

    p = sub.add_parser("image-plan", help="print the workload -> image map this app renders to")
    p.add_argument("--app", required=True)
    p.add_argument("--image", help="image name; defaults to --app")
    p.add_argument("--tag", required=True)
    p.add_argument("--registry", required=True)
    p.add_argument("--app-dir", required=True)
    p.add_argument("--tag-strategy", choices=("commit", "content"), default="commit")
    p.set_defaults(func=cmd_image_plan)

    p = sub.add_parser("preflight")
    p.add_argument("--require-cluster", action="store_true")
    p.add_argument("--kubeconfig")
    p.set_defaults(func=cmd_preflight)

    p = sub.add_parser("render")
    add_render_flags(p, paths_required=True)
    p.add_argument("--env", required=True, choices=("staging", "prod"))
    p.add_argument("--out", required=True)
    p.set_defaults(func=cmd_render)

    p = sub.add_parser("apply-secrets")
    p.add_argument("--app", required=True)
    p.add_argument("--env", required=True, choices=("staging", "prod"))
    p.add_argument("--secrets", required=True)
    p.add_argument("--harbor-host")
    p.add_argument("--harbor-user")
    p.add_argument("--harbor-pass")
    p.add_argument("--kubeconfig")
    p.set_defaults(func=cmd_apply_secrets)

    p = sub.add_parser("commit")
    p.add_argument("--config-dir", required=True)
    p.add_argument("--app", required=True)
    p.add_argument("--env", required=True, choices=("staging", "prod"))
    p.add_argument("--sha", required=True)
    p.add_argument("--app-dir", help="app checkout, needed for the ancestry guard")
    p.add_argument("--catalog-ref")
    p.set_defaults(func=cmd_commit)

    p = sub.add_parser("promote")
    add_render_flags(p, paths_required=False)
    p.add_argument("--mode", required=True,
                   choices=("from-staging", "tag-only", "re-render"))
    p.add_argument("--config-dir", required=True)
    p.set_defaults(func=cmd_promote)

    args = ap.parse_args(argv)
    if getattr(args, "image", None) is None and hasattr(args, "app"):
        args.image = args.app
    args.func(args)


if __name__ == "__main__":
    main()
