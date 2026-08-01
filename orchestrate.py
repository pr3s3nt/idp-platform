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

# --------------------------------------------------------------------------------------
# environment config
# --------------------------------------------------------------------------------------
# Every infrastructure-dependent value lives in platform.env.yaml, never in code. Moving
# this platform to another company's cluster must be a config edit, not a patch.
#
# The DEFAULTS below are the sandbox's values and exist only so the tool still runs with no
# config file — tests and hand-replay on a runner. Anything real passes --env-config.
DEFAULTS: dict = {
    "git": {"org": "", "config_repo_pattern": "{app}-config", "default_branch": "main",
            "committer_name": "idp-orchestrator",
            "committer_email": "idp-orchestrator@noreply.invalid"},
    "registry": {"host": "", "path": "", "pull_secret": "registry-pull"},
    "kubernetes": {
        "state_namespace": "cluster-state",
        "namespace_pattern": "{app}-{env}",
        "storage_class": "",
        "sha_record_dir": ".platform",
    },
    "ingress": {"gateway_name": "", "gateway_namespace": ""},
    "images": {},
    "environments": {},
}

# Placeholder syntax for provisioners and patch templates. Deliberately NOT {{ }} — those
# files are Go templates owned by score-k8s, and NOT ${ } — that is score's own resource
# reference syntax. %% %% collides with neither and greps cleanly.
PLACEHOLDER = re.compile(r"%%([a-zA-Z0-9_.]+)%%")


class EnvConfig:
    """platform.env.yaml, with dotted lookup."""

    def __init__(self, data: dict | None = None):
        self.data = _deep_merge(DEFAULTS, data or {})

    @classmethod
    def load(cls, path: str | None) -> EnvConfig:
        if not path:
            return cls()
        p = Path(path)
        if not p.is_file():
            raise SystemExit(f"env config not found: {p}")
        log(f"loaded environment config from {p}")
        return cls(yaml.safe_load(p.read_text()) or {})

    def get(self, dotted: str, default=None):
        node = self.data
        for part in dotted.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node

    def require(self, dotted: str):
        value = self.get(dotted)
        if value in (None, ""):
            raise SystemExit(
                f"platform.env.yaml is missing '{dotted}'. Every infrastructure value must "
                "come from that file — nothing is hardcoded in the renderer."
            )
        return value

    def for_env(self, env: str) -> dict:
        """Flat {dotted key: value}, with the chosen environment exposed under `env.`."""
        flat: dict[str, object] = {}

        def walk(node, prefix=""):
            for key, value in (node or {}).items():
                if key == "environments":
                    continue
                path = f"{prefix}{key}"
                if isinstance(value, dict):
                    walk(value, f"{path}.")
                else:
                    flat[path] = value

        walk(self.data)
        for key, value in (self.get(f"environments.{env}") or {}).items():
            flat[f"env.{key}"] = value
        flat["env.name"] = env

        # A provisioner that points at ANOTHER app's namespace only learns that app's name
        # at render time, from a param — so it cannot be substituted here. What we CAN do is
        # bake the environment in and leave the app as a Go template expression, so the
        # namespace convention still lives in config and not in the provisioner:
        #   "{app}-{env}"  ->  "{{ .Params.app }}-staging"
        pattern = self.get("kubernetes.namespace_pattern", "{app}-{env}") or "{app}-{env}"
        flat["computed.namespace_go_template"] = (
            pattern.replace("{env}", env).replace("{app}", "{{ .Params.app }}")
        )
        return flat

    def render(self, text: str, env: str, *, where: str) -> str:
        """Substitute %%key%% placeholders. An unknown key is fatal, never silent.

        Silence is how this whole project's worst bugs behaved — a wrong gateway name or a
        wrong storage class produces no error anywhere, just a route that never attaches or
        a volume that never binds. A typo'd placeholder must not join that club.
        """
        table = self.for_env(env)

        def replace(match: re.Match) -> str:
            key = match.group(1)
            if key not in table:
                known = ", ".join(sorted(table)[:8])
                raise SystemExit(
                    f"{where}: unknown placeholder %%{key}%%. "
                    f"Add it to platform.env.yaml. Known keys include: {known}…"
                )
            return str(table[key])

        return PLACEHOLDER.sub(replace, text)


def _deep_merge(base: dict, override: dict) -> dict:
    out = dict(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


# Set from --env-config at startup; the defaults keep everything runnable without one.
CONFIG = EnvConfig()


# Small accessors instead of module constants: the values now come from platform.env.yaml,
# which is loaded after import, so they cannot be frozen at module level.
def state_ns() -> str:
    return CONFIG.get("kubernetes.state_namespace")


def pull_secret() -> str:
    return CONFIG.get("registry.pull_secret")


def sha_record_dir() -> str:
    return CONFIG.get("kubernetes.sha_record_dir")


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
            ["get", "secret", self.name, "-n", state_ns(), "-o", "json"],
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
        log(f"restored state from Secret {CONFIG.get('kubernetes.state_namespace')}/{self.name}"
            f" (resourceVersion {self.resource_version})")
        return True

    def push(self, src: Path) -> None:
        ensure_namespace(state_ns(), self.kubeconfig)
        body = {
            "apiVersion": "v1",
            "kind": "Secret",
            "type": "Opaque",
            "metadata": {"name": self.name, "namespace": state_ns()},
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
            log(f"saved state to Secret {state_ns()}/{self.name} ({verb})")
            return

        err = (cp.stderr or "") + (cp.stdout or "")
        if any(s in err for s in ("AlreadyExists", "the object has been modified",
                                  "Operation cannot be fulfilled", "Conflict")):
            raise StateConflict(
                f"state Secret {state_ns()}/{self.name} changed while this render was "
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


def materialise_catalog(
    provisioners: list[Path], patch: Path, dest: Path, env: str,
) -> dict[str, object]:
    """Copy the catalog into `dest` with every %%placeholder%% resolved for `env`.

    The originals are never modified — the catalog checkout is shared and pinned. Writing
    the resolved copies to disk (rather than piping them) is deliberate: when a render goes
    wrong, the exact files score-k8s was handed are still sitting in the work directory.
    """
    dest.mkdir(parents=True, exist_ok=True)
    out_provisioners = []
    for src in provisioners:
        target = dest / src.name
        target.write_text(CONFIG.render(src.read_text(), env, where=str(src)))
        out_provisioners.append(target)
    out_patch = dest / patch.name
    out_patch.write_text(CONFIG.render(patch.read_text(), env, where=str(patch)))
    log(f"resolved {len(out_provisioners)} provisioner(s) + patch for env={env} -> {dest}")
    return {"provisioners": out_provisioners, "patch": out_patch}


def cmd_render(args) -> None:
    work, catalog, app_dir = Path(args.work), Path(args.catalog), Path(args.app_dir)
    if work.exists():
        shutil.rmtree(work)
    work.mkdir(parents=True)

    store = make_state_store(args)
    store.pull(work / ".score-k8s" / "state.yaml")

    services = discover(app_dir)
    plan = plan_images(services, args.registry, args.image, args.tag, app_dir,
                       getattr(args, "tag_strategy", "content"))
    rewrite_images(services, plan)

    provisioners = sorted(catalog.glob("provisioners/*.provisioners.yaml"))
    if not provisioners:
        raise SystemExit(f"no provisioners found under {catalog}/provisioners")
    patch = catalog / "patches" / f"{args.env}.tpl"
    if not patch.is_file():
        raise SystemExit(f"missing patch template {patch}")

    # Resolve %%placeholders%% into a scratch copy before score-k8s ever sees these files.
    # The catalog stores the SHAPE of a resource (a route becomes an HTTPRoute); this fills
    # in the COORDINATES of the cluster it is being rendered for (which gateway, which
    # storage class). Keeping the two apart is what lets one catalog serve every environment.
    resolved = materialise_catalog(provisioners, patch, work / "catalog", args.env)

    init = ["score-k8s", "init", "--no-sample"]
    for p in resolved["provisioners"]:
        init += ["--provisioners", str(p.resolve())]
    init += ["--patch-templates", str(resolved["patch"].resolve())]
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
                ["create", "secret", "docker-registry", pull_secret(), "-n", ns,
                 f"--docker-server={args.harbor_host}",
                 f"--docker-username={args.harbor_user}",
                 f"--docker-password={args.harbor_pass}"],
                kubeconfig=args.kubeconfig, check=False, capture=True,
            ),
            f"{pull_secret()} in {ns}",
        )
    else:
        warn(f"no --harbor-host given: skipping {pull_secret()} in {ns}")

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


def upstream_record(config: Path, env: str, base: str) -> str:
    """The deploy record as it exists on the remote RIGHT NOW ('' if absent).

    Read after a fetch, so it reflects writers that landed since this job cloned the repo.
    `base` is the branch this deploy targets — with the PR flow the working branch is a
    throwaway, so reading HEAD's name would compare against the wrong thing.
    """
    cp = run(["git", "show", f"origin/{base}:{sha_record_dir()}/{env}.sha"],
             cwd=config, check=False, capture=True)
    return cp.stdout.strip() if cp.returncode == 0 else ""


def branch_is_protected(config: Path, branch: str) -> bool | None:
    """Nhánh đích có branch protection không? None nghĩa là không xác định được.

    Đây là NGUỒN SỰ THẬT DUY NHẤT cho việc "môi trường này có cần duyệt không". Trước đây
    nó là một cờ trong platform.env.yaml, tức là hai nơi cùng khai một sự thật — và một cờ
    ghi `require_pr: false` trong khi nhánh thật đang được bảo vệ là một lời nói dối chỉ
    vỡ ra lúc push.

    Hệ quả thực tế: một repo demo không bật bảo vệ thì tự phục vụ hoàn toàn, không phải
    khai gì. Đến khi đội đó làm nghiêm túc, họ bật protection và platform tự chuyển sang
    chế độ pull request — không sửa cấu hình, không deploy lại.
    """
    url = run(["git", "remote", "get-url", "origin"],
              cwd=config, check=False, capture=True).stdout.strip()
    m = re.search(r"[:/]([^/]+)/([^/]+?)(?:\.git)?$", url)
    if not m:
        return None
    cp = run(["gh", "api", f"repos/{m.group(1)}/{m.group(2)}/branches/{branch}",
              "--jq", ".protected"], cwd=config, check=False, capture=True)
    if cp.returncode != 0:
        return None
    value = cp.stdout.strip()
    return {"true": True, "false": False}.get(value)


def open_pull_request(config: Path, base: str, head: str, title: str, body: str) -> str:
    """Open a PR and return its URL. Does NOT merge — a human approves and merges.

    Used for environments whose branch requires review. Deliberately stops here: the point
    of the approval is that a person looks at the manifest diff before production changes,
    and a bot that merges its own PR would defeat it.
    """
    cp = run(["gh", "pr", "create", "--base", base, "--head", head,
              "--title", title, "--body", body],
             cwd=config, check=False, capture=True)
    if cp.returncode != 0:
        err = (cp.stderr or "") + (cp.stdout or "")
        # A PR for this branch may already exist if the job is being re-run.
        if "already exists" in err:
            existing = run(["gh", "pr", "view", head, "--json", "url", "-q", ".url"],
                           cwd=config, check=False, capture=True)
            if existing.returncode == 0:
                return existing.stdout.strip()
        raise SystemExit(f"could not open pull request: {err.strip()}")
    return cp.stdout.strip().splitlines()[-1] if cp.stdout.strip() else ""


def cmd_commit(args) -> None:
    config, app_dir = Path(args.config_dir), Path(args.app_dir) if args.app_dir else None
    record = config / sha_record_dir() / f"{args.env}.sha"

    if record.is_file():
        guard_ordering(record.read_text().strip(), args.sha, app_dir, args.env)

    record.parent.mkdir(parents=True, exist_ok=True)
    record.write_text(args.sha + "\n")

    # Branch and review requirement come from platform.env.yaml, read HERE rather than
    # compared as strings in the workflow YAML. `require_pr: "true"` written as a string
    # instead of a boolean would silently fail a YAML string comparison and push straight
    # at a protected branch; Python decides once, correctly, for every caller.
    # Base is ALWAYS the branch actually checked out — we must push to the thing we
    # rendered against, never to a branch named somewhere else. The configured value is
    # used to CHECK that, not to override it: if they disagree the job cloned one branch
    # and would publish to another, which is how an environment silently gets the wrong
    # manifests. Fail instead.
    checked_out = run(["git", "rev-parse", "--abbrev-ref", "HEAD"],
                      cwd=config, capture=True).stdout.strip()
    base = getattr(args, "branch", None) or checked_out
    configured = CONFIG.get(f"environments.{args.env}.config_branch")
    if configured and configured != base:
        raise SystemExit(
            f"config repo is checked out on '{base}' but platform.env.yaml says {args.env} "
            f"lives on '{configured}'. The checkout and the target must match, or this "
            "deploy would publish manifests to a branch it never rendered against."
        )
    # Hỏi GitHub, không đọc cấu hình. Xem branch_is_protected().
    protected = branch_is_protected(config, base)
    if protected is None:
        # Không xác định được thì đi đường push thẳng — CỐ Ý.
        # Nếu nhánh thật ra có bảo vệ, GitHub sẽ từ chối push kèm thông báo rõ ràng
        # (GH006), tức là hỏng ỒN ÀO. Còn đoán ngược lại thì sinh ra một pull request
        # nằm im không ai biết, trên một repo demo chẳng ai chờ pull request nào cả.
        # Việc cưỡng chế nằm ở phía GitHub, không phải ở đoán của chúng ta.
        warn(f"không xác định được nhánh {base} có bảo vệ hay không -> thử push thẳng")
    via_pr = getattr(args, "via_pr", False) or bool(protected)
    log(f"{base}: bảo vệ={protected} -> {'mở pull request' if via_pr else 'push thẳng'}")

    # Danh tính này quyết định lịch sử config repo ghi công cho ai — thứ dùng để truy vết
    # "ai deploy cái gì". Lấy từ cấu hình, không gắn cứng.
    run(["git", "config", "user.name",
         CONFIG.get("git.committer_name", "idp-orchestrator")], cwd=config)
    run(["git", "config", "user.email",
         CONFIG.get("git.committer_email", "idp-orchestrator@noreply.invalid")], cwd=config)
    run(["git", "add", "."], cwd=config)
    nothing_staged = run(["git", "diff", "--cached", "--quiet"],
                         cwd=config, check=False).returncode == 0

    # "Nothing new to stage" is NOT the same as "nothing to do". A previous attempt may have
    # committed and then failed to push; returning here would leave that commit stranded and
    # report success, so re-running a broken deploy would fix nothing. Check for unpushed
    # work before giving up.
    run(["git", "fetch", "origin", base], cwd=config, check=False)
    unpushed = run(["git", "rev-list", "--count", f"origin/{base}..HEAD"],
                   cwd=config, check=False, capture=True)
    ahead = unpushed.returncode == 0 and unpushed.stdout.strip() not in ("", "0")

    if nothing_staged and not ahead:
        log("no manifest changes")
        return
    if not nothing_staged:
        msg = f"deploy({args.app}): {args.env} {args.sha}"
        if args.catalog_ref:
            msg += f" (catalog: {args.catalog_ref})"
        run(["git", "commit", "-m", msg], cwd=config)
    else:
        msg = f"deploy({args.app}): {args.env} {args.sha}"
        log(f"nothing new to commit, but {unpushed.stdout.strip()} commit(s) never reached "
            f"origin/{base} -> pushing those")

    # Environments whose branch requires review never get a direct push. The bot puts the
    # change on its own branch and opens a PR; a person reads the manifest diff and merges.
    if via_pr:
        head = f"deploy/{args.app}-{args.env}-{args.sha[:8]}"
        run(["git", "checkout", "-B", head], cwd=config)
        run(["git", "push", "--force-with-lease", "origin", head], cwd=config)
        url = open_pull_request(
            config, base, head,
            title=msg,
            body=(
                f"Triển khai tự động do orchestrator sinh ra.\n\n"
                f"| | |\n|---|---|\n"
                f"| app | `{args.app}` |\n| môi trường | `{args.env}` |\n"
                f"| commit | `{args.sha}` |\n| catalog | `{args.catalog_ref or 'n/a'}` |\n\n"
                "Diff bên dưới chính là thứ sẽ thay đổi trên cụm sau khi merge.\n"
                "**Không sửa tay** — lần triển khai sau sẽ ghi đè."
            ),
        )
        log(f"opened pull request into {base}: {url}")
        print(url)
        return

    # Push EXPLICITLY to the branch we validated, never a bare `git push`. A bare push
    # depends on tracking configuration: a branch checked out without an upstream fails
    # with "no upstream branch", which the retry below then misreads as "somebody pushed
    # first" and sends into a rebase that cannot work. Naming the target also removes any
    # chance of publishing to whatever branch tracking happens to point at.
    for attempt in (1, 2, 3):
        if run(["git", "push", "origin", f"HEAD:{base}"],
               cwd=config, check=False).returncode == 0:
            log(f"pushed to {base}")
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
        guard_ordering(upstream_record(config, args.env, base), args.sha, app_dir, args.env)

        rebase = run(["git", "pull", "--rebase", "origin", base],
                     cwd=config, check=False, capture=True)
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
def cmd_config(args) -> None:
    """Expose platform.env.yaml to the workflow, so the YAML holds no infrastructure value.

    `--export` prints KEY=value lines the workflow appends to $GITHUB_ENV. The one thing
    that CANNOT come from here is `runs-on`: GitHub resolves it before any step executes,
    so runner labels have to be a repository variable. That is still configuration rather
    than code, but it is a second place to edit and the docs must say so.
    """
    if args.get:
        value = CONFIG.get(args.get)
        if value is None:
            raise SystemExit(f"no such key in platform.env.yaml: {args.get}")
        print(value)
        return
    if args.export:
        table = CONFIG.for_env(args.env)
        for key in ("git.org", "registry.host", "registry.path", "registry.pull_secret",
                    "kubernetes.state_namespace", "ingress.gateway_name"):
            if key in table:
                print(f"{key.replace('.', '_').upper()}={table[key]}")
        pattern = CONFIG.get("git.config_repo_pattern", "{app}-config")
        print(f"CONFIG_REPO_PATTERN={pattern}")
        return
    print(json.dumps(CONFIG.data, indent=2, sort_keys=True))


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
    # gh cần cho việc đọc branch protection và mở pull request trong bước commit.
    missing = [t for t in ("score-k8s", "kubectl", "git", "gh") if not shutil.which(t)]
    if missing:
        raise SystemExit(
            f"runner is missing required tool(s): {', '.join(missing)}. "
            "Check the job landed on a correctly-labelled runner."
        )
    for tool in ("score-k8s", "kubectl", "git", "gh"):
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
    # Global: every subcommand may need an infrastructure value, and none of them should
    # ever have one baked in.
    ap.add_argument("--env-config", help="path to platform.env.yaml")
    sub = ap.add_subparsers(dest="cmd", required=True)

    def add_state_flags(p):
        p.add_argument("--state-file", help="persist state in this file instead of a cluster Secret")
        p.add_argument("--no-state", action="store_true",
                       help="disable state persistence (reproduces the churn bug; tests only)")

    def add_render_flags(p, *, paths_required: bool):
        p.add_argument("--app", required=True)
        p.add_argument("--image", help="Harbor image name; defaults to --app")
        p.add_argument("--tag", required=True, help="image tag, normally the commit SHA")
        # No default: the registry is infrastructure, so it comes from platform.env.yaml
        # via the workflow. A hardcoded fallback here is exactly how a deploy ends up
        # pushing to the wrong company's registry.
        p.add_argument("--registry", required=True)
        p.add_argument(
            "--tag-strategy", choices=("commit", "content"), default="content",
            help="content (mặc định): mỗi workload mang mã băm THƯ MỤC của chính nó. "
                 "commit: mọi workload dùng --tag. Xem ghi chú trong plan_images.",
        )
        # Optional for `promote --mode tag-only`, which rewrites an existing manifest and
        # needs no catalog, app checkout or scratch dir.
        p.add_argument("--catalog", required=paths_required, help="checkout of the idp catalog")
        p.add_argument("--app-dir", required=paths_required, help="checkout of the app repo")
        p.add_argument("--work", required=paths_required, help="scratch dir for this render")
        p.add_argument("--kubeconfig")
        add_state_flags(p)

    p = sub.add_parser("config", help="read platform.env.yaml (for the workflow to consume)")
    p.add_argument("--get", help="dotted key, e.g. registry.path")
    p.add_argument("--export", action="store_true",
                   help="print KEY=value lines for $GITHUB_ENV")
    p.add_argument("--env", default="staging")
    p.set_defaults(func=cmd_config)

    p = sub.add_parser("image-plan", help="print the workload -> image map this app renders to")
    p.add_argument("--app", required=True)
    p.add_argument("--image", help="image name; defaults to --app")
    p.add_argument("--tag", required=True)
    p.add_argument("--registry", required=True)
    p.add_argument("--app-dir", required=True)
    p.add_argument("--tag-strategy", choices=("commit", "content"), default="content")
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
    p.add_argument("--branch", help="branch of the config repo this environment targets")
    p.add_argument("--via-pr", action="store_true",
                   help="open a pull request instead of pushing; for environments that "
                        "require review before the cluster changes")
    p.set_defaults(func=cmd_commit)

    p = sub.add_parser("promote")
    add_render_flags(p, paths_required=False)
    p.add_argument("--mode", required=True,
                   choices=("from-staging", "tag-only", "re-render"))
    p.add_argument("--config-dir", required=True)
    p.set_defaults(func=cmd_promote)

    args = ap.parse_args(argv)
    global CONFIG
    CONFIG = EnvConfig.load(args.env_config)
    if getattr(args, "image", None) is None and hasattr(args, "app"):
        args.image = args.app
    args.func(args)


if __name__ == "__main__":
    main()
