"""Stack catalog loading, generation, validation and upgrade operations."""
from __future__ import annotations

from . import context as _context
from . import values as _values
for _module in (_context, _values):
    globals().update({n: getattr(_module, n) for n in dir(_module) if not n.startswith("__")})


# --------------------------------------------------------------------------------------
# Stack catalog — archetype × runtime × capability
# --------------------------------------------------------------------------------------
# See section 9 of the plan. There is deliberately NO template per combination: a stack is
# an assembly of components plus capabilities, so fixing `node-api` once fixes it in every
# stack that contains it. A copied template is a copy that gets forgotten.
#
# What lives where:
#   templates/stacks/<id>.stack.yaml       which components and capabilities a stack has
#   templates/stacks/components/<id>/      the files one component contributes
#   templates/stacks/capabilities/<id>/    YAML spliced into a consuming workload's Score
#   templates/stacks/base/files/           repo-level files every stack gets
#   templates/score-compose/               local provisioners vendored into the app repo
STACK_REL = ".idp/stack.yaml"
STACK_API_VERSION = "idp.company/v1"
STACK_KIND = "Stack"
STACK_COMPONENT_KIND = "StackComponent"
STACK_CAPABILITY_KIND = "StackCapability"
STACK_INSTANCE_KIND = "StackInstance"

# __TOKEN__ substitution, checked for leftovers after every render. Same reasoning as the
# %%placeholder%% scanner: a typo'd token that silently survives into a generated app repo
# is a defect nobody sees until a developer reads their own Dockerfile and finds __PORT__.
STACK_TOKEN = re.compile(r"__[A-Z][A-Z0-9_]*__")


def stacks_dir(catalog) -> Path:
    d = Path(catalog) / "templates" / "stacks"
    if not d.is_dir():
        raise SystemExit(
            f"no stack catalog at {d}. --catalog must point at a checkout of the idp "
            "platform repo (the one holding templates/stacks/)."
        )
    return d


def _stack_doc(path: Path, kind: str) -> dict:
    """Load and check the envelope of a stack-catalog document."""
    doc = yaml.safe_load(path.read_text())
    if not isinstance(doc, dict):
        raise SystemExit(f"{path}: expected a YAML mapping.")
    if doc.get("apiVersion") != STACK_API_VERSION:
        raise SystemExit(
            f"{path}: apiVersion must be {STACK_API_VERSION!r}, got {doc.get('apiVersion')!r}."
        )
    if doc.get("kind") != kind:
        raise SystemExit(f"{path}: kind must be {kind!r}, got {doc.get('kind')!r}.")
    return doc


def list_stacks(catalog) -> list[dict]:
    """Every stack the catalog publishes, sorted by id."""
    out = []
    for path in sorted(stacks_dir(catalog).glob("*.stack.yaml")):
        doc = _stack_doc(path, STACK_KIND)
        meta = doc.get("metadata") or {}
        if meta.get("id") != path.name[: -len(".stack.yaml")]:
            # The filename is how `--stack` finds it, so a mismatch means `stack-new` would
            # report "unknown stack" for something plainly listed in the catalog.
            raise SystemExit(
                f"{path}: metadata.id {meta.get('id')!r} does not match the filename. "
                f"Rename one of them — the file must be <id>.stack.yaml."
            )
        out.append(doc)
    return out


def load_stack(catalog, stack_id: str) -> dict:
    path = stacks_dir(catalog) / f"{stack_id}.stack.yaml"
    if not path.is_file():
        known = ", ".join((d["metadata"]["id"]) for d in list_stacks(catalog)) or "(none)"
        raise SystemExit(f"unknown stack {stack_id!r}. This catalog publishes: {known}")
    return _stack_doc(path, STACK_KIND)


def load_component(catalog, component_id: str) -> dict:
    path = stacks_dir(catalog) / "components" / component_id / "component.yaml"
    if not path.is_file():
        raise SystemExit(f"stack references component {component_id!r}, but {path} is missing.")
    return _stack_doc(path, STACK_COMPONENT_KIND)


def load_capability(catalog, capability_id: str) -> dict:
    path = stacks_dir(catalog) / "capabilities" / capability_id / "capability.yaml"
    if not path.is_file():
        raise SystemExit(
            f"stack references capability {capability_id!r}, but {path} is missing."
        )
    return _stack_doc(path, STACK_CAPABILITY_KIND)


def _substitute(text: str, tokens: dict[str, str], *, where: str) -> str:
    for token, value in tokens.items():
        text = text.replace(token, value)
    if left := sorted(set(STACK_TOKEN.findall(text))):
        raise SystemExit(
            f"{where}: unresolved template token(s) {left}. Every __TOKEN__ in a stack "
            "template must be supplied by the generator — add it there rather than leaving "
            "the literal text in a generated app repo."
        )
    return text


def _splice(text: str, token: str, block: str, indent: int) -> str:
    """Replace a whole `__TOKEN__` LINE with `block` re-indented, or drop the line.

    Line-oriented rather than inline because the payload is multi-line YAML carrying its own
    comments: keeping it as text (not a parsed structure re-dumped) is what lets a generated
    app repo still explain itself to the developer who opens it.
    """
    line = f"{token}\n"
    if not block.strip():
        return text.replace(line, "")
    return text.replace(line, _indent(block.rstrip("\n"), indent))


def stack_components(catalog, stack: dict) -> list[dict]:
    """Resolve each component entry against the catalog, merging stack overrides on top.

    The stack entry wins over the component default: the same `node-api` component can sit
    in `backend/` in one stack and somewhere else in another.
    """
    resolved = []
    for entry in (stack.get("spec") or {}).get("components") or []:
        if not isinstance(entry, dict) or not entry.get("id"):
            raise SystemExit(f"stack {stack['metadata']['id']}: component entry needs an 'id'.")
        component = load_component(catalog, entry["id"])
        merged = dict(component.get("spec") or {})
        merged.update({k: v for k, v in entry.items() if k != "id"})
        merged["id"] = entry["id"]
        merged["archetype"] = (component.get("metadata") or {}).get("archetype", "")
        merged["runtime"] = (component.get("metadata") or {}).get("runtime", "")
        if not merged.get("dir"):
            raise SystemExit(
                f"stack {stack['metadata']['id']}: component {entry['id']!r} has no 'dir'."
            )
        # component.yaml says `workload: true`; the stack entry replaces that boolean with
        # the actual score metadata.name. A leftover `True` means the entry forgot to.
        if merged.get("workload") is True:
            raise SystemExit(
                f"stack {stack['metadata']['id']}: component {entry['id']!r} is a workload "
                "and needs a 'workload' name (it becomes score metadata.name)."
            )
        resolved.append(merged)
    if not resolved:
        raise SystemExit(f"stack {stack['metadata']['id']}: has no components.")
    return resolved


def _is_workload(component: dict) -> bool:
    # `workload` carries the score metadata.name for workload components and is absent for
    # library ones, where component.yaml sets it to the boolean False.
    return bool(component.get("workload"))


def stack_score_files(components: list[dict]) -> list[str]:
    return [f"{c['dir']}/score.yaml" for c in components if _is_workload(c)]


def stack_generate_steps(components: list[dict]) -> str:
    """The `score-compose generate` lines for a Makefile recipe — one per workload.

    ONE CALL PER SCORE FILE, not one call listing them all, because score-compose refuses
    `--build` when several score files are passed at once ("--build cannot be used when
    multiple score files are provided"). Successive calls accumulate into the same project
    state, so cross-workload resources — notably the shared same-origin nginx — still see
    every workload.

    `--build` is addressed by CONTAINER name, not workload name. Getting that wrong leaves
    `image: .` in the compose file, which only fails later inside docker.
    """
    lines = []
    for c in components:
        if not _is_workload(c):
            continue
        spec = json.dumps({"context": c.get("buildContext") or ".",
                           "dockerfile": f"{c['dir']}/Dockerfile"},
                          separators=(",", ":"))
        lines.append(f"\tscore-compose generate {c['dir']}/score.yaml "
                     f"--build '{c['container']}={spec}'")
    return " && \\\n".join(lines)


def _yaml_scalar(value) -> str:
    """Quote a values entry the way the values file wants it: always a string."""
    return json.dumps(str(value))


def stack_values_text(stack: dict, tokens: dict[str, str]) -> str:
    """`.score-values/values.yaml` as TEXT, so the comments explaining it survive.

    Round-tripping through yaml.safe_dump would produce a valid file that tells the
    developer who opens it nothing at all.
    """
    spec = stack.get("spec") or {}
    values = spec.get("values") or {}
    out = [
        "# Cấu hình theo môi trường của app này.",
        "#",
        "# HAI TẦNG: `application` áp cho mọi môi trường, khối trong `environments` ghi đè",
        "# lên nó. Nền tảng có ĐÚNG HAI môi trường — staging và prod; `production` không",
        "# phải tên khác của `prod`, và một khối đặt sai tên sẽ không áp cho gì cả.",
        "#",
        "# GIÁ TRỊ BÍ MẬT KHÔNG BAO GIỜ ĐẶT Ở ĐÂY. File này nằm trong git. Bí mật khai bằng",
        "# tham chiếu, còn giá trị thật chỉ sống trong Vault:",
        "#",
        "#   STRIPE_KEY:",
        "#     secretRef:",
        "#       name: stripe        # tên secret logic",
        "#       key: api_key        # khoá bên trong nó",
        "#",
        "# Đường dẫn Vault do platform SUY RA từ app + môi trường + tên đó — app không khai",
        "# mount hay path, vì nếu khai được thì app này đọc được secret của app khác.",
        "#",
        "# Mọi giá trị là CHUỖI. YAML đọc yes/no/on/off thành boolean và 1.10 thành 1.1,",
        "# nên hãy để trong ngoặc kép.",
        f"apiVersion: {VALUES_API_VERSION}",
        f"kind: {VALUES_KIND}",
        "",
        "spec:",
    ]

    def block(name: str, entries: dict, indent: int) -> None:
        pad = " " * indent
        out.append(f"{pad}{name}:")
        if not entries:
            out.append(f"{pad}  {{}}")
            return
        for key, value in entries.items():
            if isinstance(value, dict) and "secretRef" in value:
                ref = value["secretRef"]
                out.append(f"{pad}  {key}:")
                out.append(f"{pad}    secretRef:")
                out.append(f"{pad}      name: {ref['name']}")
                out.append(f"{pad}      key: {ref['key']}")
            else:
                rendered = _substitute(str(value), tokens, where=".score-values/values.yaml")
                out.append(f"{pad}  {key}: {_yaml_scalar(rendered)}")

    block("application", values.get("application") or {}, 2)
    environments = values.get("environments") or {}
    if environments:
        out.append("")
        out.append("  environments:")
        for env in ENVIRONMENTS:
            if env in environments:
                block(env, environments[env] or {}, 4)
    return "\n".join(out) + "\n"


def stack_env_example(stack: dict, tokens: dict[str, str], *, app: str) -> str:
    """`.env.example` — the same KEY SET the workloads reference, with local values.

    Generated rather than hand-written for one reason: score-compose's `environment`
    provisioner reads process environment variables, and a MISSING one becomes an empty
    string, not an error. If this file and the values file could drift, `make dev` would
    start containers with silently blank configuration.

    Local is NOT a third environment. It reuses the staging tier, with the stack's
    `localValues` on top — that is where `*.localhost` comes from, so a browser resolves it
    without anyone editing /etc/hosts.
    """
    spec = stack.get("spec") or {}
    values = spec.get("values") or {}
    resolved = dict(values.get("application") or {})
    resolved.update((values.get("environments") or {}).get("staging") or {})
    local = spec.get("localValues") or {}

    out = [
        "# Cấu hình local cho `make dev`. Bản mẫu này ĐƯỢC COMMIT; bản `.env` bạn tạo ra từ",
        "# nó thì KHÔNG (đã nằm trong .gitignore).",
        "#",
        "# Bộ khoá ở đây được SINH RA từ .score-values/values.yaml, nên nó không thể lệch",
        "# khỏi những gì workload thật sự đọc. Đừng thêm khoá bằng tay: thêm vào values file",
        "# rồi chạy lại `stack-upgrade`.",
        "#",
        "# Khoá nào để TRỐNG là bí mật của bên thứ ba do bạn sở hữu. Trên staging/prod chúng",
        "# tới từ Vault và platform không bao giờ đọc giá trị; ở local bạn tự điền.",
        "",
    ]
    for key in sorted(resolved):
        value = resolved[key]
        if isinstance(value, dict) and "secretRef" in value:
            ref = value["secretRef"]
            out.append(f"# bí mật: Vault {vault_path(app, 'staging', ref['name'])}, khoá {ref['key']}")
            out.append(f"{key}=")
        else:
            raw = local.get(key, value)
            out.append(f"{key}={_substitute(str(raw), tokens, where='.env.example')}")
    return "\n".join(out) + "\n"


def stack_instance_text(stack: dict, capabilities: dict[str, dict], *,
                        app: str, owner: str) -> str:
    """`.idp/stack.yaml` — the app repo's record of which stack it was generated from."""
    meta = stack.get("metadata") or {}
    spec = stack.get("spec") or {}
    enabled = spec.get("capabilities") or []
    out = [
        "# Stack và trạng thái onboarding mong muốn của app này.",
        "#",
        "# PHÂN BIỆT VỚI BA FILE DỄ NHẦM:",
        "#   .idp/stack.yaml        <- file này: app được sinh từ stack nào, phiên bản nào",
        "#   .score-values/values.yaml  cấu hình theo môi trường",
        "#   platform.lock              phiên bản CATALOG dùng để render",
        "#   .platform/ (kho cấu hình)  sổ ghi chép của lần deploy",
        "#",
        "# Phiên bản stack và phiên bản catalog được ghim ĐỘC LẬP: nâng catalog không đụng",
        "# file nào trong kho này, còn nâng stack là một pull request có diff.",
        f"apiVersion: {STACK_API_VERSION}",
        f"kind: {STACK_INSTANCE_KIND}",
        "",
        "metadata:",
        f"  application: {app}",
        f"  owner: {owner or 'CHUA-DAT'}" + ("" if owner else "   # <-- điền đội sở hữu"),
        "",
        "spec:",
        "  stack:",
        f"    id: {meta['id']}",
        f"    version: {meta['version']}",
        "",
        "  # Mọi workload trong kho mang cùng một tag = SHA của commit.",
        "  # `content` băm theo THƯ MỤC của từng workload, nên nó KHÔNG thấy thay đổi ở gói",
        "  # dùng chung nằm ngoài các thư mục đó và sẽ deploy lại ảnh cũ mà không báo gì.",
        f"  tagStrategy: {spec.get('tagStrategy', 'commit')}",
    ]
    if enabled:
        out += ["", "  capabilities:"]
        for cap_id in enabled:
            # The shape comes from the capability definition, not from a branch here:
            # catalog = shape, and a second copy of it in the renderer is a second copy to
            # forget to update.
            instance = ((capabilities.get(cap_id) or {}).get("spec") or {}).get("instance") or ""
            out.append(f"    {cap_id}:" if instance.strip() else f"    {cap_id}: {{}}")
            if instance.strip():
                out.append(_indent(instance.rstrip("\n"), 6).rstrip("\n"))
    return "\n".join(out) + "\n"


# App names become a Kubernetes namespace prefix, an image name and an npm scope, so the
# intersection of what all three accept is what we allow — checked once, here, rather than
# discovered as three different errors much later.
APP_NAME = re.compile(r"^[a-z0-9]([a-z0-9-]{0,38}[a-z0-9])?$")


def validate_app_name(app: str) -> str:
    if not APP_NAME.match(app or ""):
        raise SystemExit(
            f"invalid application name {app!r}. Lowercase letters, digits and hyphens; must "
            "start and end with a letter or digit; at most 40 characters. The name becomes a "
            "Kubernetes namespace prefix, a container image name and an npm scope."
        )
    return app


def validate_stack_instance(doc, where: str) -> dict:
    """Check `.idp/stack.yaml` and return its spec."""
    if not isinstance(doc, dict):
        raise SystemExit(f"{where}: expected a YAML mapping.")
    if doc.get("apiVersion") != STACK_API_VERSION:
        raise SystemExit(
            f"{where}: apiVersion must be {STACK_API_VERSION!r}, got {doc.get('apiVersion')!r}."
        )
    if doc.get("kind") != STACK_INSTANCE_KIND:
        raise SystemExit(
            f"{where}: kind must be {STACK_INSTANCE_KIND!r}, got {doc.get('kind')!r}."
        )
    spec = doc.get("spec") or {}
    if not isinstance(spec, dict):
        raise SystemExit(f"{where}: spec must be a mapping.")
    stack = spec.get("stack") or {}
    if not isinstance(stack, dict) or not stack.get("id") or not stack.get("version"):
        raise SystemExit(f"{where}: spec.stack needs both an 'id' and a 'version'.")
    strategy = spec.get("tagStrategy", "commit")
    if strategy not in ("commit", "content"):
        raise SystemExit(
            f"{where}: tagStrategy must be 'commit' or 'content', got {strategy!r}."
        )
    return spec


def load_stack_instance(app_dir) -> dict | None:
    """Validated `.idp/stack.yaml` spec, or None for an app that predates the stack model.

    Unparseable YAML becomes a SystemExit rather than a yaml.YAMLError, so every caller has
    exactly one failure type to reason about. That matters here specifically: the deploy
    path only CONSULTS this file, and a raw parser exception escaping into `render` would
    take down a deploy over a file it never needed.
    """
    path = Path(app_dir) / STACK_REL
    if not path.is_file():
        return None
    try:
        doc = yaml.safe_load(path.read_text())
    except yaml.YAMLError as err:
        raise SystemExit(f"{STACK_REL}: not valid YAML — {err}") from err
    return validate_stack_instance(doc, STACK_REL)


def resolve_tag_strategy(app_dir, cli_value: str | None) -> str:
    """Explicit flag wins, then `.idp/stack.yaml`, then the historical default.

    Order matters for the brownfield promise: every app deployed before the stack model has
    no `.idp/stack.yaml`, so it keeps landing on `content` exactly as it did. An app that
    DOES declare a strategy only gets it once features.stack_onboarding is on — until then
    the declaration is inert, and saying so out loud beats letting a monorepo quietly deploy
    stale images because the flag was still off.
    """
    if cli_value:
        return cli_value
    try:
        instance = load_stack_instance(app_dir) if app_dir else None
    except SystemExit:
        # A malformed stack file must not take down a deploy that never needed it. It is
        # reported properly by `stack-validate`.
        instance = None
    declared = (instance or {}).get("tagStrategy")
    if not declared:
        return "content"
    if not feature("stack_onboarding"):
        warn(f"{STACK_REL} asks for tag_strategy={declared!r}, but features.stack_onboarding "
             "is off, so this render uses 'content'. For a monorepo with a shared workspace "
             "package that means a change to the shared package does NOT retag the workloads "
             "that import it.")
        return "content"
    log(f"tag_strategy={declared} (from {STACK_REL})")
    return declared


def _capability_text(capabilities: dict[str, dict], wanted: list, field: str) -> str:
    return "".join((capabilities[c].get("spec") or {}).get(field) or ""
                   for c in wanted if c in capabilities)


def generate_stack(catalog, stack_id: str, app: str, dest, *, owner: str = "",
                   catalog_ref: str = "", force: bool = False) -> dict:
    """Materialise a stack into an app repository. Returns {created, skipped, stack}.

    NOT destructive by default: a file that already exists is left alone and reported. That
    is what makes re-running safe, which is what the onboarding workflow needs in order to
    retry a half-finished run without producing duplicates.
    """
    validate_app_name(app)
    dest = Path(dest)
    stack = load_stack(catalog, stack_id)
    meta = stack["metadata"]
    spec = stack.get("spec") or {}
    components = stack_components(catalog, stack)
    enabled = list(spec.get("capabilities") or [])
    capabilities = {c: load_capability(catalog, c) for c in enabled}

    for cap_id, cap in capabilities.items():
        need = (cap.get("spec") or {}).get("requiresFeature")
        if need and not feature(need):
            # A warning, not a failure: scaffolding the repo is useful before the platform
            # flag is flipped, and `stack-validate` says the same thing at deploy time.
            warn(f"stack {stack_id} uses capability {cap_id!r}, which needs "
                 f"features.{need}: true. It is currently off, so rendering this app will "
                 "fail until the platform enables it.")

    workspaces = [c["dir"] for c in components if c.get("workspace")]
    tokens_global = {
        "__APP__": app,
        "__OWNER__": owner,
        "__STACK_ID__": meta["id"],
        "__STACK_VERSION__": str(meta["version"]),
        "__SCORE_FILES__": " ".join(stack_score_files(components)),
        "__GENERATE_STEPS__": stack_generate_steps(components),
        "__NODE_IMAGE__": str(CONFIG.require("images.node")),
        "__NGINX_IMAGE__": str(CONFIG.require("images.nginx")),
        "__DOMAIN_STAGING__": str(CONFIG.require("environments.staging.domain")),
        "__DOMAIN_PROD__": str(CONFIG.require("environments.prod.domain")),
        "__WORKSPACE_PKG_COPIES__": "\n".join(
            f"COPY {d}/package.json {d}/" for d in workspaces),
    }

    created: list[str] = []
    skipped: list[str] = []

    def write(rel: str, text: str) -> None:
        path = dest / rel
        if path.exists() and not force:
            skipped.append(rel)
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        created.append(rel)

    # ---- repo-level files shared by every stack
    base = stacks_dir(catalog) / "base" / "files"
    for src in sorted(p for p in base.rglob("*") if p.is_file()):
        rel = str(src.relative_to(base))
        write(rel, _substitute(src.read_text(), tokens_global, where=rel))

    # ---- one pass per component
    for component in components:
        comp_dir = component["dir"]
        wanted = [c for c in (component.get("consumes") or []) if c in enabled]
        tokens = dict(tokens_global)
        tokens.update({
            "__WORKLOAD__": str(component.get("workload") or ""),
            "__CONTAINER__": str(component.get("container") or ""),
            "__PORT__": "" if component.get("port") is None else str(component["port"]),
            "__ROUTE_PATH__": str(component.get("routePath") or ""),
            "__DIR__": comp_dir,
        })
        src_root = stacks_dir(catalog) / "components" / component["id"] / "files"
        if not src_root.is_dir():
            raise SystemExit(f"component {component['id']!r} has no files/ directory.")
        for src in sorted(p for p in src_root.rglob("*") if p.is_file()):
            rel = f"{comp_dir}/{src.relative_to(src_root)}"
            text = src.read_text()
            # Capability YAML is spliced BEFORE token substitution so the spliced text gets
            # its own tokens resolved in the same pass.
            text = _splice(text, "__CAPABILITY_VARIABLES__",
                           _capability_text(capabilities, wanted, "variables"), 6)
            text = _splice(text, "__CAPABILITY_RESOURCES__",
                           _capability_text(capabilities, wanted, "resources"), 2)
            write(rel, _substitute(text, tokens, where=rel))

    # ---- generated files that depend on the component list
    write("package.json", json.dumps({
        "name": app,
        "private": True,
        "version": "1.0.0",
        "workspaces": workspaces,
    }, indent=2, ensure_ascii=False) + "\n")
    write(STACK_REL, stack_instance_text(stack, capabilities, app=app, owner=owner))
    write(VALUES_REL, stack_values_text(stack, tokens_global))
    write(".env.example", stack_env_example(stack, tokens_global, app=app))
    write("platform.lock", _stack_lock_text(catalog_ref or _catalog_ref_default(catalog)))

    # ---- local provisioners, vendored so `make dev` needs no platform checkout
    for src, text in materialise_compose_provisioners(catalog, app=app).items():
        write(f".idp/score-compose/{src}", text)

    log(f"stack {stack_id} v{meta['version']} -> {dest}: "
        f"{len(created)} file(s) written, {len(skipped)} left alone")
    return {"created": created, "skipped": skipped, "stack": stack,
            "components": components, "capabilities": capabilities}


def _catalog_ref_default(catalog) -> str:
    """What a new app should pin in its own platform.lock.

    Read from the catalog's own lock file rather than hardcoded: a company that renames its
    default branch should not have to patch the renderer.
    """
    own = Path(catalog) / "platform.lock"
    if own.is_file():
        for line in own.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                return line
    return "main"


def _stack_lock_text(ref: str) -> str:
    return (
        "# Phiên bản CATALOG mà app này được render bằng — một tag, một nhánh hoặc một SHA\n"
        "# của kho platform. Orchestrator render app bằng ĐÚNG ref ghi ở đây, nên thay đổi\n"
        "# landing trên nhánh chính của catalog KHÔNG ảnh hưởng app đang chạy cho tới khi\n"
        "# chính app nâng dòng này bằng một pull request.\n"
        "#\n"
        "# Khác với .idp/stack.yaml: file kia ghim BỘ FILE app được sinh ra từ đó, file này\n"
        "# ghim CÁCH resource được hiện thực hoá. Hai thứ nâng cấp độc lập nhau.\n"
        f"{ref}\n"
    )


def check_local_postgres_image() -> None:
    """Local Postgres must be the same MAJOR version staging runs. Fail loudly if not.

    Local development cannot use the CloudNativePG operand image: measured on
    `ghcr.io/cloudnative-pg/postgresql:17`, its CMD is `bash` and it runs as uid 26, so a
    plain `docker run` exits 0 immediately with an EMPTY log — the operator drives it, it is
    not a standalone server. So local uses `images.postgres`, which is a different key and
    therefore a place where the two can drift apart.

    A drift here is worth blocking rather than warning about: the whole claim of `make dev`
    is that what runs on a laptop rehearses what runs on the cluster, and "works on 16,
    breaks on 17" is exactly the class of bug it exists to catch.
    """
    image = str(CONFIG.require("images.postgres"))
    engine = str(CONFIG.get("database_profiles.staging.application.engine_version") or "")
    if not engine:
        return
    tag = image.rsplit(":", 1)[-1] if ":" in image.rsplit("/", 1)[-1] else ""
    major = re.match(r"(\d+)", tag)
    if not major:
        warn(f"images.postgres ({image}) has no version in its tag, so it cannot be checked "
             f"against database_profiles.staging.application.engine_version ({engine}). "
             "Local development may run a different PostgreSQL major version than staging.")
        return
    if major.group(1) != engine:
        raise SystemExit(
            f"images.postgres is {image} (major {major.group(1)}) but "
            f"database_profiles.staging.application.engine_version is {engine}. Local "
            "development would run a different PostgreSQL major version than staging, which "
            "defeats the point of generating both from the same Score files. Fix one of the "
            "two in platform.env.yaml."
        )


def materialise_compose_provisioners(catalog, *, app: str) -> dict[str, str]:
    """The local provisioner catalog, %%placeholders%% resolved, keyed by filename.

    Resolved against STAGING deliberately. Local development is not a third environment —
    it is a rehearsal of staging, so it must use the same Postgres major version and the
    same base images. Resolving here (rather than in the app repo) is also what keeps the
    app repo free of platform config: it receives values, never the config file.
    """
    src_dir = Path(catalog) / "templates" / "score-compose"
    if not src_dir.is_dir():
        raise SystemExit(f"no local provisioner catalog at {src_dir}.")
    check_local_postgres_image()
    out = {}
    for src in sorted(src_dir.glob("*.provisioners.yaml")):
        out[src.name] = CONFIG.render(src.read_text(), "staging", where=str(src), app=app)
    if not out:
        raise SystemExit(f"{src_dir} holds no *.provisioners.yaml files.")
    return out
# --------------------------------------------------------------------------------------
# stack commands
# --------------------------------------------------------------------------------------
def cmd_stack_list(args) -> None:
    for doc in list_stacks(args.catalog):
        meta = doc["metadata"]
        spec = doc.get("spec") or {}
        components = stack_components(args.catalog, doc)
        print(f"{meta['id']}  v{meta['version']}")
        print(f"    {meta.get('description', '')}")
        for c in components:
            role = f"{c['archetype']}/{c['runtime']}"
            extra = f" -> {c['dir']}/ ({role})"
            if _is_workload(c):
                path = c.get("routePath")
                extra += f", workload {c['workload']}" + (f", route {path}" if path else "")
            print(f"    - {c['id']}{extra}")
        if caps := spec.get("capabilities"):
            print(f"    capabilities: {', '.join(caps)}")
        print(f"    tagStrategy: {spec.get('tagStrategy', 'commit')}")
        print()


def cmd_stack_new(args) -> None:
    result = generate_stack(args.catalog, args.stack, args.app, args.out,
                            owner=args.owner or "", catalog_ref=args.catalog_ref or "",
                            force=args.force)
    for rel in result["created"]:
        log(f"  + {rel}")
    for rel in result["skipped"]:
        log(f"  = {rel} (đã có -> giữ nguyên; dùng --force để ghi đè)")
    meta = result["stack"]["metadata"]
    print(f"""
==> {args.app}: stack {meta['id']} v{meta['version']} đã được dựng tại {args.out}

Chạy thử ngay trên máy (chỉ cần docker + score-compose, không cần cụm):

    cd {args.out}
    make dev

Trước khi đưa lên staging:

  1. Điền `owner` trong {STACK_REL}.
  2. Xem lại {VALUES_REL} — nhất là PUBLIC_HOST của từng môi trường.
  3. Tạo kho và onboard app: tools/tao-app-moi.sh, rồi `vault-onboard` nếu app dùng secret.
""")


def _managed_globs(stack: dict) -> list[str]:
    return list((stack.get("spec") or {}).get("managedFiles") or [])


def _is_managed(rel: str, globs: list[str]) -> bool:
    return any(fnmatch.fnmatch(rel, pattern) for pattern in globs)


def cmd_stack_validate(args) -> None:
    """Is this app repo still consistent with the stack it claims to be?"""
    app_dir = Path(args.app_dir)
    instance = load_stack_instance(app_dir)
    if instance is None:
        raise SystemExit(
            f"{app_dir}/{STACK_REL} not found. This app was not generated from a stack; "
            "that is allowed and nothing else here applies to it."
        )
    stack_id = instance["stack"]["id"]
    stack = load_stack(args.catalog, stack_id)
    catalog_version = str(stack["metadata"]["version"])
    pinned = str(instance["stack"]["version"])

    problems: list[str] = []
    components = stack_components(args.catalog, stack)
    for component in components:
        if not _is_workload(component):
            continue
        score = app_dir / component["dir"] / "score.yaml"
        if not score.is_file():
            problems.append(f"thiếu {score.relative_to(app_dir)} cho component "
                            f"{component['id']!r}")
            continue
        doc = yaml.safe_load(score.read_text()) or {}
        name = (doc.get("metadata") or {}).get("name")
        if name != component["workload"]:
            problems.append(
                f"{score.relative_to(app_dir)}: metadata.name là {name!r} nhưng stack khai "
                f"workload {component['workload']!r}. Tên workload quyết định tên ảnh và tên "
                "Deployment — lệch là deploy ra một workload thứ hai bên cạnh cái đang chạy."
            )

    for cap_id in (stack.get("spec") or {}).get("capabilities") or []:
        need = ((load_capability(args.catalog, cap_id).get("spec") or {})
                .get("requiresFeature"))
        if need and not feature(need):
            problems.append(f"capability {cap_id!r} cần features.{need}: true, hiện đang tắt")

    if load_application_values(app_dir) is None:
        problems.append(f"thiếu {VALUES_REL}")

    log(f"app {(app_dir / STACK_REL)}: stack {stack_id} v{pinned}")
    if pinned != catalog_version:
        log(f"  nâng cấp có sẵn: v{pinned} -> v{catalog_version} "
            f"(xem `stack-upgrade --app-dir {app_dir}`)")
    else:
        log(f"  đang ở phiên bản mới nhất (v{catalog_version})")
    log(f"  tagStrategy: {instance.get('tagStrategy', 'commit')}")

    if problems:
        raise SystemExit("stack-validate thất bại:\n  - " + "\n  - ".join(problems))
    log("  stack-validate OK")


def cmd_stack_upgrade(args) -> None:
    """Show — and optionally write — what the CURRENT stack version would change.

    Deliberately a diff and not an overwrite. Section 9.4 of the plan: an upgrade is a pull
    request a human reads, because only a human knows whether the local edit that a hunk
    would revert was deliberate.
    """
    app_dir = Path(args.app_dir)
    instance = load_stack_instance(app_dir)
    if instance is None:
        raise SystemExit(f"{app_dir}/{STACK_REL} not found — nothing to upgrade.")
    app = (yaml.safe_load((app_dir / STACK_REL).read_text()).get("metadata") or {}) \
        .get("application") or args.app
    if not app:
        raise SystemExit(f"{STACK_REL}: metadata.application is empty; pass --app.")

    stack_id = instance["stack"]["id"]
    stack = load_stack(args.catalog, stack_id)
    globs = _managed_globs(stack)

    work = Path(args.work or (app_dir / ".idp" / ".stack-upgrade"))
    if work.exists():
        shutil.rmtree(work)
    result = generate_stack(args.catalog, stack_id, app, work,
                            owner=(yaml.safe_load((app_dir / STACK_REL).read_text())
                                   .get("metadata") or {}).get("owner") or "",
                            catalog_ref=_catalog_ref_default(args.catalog), force=True)

    changed = 0
    for rel in result["created"]:
        if not args.all and not _is_managed(rel, globs):
            continue
        fresh = (work / rel).read_text()
        current = (app_dir / rel).read_text() if (app_dir / rel).is_file() else ""
        if fresh == current:
            continue
        changed += 1
        sys.stdout.writelines(difflib.unified_diff(
            current.splitlines(keepends=True), fresh.splitlines(keepends=True),
            fromfile=f"a/{rel}", tofile=f"b/{rel}",
        ))
        if args.write:
            (app_dir / rel).parent.mkdir(parents=True, exist_ok=True)
            (app_dir / rel).write_text(fresh)
    shutil.rmtree(work, ignore_errors=True)

    scope = "mọi file của stack" if args.all else f"file platform sở hữu ({', '.join(globs)})"
    if not changed:
        log(f"không có thay đổi nào trong {scope}: app đã khớp stack "
            f"{stack_id} v{stack['metadata']['version']}")
        return
    if args.write:
        log(f"đã ghi {changed} file vào working tree. Xem `git diff`, rồi mở pull request — "
            "platform KHÔNG tự commit vào kho ứng dụng.")
    else:
        log(f"{changed} file khác biệt trong {scope}. Thêm --write để ghi vào working tree "
            "(vẫn phải tự review và mở pull request), hoặc --all để xem cả mã nguồn.")

