"""Score discovery, image planning, state handling and manifest rendering."""
from __future__ import annotations

from . import context as _context
from . import resources as _resources
from . import values as _values
from . import catalog as _catalog
for _module in (_context, _resources, _values, _catalog):
    globals().update({n: getattr(_module, n) for n in dir(_module) if not n.startswith("__")})


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


def build_specs(app_dir, services: list[Service], catalog=None) -> dict[str, dict]:
    """workload -> {"context": …, "dockerfile": …}: CÁCH build ảnh, không phải tên ảnh.

    Vì sao platform phải trả lời câu này thay vì để CI đoán: golden path là monorepo có gói
    dùng chung. `backend/Dockerfile` COPY cả `shared/`, nên context bắt buộc là GỐC KHO —
    build với context `backend/` thì npm không thấy gói workspace và hỏng ngay ở bước cài
    dependency. Mẫu CI cũ gắn cứng `docker build "<workload>/"`, tức là mọi app sinh từ
    stack đều không build được ngay lần chạy CI đầu tiên. Đo được: `COPY shared/ ./shared/`
    → "shared: not found".

    Nguồn sự thật là CATALOG (`buildContext` của component), không phải một bản sao ghi vào
    kho ứng dụng: hai bản sao là hai chỗ phải nhớ sửa. App không có `.idp/stack.yaml` —
    tức mọi app đang chạy — nhận đúng hành vi cũ (context = thư mục của service), nên thay
    đổi này không đụng gì tới chúng.
    """
    catalog = Path(catalog or REPO_ROOT)
    from_stack: dict[str, dict] = {}
    instance = load_stack_instance(app_dir)
    if instance:
        try:
            stack = load_stack(catalog, instance["stack"]["id"])
            for component in stack_components(catalog, stack):
                if _is_workload(component):
                    from_stack[str(component["workload"])] = {
                        "context": str(component.get("buildContext") or component["dir"]),
                        "dockerfile": f"{component['dir']}/Dockerfile",
                    }
        except SystemExit as exc:
            # Kho app ghim một stack mà catalog này không phát hành nữa. Đó là chuyện của
            # `stack-validate`; ở đây chỉ cần build được, nên rơi về quy ước thư mục.
            warn(f"không đọc được stack của app ({exc}) -> dùng quy ước thư mục để build")

    out: dict[str, dict] = {}
    for svc in services:
        rel = service_dir(app_dir, svc)
        out[svc.workload] = from_stack.get(svc.workload) or {
            "context": rel,
            "dockerfile": "Dockerfile" if rel == "." else f"{rel}/Dockerfile",
        }
    return out


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


# The two catalog files that BOTH declare `type: postgres, class: application`. Exactly one
# may be handed to score-k8s per render — a provisioner with a class matches only that
# class, but TWO provisioners for the same type+class is the ambiguous-match trap the
# route/postgres comments already warn about (last one loaded wins, load order is a temp
# filename). So the backend config picks one and the other is left out of the render.
_POSTGRES_APPLICATION_FILES = {
    "cnpg": "postgres-application.provisioners.yaml",
    "statefulset": "postgres-application-statefulset.provisioners.yaml",
}


def select_provisioner_files(catalog: Path) -> list[Path]:
    """The provisioner files to render, with the database backend resolved from config.

    Every `*.provisioners.yaml` is used EXCEPT the `class: application` file for the
    backend NOT chosen. Default backend is cnpg, so a catalog that only ships the CNPG
    file behaves exactly as before; the StatefulSet file is inert unless it exists AND
    database.backend is statefulset.
    """
    backend = database_backend()
    drop = {name for key, name in _POSTGRES_APPLICATION_FILES.items() if key != backend}
    chosen = [p for p in sorted(catalog.glob("provisioners/*.provisioners.yaml"))
              if p.name not in drop]
    if not chosen:
        raise SystemExit(f"no provisioners found under {catalog}/provisioners")
    wanted = _POSTGRES_APPLICATION_FILES[backend]
    if feature("postgres_application") and not any(p.name == wanted for p in chosen):
        raise SystemExit(
            f"database.backend={backend!r} needs {wanted} in {catalog}/provisioners, "
            "but the catalog does not ship it."
        )
    return chosen


def materialise_catalog(
    provisioners: list[Path], patch: Path, dest: Path, env: str, app: str | None = None,
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
        target.write_text(CONFIG.render(src.read_text(), env, where=str(src), app=app))
        out_provisioners.append(target)
    out_patch = dest / patch.name
    out_patch.write_text(CONFIG.render(patch.read_text(), env, where=str(patch), app=app))
    log(f"resolved {len(out_provisioners)} provisioner(s) + patch for env={env} -> {dest}")
    return {"provisioners": out_provisioners, "patch": out_patch}


def ensure_fleet_yaml(env_dir: Path, app: str, env: str) -> None:
    """Sinh fleet.yaml nếu thư mục môi trường chưa có.

    Fleet coi mỗi thư mục có fleet.yaml là một Bundle riêng, và lấy defaultNamespace
    trong đó làm nơi đặt tài nguyên. THIẾU file này thì namespace của app trống trơn
    trong khi manifest vẫn nằm đúng trong git — bước verify báo "chưa tồn tại trên cụm"
    và rất khó đoán ra nguyên nhân. Đã mất một buổi vì nó khi triển khai ở công ty.

    KHÔNG ghi đè nếu đã có: ai muốn tuỳ biến (thêm helm values, đổi tên Bundle) thì vẫn
    tuỳ biến được, platform không giẫm lên.
    """
    f = env_dir / "fleet.yaml"
    if f.exists():
        log(f"{f} đã có -> giữ nguyên")
        return
    ns = app_namespace(app, env)
    f.write_text(
        "# Sinh tự động bởi idpctl. Sửa tay được — lần render sau sẽ không ghi đè.\n"
        f"namespace: {ns}\n"
        f"defaultNamespace: {ns}\n"
    )
    log(f"sinh {f} (namespace {ns})")


def apply_application_values(services: list[Service], app_dir: Path, catalog_dir: Path, *,
                             app: str, env: str) -> list[Path]:
    """Validate an app's Score against ApplicationValues and emit the environment provisioner.

    Returns extra provisioner files for score-k8s, empty when the app does not use the
    feature. Runs BEFORE score-k8s so every diagnostic here names the app's own file rather
    than a generated manifest.
    """
    spec = load_application_values(app_dir)
    hard = feature("application_values")

    # The placeholder scan runs for every app, values file or not — a `${resources.…}` in
    # command or args is broken regardless. It only WARNS while the feature is off, because
    # such an app is already deployed and already broken in that spot, and turning a
    # long-standing latent bug into a failed deploy is not this change's job.
    scores = []
    for service in services:
        doc = yaml.safe_load(service.path.read_text()) or {}
        scores.append((service, doc))
        scan_placeholders(doc, where=f"{service.path.name} ({service.workload})", hard=hard)

    check_database_classes(scores, env)

    aliases = [(service, doc, environment_alias(doc, where=f"{service.path.name} "
                                                             f"({service.workload})"))
               for service, doc in scores]
    wants_environment = [s.workload for s, _, alias in aliases if alias]

    if not hard:
        # Fail here rather than letting score-k8s do it. Its message for an unprovisioned
        # type is "resource 'environment.default#web.cfg' is not supported by any
        # provisioner. Please implement a custom resource provisioner", which sends the
        # reader off to write one — when the actual answer is a one-line platform config
        # change they have no way to guess from that text.
        if wants_environment:
            raise SystemExit(
                f"workload(s) {wants_environment} declare a `type: environment` resource, "
                "but features.application_values is off for this platform. Set "
                "features.application_values: true in platform.env.yaml to enable "
                f"{VALUES_REL}, or remove the resource."
            )
        if spec is not None:
            warn(f"{VALUES_REL} is present but features.application_values is off — the "
                 "file is being ignored. Enable the flag in platform.env.yaml to use it.")
        return []
    if spec is None:
        if wants_environment:
            raise SystemExit(
                f"workload(s) {wants_environment} declare a `type: environment` resource, "
                f"but the app has no {VALUES_REL} to fill it from."
            )
        return []

    resolved = resolve_application_values(spec, env)
    used: set[str] = set()
    # Per workload, not just the union: a secretRef becomes a reference to a Secret that
    # belongs to ONE workload, so the renderer needs to know who asked for what.
    used_by_workload: dict[str, set[str]] = {}
    consumers = 0
    for service, doc, alias in aliases:
        if alias is None:
            continue
        where = f"{service.path.name} ({service.workload})"
        consumers += 1
        check_file_secrets(doc, resolved, where=where)
        mine = check_referenced_keys(doc, alias, resolved, where=where)
        used |= mine
        used_by_workload.setdefault(service.workload, set()).update(mine)

    if not consumers:
        warn(f"{VALUES_REL} defines {len(resolved)} value(s) for {env}, but no workload "
             "declares a `type: environment` resource, so none of them reach a container.")
        return []
    if unused := sorted(set(resolved) - used):
        # A warning, not an error: a key can legitimately serve only one of several
        # environments, or be staged ahead of the code that will read it.
        warn(f"{VALUES_REL}: value(s) not referenced by any workload in {env}: {unused}")

    return [write_environment_provisioner(
        resolved, catalog_dir / "generated.environment.provisioners.yaml", app=app, env=env,
        used_by_workload=used_by_workload)]


# ------------------------------------------------------------------- prod values digest
def prod_values_record(config_dir: Path) -> Path:
    return Path(config_dir) / sha_record_dir() / "prod.values.sha256"


def record_prod_values_digest(app_dir: Path, config_dir: Path, env: str) -> None:
    """After rendering prod, record which values that render was built from.

    Only prod, and only for apps that use the feature — an app with no values file leaves
    no record and is therefore never subject to the guard below.
    """
    if env != "prod" or not feature("application_values"):
        return
    spec = load_application_values(app_dir)
    if spec is None:
        return
    record = prod_values_record(config_dir)
    record.parent.mkdir(parents=True, exist_ok=True)
    record.write_text(values_digest(spec) + "\n")
    log(f"recorded prod values digest -> {record}")


def guard_prod_values(args) -> None:
    """Refuse a no-render promotion when the prod values have moved since the last render.

    `tag-only` and `from-staging` are fast because they rewrite image tags in a manifest
    that already exists — they never run the renderer. That is correct for a pure version
    bump and WRONG the moment someone also edited the prod values block: the promotion
    reports success, production keeps the old configuration, and the edit appears to have
    been applied. Comparing digests turns that silent skip into a refusal that names the
    fix.

    An app with no record has never rendered prod through this feature, so there is nothing
    to compare and nothing to guard — that is the entire legacy fleet, left alone.
    """
    record = prod_values_record(Path(args.config_dir))
    if not record.is_file():
        return
    recorded = record.read_text().strip()
    app_dir = getattr(args, "app_dir", None)
    if not app_dir:
        raise SystemExit(
            f"{record} exists, so this app's prod render depends on {VALUES_REL}, but "
            "--app-dir was not supplied. Promotion cannot check whether those values "
            "changed without a checkout of the app at the tag being promoted."
        )
    current = values_digest(load_application_values(Path(app_dir)) or {})
    if current != recorded:
        raise SystemExit(
            f"prod values have changed since the last prod render.\n"
            f"  recorded: {recorded[:16]}…\n"
            f"  current:  {current[:16]}…\n"
            f"--mode {args.mode} only rewrites image tags in the existing manifest, so the "
            "new values would NOT reach production while the promotion reported success. "
            "Use --mode re-render."
        )
    log("prod values digest matches the last render")


def cmd_render(args) -> None:
    # Checked here and not only in preflight. preflight is a separate workflow step, so it
    # proves the runner was sane at the top of the job — not that THIS render used the
    # pinned binary. Anyone replaying a render by hand skips preflight entirely. The check
    # memoises, so it costs one subprocess per process, not one per workload.
    check_tool_versions(["score-k8s"])

    work, catalog, app_dir = Path(args.work), Path(args.catalog), Path(args.app_dir)
    if work.exists():
        shutil.rmtree(work)
    work.mkdir(parents=True)

    store = make_state_store(args)
    store.pull(work / ".score-k8s" / "state.yaml")

    services = discover(app_dir)
    check_postgres_class_migration(
        services, work / ".score-k8s" / "state.yaml",
        accepted=getattr(args, "accept_empty_database", False))
    plan = plan_images(services, args.registry, args.image, args.tag, app_dir,
                       resolve_tag_strategy(app_dir, getattr(args, "tag_strategy", "")))
    rewrite_images(services, plan)

    provisioners = select_provisioner_files(catalog)
    patch = catalog / "patches" / f"{args.env}.tpl"
    if not patch.is_file():
        raise SystemExit(f"missing patch template {patch}")

    # Resolve %%placeholders%% into a scratch copy before score-k8s ever sees these files.
    # The catalog stores the SHAPE of a resource (a route becomes an HTTPRoute); this fills
    # in the COORDINATES of the cluster it is being rendered for (which gateway, which
    # storage class). Keeping the two apart is what lets one catalog serve every environment.
    resolved = materialise_catalog(provisioners, patch, work / "catalog", args.env,
                                   app=args.app)

    extra_provisioners = apply_application_values(services, app_dir, work / "catalog",
                                                  app=args.app, env=args.env)

    init = ["score-k8s", "init", "--no-sample"]
    for p in list(resolved["provisioners"]) + extra_provisioners:
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
    ensure_fleet_yaml(out.parent, args.app, args.env)
    record_prod_values_digest(app_dir, out.parent.parent, args.env)

    store.push(work / ".score-k8s" / "state.yaml")
