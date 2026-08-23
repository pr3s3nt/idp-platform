"""Shared configuration, naming contracts, command execution and YAML plumbing."""
from __future__ import annotations

import argparse
import base64
import difflib
import fnmatch
import getpass
import hashlib
import json
import os
import re
import secrets
import shutil
import string
import subprocess
import sys
import time
import urllib.error
import urllib.request
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
            # Kho ứng dụng mà onboarding tạo, và kho platform mà CI của app checkout để
            # hỏi tên ảnh. Cả hai là toạ độ: tên tổ chức và tên kho khác nhau ở mỗi nơi cài.
            "app_repo_pattern": "{app}", "platform_repo": "",
            "committer_name": "idp-orchestrator",
            "committer_email": "idp-orchestrator@noreply.invalid"},
    "registry": {"host": "", "path": "", "pull_secret": "registry-pull"},
    "kubernetes": {
        "state_namespace": "cluster-state",
        "namespace_pattern": "{app}-{env}",
        "storage_class": "",
        "sha_record_dir": ".platform",
    },
    "ingress": {"gateway_name": "", "gateway_namespace": "",
                # Which Gateway listener a route attaches to (parentRefs.sectionName). A
                # Gateway with both an HTTP and an HTTPS listener will attach an
                # HTTPRoute that names NO section to whichever listener the controller
                # picks — usually the plain HTTP one — and nothing reports the mismatch.
                # EMPTY is the backward-compatible default: no sectionName is written, so
                # a single-listener Gateway renders byte-identically to before.
                "section_name": "",
                # Scheme for USER-FACING urls the platform prints (onboarding output).
                # Not a routing decision — the Gateway terminates TLS — just what URL a
                # human is told to open. http keeps the historical output unchanged.
                "route_scheme": "http"},
    "images": {},
    "environments": {},
    # Empty version strings mean "do not check" — see check_tool_versions. A brownfield
    # platform must keep running on a runner nobody has re-provisioned yet.
    "ci": {"score_k8s_version": "", "score_compose_version": ""},
    "vault": {
        "operator_version": "",
        # No default address on purpose: it is THE coordinate that differs between every
        # install, and a fallback here is how a render quietly points at the wrong Vault.
        "address": "",
        "namespace": "",
        "skip_tls_verify": False,
        "ca_cert_secret": "",
        "tls_server_name": "",
        "kv_mount": "kv",
        "kv_type": "kv-v2",
        "path_template": "apps/{application}/{environment}/{name}",
        "auth_mount": "kubernetes",
        "auth_audience": "vault",
        "auth_role_template": "idp-{application}-{environment}",
        "policy_template": "idp-{application}-{environment}",
        "service_account_template": "idp-{application}",
        "operator_namespace": "vault-secrets-operator-system",
        "connection_name": "default",
        "auth_global_name": "default",
        "allowed_namespaces": [],
        "auth_ref": "app-vault",
        "refresh_after": "5m",
        "initial_sync_timeout_seconds": 60,
        "token_ttl": "1h",
    },
    "database_profiles": {},
    "database": {
        # Which implementation backs `class: application`. An infrastructure decision, so
        # it lives in config rather than being fixed in the catalog:
        #   cnpg        -> a CloudNativePG Cluster (HA, managed backup). The default, so an
        #                  install that says nothing renders exactly as it always has.
        #   statefulset -> a plain PostgreSQL StatefulSet the platform owns. For a cluster
        #                  that has no CNPG operator and does not want one. No HA and no
        #                  built-in backup, so it is refused in prod (see check_database_
        #                  classes) until a backup backend exists.
        # The app-facing OUTPUT contract is identical for both — host/port/database/
        # username/password — so switching backend never touches app code.
        "backend": "cnpg",
        # Which operator provides `class: application`. Named in config because the choice
        # is an infrastructure decision: a company with a DBA-run service replaces this
        # provisioner wholesale rather than running an operator at all.
        "provider": "cloudnative-pg",
        "operator_version": "",
        "operator_namespace": "cnpg-system",
        # No default: the postgres image lives in whichever registry this install pulls
        # from. Combined with the profile's engine_version to form the full reference.
        "image_repository": "",
        "storage_class": "",
        # The logical secret name, under the app's own Vault prefix, holding the database
        # username and password. Same prefix as every other app secret, so the same policy
        # already grants it.
        "credential_secret": "database",
        "backup": {"object_store_url": "", "credentials_secret": "", "destination_path": "",
                   # Rỗng = AWS S3. Mọi kho S3-compatible khác (MinIO, Ceph, kho nội bộ)
                   # phải khai địa chỉ ở đây.
                   "endpoint_url": "",
                   # LỊCH CHỤP BASE BACKUP. `barmanObjectStore` một mình CHỈ bật WAL
                   # archiving — nó không chụp lấy một base backup nào, và WAL không có
                   # base thì phục hồi được ĐÚNG KHÔNG GÌ CẢ. Đo được trên harness:
                   # Cluster `Ready`, condition `ContinuousArchiving=True` với thông điệp
                   # "Continuous archiving is working", WAL nằm thật trong bucket — mà
                   # bootstrap.recovery chết ngay với `no target backup found`.
                   #
                   # Cron của CNPG có SÁU trường (giây đứng đầu), không phải năm như cron
                   # Unix. Viết "0 2 * * *" kiểu Unix thì CNPG đọc thành "giây 0, phút 2,
                   # mọi giờ" — tức là chụp base backup MỖI GIỜ, và không ai thấy gì cho
                   # tới khi hoá đơn kho object về.
                   #
                   # Mặc định 02:00 hằng ngày. Đây là giá trị chính sách, không phải toạ
                   # độ hạ tầng, nên có mặc định chạy được là đúng: quên khai thì được
                   # backup hằng ngày, chứ không phải im lặng không có backup nào.
                   "schedule": "0 0 2 * * *",
                   # `verify` chờ base backup ĐẦU TIÊN xong bao lâu (firstRecoverability
                   # Point xuất hiện). Chỉ áp dụng khi đã cấu hình kho object.
                   "first_backup_timeout_seconds": 600},
        "ready_timeout_seconds": 600,
    },
    # Every capability added by the secret programme is off until switched on
    # per environment. The existing platform must render byte-identically with these unset.
    "features": {
        "application_values": False,
        "vault_secrets": False,
        "postgres_application": False,
        "stack_onboarding": False,
    },
    # Audit/history store. OFF by default and self-contained: with `enabled: false`
    # every `idpctl audit-*` command returns before touching a driver or a socket, so a
    # brownfield install renders and deploys exactly as it did before this module existed.
    # The connection string is NEVER stored here — only the NAME of the environment
    # variable that carries it (see engine/audit.py, docs/adr/0002-vault-only-secret-store).
    "audit": {
        "enabled": False,
        # fail-open (False) vs fail-closed (True): may a history-store outage let the
        # deploy proceed with a warning, or must it fail the run? Default fail-open so the
        # audit trail never becomes a new way to take real deploys down.
        "required": False,
        # Name of the env var holding the libpq connection string. Value lives in a CI
        # secret; it is never logged and never written to the database.
        "database_url_env": "AUDIT_DATABASE_URL",
        # A short connect timeout keeps the history store from ever hanging a deploy.
        "connect_timeout_seconds": 5,
        # Minimum retention for the manual prune helper; audit itself never UPDATEs or
        # DELETEs history. 0 means "keep everything".
        "retention_days": 365,
        "notify_failure": True,
        "notification_mode": "commit-comment",
    },
}

# The two environment names the platform accepts, everywhere. `production` is deliberately
# NOT an accepted alias: two spellings for one environment is how a values file ends up
# with a `production:` block that silently never applies.
ENVIRONMENTS = ("staging", "prod")

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

    def for_env(self, env: str, app: str | None = None) -> dict:
        """Flat {dotted key: value}, with the chosen environment exposed under `env.`."""
        flat: dict[str, object] = {}
        # The application being rendered is not configuration, but a provisioner that
        # derives a Vault path needs it, and score-k8s only ever tells a provisioner the
        # WORKLOAD name. Exposing it here keeps the path derivation in one place instead of
        # making every app pass its own name as a resource param it could get wrong.
        if app:
            flat["computed.app"] = app

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

        # The ACTIVE environment's database profile, flattened under one stable prefix.
        # A provisioner cannot name the environment it is being rendered for — that is the
        # whole point of one catalog serving both — so `%%database_profiles.prod.…%%` would
        # have to be written into a file that also renders for staging. Resolving it here
        # means the provisioner says `%%computed.database.instances%%` and the difference
        # between one replica and three stays entirely in platform.env.yaml.
        profile = (self.get(f"database_profiles.{env}.application") or {})

        def walk_profile(node, prefix="computed.database."):
            for key, value in (node or {}).items():
                if isinstance(value, dict):
                    walk_profile(value, f"{prefix}{key}.")
                else:
                    flat[f"{prefix}{key}"] = value

        walk_profile(profile)
        # CNPG-style image reference: repository from config, tag from the profile, so a
        # company mirror is a config edit and the major version stays one number in one place.
        repo = self.get("database.image_repository") or ""
        version = profile.get("engine_version") or ""
        flat["computed.database.image"] = f"{repo}:{version}" if repo and version else ""
        storage_class = self.get("database.storage_class") or self.get("kubernetes.storage_class") or ""
        flat["computed.database.storage_class"] = storage_class
        # Lịch base backup: profile của môi trường thắng, không có thì lấy mặc định chung.
        # Hai mức là có lý do — prod thường phải chụp trong cửa sổ bảo trì do DBA chốt,
        # còn staging chụp lúc nào cũng được; nhưng một install chỉ muốn MỘT lịch cho cả
        # hai thì khai đúng một chỗ dưới `database.backup.schedule`.
        flat["computed.database.backup.schedule"] = (
            ((profile.get("backup") or {}).get("schedule")
             or self.get("database.backup.schedule") or "")
        )
        return flat

    def render(self, text: str, env: str, *, where: str, app: str | None = None) -> str:
        """Substitute %%key%% placeholders. An unknown key is fatal, never silent.

        Silence is how this whole project's worst bugs behaved — a wrong gateway name or a
        wrong storage class produces no error anywhere, just a route that never attaches or
        a volume that never binds. A typo'd placeholder must not join that club.
        """
        table = self.for_env(env, app)

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


# Repository checkout root. Module files live one directory below it after the refactor;
# keeping this coordinate here prevents defaults from accidentally pointing at engine/.
REPO_ROOT = Path(__file__).resolve().parent.parent


class ConfigState:
    """Stable proxy to the currently selected profile.

    Every engine module imports this one object. Replacing the profile mutates the proxy
    instead of rebinding a module global, so a command can never leave half the engine on
    the default profile and the other half on --env-config.
    """

    def __init__(self, current: EnvConfig | None = None):
        self.current = current or EnvConfig()

    def replace(self, current: EnvConfig) -> None:
        self.current = current

    def __getattr__(self, name):
        return getattr(self.current, name)


# Set from --env-config at startup; the defaults keep everything runnable without one.
CONFIG = ConfigState()


def set_config(config: EnvConfig) -> None:
    CONFIG.replace(config)


# Small accessors instead of module constants: the values now come from platform.env.yaml,
# which is loaded after import, so they cannot be frozen at module level.
def state_ns() -> str:
    return CONFIG.get("kubernetes.state_namespace")


def pull_secret() -> str:
    return CONFIG.get("registry.pull_secret")


def sha_record_dir() -> str:
    return CONFIG.get("kubernetes.sha_record_dir")


def app_namespace(app: str, env: str) -> str:
    """Namespace của một app trong một môi trường — MỘT chỗ tính, mọi lệnh dùng chung.

    Trước đây `render` và `verify` đọc namespace_pattern còn `apply-secrets` ghi cứng
    "{app}-{env}". Chừng nào pattern còn để mặc định thì hai cách cho ra cùng kết quả nên
    không ai thấy gì. Đổi pattern — việc bắt buộc phải làm khi một đội chỉ được cấp sẵn
    vài namespace và không được tự tạo — thì manifest vào một namespace còn secret vào
    namespace khác: apply-secrets vẫn báo thành công, orchestrator vẫn xanh, chỉ có pod
    là không kéo nổi ảnh vì thiếu secret kéo ảnh.
    """
    pattern = CONFIG.get("kubernetes.namespace_pattern", "{app}-{env}") or "{app}-{env}"
    return pattern.replace("{app}", app).replace("{env}", env)


def config_int(key: str, default: int) -> int:
    """An integer from config, where a configured 0 MEANS 0.

    `int(CONFIG.get(key) or default)` reads naturally and is wrong: 0 is falsy, so setting
    a timeout to zero — "do not wait, fail immediately" — silently gets the default back,
    and the only symptom is a command that hangs for ten minutes instead of failing.
    """
    value = CONFIG.get(key, default)
    if value is None or value == "":
        return default
    return int(value)


def poll_interval(default: int) -> int:
    """Giây chờ GIỮA hai lần poll trong một vòng chờ (rollout, VSO sync, base backup…).

    Tách khỏi `timeout` của vòng lặp một cách có chủ ý: `timeout` là tổng thời gian sẵn
    sàng chờ thực tại hội tụ, còn interval chỉ là tần suất kiểm lại — trộn hai thứ làm một
    hằng số là lý do bộ test "logic thuần" tốn ~70s ngồi `sleep` thật trong các vòng verify.
    Vận hành có thể chỉnh `verify.poll_interval_seconds` trong platform.env.yaml; biến môi
    trường IDP_POLL_INTERVAL_SECONDS đè lên cả hai — escape hatch phạm vi tiến trình để
    conftest của test đặt về 0 mà không test riêng lẻ nào phải biết mình chạm vòng lặp nào.
    """
    override = os.environ.get("IDP_POLL_INTERVAL_SECONDS")
    if override is not None and override != "":
        return int(override)
    return config_int("verify.poll_interval_seconds", default)


def feature(name: str) -> bool:
    """Is an opt-in capability switched on for this platform install?

    Everything the secret/onboarding programme adds sits behind one of these. The reason
    is not caution for its own sake: this platform already deploys real apps to staging,
    and a new code path that only *usually* behaves like the old one is indistinguishable
    from the old one right up until the deploy it breaks. Off by default means the blast
    radius of a bug is the apps that opted in, not every app at once.
    """
    return bool(CONFIG.get(f"features.{name}", False))


DATABASE_BACKENDS = ("cnpg", "statefulset")


def database_backend() -> str:
    """Which implementation serves `class: application` — validated, with a safe default.

    An unknown value is fatal rather than silently falling back: a typo like `statefull`
    would otherwise render the CNPG default while the operator that config claims to be
    avoiding is nowhere on the cluster — exactly the silent-wrong-place failure the whole
    config layer exists to prevent.
    """
    value = str(CONFIG.get("database.backend") or "cnpg").strip()
    if value not in DATABASE_BACKENDS:
        raise SystemExit(
            f"database.backend={value!r} is not one of {DATABASE_BACKENDS}. "
            "Leave it unset for the CloudNativePG default, or set 'statefulset' for a "
            "plain platform-owned PostgreSQL StatefulSet."
        )
    return value


# --------------------------------------------------------------------------------------
# naming and path contracts
# --------------------------------------------------------------------------------------
# Everything in this section is a pure function of its arguments plus platform.env.yaml.
# They are grouped here because they are CONTRACTS: their output ends up in Kubernetes
# object names, Vault paths and promotion records, so changing one silently renames live
# resources or moves a secret out from under a running app. Change them only with a
# migration, never as a refactor.

# DNS-1123 label, which is what a Kubernetes object name and a Vault path segment can both
# safely be. Anchored on purpose: a partial match would let "stripe/../../admin" through
# on the strength of the "stripe" prefix.
DNS_LABEL = re.compile(r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$")


def validate_secret_name(name: str) -> str:
    """Gate on the ONE app-supplied component of a Vault path.

    An app declares `secretRef: {name: stripe}` and the platform derives the full path
    from it. That makes `name` the only attacker-controlled segment, so it gets checked
    against an allowlist rather than scanned for bad characters: `/` and `..` are the
    obvious traversals, but so are a leading `-`, an empty string, and unicode that
    normalises to a separator. Anything not matching a plain DNS label is refused.
    """
    if not isinstance(name, str) or not DNS_LABEL.match(name):
        raise SystemExit(
            f"invalid secret name {name!r}. Use a DNS-style name: lowercase letters, "
            "digits and '-', starting and ending alphanumeric, at most 63 characters. "
            "'/' and '..' are refused because the platform derives the Vault path from "
            "this value."
        )
    return name


def validate_environment(env: str) -> str:
    if env not in ENVIRONMENTS:
        raise SystemExit(
            f"unknown environment {env!r}. This platform has exactly two: "
            f"{', '.join(ENVIRONMENTS)}. ('production' is not an alias for 'prod' — "
            "one spelling only, so a values block cannot quietly never apply.)"
        )
    return env


def vault_path(app: str, env: str, name: str) -> str:
    """Where a logical secret lives in Vault. Derived, never taken from the app.

    Returns the path WITHOUT the KV-v2 `/data/` infix — that is a wire-format detail VSO
    adds itself, and baking it in here would break kv-v1 mounts and double up on v2.
    """
    validate_environment(env)
    validate_secret_name(name)
    validate_secret_name(app)
    mount = CONFIG.get("vault.kv_mount") or "kv"
    template = CONFIG.get("vault.path_template") or "apps/{application}/{environment}/{name}"
    path = (template
            .replace("{application}", app)
            .replace("{environment}", env)
            .replace("{name}", name))
    if "{" in path or "}" in path:
        raise SystemExit(
            f"vault.path_template has an unknown placeholder: {template!r}. "
            "Only {application}, {environment} and {name} are substituted."
        )
    return f"{mount}/{path}"


def vault_prefix_for(app: str, env: str) -> str:
    """Tiền tố (không có mount) chứa MỌI bí mật của một app/env.

    Đúng ranh giới mà policy của app vẽ ra, nên nó cũng là ranh giới an toàn duy nhất cho
    một thao tác xoá: rộng hơn một ký tự là chạm sang app khác.
    """
    leaf = vault_relative_path(app, env, "x")
    return leaf[: -len("x")].rstrip("/")


def vault_relative_path(app: str, env: str, name: str) -> str:
    """The same path WITHOUT the mount prefix.

    VSO takes mount and path as two separate fields, so the CR needs the path relative to
    the mount while a `vault kv` command and every policy need the full one. Deriving both
    from `vault_path` keeps them from drifting apart.
    """
    full = vault_path(app, env, name)
    mount = CONFIG.get("vault.kv_mount") or "kv"
    return full[len(mount) + 1:]


def _slug(text: str) -> str:
    return re.sub(r"-+", "-", re.sub(r"[^a-z0-9]+", "-", str(text).lower())).strip("-")


def resource_name(*parts: str, prefix: str = "idp") -> str:
    """A DNS-safe, <=63 character, collision-resistant name for a generated object.

    Two constraints pull against each other. Names must be READABLE — when a
    VaultStaticSecret is stuck, whoever is paged has to see which app, environment and
    workload it belongs to without cross-referencing anything. And they must be UNIQUE and
    STABLE — 63 characters is the hard limit for a label value, so long inputs get
    truncated, and truncation alone would map two different apps onto one name.

    So: readable prefix, truncated, plus a short SHA-256 of the FULL tuple. The hash is
    over the untruncated input, so it still separates names whose readable parts collided.

    Not Python's hash(): it is salted per process (PYTHONHASHSEED), so the same input
    produces a different name on every run — which is exactly the resource-churn bug the
    state store exists to prevent, reintroduced through the back door.
    """
    digest = hashlib.sha256("\x1f".join(str(p) for p in parts).encode()).hexdigest()[:8]
    body = "-".join(x for x in [_slug(prefix)] + [_slug(p) for p in parts] if x)
    body = body[: 63 - len(digest) - 1].rstrip("-")
    return f"{body}-{digest}" if body else digest


def canonical_json(payload) -> str:
    """The one serialisation used for anything that gets hashed.

    Sorted keys and no incidental whitespace, so a digest describes the DATA and not the
    file it happened to be typed into. Re-indenting a YAML file, reordering its keys or
    adding a comment must not read as a configuration change.
    """
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def values_digest(spec: dict, env: str = "prod") -> str:
    """Fingerprint of the values an environment will actually be rendered with.

    This is what makes a fast promotion honest. `promote --mode tag-only` rewrites image
    tags in an already-rendered manifest without re-running the renderer, which is quick
    and reproducible — but it also means an edit to the prod values block would NOT reach
    production, while the promotion still reports success. Recording this digest at render
    time and comparing it at promote time turns that silent skip into a refusal.

    Includes secretRef metadata (name and key) because changing which Vault secret feeds a
    variable is a real configuration change. Never includes a secret VALUE — the renderer
    does not have one.
    """
    return hashlib.sha256(canonical_json({
        "application": (spec or {}).get("application") or {},
        "environment": ((spec or {}).get("environments") or {}).get(env) or {},
    }).encode()).hexdigest()
# --------------------------------------------------------------------------------------
# toolchain pinning
# --------------------------------------------------------------------------------------
# score-k8s decides the SHAPE of every manifest this platform produces. Two runners on two
# versions render the same app commit into two different manifests, with no error anywhere
# — one deploy simply changes something nobody edited. Pinning turns that into a refusal.
PINNED_TOOLS = {
    "score-k8s": "ci.score_k8s_version",
    "score-compose": "ci.score_compose_version",
}

# Matches 0.15.0 and 1.2.3-rc1, but not the go1.26.4 on the same line: there is no word
# boundary between "go" and "1", so the compiler version cannot be mistaken for the tool's.
_SEMVER = re.compile(r"\b(\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?)\b")

_version_checked: set[str] = set()


def tool_version(tool: str) -> str | None:
    """Version `tool --version` reports, or None if it is absent or unparseable."""
    if not shutil.which(tool):
        return None
    cp = run([tool, "--version"], check=False, capture=True)
    first = ((cp.stdout or "") + (cp.stderr or "")).strip().splitlines()
    match = _SEMVER.search(first[0]) if first else None
    return match.group(1) if match else None


def check_tool_versions(tools: list[str], *, force: bool = False) -> None:
    """Fail unless each tool matches the version pinned in platform.env.yaml.

    An EMPTY pin disables the check for that tool. That is deliberate rather than lax:
    this platform is already deploying real apps from runners nobody has re-provisioned,
    and a version check that fails closed on first upgrade would take those apps down to
    enforce a policy they predate. Real environments should pin; the empty default is the
    brownfield on-ramp, and preflight says out loud when it is taking it.
    """
    for tool in tools:
        want = str(CONFIG.get(PINNED_TOOLS[tool]) or "").strip()
        if not want:
            log(f"{tool}: no version pinned ({PINNED_TOOLS[tool]} is empty) — check skipped")
            continue
        if tool in _version_checked and not force:
            continue
        have = tool_version(tool)
        if have is None:
            raise SystemExit(
                f"{tool} is pinned to {want} in platform.env.yaml but its version could "
                f"not be determined ({'not on PATH' if not shutil.which(tool) else 'unparseable --version output'})."
            )
        if have != want:
            raise SystemExit(
                f"{tool} version mismatch: runner has {have}, platform.env.yaml pins "
                f"{PINNED_TOOLS[tool]}={want}. Rendering with the wrong version silently "
                "changes manifest shape. Install the pinned version on this runner, or "
                "update the pin and re-run the full test suite."
            )
        _version_checked.add(tool)
        log(f"{tool} {have} matches pinned {PINNED_TOOLS[tool]}")


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
    sensitive: tuple[str, ...] = (),
) -> subprocess.CompletedProcess:
    """Run a command, logging replayable argv with explicit secret values redacted.

    Bash gives you `set -x` for free; in Python you have to be deliberate about it. Every
    external call is logged so a failed run reads like a transcript you can replay. A few
    kubectl create commands still have no stdin form convenient for hand replay, so their
    callers MUST name credential values in ``sensitive`` rather than leak them here.
    """
    shown = list(argv)
    for secret in sensitive:
        if secret:
            shown = [part.replace(secret, "<redacted>") for part in shown]
    log(f"$ {' '.join(shown)}" + (f"   (cwd={cwd})" if cwd else ""))
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
