"""Kubernetes, Vault, database, secret and capability operations."""
from __future__ import annotations

from . import context as _context
globals().update({n: getattr(_context, n) for n in dir(_context) if not n.startswith("__")})
# The objects below are what must exist BEFORE any app can reference a secret: how the
# Vault Secrets Operator reaches Vault (VaultConnection), how it authenticates
# (VaultAuthGlobal plus a VaultAuth in each app namespace), and what one app/environment
# is allowed to read (a Vault policy and a Kubernetes auth role).
#
# Two rules shape all of it:
#
# 1. NOTHING HERE CARRIES A SECRET VALUE. These are coordinates and permissions. The value
#    only ever travels Vault -> VSO -> a runtime Secret; the platform never sees it, so it
#    cannot leak it into Git, a log or the render state.
# 2. EVERY COORDINATE COMES FROM platform.env.yaml. Vault address, mount, auth path, role
#    and policy naming all differ between installs. A default baked in here is a deploy
#    that authenticates against the wrong Vault while reporting success.
#
# Naming: a Kubernetes object name must be a DNS label, but Vault policy and role names
# accept more, and a company with an existing convention ("platform_payment-api_staging")
# must be able to keep it. So the two are validated against different alphabets rather
# than forcing Vault to look like Kubernetes.
VAULT_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")

VAULT_API = "secrets.hashicorp.com/v1beta1"


def _vault_str(key: str, default: str = "") -> str:
    value = CONFIG.get(f"vault.{key}", default)
    return "" if value is None else str(value).strip()


def _vault_derive(key: str, default: str, app: str, env: str, *, dns: bool) -> str:
    """Expand a `vault.*_template` for one app/environment.

    Kept template-driven rather than hardcoded because role and policy names are the
    boundary between this platform and a Vault someone else administers: they may already
    have a naming standard, and renaming a role after the fact means every app's auth
    breaks at once.
    """
    validate_secret_name(app)
    validate_environment(env)
    template = _vault_str(key, default) or default
    name = template.replace("{application}", app).replace("{environment}", env)
    if "{" in name or "}" in name:
        raise SystemExit(
            f"vault.{key} has an unknown placeholder: {template!r}. "
            "Only {application} and {environment} are substituted."
        )
    pattern = DNS_LABEL if dns else VAULT_NAME
    if not pattern.match(name):
        raise SystemExit(
            f"vault.{key} produced {name!r}, which is not a valid "
            f"{'Kubernetes object name' if dns else 'Vault role/policy name'}."
        )
    return name


def vault_role_name(app: str, env: str) -> str:
    return _vault_derive("auth_role_template", "idp-{application}-{environment}",
                         app, env, dns=False)


def vault_policy_name(app: str, env: str, *, write: bool = False) -> str:
    base = _vault_derive("policy_template", "idp-{application}-{environment}",
                         app, env, dns=False)
    return f"{base}-{'write' if write else 'read'}"


def vault_service_account(app: str, env: str) -> str:
    """The ServiceAccount in the app namespace that VSO presents to Vault.

    Deliberately NOT `default`: the Vault role is bound to (namespace, serviceAccount), and
    binding it to `default` would let every pod in that namespace mint a token that reads
    the app's secrets, whether or not it is part of the app.
    """
    return _vault_derive("service_account_template", "idp-{application}",
                         app, env, dns=True)


def vault_policy_prefix(app: str, env: str) -> str:
    """The KV prefix, inside the mount, that this app/environment owns. Ends with '/'.

    This is the single load-bearing string of the whole secret feature: the policy granted
    to an app is a prefix policy, so if the prefix does not pin BOTH the application and
    the environment, one app can read another's secrets — or staging credentials read
    production's — and nothing anywhere reports an error.
    """
    template = _vault_str("path_template") or "apps/{application}/{environment}/{name}"
    missing = [p for p in ("{application}", "{environment}") if p not in template]
    if missing:
        raise SystemExit(
            f"vault.path_template must contain {' and '.join(missing)}: "
            f"got {template!r}. Without it every app shares one prefix, and the per-app "
            "policy generated from it would grant read access to every other app."
        )
    if not template.endswith("{name}"):
        raise SystemExit(
            f"vault.path_template must end with {{name}}: got {template!r}. The per-app "
            "policy is a prefix policy, so the app-supplied segment has to be last — "
            "otherwise the wildcard would have to span a segment the platform derives."
        )
    validate_secret_name(app)
    validate_environment(env)
    body = (template[: -len("{name}")]
            .replace("{application}", app)
            .replace("{environment}", env))
    mount = _vault_str("kv_mount") or "kv"
    return f"{mount}/{body}"


def vault_policy(app: str, env: str, *, write: bool = False) -> str:
    """Vault policy HCL scoped to exactly one app/environment prefix.

    kv-v2 splits one logical path into two real ones — `<mount>/data/<path>` for the value
    and `<mount>/metadata/<path>` for versions — and a policy that only covers `data` makes
    `vault kv list`/`get` fail in a way that reads like the secret is missing. kv-v1 has
    neither infix. Getting this wrong surfaces as "permission denied", which sends whoever
    is debugging to look at the role, not at the mount type.
    """
    prefix = vault_policy_prefix(app, env)
    mount = _vault_str("kv_mount") or "kv"
    rest = prefix[len(mount) + 1:]
    kv_type = (_vault_str("kv_type") or "kv-v2").lower()
    if kv_type not in ("kv-v1", "kv-v2"):
        raise SystemExit(
            f"vault.kv_type must be kv-v1 or kv-v2, got {kv_type!r}. VSO reads the two "
            "through different paths, so a guess here fails as 'permission denied'."
        )
    data_caps = '["create", "update", "read"]' if write else '["read"]'
    header = (
        f"# GENERATED by idpctl — Vault policy for {app}/{env} "
        f"({'write' if write else 'read'}).\n"
        f"# Scope: {prefix}* — one application, one environment, nothing else.\n"
    )
    if kv_type == "kv-v2":
        return header + (
            f'path "{mount}/data/{rest}*" {{\n'
            f"  capabilities = {data_caps}\n"
            "}\n\n"
            f'path "{mount}/metadata/{rest}*" {{\n'
            '  capabilities = ["read", "list"]\n'
            "}\n"
        )
    return header + (
        f'path "{prefix}*" {{\n'
        f"  capabilities = {'[\"create\", \"update\", \"read\", \"list\"]' if write else '[\"read\", \"list\"]'}\n"
        "}\n"
    )


def _vault_labels(**extra: str) -> dict:
    # Not app.kubernetes.io/managed-by: render strips that label so Fleet/Helm can own the
    # objects it produces, and these are applied directly by an operator instead.
    labels = {"app.kubernetes.io/part-of": "idp-platform"}
    labels.update({k: v for k, v in extra.items() if v})
    return labels


def vault_connection_manifest() -> dict:
    """How VSO reaches Vault. One per cluster, in the operator's namespace."""
    address = _vault_str("address")
    if not address:
        raise SystemExit(
            "vault.address is empty in platform.env.yaml. It has no default because it is "
            "the address the CLUSTER uses to reach Vault — an in-cluster Service on one "
            "install, a company endpoint on the next. Set it before generating the "
            "Vault foundation."
        )
    spec: dict = {"address": address,
                  "skipTLSVerify": bool(CONFIG.get("vault.skip_tls_verify", False))}
    if _vault_str("ca_cert_secret"):
        spec["caCertSecretRef"] = _vault_str("ca_cert_secret")
    if _vault_str("tls_server_name"):
        spec["tlsServerName"] = _vault_str("tls_server_name")
    return {
        "apiVersion": VAULT_API,
        "kind": "VaultConnection",
        "metadata": {
            "name": _vault_str("connection_name") or "default",
            "namespace": _vault_str("operator_namespace") or "vault-secrets-operator-system",
            "labels": _vault_labels(),
        },
        "spec": spec,
    }


def vault_auth_global_manifest() -> dict:
    """Shared auth defaults every app namespace inherits.

    Only what is genuinely global lives here — connection, method, mount, Vault namespace.
    Role and ServiceAccount are per-app and stay in the per-namespace VaultAuth; putting
    them here would hand every namespace one shared identity and undo the prefix policy.
    """
    # Namespace-QUALIFIED on purpose. An unqualified ref is resolved against the namespace
    # of the resource doing the referring — the app's namespace, not this one — so a bare
    # "default" sends VSO looking for a VaultConnection in every app namespace and every
    # VaultAuth fails with `VaultConnection "default" not found`. Measured on VSO 1.5.0.
    spec: dict = {
        "vaultConnectionRef": f"{_vault_str('operator_namespace') or 'vault-secrets-operator-system'}"
                              f"/{_vault_str('connection_name') or 'default'}",
        "defaultAuthMethod": "kubernetes",
        "defaultMount": _vault_str("auth_mount") or "kubernetes",
    }
    if _vault_str("namespace"):
        spec["defaultVaultNamespace"] = _vault_str("namespace")
    audience = _vault_str("auth_audience")
    if audience:
        spec["kubernetes"] = {"audiences": [audience]}
    allowed = CONFIG.get("vault.allowed_namespaces") or []
    if allowed:
        spec["allowedNamespaces"] = [str(ns) for ns in allowed]
    return {
        "apiVersion": VAULT_API,
        "kind": "VaultAuthGlobal",
        "metadata": {
            "name": _vault_str("auth_global_name") or "default",
            "namespace": _vault_str("operator_namespace") or "vault-secrets-operator-system",
            "labels": _vault_labels(),
        },
        "spec": spec,
    }


def vault_foundation_manifests() -> list[dict]:
    return [vault_connection_manifest(), vault_auth_global_manifest()]


def vault_auth_manifests(app: str, env: str) -> list[dict]:
    """The per-namespace half: a dedicated ServiceAccount and the VaultAuth that uses it.

    Every VaultStaticSecret for this app points at `vault.auth_ref` in its own namespace —
    never at the VaultAuthGlobal directly, because a VaultStaticSecret that references the
    global bypasses the per-namespace identity and authenticates as whatever the global
    happens to name.
    """
    namespace = app_namespace(app, env)
    sa = vault_service_account(app, env)
    spec: dict = {
        "method": "kubernetes",
        "mount": _vault_str("auth_mount") or "kubernetes",
        "vaultAuthGlobalRef": {
            "name": _vault_str("auth_global_name") or "default",
            "namespace": _vault_str("operator_namespace") or "vault-secrets-operator-system",
        },
        "kubernetes": {"role": vault_role_name(app, env), "serviceAccount": sa},
    }
    if _vault_str("namespace"):
        spec["namespace"] = _vault_str("namespace")
    audience = _vault_str("auth_audience")
    if audience:
        spec["kubernetes"]["audiences"] = [audience]
    labels = _vault_labels(**{"idp.platform/application": app, "idp.platform/environment": env})
    return [
        {"apiVersion": "v1", "kind": "ServiceAccount",
         "metadata": {"name": sa, "namespace": namespace, "labels": labels}},
        {"apiVersion": VAULT_API, "kind": "VaultAuth",
         "metadata": {"name": _vault_str("auth_ref") or "app-vault",
                      "namespace": namespace, "labels": labels},
         "spec": spec},
    ]


# --------------------------------------------------------------------- verify-only access
# The identity that answers "did the deploy actually come up?" is NOT the identity that
# performed the deploy. It gets read access to the objects whose status tells the story —
# and no access to Secrets at all.
#
# There is no half-measure available: Kubernetes RBAC has no verb that reveals a Secret's
# name and keys while hiding its values, so `get secrets` is `get secret values`. Since
# verification only ever needs to know that VSO reported Ready, the answer is to not grant
# it. Whoever runs verify with a broader kubeconfig gets the broader access — this
# generates the narrow one so that is a choice, not an accident.
VERIFY_RULES = [
    {"apiGroups": ["secrets.hashicorp.com"],
     "resources": ["vaultauths", "vaultstaticsecrets", "vaultdynamicsecrets"],
     "verbs": ["get", "list", "watch"]},
    {"apiGroups": ["apps"], "resources": ["deployments", "statefulsets", "replicasets"],
     "verbs": ["get", "list", "watch"]},
    {"apiGroups": [""], "resources": ["pods", "pods/log", "services", "events"],
     "verbs": ["get", "list", "watch"]},
    {"apiGroups": [""], "resources": ["persistentvolumeclaims"], "verbs": ["get", "list", "watch"]},
    {"apiGroups": ["batch"], "resources": ["jobs"], "verbs": ["get", "list", "watch"]},
    {"apiGroups": ["gateway.networking.k8s.io"], "resources": ["httproutes"],
     "verbs": ["get", "list", "watch"]},
]


def verify_rbac_manifests(app: str, env: str) -> list[dict]:
    namespace = app_namespace(app, env)
    name = resource_name(app, env, "verify")
    labels = _vault_labels(**{"idp.platform/application": app, "idp.platform/environment": env})
    meta = {"name": name, "namespace": namespace, "labels": labels}
    return [
        {"apiVersion": "v1", "kind": "ServiceAccount", "metadata": dict(meta)},
        {"apiVersion": "rbac.authorization.k8s.io/v1", "kind": "Role",
         "metadata": dict(meta), "rules": [dict(r) for r in VERIFY_RULES]},
        {"apiVersion": "rbac.authorization.k8s.io/v1", "kind": "RoleBinding",
         "metadata": dict(meta),
         "roleRef": {"apiGroup": "rbac.authorization.k8s.io", "kind": "Role", "name": name},
         "subjects": [{"kind": "ServiceAccount", "name": name, "namespace": namespace}]},
    ]




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
    """Tạo namespace nếu chưa có — nhưng HỎI trước khi tạo.

    Gọi thẳng `create` rồi tha lỗi "đã tồn tại" chỉ đúng khi mình có quyền tạo. Một đội
    được cấp sẵn vài namespace và KHÔNG có quyền create thì Kubernetes trả Forbidden chứ
    không phải AlreadyExists — vì nó kiểm quyền trước khi kiểm tồn tại. Khi đó
    _tolerate_exists giết cả lần deploy dù namespace đã nằm sẵn đó.

    Hỏi trước thì trường hợp phổ biến nhất ở công ty — namespace tạo sẵn, quyền tạo không
    có — chạy bình thường, mà vẫn giữ nguyên tính chất "thiếu quyền thật thì hỏng ồn ào".
    """
    cp = kubectl(["get", "namespace", ns, "-o", "name"],
                 kubeconfig=kubeconfig, check=False, capture=True)
    if cp.returncode == 0:
        log(f"namespace {ns} đã có -> không tạo")
        return
    _tolerate_exists(
        kubectl(["create", "namespace", ns], kubeconfig=kubeconfig, check=False, capture=True),
        f"namespace {ns}",
    )


def ensure_backup_credentials(ns: str, args) -> None:
    """Credential kho object cho CNPG, TRONG namespace của app.

    Lỗi thật thứ mười bảy. Provisioner sinh `barmanObjectStore` tham chiếu Secret
    `database.backup.credentials_secret`, nhưng KHÔNG có ai tạo Secret đó trong namespace
    mà onboarding vừa dựng — và **CNPG không đọc chéo namespace**. Kết quả đo được trên
    cụm: `ScheduledBackup` chạy ngay như thiết kế, nhưng Backup đứng ở
    `walArchivingFailing`, `firstRecoverabilityPoint` không bao giờ xuất hiện, và database
    thì vẫn `Ready` và vẫn phục vụ bình thường.

    Trước Phase 7 việc này trôi qua trong im lặng — không ai kiểm base backup. Nay `verify`
    chặn đúng chỗ, nên gốc rễ phải được sửa chứ không phải nới cái chặn.

    Cùng kỷ luật với registry-pull: KHÔNG tạo Secret rỗng. Một credential sai còn tệ hơn
    không có, vì `create-if-missing` khiến nó sống mãi.
    """
    name = str(CONFIG.get("database.backup.credentials_secret") or "")
    if not name or not (CONFIG.get("database.backup.object_store_url") or ""):
        return
    cp = kubectl(["get", "secret", name, "-n", ns, "-o", "name"],
                 kubeconfig=args.kubeconfig, check=False, capture=True)
    if cp.returncode == 0:
        log(f"secret backup {name} trong {ns} đã có -> giữ nguyên")
        return
    key_id = getattr(args, "backup_key_id", None) or os.environ.get("BACKUP_ACCESS_KEY_ID")
    secret_key = (getattr(args, "backup_secret_key", None)
                  or os.environ.get("BACKUP_ACCESS_SECRET_KEY"))
    if not key_id or not secret_key:
        missing = [n for n, v in (("BACKUP_ACCESS_KEY_ID", key_id),
                                  ("BACKUP_ACCESS_SECRET_KEY", secret_key)) if not v]
        warn(f"KHÔNG tạo secret backup {name} trong {ns}: thiếu {', '.join(missing)}. "
             "WAL archiving sẽ hỏng và sẽ KHÔNG có base backup nào — database vẫn Ready, "
             "vẫn phục vụ, và không phục hồi được. `verify` sẽ dừng ở đúng chỗ đó.")
        return
    _tolerate_exists(
        kubectl(["create", "secret", "generic", name, "-n", ns,
                 f"--from-literal=ACCESS_KEY_ID={key_id}",
                 f"--from-literal=ACCESS_SECRET_KEY={secret_key}"],
                kubeconfig=args.kubeconfig, check=False, capture=True,
                sensitive=(key_id, secret_key)),
        f"secret backup {name} in {ns}",
    )


def cmd_apply_secrets(args) -> None:
    ns = app_namespace(args.app, args.env)
    ensure_namespace(ns, args.kubeconfig)

    if args.harbor_host and args.harbor_user and args.harbor_pass:
        _tolerate_exists(
            kubectl(
                ["create", "secret", "docker-registry", pull_secret(), "-n", ns,
                 f"--docker-server={args.harbor_host}",
                 f"--docker-username={args.harbor_user}",
                 f"--docker-password={args.harbor_pass}"],
                kubeconfig=args.kubeconfig, check=False, capture=True,
                sensitive=(args.harbor_pass,),
            ),
            f"{pull_secret()} in {ns}",
        )
    elif not args.harbor_host:
        warn(f"no --harbor-host given: skipping {pull_secret()} in {ns}")
    else:
        # KHÔNG tạo một Secret rỗng. Thiếu user/pass mà vẫn `create secret docker-registry`
        # thì Python nội suy `None` thành CHUỖI "None", và cụm nhận một credential trông
        # như đã cấu hình đầy đủ — `kubectl get secret` thấy `registry-pull` tồn tại, kiểu
        # đúng, dữ liệu có. Ảnh thì không kéo được, và thông báo lỗi là
        # `403 Forbidden` từ registry, không một chữ nào nhắc tới biến môi trường còn
        # thiếu. Cộng thêm việc đây là create-if-missing, cái Secret hỏng đó SỐNG MÃI cho
        # tới khi có người xoá tay — đúng cái bẫy đã trả giá để biết.
        #
        # Nên: không tạo gì cả, và nói thẳng cái gì thiếu. Không có Secret thì kubelet báo
        # `FailedToRetrieveImagePullSecret`, một thông báo TRUNG THỰC.
        missing = [n for n, v in (("REGISTRY_USER", args.harbor_user),
                                  ("REGISTRY_PASS", args.harbor_pass)) if not v]
        warn(f"KHÔNG tạo {pull_secret()} trong {ns}: thiếu {', '.join(missing)}. "
             f"Ảnh từ {args.harbor_host} sẽ không kéo được nếu registry là private — và "
             "lỗi khi đó là 403 từ registry, không nhắc gì tới biến này. Đặt biến rồi "
             f"chạy lại, hoặc tạo tay: kubectl -n {ns} create secret docker-registry "
             f"{pull_secret()} --docker-server={args.harbor_host} "
             "--docker-username=<user> --docker-password=<token>")

    ensure_backup_credentials(ns, args)

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

# --------------------------------------------------------------------------------------
# preflight
# --------------------------------------------------------------------------------------
def vault_secret_status(doc: dict, ns: str, args) -> tuple[bool, str]:
    """(synced?, một dòng chẩn đoán) cho một VaultStaticSecret.

    Chẩn đoán KHÔNG BAO GIỜ chứa giá trị bí mật — chỉ toạ độ và lý do: app, môi trường,
    workload, tên secret logic, đường dẫn Vault suy ra, condition và reason của VSO. Đó
    đúng là bộ thông tin cần để biết phải sửa ở đâu (policy Vault? sai path? chưa ghi
    secret?), và không có gì trong đó lộ ra thứ đang được bảo vệ.
    """
    name = doc["metadata"]["name"]
    meta = doc["metadata"].get("annotations") or {}
    labels = doc["metadata"].get("labels") or {}
    where = (f"{labels.get('idp.platform/application', args.app)}/"
             f"{labels.get('idp.platform/environment', args.env)}"
             f"[{labels.get('idp.platform/workload', '?')}]"
             f" secret={meta.get('idp.platform/logical-secret', '?')}"
             f" path={_vault_str('kv_mount') or 'kv'}/{meta.get('idp.platform/vault-path', '?')}")

    cp = kubectl(["get", "vaultstaticsecret", name, "-n", ns, "-o", "json"],
                 kubeconfig=args.kubeconfig, check=False, capture=True)
    if cp.returncode != 0:
        return False, f"{where}: VaultStaticSecret {name} chưa có trên cụm (Fleet đã đồng bộ chưa?)"
    obj = json.loads(cp.stdout or "{}")
    conditions = (obj.get("status") or {}).get("conditions") or []
    if not conditions:
        return False, f"{where}: VSO chưa xử lý {name} (chưa có condition nào)"
    cond = conditions[0]
    if cond.get("status") == "True" and cond.get("reason") in ("Accepted", "SecretSynced", "Synced"):
        return True, ""
    return False, (f"{where}: chưa đồng bộ — reason={cond.get('reason')} "
                   f"message={' '.join(str(cond.get('message', '')).split())[:200]}")


def wait_for_vault_secrets(docs: list[dict], ns: str, args) -> None:
    """Chờ mọi VaultStaticSecret vừa render báo đã đồng bộ, trong SLO đã cấu hình.

    `CreateContainerConfigError` xuất hiện thoáng qua là BÌNH THƯỜNG: Fleet apply
    Deployment và VaultStaticSecret cùng lúc, nên pod có thể khởi động trước khi Secret
    kịp tồn tại. Định nghĩa hoàn thành là "tự hội tụ trong SLO", không phải "không bao giờ
    thấy trạng thái đó".
    """
    targets = [d for d in docs if d.get("kind") == "VaultStaticSecret"]
    if not targets:
        return
    timeout = config_int("vault.initial_sync_timeout_seconds", 60)
    log(f"chờ {len(targets)} VaultStaticSecret trong {ns} đồng bộ (tối đa {timeout}s)")
    deadline = time.time() + timeout
    while True:
        pending = [msg for ok, msg in
                   (vault_secret_status(d, ns, args) for d in targets) if not ok]
        if not pending:
            log(f"tất cả {len(targets)} VaultStaticSecret đã đồng bộ")
            return
        if time.time() >= deadline:
            break
        time.sleep(5)
    for line in pending:
        print(f"::error::{line}", file=sys.stderr, flush=True)
    raise SystemExit(
        f"{args.app}/{args.env}: bí mật chưa được VSO đồng bộ sau {timeout}s. Pod sẽ kẹt ở "
        "CreateContainerConfigError chừng nào Secret đích chưa tồn tại. Kiểm theo thứ tự: "
        "secret đã được ghi vào đúng đường dẫn Vault ở trên chưa; role/policy của app có "
        "đọc được tiền tố đó không; VaultAuth trong namespace có Ready không."
    )


def wait_for_databases(docs: list[dict], ns: str, args) -> None:
    """Chờ mọi Cluster (CloudNativePG) vừa render báo Ready.

    Đọc condition `Ready` chứ không đếm pod: một cluster ba bản sao có pod chạy từ sớm
    trong khi bootstrap/join replica còn chưa xong, và app kết nối vào lúc đó thì gặp
    "the database system is starting up" — trông y hệt một lỗi cấu hình.
    """
    targets = [d for d in docs if d.get("kind") == "Cluster"
               and str(d.get("apiVersion", "")).startswith("postgresql.cnpg.io/")]
    if not targets:
        return
    timeout = config_int("database.ready_timeout_seconds", 600)
    log(f"chờ {len(targets)} Cluster postgres trong {ns} sẵn sàng (tối đa {timeout}s)")
    deadline = time.time() + timeout
    while True:
        pending = []
        for doc in targets:
            name = doc["metadata"]["name"]
            cp = kubectl(["get", "cluster.postgresql.cnpg.io", name, "-n", ns, "-o", "json"],
                         kubeconfig=args.kubeconfig, check=False, capture=True)
            if cp.returncode != 0:
                pending.append(f"{name}: Cluster chưa tồn tại trên cụm")
                continue
            obj = json.loads(cp.stdout or "{}")
            status = obj.get("status") or {}
            ready = next((c for c in status.get("conditions") or []
                          if c.get("type") == "Ready"), None)
            if ready and ready.get("status") == "True":
                continue
            want_instances = (obj.get("spec") or {}).get("instances", 1)
            pending.append(
                f"{name}: {status.get('readyInstances', 0)}/{want_instances} bản sao sẵn "
                f"sàng, phase={status.get('phase', '?')} "
                f"reason={(ready or {}).get('reason', '?')}")
        if not pending:
            log(f"tất cả {len(targets)} Cluster postgres đã Ready")
            wait_for_recoverability(targets, ns, args)
            return
        if time.time() >= deadline:
            break
        time.sleep(10)
    for line in pending:
        print(f"::error::{line}", file=sys.stderr, flush=True)
    raise SystemExit(
        f"{args.app}/{args.env}: cơ sở dữ liệu chưa Ready sau {timeout}s. Kiểm: Secret "
        "credential đã được VSO đồng bộ chưa (nó là nguồn user/password của initdb), "
        "PVC có bound không, và image postgres có kéo được từ registry không."
    )


def wait_for_recoverability(targets: list[dict], ns: str, args) -> None:
    """Chờ base backup ĐẦU TIÊN, cho mọi Cluster có khai kho object.

    Vì sao đây là một bước riêng chứ không gộp vào điều kiện `Ready`: `Ready` và
    `ContinuousArchiving=True` đều KHÔNG nói gì về việc có phục hồi được hay không. Đo
    được trên harness — một Cluster `Ready`, `ContinuousArchiving=True` với thông điệp
    "Continuous archiving is working", WAL nằm thật trong bucket, mà `bootstrap.recovery`
    chết ngay lập tức với `no target backup found`. Trường DUY NHẤT phân biệt hai trạng
    thái đó là `status.firstRecoverabilityPoint`: nó chỉ xuất hiện sau khi CNPG chụp xong
    một base backup.

    Nên `verify` khẳng định đúng trường đó. Guard ở mục 8 của kế hoạch nói "database
    production không phục hồi được thì không đáng gọi là chạy" — câu ấy chỉ có nghĩa khi
    có một phép đo đứng sau nó.
    """
    # Chỉ những Cluster THẬT SỰ có backup trong manifest vừa render. Một cụm staging
    # không khai kho object thì không có gì để chờ, và chờ nó là treo 10 phút vô ích.
    want = [d for d in targets
            if ((d.get("spec") or {}).get("backup") or {}).get("barmanObjectStore")]
    if not want:
        return
    timeout = config_int("database.backup.first_backup_timeout_seconds", 600)
    log(f"chờ base backup đầu tiên của {len(want)} Cluster trong {ns} (tối đa {timeout}s)")
    deadline = time.time() + timeout
    while True:
        pending = []
        for doc in want:
            name = doc["metadata"]["name"]
            cp = kubectl(["get", "cluster.postgresql.cnpg.io", name, "-n", ns, "-o", "json"],
                         kubeconfig=args.kubeconfig, check=False, capture=True)
            if cp.returncode != 0:
                pending.append(f"{name}: Cluster không đọc được")
                continue
            status = (json.loads(cp.stdout or "{}").get("status") or {})
            if status.get("firstRecoverabilityPoint"):
                continue
            pending.append(
                f"{name}: chưa có firstRecoverabilityPoint — kho object mới chỉ nhận WAL, "
                f"chưa có base backup nào (lastSuccessfulBackup="
                f"{status.get('lastSuccessfulBackup') or 'chưa có'})")
        if not pending:
            for doc in want:
                log(f"{doc['metadata']['name']}: phục hồi được")
            return
        if time.time() >= deadline:
            break
        time.sleep(10)
    for line in pending:
        print(f"::error::{line}", file=sys.stderr, flush=True)
    raise SystemExit(
        f"{args.app}/{args.env}: có kho object nhưng sau {timeout}s vẫn chưa có base "
        "backup nào. Kiểm ScheduledBackup trong namespace (`kubectl get scheduledbackup`) "
        "và log của pod backup. Một cụm ở trạng thái này VẪN báo Ready và VẪN đẩy WAL đi "
        "— nhưng `bootstrap.recovery` sẽ fail với `no target backup found`."
    )


# --------------------------------------------------------------------------------------
# vault foundation commands
# --------------------------------------------------------------------------------------
# The CRDs and the controller ship as two objects and upgrade separately. A cluster
# running 1.4 CRDs under a 1.5 controller (or the reverse) accepts a new CR, reports
# nothing, and never syncs it — so the version check is against BOTH, not just the pod.
VSO_CRDS = (
    "vaultconnections.secrets.hashicorp.com",
    "vaultauthglobals.secrets.hashicorp.com",
    "vaultauths.secrets.hashicorp.com",
    "vaultstaticsecrets.secrets.hashicorp.com",
)


def vso_installed_version(kubeconfig: str | None) -> str | None:
    """Version of the running VSO controller, from its image tag. None if not installed."""
    ns = _vault_str("operator_namespace") or "vault-secrets-operator-system"
    cp = kubectl(["-n", ns, "get", "deploy", "-o", "json"],
                 kubeconfig=kubeconfig, check=False, capture=True)
    if cp.returncode != 0:
        return None
    try:
        items = json.loads(cp.stdout or "{}").get("items", [])
    except json.JSONDecodeError:
        return None
    for dep in items:
        for container in dep.get("spec", {}).get("template", {}).get("spec", {}).get("containers", []):
            image = container.get("image", "")
            if "vault-secrets-operator" in image and ":" in image:
                return image.rsplit(":", 1)[1].lstrip("v")
    return None


def check_vault_foundation(kubeconfig: str | None) -> None:
    """Fail unless VSO is installed at the pinned version and the foundation objects exist.

    Ordered from the failure that is hardest to diagnose to the easiest: a missing CRD at
    least makes `kubectl apply` fail loudly, whereas a version skew or a missing
    VaultConnection produces a VaultStaticSecret that simply sits there.
    """
    cp = kubectl(["get", "crd", "-o", "name"], kubeconfig=kubeconfig, check=False, capture=True)
    if cp.returncode != 0:
        raise SystemExit(f"cannot list CRDs: {(cp.stderr or '').strip()}")
    present = set((cp.stdout or "").split())
    missing = [c for c in VSO_CRDS if f"customresourcedefinition.apiextensions.k8s.io/{c}" not in present]
    if missing:
        raise SystemExit(
            f"Vault Secrets Operator CRDs missing: {', '.join(missing)}. Install VSO "
            f"{_vault_str('operator_version') or '(version unpinned)'} before enabling "
            "features.vault_secrets."
        )

    want = _vault_str("operator_version")
    have = vso_installed_version(kubeconfig)
    if not want:
        log("vault.operator_version is empty — VSO version check skipped")
    elif have is None:
        raise SystemExit(
            f"VSO is pinned to {want} but no vault-secrets-operator Deployment was found in "
            f"namespace {_vault_str('operator_namespace')}. Check vault.operator_namespace."
        )
    elif have != want:
        raise SystemExit(
            f"VSO version mismatch: cluster runs {have}, platform.env.yaml pins "
            f"vault.operator_version={want}. Controller and CRDs must be upgraded together "
            "— a new CR under an old controller is ignored silently."
        )
    else:
        log(f"VSO {have} matches pinned vault.operator_version")

    ns = _vault_str("operator_namespace") or "vault-secrets-operator-system"
    for kind, name in (("vaultconnection", _vault_str("connection_name") or "default"),
                       ("vaultauthglobal", _vault_str("auth_global_name") or "default")):
        cp = kubectl(["-n", ns, "get", kind, name, "-o", "name"],
                     kubeconfig=kubeconfig, check=False, capture=True)
        if cp.returncode != 0:
            raise SystemExit(
                f"{kind}/{name} not found in namespace {ns}. Run "
                "`idpctl vault-foundation --apply` with cluster-admin first."
            )
        log(f"found {kind}/{name} in {ns}")


def _emit(docs: list[dict], args) -> None:
    """Print manifests, and apply them only when explicitly asked.

    Print-by-default is the point: these objects grant access to secrets, so the normal
    path is that a human reads the YAML and applies it with their own credentials.
    """
    text = "".join("---\n" + yaml.safe_dump(d, sort_keys=False) for d in docs)
    if not getattr(args, "apply", False):
        print(text, end="")
        return
    cp = kubectl(["apply", "-f", "-"], kubeconfig=args.kubeconfig, stdin=text,
                 check=False, capture=True)
    if cp.returncode != 0:
        raise SystemExit(f"apply failed: {(cp.stderr or '').strip()}")
    log((cp.stdout or "").strip())


def cmd_vault_foundation(args) -> None:
    """VaultConnection + VaultAuthGlobal — one set per cluster, applied by an operator."""
    _emit(vault_foundation_manifests(), args)


def cmd_vault_onboard(args) -> None:
    """Everything one app/environment needs to read its own Vault prefix, and nothing else.

    Two halves with two different owners, so this prints rather than performs the Vault
    half: the Kubernetes objects (ServiceAccount + VaultAuth) belong to the platform, while
    the policy and role are written by whoever administers Vault. Deliberately no Vault
    token is used or required here — CI holding a Vault token would defeat the entire
    arrangement, since that token can read what the policy allows.
    """
    app, env = validate_secret_name(args.app), validate_environment(args.env)
    namespace = app_namespace(app, env)
    role, sa = vault_role_name(app, env), vault_service_account(app, env)
    read_policy = vault_policy_name(app, env)
    write_policy = vault_policy_name(app, env, write=True)

    if args.print_policy:
        print(vault_policy(app, env, write=args.write))
        return

    _emit(vault_auth_manifests(app, env), args)

    if getattr(args, "apply", False):
        return
    ttl = _vault_str("token_ttl") or "1h"
    mount = _vault_str("auth_mount") or "kubernetes"
    print(f"""
# ---------------------------------------------------------------------------
# Vault side — run by whoever administers Vault, with THEIR token, not CI's.
# Writing prod secrets is expected to sit behind your own approval policy;
# this only prints what to create.
# ---------------------------------------------------------------------------
# 1. Policies (see `vault-onboard --app {app} --env {env} --print-policy [--write]`)
idpctl vault-onboard --app {app} --env {env} --print-policy \\
  | vault policy write {read_policy} -
idpctl vault-onboard --app {app} --env {env} --print-policy --write \\
  | vault policy write {write_policy} -

# 2. Kubernetes auth role, bound to exactly one ServiceAccount in one namespace.
vault write auth/{mount}/role/{role} \\
  bound_service_account_names={sa} \\
  bound_service_account_namespaces={namespace} \\
  policies={read_policy} \\
  ttl={ttl}

# 3. Grant the write policy to the humans/automation that store secrets for this app.
#    VSO itself must NEVER get it: the operator only reads.
""".rstrip())


def cmd_verify_rbac(args) -> None:
    """A least-privilege identity for post-deploy verification. No access to Secrets."""
    _emit(verify_rbac_manifests(validate_secret_name(args.app), validate_environment(args.env)),
          args)


def read_secret_value(args) -> str:
    """Get the value from stdin or a hidden prompt. NEVER from an argument.

    A value passed as `--value` lands in the shell history, in the process table where any
    other user on the box can read it with `ps`, and in the CI log if this is ever scripted.
    None of those are things you can un-leak, so the flag simply does not exist.
    """
    if getattr(args, "generate", False):
        # For credentials the PLATFORM owns — a database password nobody should ever see,
        # type or paste. Generated here and written straight to Vault; it is never printed,
        # never returned to a caller, and never written to a file.
        alphabet = string.ascii_letters + string.digits
        return "".join(secrets.choice(alphabet) for _ in range(32))
    if args.stdin:
        value = sys.stdin.read()
        # Only the trailing newline the shell/pipe adds — a secret may legitimately end in
        # whitespace, and silently stripping it produces an auth failure nobody can explain.
        return value[:-1] if value.endswith("\n") else value
    first = getpass.getpass(f"value for {args.key}: ")
    if first != getpass.getpass("repeat: "):
        raise SystemExit("the two values differ — nothing was written")
    if not first:
        raise SystemExit("empty value refused: an empty secret fails at runtime, not here")
    return first


def cmd_secret_set(args) -> None:
    """Write one key of one logical secret into Vault, at the platform-derived path.

    Run by a HUMAN (or the onboarding service acting for one), never by app CI — it needs
    a Vault token with the write policy from `vault-onboard`. The path is derived exactly
    like the reader's, so a secret written here is one the app's role can read: getting
    that pairing wrong by hand is the single most common way this ends in 'permission
    denied' against a path that looks right.
    """
    app, env = validate_secret_name(args.app), validate_environment(args.env)
    name = validate_secret_name(args.name)
    if not re.match(r"^[A-Za-z0-9._-]{1,253}$", args.key or ""):
        raise SystemExit(f"invalid key {args.key!r}: letters, digits, '.', '_' and '-' only")

    address = (os.environ.get("VAULT_ADDR") or "").rstrip("/")
    token = os.environ.get("VAULT_TOKEN")
    if not address or not token:
        raise SystemExit(
            "VAULT_ADDR and VAULT_TOKEN must be set. Deliberately not read from "
            "platform.env.yaml: vault.address is the address the CLUSTER uses, which is "
            "often unreachable from a laptop, and a token must never live in a config file."
        )

    path = vault_relative_path(app, env, name)
    mount = _vault_str("kv_mount") or "kv"
    kv_type = (_vault_str("kv_type") or "kv-v2").lower()
    url = (f"{address}/v1/{mount}/data/{path}" if kv_type == "kv-v2"
           else f"{address}/v1/{mount}/{path}")
    value = read_secret_value(args)
    payload = {"data": {args.key: value}} if kv_type == "kv-v2" else {args.key: value}

    # kv-v2 patch (not put) so writing one key does not delete the others in the same
    # secret — that would silently break every other workload reading the same path.
    headers = {"X-Vault-Token": token, "Content-Type": "application/json"}
    if kv_type == "kv-v2" and not args.replace:
        headers["Content-Type"] = "application/merge-patch+json"
        method = "PATCH"
    else:
        method = "POST"
    if _vault_str("namespace"):
        headers["X-Vault-Namespace"] = _vault_str("namespace")

    request = urllib.request.Request(url, data=json.dumps(payload).encode(),
                                     headers=headers, method=method)
    # Note what is NOT logged: the URL is, the token and value are not.
    log(f"{method} {mount}/{path} (key {args.key}) for {app}/{env}")
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            response.read()
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace")[:300]
        if exc.code == 404 and method == "PATCH":
            raise SystemExit(
                f"{mount}/{path} does not exist yet, and a patch cannot create it. "
                "Re-run with --replace to write the first version of this secret."
            ) from None
        raise SystemExit(f"Vault refused the write ({exc.code}): {detail}") from None
    except urllib.error.URLError as exc:
        raise SystemExit(f"cannot reach Vault at {address}: {exc.reason}") from None
    log(f"wrote {args.key} to {mount}/{path} — the value was not printed or stored locally")


def _consumers_of_secret(ns: str, secret: str, kubeconfig) -> list[str]:
    """Deployment nào đang lấy biến môi trường từ Secret này."""
    cp = kubectl(["get", "deploy", "-n", ns, "-o", "json"],
                 kubeconfig=kubeconfig, check=False, capture=True)
    if cp.returncode != 0:
        return []
    out = []
    for dep in (json.loads(cp.stdout or "{}").get("items") or []):
        spec = ((dep.get("spec") or {}).get("template") or {}).get("spec") or {}
        refs = json.dumps(spec.get("containers") or [])
        if f'"{secret}"' in refs:
            out.append(dep["metadata"]["name"])
    return sorted(out)


def cmd_rotate_db_credential(args) -> None:
    """Xoay vòng mật khẩu database THEO ĐÚNG THỨ TỰ, và kiểm từng bước.

    Vì sao phải là một lệnh chứ không phải "ghi vào Vault rồi để hệ tự lo": ba thành phần
    phải đổi theo đúng thứ tự, và không cái nào tự kích hoạt cái kế tiếp.

      1. Vault  — nguồn sự thật.
      2. Secret — VSO đồng bộ xuống, trong vòng `refreshAfter`.
      3. ROLE trong PostgreSQL — CNPG chỉ đọc lại `passwordSecret` khi đối tượng CLUSTER
         được reconcile. Một Secret đổi KHÔNG kích hoạt việc đó. Đo trên harness: sau 8
         phút, `status.managedRolesStatus.passwordStatus.<role>.resourceVersion` vẫn đứng
         ở bản cũ, mật khẩu trong Secret KHÔNG đăng nhập được, còn pod vẫn chạy bằng mật
         khẩu cũ. Chạm vào Cluster một cái thì role đổi trong dưới 20 giây.
      4. POD  — biến môi trường chỉ đọc lúc container khởi động, nên pod đang chạy vẫn
         giữ mật khẩu cũ cho tới khi được restart.

    Làm sai thứ tự là tự tạo sự cố: restart pod TRƯỚC khi role đổi thì pod nhận mật khẩu
    mới trong khi database vẫn dùng mật khẩu cũ, và app chết cho tới lần reconcile sau.

    Lệnh này KHÔNG đọc giá trị bí mật ở bất kỳ bước nào — nó theo dõi `resourceVersion`
    của Secret, đúng thứ mà CNPG cũng ghi lại. Bí mật vẫn chỉ đi từ Vault tới VSO.

    Cửa sổ gián đoạn còn lại là có thật và không tránh được với một credential duy nhất:
    từ lúc role đổi (bước 3) tới lúc pod cuối cùng lên lại (bước 4), pod cũ dùng mật khẩu
    cũ sẽ bị từ chối. Nó dài bằng một lần rollout. Muốn bằng không thì phải hai credential
    song song, và đó là một thay đổi contract, không phải một cờ.
    """
    ns = app_namespace(args.app, args.env)
    cp = kubectl(["get", "cluster.postgresql.cnpg.io", "-n", ns, "-o", "json"],
                 kubeconfig=args.kubeconfig, check=False, capture=True)
    if cp.returncode != 0:
        raise SystemExit(f"không đọc được Cluster nào trong {ns}: {(cp.stderr or '').strip()}")
    targets = []
    for obj in (json.loads(cp.stdout or "{}").get("items") or []):
        for role in (((obj.get("spec") or {}).get("managed") or {}).get("roles") or []):
            secret = (role.get("passwordSecret") or {}).get("name")
            if secret:
                targets.append((obj["metadata"]["name"], role["name"], secret))
    if not targets:
        raise SystemExit(
            f"{ns}: không có Cluster nào khai `managed.roles[].passwordSecret`. Cụm này "
            "được render bằng catalog cũ — render lại rồi apply trước khi xoay vòng, nếu "
            "không thì mật khẩu mới sẽ nằm trong Secret mà database không bao giờ nhận."
        )

    for cluster, role, secret in targets:
        log(f"xoay vòng {ns}/{cluster} role={role} secret={secret}")

        def rv() -> str:
            got = kubectl(["get", "secret", secret, "-n", ns, "-o",
                           "jsonpath={.metadata.resourceVersion}"],
                          kubeconfig=args.kubeconfig, check=False, capture=True)
            return (got.stdout or "").strip()

        before = rv()

        # 1. Vault. Dùng lại đúng đường ghi của `secret-set`, nên đường dẫn không thể lệch.
        cmd_secret_set(argparse.Namespace(
            app=args.app, env=args.env, name=CONFIG.get("database.credential_secret") or "database",
            key="password", generate=True, stdin=False, replace=False))

        # 2. VSO. Chờ Secret thật sự đổi — không đoán theo refreshAfter.
        deadline = time.time() + config_int("vault.sync_timeout_seconds", 300)
        while rv() == before:
            if time.time() >= deadline:
                raise SystemExit(
                    f"{secret}: VSO chưa đồng bộ giá trị mới sau khi ghi vào Vault. Kiểm "
                    f"`kubectl -n {ns} get vaultstaticsecret` — điều kiện SecretSynced.")
            time.sleep(5)
        after = rv()
        log(f"VSO đã đồng bộ {secret} (resourceVersion {before} -> {after})")

        # 3. CNPG. Chạm vào Cluster để ép reconcile, rồi CHỜ nó xác nhận đã đọc đúng bản
        #    đó — `passwordStatus.resourceVersion` là lời khai của chính operator.
        kubectl(["annotate", "cluster.postgresql.cnpg.io", cluster, "-n", ns,
                 f"idp.platform/credential-rotated-at={int(time.time())}", "--overwrite"],
                kubeconfig=args.kubeconfig, check=False, capture=True)
        deadline = time.time() + config_int("database.ready_timeout_seconds", 600)
        while True:
            got = kubectl(["get", "cluster.postgresql.cnpg.io", cluster, "-n", ns, "-o",
                           "jsonpath={.status.managedRolesStatus.passwordStatus."
                           f"{role}.resourceVersion}}"],
                          kubeconfig=args.kubeconfig, check=False, capture=True)
            if (got.stdout or "").strip() == after:
                break
            if time.time() >= deadline:
                raise SystemExit(
                    f"{cluster}: CNPG chưa áp mật khẩu mới cho role {role}. Ở trạng thái "
                    "này Secret chứa mật khẩu mà database TỪ CHỐI; pod cũ vẫn chạy được "
                    "bằng mật khẩu cũ, nên không có gì đỏ. Kiểm log của operator.")
            time.sleep(5)
        log(f"CNPG đã áp mật khẩu mới cho role {role}")

        # 4. Pod. Đúng một lần restart cho mỗi workload đang dùng Secret này.
        consumers = _consumers_of_secret(ns, secret, args.kubeconfig)
        if not consumers:
            warn(f"không thấy Deployment nào dùng {secret} — bỏ qua bước restart")
        for dep in consumers:
            kubectl(["rollout", "restart", f"deploy/{dep}", "-n", ns],
                    kubeconfig=args.kubeconfig, check=True, capture=True)
            log(f"restart deploy/{dep}")
        for dep in consumers:
            kubectl(["rollout", "status", f"deploy/{dep}", "-n", ns, "--timeout=300s"],
                    kubeconfig=args.kubeconfig, check=True, capture=True)
        log(f"{ns}/{cluster}: xoay vòng xong, {len(consumers)} workload đã chạy lại")



# --------------------------------------------------------------------------------------
# doctor — read-only, feature/backend-aware capability check
# --------------------------------------------------------------------------------------
# preflight proves the RUNNER can render (tools + versions + cluster reachable). doctor
# proves the CLUSTER matches what config CLAIMS, per feature and per database backend,
# BEFORE a render is committed and Fleet applies it — so an infrastructure mismatch (no
# CNPG operator, wrong StorageClass name, missing Gateway) surfaces here and not as a
# silently-never-attached route or a permanently-Pending PVC days later.
#
# Every check is READ-ONLY (get/version) and every result carries the config key it is
# about, so a FAIL names the exact line to fix. A check that cannot run (no permission,
# no cluster) is a WARN, never a false OK — the whole point is to not report green when
# the truth is unknown.
DOCTOR_FAIL, DOCTOR_WARN, DOCTOR_OK, DOCTOR_SKIP = "FAIL", "WARN", "OK", "SKIP"


def _resource_present(probe, args: list[str]):
    """True/False whether a cluster resource exists, or None if it could not be checked.

    None (not False) on permission-denied or unreachable is deliberate: absent and
    can't-tell are different answers, and only the first is a blocker.
    """
    rc, out, err = probe(args + ["-o", "name"])
    if rc == 0:
        return bool(out.strip())
    low = (err or "").lower()
    if "notfound" in low or "not found" in low:
        return False
    return None


def run_doctor_checks(probe=None) -> list[dict]:
    """Capability findings for the CURRENT config. probe=None → config-only (no cluster).

    Pure over CONFIG + probe, so tests drive it with a fake probe and no cluster.
    """
    results: list[dict] = []

    def add(level, capability, key, msg):
        results.append({"level": level, "capability": capability,
                        "config_key": key, "message": msg})

    def cluster(args):
        """None-safe cluster presence check; skipped entirely when probe is None."""
        return None if probe is None else _resource_present(probe, args)

    # ---- database (only when the feature is on) --------------------------------------
    if not feature("postgres_application"):
        add(DOCTOR_SKIP, "database", "features.postgres_application",
            "postgres_application off — không kiểm CNPG / StorageClass / object store")
    else:
        backend = database_backend()
        repo = CONFIG.get("database.image_repository") or ""
        if not repo:
            add(DOCTOR_FAIL, "database.image", "database.image_repository",
                "ảnh database rỗng — Cluster/StatefulSet sẽ không có image")
        else:
            add(DOCTOR_OK, "database.image", "database.image_repository", f"image={repo}:<engine_version>")

        sc = CONFIG.get("database.storage_class") or CONFIG.get("kubernetes.storage_class") or ""
        if not sc:
            add(DOCTOR_FAIL, "database.storage", "kubernetes.storage_class",
                "StorageClass rỗng — PVC sẽ treo Pending")
        else:
            present = cluster(["get", "storageclass", sc])
            if present is True:
                add(DOCTOR_OK, "database.storage", "kubernetes.storage_class", f"StorageClass {sc} có mặt")
            elif present is False:
                add(DOCTOR_FAIL, "database.storage", "kubernetes.storage_class",
                    f"StorageClass {sc} KHÔNG tồn tại trên cụm")
            else:
                add(DOCTOR_WARN, "database.storage", "kubernetes.storage_class",
                    f"chưa kiểm được StorageClass {sc} (thiếu quyền / không có cụm)")

        if backend == "cnpg":
            present = cluster(["get", "crd", "clusters.postgresql.cnpg.io"])
            if present is True:
                add(DOCTOR_OK, "database.cnpg", "database.backend", "CNPG CRD có mặt")
            elif present is False:
                add(DOCTOR_FAIL, "database.cnpg", "database.backend",
                    "backend=cnpg nhưng CNPG CRD (clusters.postgresql.cnpg.io) KHÔNG có — "
                    "cài operator hoặc đổi database.backend=statefulset")
            else:
                add(DOCTOR_WARN, "database.cnpg", "database.backend", "chưa kiểm được CNPG CRD")
            if not (CONFIG.get("database.backup.object_store_url") or ""):
                add(DOCTOR_WARN, "database.backup", "database.backup.object_store_url",
                    "object store rỗng — render prod sẽ bị CHẶN (staging vẫn OK)")
        else:  # statefulset
            add(DOCTOR_OK, "database.backend", "database.backend",
                "backend=statefulset — KHÔNG kiểm CNPG (đúng chủ ý)")
            add(DOCTOR_WARN, "database.backup", "database.backend",
                "backend=statefulset không có backup nội tại — render prod bị CHẶN (staging OK)")

        ps = CONFIG.get("registry.pull_secret") or ""
        if not ps:
            add(DOCTOR_FAIL, "registry.pull_secret", "registry.pull_secret",
                "imagePullSecret rỗng — workload không kéo được ảnh private")

    # ---- gateway (whenever one is configured — routes need it regardless of feature) --
    gw = CONFIG.get("ingress.gateway_name") or ""
    gns = CONFIG.get("ingress.gateway_namespace") or ""
    if gw:
        present = cluster(["get", "gateway", gw, "-n", gns])
        if present is True:
            add(DOCTOR_OK, "gateway", "ingress.gateway_name", f"Gateway {gns}/{gw} có mặt")
        elif present is False:
            add(DOCTOR_FAIL, "gateway", "ingress.gateway_name",
                f"Gateway {gns}/{gw} KHÔNG tồn tại — HTTPRoute sẽ không bao giờ attach")
        else:
            add(DOCTOR_WARN, "gateway", "ingress.gateway_name",
                f"chưa kiểm được Gateway {gns}/{gw} (thiếu quyền / không có cụm)")
    sec = CONFIG.get("ingress.section_name") or ""
    if sec:
        add(DOCTOR_OK, "gateway.section", "ingress.section_name",
            f"route attach listener sectionName={sec}")

    # ---- vault (only when the feature is on) -----------------------------------------
    if feature("vault_secrets"):
        addr = str(CONFIG.get("vault.address") or "")
        if not addr:
            add(DOCTOR_FAIL, "vault.address", "vault.address", "vault_secrets bật nhưng vault.address rỗng")
        else:
            add(DOCTOR_OK, "vault.address", "vault.address", f"vault.address={addr}")
        present = cluster(["get", "crd", "vaultstaticsecrets.secrets.hashicorp.com"])
        if present is True:
            add(DOCTOR_OK, "vault.vso", "vault.operator_version", "VSO CRD có mặt")
        elif present is False:
            add(DOCTOR_FAIL, "vault.vso", "vault.operator_version",
                "vault_secrets bật nhưng VSO CRD (secrets.hashicorp.com) KHÔNG có")
        else:
            add(DOCTOR_WARN, "vault.vso", "vault.operator_version", "chưa kiểm được VSO CRD")
        if addr.startswith("https") and not CONFIG.get("vault.skip_tls_verify", False) \
                and not (CONFIG.get("vault.ca_cert_secret") or ""):
            add(DOCTOR_WARN, "vault.tls", "vault.ca_cert_secret",
                "Vault HTTPS, skip_tls_verify=false, chưa khai ca_cert_secret — "
                "chỉ chạy được nếu CA đã nằm trong trust store hệ thống")
    else:
        add(DOCTOR_SKIP, "vault", "features.vault_secrets",
            "vault_secrets off — không kiểm Vault / VSO / CA")

    # ---- registry (config presence) --------------------------------------------------
    rh = CONFIG.get("registry.host") or ""
    if rh:
        add(DOCTOR_OK, "registry", "registry.host", f"registry.host={rh}")
    else:
        add(DOCTOR_WARN, "registry", "registry.host", "registry.host rỗng")

    return results


def cmd_doctor(args) -> None:
    probe = None
    if not getattr(args, "no_cluster", False):
        cp = kubectl(["version", "--output=json"], kubeconfig=args.kubeconfig,
                     check=False, capture=True)
        if cp.returncode != 0:
            warn(f"cụm không truy cập được ({(cp.stderr or '').strip()[:80]}) — "
                 "chạy doctor ở chế độ CHỈ-CONFIG. Thêm --no-cluster để tắt cảnh báo này.")
        else:
            def probe(pargs):
                r = kubectl(pargs, kubeconfig=args.kubeconfig, check=False, capture=True)
                return r.returncode, (r.stdout or ""), (r.stderr or "")

    results = run_doctor_checks(probe)
    icon = {DOCTOR_OK: "  ok ", DOCTOR_WARN: " warn", DOCTOR_FAIL: "FAIL ", DOCTOR_SKIP: " skip"}
    print("\n=== doctor: capability vs config (read-only) ===\n")
    for r in results:
        print(f"[{icon[r['level']]}] {r['capability']:22} {r['message']}  ({r['config_key']})")
    fails = [r for r in results if r["level"] == DOCTOR_FAIL]
    warns = [r for r in results if r["level"] == DOCTOR_WARN]
    print(f"\n==> {len(fails)} blocker, {len(warns)} cảnh báo.")
    if fails:
        raise SystemExit(1)


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

    # score-compose is not needed to deploy — it belongs to the local-development and
    # stack-CI paths — so the orchestrator's own preflight does not demand it. A stack's
    # CI passes --require-score-compose and gets the same pinning guarantee.
    pinned = ["score-k8s"]
    if getattr(args, "require_score_compose", False):
        if not shutil.which("score-compose"):
            raise SystemExit("score-compose requested but not on PATH")
        pinned.append("score-compose")
    check_tool_versions(pinned, force=True)

    if args.require_cluster:
        cp = kubectl(["version", "--output=json"], kubeconfig=args.kubeconfig,
                     check=False, capture=True)
        if cp.returncode != 0:
            raise SystemExit(f"cluster unreachable: {(cp.stderr or '').strip()}")
        log("cluster reachable")

    # Separate flag rather than "check it whenever features.vault_secrets is on": the
    # foundation is applied by an operator with cluster-admin, so an app's deploy job may
    # legitimately be unable to read CRDs or the operator namespace.
    if getattr(args, "require_vault", False):
        if not args.require_cluster:
            raise SystemExit("--require-vault needs --require-cluster (it queries the cluster)")
        check_vault_foundation(args.kubeconfig)
    log("preflight OK")


# --------------------------------------------------------------------------------------
# cli
# --------------------------------------------------------------------------------------
