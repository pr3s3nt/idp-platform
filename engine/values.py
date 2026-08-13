"""ApplicationValues validation and environment provisioner generation."""
from __future__ import annotations

from . import context as _context
from . import resources as _resources
for _module in (_context, _resources):
    globals().update({n: getattr(_module, n) for n in dir(_module) if not n.startswith("__")})


# --------------------------------------------------------------------------------------
# ApplicationValues — per-environment configuration
# --------------------------------------------------------------------------------------
# See docs/adr/0001-application-values-v1.md. One file at the root of an app repo carries
# the values that differ between staging and prod; a `type: environment` resource hands
# them to a workload. An app with no such file behaves exactly as it did before.
VALUES_REL = ".score-values/values.yaml"
VALUES_API_VERSION = "idp.company/v1"
VALUES_KIND = "ApplicationValues"

# ${resources.<alias>.<KEY>} — score's own reference syntax.
RESOURCE_REF = re.compile(r"\$\{resources\.([A-Za-z0-9_.-]+)\.([A-Za-z0-9_.-]+)\}")
# Cheap pre-check used to decide whether a string is worth parsing at all.
ANY_RESOURCE_REF = "${resources."


def _values_type_error(key: str, value, where: str) -> SystemExit:
    """YAML's implicit typing is the trap here, so the message has to name it.

    `FEATURE_X: false` is a bool, `PORT: 8080` is an int, and `ENABLED: yes` is ALSO a bool
    — YAML 1.1 treats yes/no/on/off as booleans. All three become environment variables,
    which are strings and nothing else. Silently calling str() on them would work until the
    day someone writes `VERSION: 1.10` and the container sees "1.1".
    """
    return SystemExit(
        f"{where}: value of {key!r} is {type(value).__name__}, not a string. Environment "
        f"variables are strings — quote it: {key}: \"{value}\". (YAML also reads yes, no, "
        "on and off as booleans, so those need quoting too.)"
    )


def _entry_kind(key: str, value, where: str) -> str:
    """Classify one values entry as 'literal' or 'secret', rejecting anything else."""
    if isinstance(value, str):
        return "literal"
    if isinstance(value, dict) and "secretRef" in value:
        extra = set(value) - {"secretRef"}
        if extra:
            raise SystemExit(
                f"{where}: {key!r} mixes secretRef with other fields: {sorted(extra)}. "
                "A value is either a literal string or exactly one secretRef."
            )
        ref = value["secretRef"]
        if not isinstance(ref, dict):
            raise SystemExit(f"{where}: {key!r} has a secretRef that is not a mapping.")
        missing = {"name", "key"} - set(ref)
        unknown = set(ref) - {"name", "key"}
        if missing:
            raise SystemExit(
                f"{where}: secretRef for {key!r} is missing {sorted(missing)}. "
                "It takes exactly two fields: name and key."
            )
        if unknown:
            # Refusing unknown fields is what keeps the Vault path derivable. A tolerated
            # `path:` or `mount:` here would be the app choosing its own prefix, and the
            # per-app policy stops meaning anything.
            raise SystemExit(
                f"{where}: secretRef for {key!r} has unknown field(s) {sorted(unknown)}. "
                "Only name and key are accepted — the Vault mount and path are derived by "
                "the platform and cannot be set by an app."
            )
        validate_secret_name(ref["name"])
        if not isinstance(ref["key"], str) or not ref["key"]:
            raise SystemExit(f"{where}: secretRef.key for {key!r} must be a non-empty string.")
        return "secret"
    if isinstance(value, dict):
        raise SystemExit(
            f"{where}: {key!r} is a mapping but has no secretRef. Nested structures are not "
            "supported — values are flat strings or secret references."
        )
    raise _values_type_error(key, value, where)


def validate_application_values(doc, where: str) -> dict:
    """Check the whole document and return `spec`. Every failure here is a fail-fast."""
    if not isinstance(doc, dict):
        raise SystemExit(f"{where}: expected a YAML mapping.")
    if doc.get("apiVersion") != VALUES_API_VERSION:
        raise SystemExit(
            f"{where}: apiVersion must be {VALUES_API_VERSION!r}, got {doc.get('apiVersion')!r}."
        )
    if doc.get("kind") != VALUES_KIND:
        raise SystemExit(f"{where}: kind must be {VALUES_KIND!r}, got {doc.get('kind')!r}.")
    spec = doc.get("spec") or {}
    if not isinstance(spec, dict):
        raise SystemExit(f"{where}: spec must be a mapping.")
    if unknown := set(spec) - {"application", "environments"}:
        raise SystemExit(f"{where}: unknown field(s) under spec: {sorted(unknown)}.")

    blocks = {"application": spec.get("application") or {}}
    environments = spec.get("environments") or {}
    if not isinstance(environments, dict):
        raise SystemExit(f"{where}: spec.environments must be a mapping.")
    if bad := set(environments) - set(ENVIRONMENTS):
        raise SystemExit(
            f"{where}: unknown environment(s) {sorted(bad)}. This platform has exactly two: "
            f"{', '.join(ENVIRONMENTS)}. ('production' is not an alias for 'prod'; a block "
            "under the wrong name applies to nothing and reports no error.)"
        )
    for env, block in environments.items():
        blocks[f"environments.{env}"] = block or {}

    # A key must be the SAME kind everywhere it appears. A literal in staging and a
    # secretRef in prod renders two different manifest shapes from one Score file, so the
    # thing staging tested is not the thing prod runs.
    kinds: dict[str, tuple[str, str]] = {}
    for block_name, block in blocks.items():
        if not isinstance(block, dict):
            raise SystemExit(f"{where}: spec.{block_name} must be a mapping.")
        for key, value in block.items():
            kind = _entry_kind(key, value, f"{where} (spec.{block_name})")
            if key in kinds and kinds[key][0] != kind:
                first_block, first_kind = kinds[key][1], kinds[key][0]
                raise SystemExit(
                    f"{where}: {key!r} is a {first_kind} in spec.{first_block} but a {kind} "
                    f"in spec.{block_name}. A key must keep the same kind in every "
                    "environment, or staging and prod render different manifest shapes."
                )
            kinds.setdefault(key, (kind, block_name))
    return spec


def load_application_values(app_dir: Path) -> dict | None:
    """Validated spec, or None when the app has no values file (the legacy path)."""
    path = Path(app_dir) / VALUES_REL
    if not path.is_file():
        return None
    doc = yaml.safe_load(path.read_text())
    return validate_application_values(doc, VALUES_REL)


def resolve_application_values(spec: dict, env: str) -> dict:
    """Flatten to {KEY: literal-or-secretRef} for one environment.

    Two tiers, environment wins. Deliberately a flat overwrite rather than a deep merge:
    values are scalars and secret references, and a deep merge of a secretRef onto a
    literal would produce a half-secret nobody wrote.
    """
    validate_environment(env)
    resolved = dict((spec or {}).get("application") or {})
    resolved.update(((spec or {}).get("environments") or {}).get(env) or {})
    return resolved


def environment_alias(score: dict, *, where: str) -> str | None:
    """Which resource alias, if any, is this workload's `type: environment`.

    Deliberately looked up rather than assumed to be `env`. Hardcoding the alias is the
    same class of bug as assuming a container is called `main`: it works for every app that
    copied the example and silently no-ops for the one that did not.
    """
    aliases = [name for name, res in ((score or {}).get("resources") or {}).items()
               if isinstance(res, dict) and res.get("type") == "environment"]
    if len(aliases) > 1:
        raise SystemExit(
            f"{where}: workload declares {len(aliases)} resources of type 'environment' "
            f"({', '.join(sorted(aliases))}). A workload gets at most one — with two, which "
            "one supplies a given key is undefined."
        )
    return aliases[0] if aliases else None


# ------------------------------------------------------------------ placeholder scanning
def _string_leaves(node, path: tuple = ()):
    if isinstance(node, dict):
        for key, value in node.items():
            yield from _string_leaves(value, path + (str(key),))
    elif isinstance(node, list):
        for index, value in enumerate(node):
            yield from _string_leaves(value, path + (index,))
    elif isinstance(node, str):
        yield path, node


def placeholder_position(path: tuple) -> str | None:
    """Which of the four substituting positions is this, or None for 'nowhere valid'.

    An ALLOWLIST, not a list of known-bad fields — see docs/adr/0004. The difference shows
    up on the next score-k8s upgrade: a new field defaults to refused and gets looked at,
    instead of quietly joining the set of places a placeholder is copied through verbatim.
    """
    if len(path) == 4 and path[0] == "containers" and path[2] == "variables":
        return "variables"
    if len(path) == 5 and path[0] == "containers" and path[2] == "files" and path[4] == "content":
        return "file"
    if len(path) == 5 and path[0] == "containers" and path[2] == "volumes" and path[4] == "source":
        return "volume-source"
    if len(path) >= 4 and path[0] == "resources" and path[2] == "params":
        return "params"
    return None


def _fmt_path(path: tuple) -> str:
    return ".".join(str(p) for p in path)


def scan_placeholders(score: dict, *, where: str, hard: bool) -> None:
    """Refuse `${resources.…}` outside the four positions score-k8s actually substitutes.

    The failure this prevents is completely silent. score-k8s copies `command`, `args`,
    `image` and probe fields straight through, so:

        command: ["/app", "--log=${resources.config.LOG_LEVEL}"]

    applies cleanly, the pod starts, and the process parses the literal string
    "${resources.config.LOG_LEVEL}" as its log level. Nothing anywhere reports a problem.

    `hard` is False while features.application_values is off, so an existing app gets a
    warning rather than a failed deploy for something that was already broken before this
    check existed.
    """
    for path, text in _string_leaves(score):
        if ANY_RESOURCE_REF not in text:
            continue
        if placeholder_position(path):
            continue
        message = (
            f"{where}: '{_fmt_path(path)}' contains a ${{resources.…}} reference, but "
            "score-k8s does not substitute there — the literal text would be copied into "
            "the manifest and used as-is, with no error. Placeholders work in "
            "containers.*.variables, container file contents, containers.*.volumes.*.source "
            "and resources.*.params."
        )
        if hard:
            raise SystemExit(message)
        warn(message)


def _effective_file_content(entry: dict) -> str | None:
    """The text score-k8s will substitute into, or None when it substitutes nothing."""
    if not isinstance(entry, dict):
        return None
    # Both are verbatim by contract: binary content has no placeholders to expand, and
    # noExpand is the escape hatch for a file that legitimately contains ${...}.
    if entry.get("noExpand") or "binaryContent" in entry:
        return None
    content = entry.get("content")
    return content if isinstance(content, str) else None


def check_file_secrets(score: dict, resolved: dict, *, where: str) -> None:
    """A file holding a secret must hold the secret and NOTHING else.

    score-k8s already refuses the mixed case, but its message is 'contained a mix of secret
    references and raw content', which does not hint at the actual cause most of the time.
    That cause is almost always one character:

        content: |            <- keeps the trailing newline, so the file is secret + "\\n"
          ${resources.cfg.KEY}

        content: |-           <- strips it; this is the one that works

    Someone hitting that at 2am reads 'mix of secret references and raw content', looks at
    a file containing exactly one reference and nothing else, and concludes the tool is
    broken. So the check runs here, before score-k8s, and names the fix.
    """
    secret_keys = {k for k, v in resolved.items() if isinstance(v, dict) and "secretRef" in v}
    if not secret_keys:
        return
    for container, spec in ((score or {}).get("containers") or {}).items():
        files = (spec or {}).get("files") or {}
        entries = files.items() if isinstance(files, dict) else enumerate(files)
        for target, entry in entries:
            content = _effective_file_content(entry)
            if content is None:
                continue
            refs = RESOURCE_REF.findall(content)
            if not any(key in secret_keys for _, key in refs):
                continue
            if RESOURCE_REF.fullmatch(content.strip()) and content == content.strip():
                continue
            hint = ""
            if RESOURCE_REF.fullmatch(content.strip()):
                # Only whitespace differs, so this is the block-scalar case.
                hint = (" The content is exactly one reference plus surrounding whitespace "
                        "— use `content: |-` (strips the trailing newline) instead of "
                        "`content: |`, or put the reference on one line in quotes.")
            raise SystemExit(
                f"{where}: containers.{container}.files.{target} mixes a secret reference "
                f"with other content. A file fed from a secret is mounted straight from the "
                f"Kubernetes Secret, so its content must be exactly one reference and "
                f"nothing else — otherwise the literal part would have to be written into "
                f"the manifest in git alongside it.{hint}"
            )


def check_referenced_keys(score: dict, alias: str | None, resolved: dict, *,
                          where: str) -> set[str]:
    """Every key a workload asks the environment resource for must exist. Returns them."""
    used: set[str] = set()
    for path, text in _string_leaves(score):
        if ANY_RESOURCE_REF not in text or not placeholder_position(path):
            continue
        for ref_alias, key in RESOURCE_REF.findall(text):
            if alias and ref_alias == alias:
                used.add(key)
    if missing := sorted(used - set(resolved)):
        raise SystemExit(
            f"{where}: references {missing} through '{alias}', but no such key resolves for "
            f"this environment. Add it under spec.application or spec.environments in "
            f"{VALUES_REL}. (An unresolved key would otherwise reach the container as an "
            "empty value, which reads like a config bug in the app.)"
        )
    return used


# ------------------------------------------------------- generated environment provisioner
def _go_template_safe(text: str) -> str:
    """Neutralise `{{` in a value that is about to be embedded in a Go template.

    Provisioner `outputs` is a Go template, so a literal value containing `{{ .Foo }}` —
    entirely plausible in a config string for some other templating system — would be
    evaluated by score-k8s instead of passed through.
    """
    return text.replace("{{", '{{"{{"}}')


# ------------------------------------------------------------------ database capability
# `class: application` is a different provisioner from the `postgres` this platform has
# always had, on purpose. The old one makes a single-replica StatefulSet with a 1Gi volume,
# no HA, no backup, and the password in Score state. It is fine for trying something out
# and catastrophic in production — and nothing about a running deploy tells the two apart.
#
# So the guard is by CLASS, and it only bites once the platform has adopted the new
# capability. With features.postgres_application off, every existing app renders exactly as
# before: the promise this whole programme is built on.
DEV_POSTGRES_CLASSES = ("default", "development")


def check_postgres_class_migration(services: list, state_path: Path, *,
                                   accepted: bool) -> None:
    """Chặn việc đổi `class` của một postgres ĐANG CÓ DỮ LIỆU mà không nói gì.

    Lỗi thật thứ mười lăm. Comment trong provisioner nói đổi từ class cũ sang
    `class: application` "KHÔNG phải sửa code app" — đúng về CONTRACT (cùng bộ output) và
    sai hoàn toàn về DỮ LIỆU. Đo trên harness, từ một app legacy có 4 dòng thật:

      score-k8s định danh resource bằng `<type>.<class>#<workload>.<tên>`, nên đổi class
      là một RESOURCE KHÁC. Nó nhận Guid mới, tên object mới, và state cũ nằm lại trong
      file state mãi mãi (kèm mật khẩu dạng thô của provisioner cũ).

      Kết quả đo được, không có một cảnh báo nào ở bất kỳ đâu:
        PGHOST      pg-api-54f63de0   -> pg-api-be0342e7-rw
        PGDATABASE  db-haKaonqu       -> app_api
        PGUSER      user-IUvGqfQK     -> app_api
      Cluster mới `Ready`, 0 bảng. StatefulSet cũ bị Fleet prune, PVC của nó KHÔNG bị xoá
      theo (PVC sinh từ volumeClaimTemplate không bị thu hồi) — dữ liệu nằm lại trên một
      ổ đĩa không còn ai trỏ tới. App vẫn xanh, vẫn kết nối được, và rỗng.

    Đây là kiểu hỏng tệ nhất trong cả hệ: mọi thứ báo thành công. Nên render DỪNG, và
    người vận hành phải chọn tường minh một trong hai đường ở
    `docs/chuyen-doi-postgres-sang-class-application.md`.
    """
    if not state_path.is_file():
        return
    state = yaml.safe_load(state_path.read_text()) or {}
    existing = set((state.get("resources") or {}).keys())
    if not existing:
        return
    for service in services:
        doc = yaml.safe_load(service.path.read_text()) or {}
        for name, resource in ((doc or {}).get("resources") or {}).items():
            if (resource or {}).get("type") != "postgres":
                continue
            if ((resource or {}).get("class") or "default") != "application":
                continue
            new_uid = f"postgres.application#{service.workload}.{name}"
            if new_uid in existing:
                continue  # đã chuyển xong ở lần render trước — không cằn nhằn nữa
            for old in DEV_POSTGRES_CLASSES:
                old_uid = f"postgres.{old}#{service.workload}.{name}"
                if old_uid not in existing:
                    continue
                old_state = ((state["resources"][old_uid] or {}).get("state") or {})
                if accepted:
                    warn(f"{old_uid} -> {new_uid}: dựng database MỚI và RỖNG theo yêu cầu "
                         f"(--accept-empty-database). Dữ liệu cũ ở lại trên PVC của "
                         f"{old_state.get('service', 'StatefulSet cũ')} và không còn ai "
                         "trỏ tới; tự xoá khi đã chắc.")
                    continue
                raise SystemExit(
                    f"{service.path.name} ({service.workload}): resource {name!r} đang đổi "
                    f"từ `class: {old}` sang `class: application`, nhưng state đã có "
                    f"{old_uid} — tức là có một database CŨ đang chạy với dữ liệu thật.\n"
                    "\n"
                    "Đổi class KHÔNG di chuyển dữ liệu. score-k8s coi đây là một resource "
                    "khác, nên lần render này sẽ dựng một Cluster RỖNG với tên/host/"
                    "database/user khác, còn dữ liệu cũ ở lại trên PVC của "
                    f"{old_state.get('service', '<StatefulSet cũ>')} sau khi Fleet prune "
                    "StatefulSet — và app vẫn báo xanh.\n"
                    "\n"
                    "Chọn một:\n"
                    "  1. Di chuyển dữ liệu bằng CNPG `bootstrap.initdb.import`, rồi render "
                    "lại. Các bước ở docs/chuyen-doi-postgres-sang-class-application.md.\n"
                    "  2. Nếu database này thật sự không có gì đáng giữ: render lại với "
                    "`--accept-empty-database`.\n"
                )


def check_database_classes(scores: list[tuple], env: str) -> None:
    """Refuse the demo-grade postgres in prod, and refuse prod without a backup target."""
    if not feature("postgres_application"):
        return
    application_users = []
    for service, doc in scores:
        for name, resource in ((doc or {}).get("resources") or {}).items():
            if (resource or {}).get("type") != "postgres":
                continue
            klass = (resource or {}).get("class") or "default"
            if klass == "application":
                application_users.append((service.workload, name))
                continue
            if env == "prod" and klass in DEV_POSTGRES_CLASSES:
                raise SystemExit(
                    f"{service.path.name} ({service.workload}): resource {name!r} is "
                    f"`type: postgres` with class {klass!r}, which is the single-replica "
                    "demo database — no HA, no backup, password in render state. It is "
                    "refused in prod. Use `class: application`, which reads its capacity, "
                    "HA and retention from database_profiles in platform.env.yaml."
                )

    # The StatefulSet backend is single-instance with no built-in backup, so a production
    # database on it cannot be made HA or restorable by any config value — the missing
    # piece is an infrastructure backend, not a coordinate. Refuse prod outright rather
    # than deploy something that looks like a production database and is not one. Staging
    # on this backend is fine and unaffected.
    if application_users and env == "prod" and database_backend() == "statefulset":
        raise SystemExit(
            f"workload(s) {[w for w, _ in application_users]} ask for a production "
            "database, but database.backend=statefulset is single-instance with no "
            "backup or failover — it is refused in prod. Use database.backend=cnpg (with "
            "a configured backup object store), or provide a production-grade backend."
        )

    # Fail-closed rather than deploying a production database nobody can restore. The
    # object store is infrastructure, so it is a config value, not a code path.
    if application_users and env == "prod" and not (CONFIG.get("database.backup.object_store_url") or ""):
        raise SystemExit(
            f"workload(s) {[w for w, _ in application_users]} ask for a production "
            "database, but database.backup.object_store_url is empty in platform.env.yaml "
            "— the cluster would run with no backup at all. Configure the object store "
            "(and verify a restore) before rendering prod."
        )

    # Kho object ĐÃ khai mà lịch base backup rỗng là trường hợp nguy hiểm nhất trong cả
    # khối này, vì nó trông giống hệt một cấu hình đầy đủ: `barmanObjectStore` được sinh
    # ra, WAL chảy vào bucket thật, Cluster báo `ContinuousArchiving=True`. Nhưng WAL
    # không có base backup thì phục hồi được ĐÚNG KHÔNG GÌ CẢ — đo trên harness:
    # bootstrap.recovery chết ngay với `no target backup found`. Chặn ở render, vì phát
    # hiện lúc cần phục hồi là quá muộn theo đúng nghĩa đen.
    if application_users and (CONFIG.get("database.backup.object_store_url") or "") \
            and not (CONFIG.for_env(env).get("computed.database.backup.schedule") or ""):
        raise SystemExit(
            f"workload(s) {[w for w, _ in application_users]} có kho object cấu hình sẵn "
            "nhưng lịch base backup rỗng (database.backup.schedule, hoặc "
            f"database_profiles.{env}.application.backup.schedule). Chỉ có "
            "barmanObjectStore thì chỉ WAL được lưu, và WAL không có base backup thì "
            "KHÔNG phục hồi được gì — cụm vẫn báo 'Continuous archiving is working'. "
            "Khai lịch dạng cron SÁU trường của CNPG, ví dụ \"0 0 2 * * *\"."
        )


# ------------------------------------------------------------------- app secret bindings
# One VaultStaticSecret per (workload, logical secret). Grouping by workload rather than
# per app is the least-privilege choice: the destination Secret is mounted into that
# workload's pods, so a worker that needs only the queue password never has a Secret
# containing the payment key sitting in its namespace next to it.
#
# Grouping by LOGICAL SECRET rather than per key is what makes rotation atomic. One Vault
# secret can hold `api_key` and `webhook_secret`; two CRs reading the same path would sync
# independently, and there is a window where the app is running the new key with the old
# webhook secret.
def secret_bindings(app: str, env: str, resolved: dict,
                    used_by_workload: dict[str, set[str]]) -> list[dict]:
    """Which VaultStaticSecret each workload needs, derived from what it actually uses.

    Deterministic in every respect — sorted, and named from a hash of a stable tuple — so
    two renders of one input produce byte-identical manifests. Anything else shows up as
    Fleet churn and, for a secret, as a pod restart nobody asked for.
    """
    groups: dict[tuple[str, str], dict] = {}
    for workload in sorted(used_by_workload):
        for key in sorted(used_by_workload[workload]):
            value = resolved.get(key)
            if not (isinstance(value, dict) and "secretRef" in value):
                continue
            ref = value["secretRef"]
            name, vault_key = ref["name"], ref["key"]
            group = groups.setdefault((workload, name), {
                "workload": workload,
                "secret": name,
                "path": vault_relative_path(app, env, name),
                "destination": resource_name(app, env, workload, name),
                "keys": {},
            })
            # Two output keys may legitimately map to the same Vault key; both are kept.
            group["keys"][key] = vault_key
    return [groups[k] for k in sorted(groups)]


def vault_static_secret_doc(binding: dict, *, app: str, env: str) -> dict:
    """The CR that makes VSO pull one logical secret into one workload's namespace.

    `includes` narrows the destination Secret to the keys this workload asked for. Vault
    secrets accumulate keys over time — someone adds `admin_token` next to `api_key` — and
    without the filter that new key lands in the workload's Secret automatically.

    `excludeRaw` is what makes that filter mean anything. Measured on VSO 1.5.0: by default
    the destination Secret also gets a `_raw` key holding the ENTIRE Vault secret as JSON,
    so `includes` filters the named keys while `_raw` hands over every one of them anyway.
    """
    wanted = sorted(set(binding["keys"].values()))
    return {
        "apiVersion": VAULT_API,
        "kind": "VaultStaticSecret",
        "metadata": {
            "name": binding["destination"],
            "annotations": {"idp.platform/logical-secret": binding["secret"],
                            "idp.platform/vault-path": binding["path"]},
            "labels": _vault_labels(**{"idp.platform/application": app,
                                       "idp.platform/environment": env,
                                       "idp.platform/workload": binding["workload"]}),
        },
        "spec": {
            # VaultAuth in this app's own namespace — never the VaultAuthGlobal, which
            # would authenticate as an identity shared with every other namespace.
            "vaultAuthRef": _vault_str("auth_ref") or "app-vault",
            "mount": _vault_str("kv_mount") or "kv",
            "type": _vault_str("kv_type") or "kv-v2",
            "path": binding["path"],
            "refreshAfter": _vault_str("refresh_after") or "5m",
            # Explicit even though the CRD defaults it to true: with hmacSecretData false,
            # VSO cannot tell a real rotation from a re-read, so it either ignores
            # rolloutRestartTargets or restarts on every sync. Both failures are quiet.
            "hmacSecretData": True,
            "destination": {
                "name": binding["destination"],
                "create": True,
                "transformation": {
                    "includes": [f"^{re.escape(k)}$" for k in wanted],
                    "excludeRaw": True,
                },
            },
            # score-k8s names the Deployment after the workload.
            "rolloutRestartTargets": [{"kind": "Deployment", "name": binding["workload"]}],
        },
    }


# ------------------------------------------------------- generated environment provisioner
def _go_template_safe(text: str) -> str:
    """Neutralise `{{` in a value that is about to be embedded in a Go template.

    Provisioner `outputs` is a Go template, so a literal value containing `{{ .Foo }}` —
    entirely plausible in a config string for some other templating system — would be
    evaluated by score-k8s instead of passed through.
    """
    return text.replace("{{", '{{"{{"}}')


def _indent(text: str, spaces: int) -> str:
    pad = " " * spaces
    return "".join(f"{pad}{line}\n" if line.strip() else "\n" for line in text.splitlines())


def write_environment_provisioner(resolved: dict, dest: Path, *, app: str, env: str,
                                  used_by_workload: dict[str, set[str]] | None = None) -> Path:
    """Materialise a provisioner for `type: environment` carrying this app's values.

    Generated per render rather than shipped in the catalog because the values ARE the
    app's, and the catalog is shared and version-pinned. It lands in the work directory
    next to the resolved catalog, so a failed render leaves behind exactly the files
    score-k8s was handed.

    Literals are emitted for every workload; secrets are emitted per workload, because a
    `secretRef` resolves to a reference to a DIFFERENT Kubernetes Secret depending on which
    workload is asking. Hence the `{{ if eq .SourceWorkload }}` guards: one provisioner
    file, one branch per consumer.
    """
    used_by_workload = used_by_workload or {}
    literals, secret_keys = {}, []
    for key, value in sorted(resolved.items()):
        if isinstance(value, dict) and "secretRef" in value:
            if not feature("vault_secrets"):
                raise SystemExit(
                    f"{VALUES_REL}: {key!r} is a secretRef, but features.vault_secrets is "
                    "off for this platform. Enable it (and install the Vault Secrets "
                    "Operator) or use a literal value."
                )
            secret_keys.append(key)
            continue
        literals[key] = value

    bindings = secret_bindings(app, env, resolved, used_by_workload) if secret_keys else []
    if secret_keys and not bindings:
        # The key exists and resolves, but nothing consumes it. Emitting a VaultStaticSecret
        # anyway would pull a real secret into the cluster for no reader.
        warn(f"{VALUES_REL}: secret value(s) {secret_keys} are not referenced by any "
             "workload — no VaultStaticSecret generated for them.")

    # A secret-only environment must not start with ``{}``.  Appending secret reference
    # fields after that flow-style empty mapping produces two top-level YAML values; the
    # provisioner decoder keeps the empty map, so Score reports every secret key missing.
    # Emit an empty prefix when bindings will supply the mapping entries.  Keep ``{}`` for
    # the genuinely empty/unused case so the provisioner still has a valid output object.
    body = (yaml.safe_dump(literals, sort_keys=True, default_flow_style=False,
                           allow_unicode=True) if literals else
            ("" if bindings else "{}\n"))
    outputs = _indent(_go_template_safe(body), 4)
    manifests = ""
    for workload in sorted({b["workload"] for b in bindings}):
        mine = [b for b in bindings if b["workload"] == workload]
        refs = "".join(
            f'{key}: {{{{ encodeSecretRef "{b["destination"]}" "{vault_key}" }}}}\n'
            for b in mine for key, vault_key in sorted(b["keys"].items()))
        outputs += f'    {{{{ if eq .SourceWorkload "{workload}" }}}}\n'
        outputs += _indent(refs, 4)
        outputs += "    {{ end }}\n"

        docs = "".join(yaml.safe_dump([vault_static_secret_doc(b, app=app, env=env)],
                                      sort_keys=False, default_flow_style=False)
                       for b in mine)
        manifests += f'    {{{{ if eq .SourceWorkload "{workload}" }}}}\n'
        manifests += _indent(docs, 4)
        manifests += "    {{ end }}\n"

    doc = (
        f"# GENERATED by idpctl for {app}/{env} — do not edit, do not commit.\n"
        f"# Source: {VALUES_REL}. Literal values only — a secretRef becomes a reference to\n"
        "# a Secret that Vault Secrets Operator fills at runtime, never a value.\n"
        "- uri: template://platform/environment\n"
        "  type: environment\n"
        f"  description: ApplicationValues for {app} in {env}\n"
        "  outputs: |\n" + outputs
        + ("  manifests: |\n" + manifests if manifests else "")
    )
    dest.write_text(doc)
    log(f"generated environment provisioner with {len(literals)} value(s) and "
        f"{len(bindings)} vault secret binding(s) -> {dest}")
    return dest
