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
    "ingress": {"gateway_name": "", "gateway_namespace": ""},
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
    "onboarding": {
        # Bản ghi state của một lần onboarding. Tên ConfigMap là quy ước đặt tên, tức là
        # thứ một công ty có thể đã có luật riêng.
        "state_configmap_pattern": "idp-onboarding-{app}",
        # Mức hiển thị routing được phép trong request. Catalog này chỉ phát hành một
        # Gateway nội bộ; thêm mức mới nghĩa là thêm gateway, nên nó là cấu hình chứ
        # không phải một nhánh if trong code.
        "visibilities": ["internal"],
        # Rỗng = ai cũng onboard được. Công ty nên liệt kê các đội được phép (mục 13.5).
        "allowed_owners": [],
        "verify_timeout_seconds": 420,
    },
    # Every capability added by the secret/onboarding programme is off until switched on
    # per environment. The existing platform must render byte-identically with these unset.
    "features": {
        "application_values": False,
        "vault_secrets": False,
        "postgres_application": False,
        "stack_onboarding": False,
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


def feature(name: str) -> bool:
    """Is an opt-in capability switched on for this platform install?

    Everything the secret/onboarding programme adds sits behind one of these. The reason
    is not caution for its own sake: this platform already deploys real apps to staging,
    and a new code path that only *usually* behaves like the old one is indistinguishable
    from the old one right up until the deploy it breaks. Off by default means the blast
    radius of a bug is the apps that opted in, not every app at once.
    """
    return bool(CONFIG.get(f"features.{name}", False))


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

    body = yaml.safe_dump(literals, sort_keys=True, default_flow_style=False,
                          allow_unicode=True) if literals else "{}\n"
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
        f"# GENERATED by orchestrate.py for {app}/{env} — do not edit, do not commit.\n"
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
        f"# GENERATED by orchestrate.py — Vault policy for {app}/{env} "
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
    catalog = Path(catalog or Path(__file__).resolve().parent)
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
        "# Sinh tự động bởi orchestrate.py. Sửa tay được — lần render sau sẽ không ghi đè.\n"
        f"namespace: {ns}\n"
        f"defaultNamespace: {ns}\n"
    )
    log(f"sinh {f} (namespace {ns})")


def repo_is_private(url: str) -> bool | None:
    """Kho có riêng tư không? None nghĩa là không hỏi được.

    Dùng để phân biệt hai trường hợp trông giống nhau khi thiếu credential: kho công khai
    thì Fleet clone ẩn danh vẫn được, kho riêng tư thì chắc chắn hỏng.
    """
    m = re.search(r"[:/]([^/]+)/([^/]+?)(?:\.git)?$", url)
    if not m:
        return None
    cp = run(["gh", "api", f"repos/{m.group(1)}/{m.group(2)}", "--jq", ".private"],
             check=False, capture=True)
    return {"true": True, "false": False}.get(cp.stdout.strip())


def cmd_ensure_gitrepo(args) -> None:
    """Tạo GitRepo của Fleet nếu chưa có. KHÔNG BAO GIỜ ghi đè cái đang có.

    Đây là lỗi im lặng số một của cả hệ thống: quên tạo GitRepo thì orchestrator xanh
    toàn tập, manifest nằm đúng trong repo cấu hình, mà cụm trống trơn — vì không ai kéo
    về. Tự tạo ở đây thì khỏi phải nhớ.

    Vì sao không ghi đè: cụm thường đã có GitRepo của đội khác. Đặt trùng tên rồi apply
    là ĐÈ LÊN họ, và ứng dụng của họ ngừng đồng bộ trong im lặng. Nên: thiếu thì tạo,
    có rồi mà trỏ đúng kho thì để yên, trỏ kho KHÁC thì dừng và báo.
    """
    name = f"{args.app}-{args.env}"
    ns = CONFIG.get("kubernetes.fleet_namespace", "fleet-local") or "fleet-local"
    branch = CONFIG.get(f"environments.{args.env}.config_branch") \
        or CONFIG.get("git.default_branch", "main")

    # Địa chỉ kho lấy từ chính bản checkout, không dựng lại từ mẫu tên — dựng lại là
    # thêm một chỗ có thể lệch với thực tế.
    url = run(["git", "remote", "get-url", "origin"],
              cwd=Path(args.config_dir), capture=True).stdout.strip()
    url = re.sub(r"^https://[^@]+@", "https://", url)      # bỏ token nếu remote có nhúng
    url = re.sub(r"\.git$", "", url)

    cp = kubectl(["get", "gitrepo", name, "-n", ns, "-o", "json"],
                 kubeconfig=args.kubeconfig, check=False, capture=True)
    if cp.returncode == 0:
        cur = (json.loads(cp.stdout).get("spec") or {}).get("repo", "")
        if re.sub(r"\.git$", "", cur) == url:
            log(f"GitRepo {ns}/{name} đã có, trỏ đúng {url} -> giữ nguyên")
            return
        raise SystemExit(
            f"GitRepo {ns}/{name} đã tồn tại nhưng trỏ tới '{cur}', không phải '{url}'. "
            "Nó có thể là của ứng dụng khác — apply đè lên sẽ làm ứng dụng đó ngừng đồng "
            "bộ mà không báo gì. Đổi tên app, hoặc xoá GitRepo cũ nếu chắc chắn nó thừa."
        )

    # Thông tin đăng nhập git cho Fleet: KHÔNG áp đặt một tên mặc định.
    # Khai một tên secret không tồn tại thì Fleet không clone được — mà lỗi đó nằm trong
    # status của GitRepo, không ai nhìn, và triệu chứng lại y hệt "quên tạo GitRepo".
    # Thứ tự: lấy từ cấu hình nếu có khai; không thì HỌC THEO các GitRepo đang chạy trên
    # cùng namespace; không có gì để học thì bỏ trống, để Fleet tự xoay như nó vẫn làm với
    # kho công khai hoặc secret mặc định của cụm.
    secret = CONFIG.get("kubernetes.fleet_git_secret") or ""

    # Đã có ai đăng ký chỗ này dưới TÊN KHÁC chưa? Bản cài cũ thường đặt tên không kèm
    # môi trường (ví dụ `demo` thay vì `demo-staging`). Tạo thêm một cái nữa thì hai
    # GitRepo cùng đồng bộ một thư mục, sinh hai Bundle chồng nhau — không hỏng ngay
    # nhưng rối và rất khó truy khi cần gỡ.
    cp = kubectl(["get", "gitrepo", "-n", ns, "-o", "json"],
                 kubeconfig=args.kubeconfig, check=False, capture=True)
    if cp.returncode == 0:
        hang_xom = []
        for item in (json.loads(cp.stdout).get("items") or []):
            spec = item.get("spec") or {}
            if (re.sub(r"\.git$", "", spec.get("repo", "")) == url
                    and args.env in (spec.get("paths") or [])):
                log(f"kho này đã được đăng ký dưới tên {item['metadata']['name']} "
                    f"-> không tạo thêm {name}")
                return
            if spec.get("clientSecretName"):
                hang_xom.append((item["metadata"]["name"], spec["clientSecretName"]))
        if not secret and hang_xom:
            ten, secret = hang_xom[0]
            log(f"học theo GitRepo {ten}: dùng clientSecretName={secret}")

    body = {
        "apiVersion": "fleet.cattle.io/v1alpha1",
        "kind": "GitRepo",
        "metadata": {"name": name, "namespace": ns},
        "spec": {"repo": url, "branch": branch, "paths": [args.env],
                 "pollingInterval": "15s"},
    }
    if secret:
        body["spec"]["clientSecretName"] = secret
    elif repo_is_private(url):
        # Kho riêng tư + không có credential = Fleet clone ẩn danh và hỏng CHẮC CHẮN với
        # "authentication required: Anonymous access denied". Nhưng nó hỏng ở status của
        # GitRepo, không ai nhìn, và triệu chứng y hệt "quên tạo GitRepo": cụm trống trơn.
        # Dừng ngay ở đây thì lỗi hiện ra đúng chỗ, đúng lúc, kèm cách sửa.
        raise SystemExit(
            f"kho cấu hình {url} là kho RIÊNG TƯ nhưng không tìm được thông tin đăng nhập "
            "git nào cho Fleet.\n"
            "Fleet sẽ clone ẩn danh và hỏng với 'Anonymous access denied' — cụm trống trơn "
            "trong khi mọi bước ở đây báo xanh.\n\n"
            "Cách sửa: tạo secret trong namespace của Fleet rồi khai tên nó vào "
            "kubernetes.fleet_git_secret:\n"
            "  kubectl -n <fleet-ns> create secret generic git-creds-idp \\\n"
            "    --type=kubernetes.io/basic-auth \\\n"
            "    --from-literal=username=<tài-khoản> --from-literal=password=<token>\n"
            "Lưu ý kiểu secret phải khớp địa chỉ kho: https -> basic-auth, ssh -> ssh-auth."
        )
    else:
        log(f"{url} là kho công khai và không có clientSecretName -> Fleet clone ẩn danh")
    tmp = Path(args.work or ".") / f"gitrepo-{name}.json"
    tmp.parent.mkdir(parents=True, exist_ok=True)
    tmp.write_text(json.dumps(body))
    _tolerate_exists(
        kubectl(["create", "-f", str(tmp)], kubeconfig=args.kubeconfig,
                check=False, capture=True),
        f"GitRepo {ns}/{name} -> {url} nhánh {branch} thư mục {args.env}",
    )


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


def cmd_apply_secrets(args) -> None:
    ns = app_namespace(args.app, args.env)
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
        return None
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
        # Trả về, không chỉ in: máy trạng thái onboarding cần chính URL này để ghi vào
        # state và dừng ở PENDING_PROD_APPROVAL. Đọc lại stdout của chính mình là cách
        # nhanh nhất để hai bên lệch nhau khi có ai đó thêm một dòng log.
        return url

    # Push EXPLICITLY to the branch we validated, never a bare `git push`. A bare push
    # depends on tracking configuration: a branch checked out without an upstream fails
    # with "no upstream branch", which the retry below then misreads as "somebody pushed
    # first" and sends into a rebase that cannot work. Naming the target also removes any
    # chance of publishing to whatever branch tracking happens to point at.
    for attempt in (1, 2, 3):
        if run(["git", "push", "origin", f"HEAD:{base}"],
               cwd=config, check=False).returncode == 0:
            log(f"pushed to {base}")
            return None
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

    if args.mode in ("from-staging", "tag-only"):
        # Both modes skip the renderer, so both can silently ship stale prod values.
        guard_prod_values(args)

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


def cmd_verify(args) -> None:
    """Chờ tới khi cụm THỰC SỰ chạy đúng thứ vừa render. Hết giờ là fail kèm chẩn đoán.

    Lý do có bước này, đo từ một sự cố thật: đổi cách đặt tên ảnh làm 5 app cùng
    ImagePullBackOff, mà TOÀN BỘ pipeline vẫn báo thành công — CI xanh, orchestrator xanh,
    manifest ghi đúng vào config repo, Fleet apply không lỗi. Chỉ pod là chết. Không ai
    phát hiện cho tới khi có người mở trang web.

    Bước đối chiếu trước đó chỉ so SHA giữa config repo và nhánh app — nó không nhìn vào
    cụm, nên không thể bắt được loại lỗi này. Đây là chỗ duy nhất trong toàn luồng thực sự
    hỏi "ứng dụng có chạy không".
    """
    ns = app_namespace(args.app, args.env)
    docs = load_all(Path(args.manifests))

    # Secrets first, and on purpose. A workload whose Secret has not synced sits in
    # CreateContainerConfigError, which the rollout check below reports as "0/1 replicas
    # ready" — true, useless, and it sends whoever is paged to look at the image. Checking
    # the VaultStaticSecret first turns the same failure into "Vault path X, reason Y".
    wait_for_vault_secrets(docs, ns, args)
    # Then the database, for the same reason: an app whose database has not finished
    # bootstrapping crash-loops on connection refused, and the rollout check would report
    # that as "0 replicas ready" without ever mentioning the database.
    wait_for_databases(docs, ns, args)

    want: dict[str, list[str]] = {}
    for doc in docs:
        if doc.get("kind") != "Deployment":
            continue
        containers = doc.get("spec", {}).get("template", {}).get("spec", {}).get("containers", [])
        want[doc["metadata"]["name"]] = [c.get("image") for c in containers]

    if not want:
        log("không có Deployment nào để kiểm — bỏ qua")
        return


    log(f"chờ {len(want)} Deployment trong {ns} chạy đúng ảnh vừa render "
        f"(tối đa {args.timeout}s)")
    deadline = time.time() + args.timeout
    while True:
        pending = []
        for name, images in sorted(want.items()):
            cp = kubectl(["get", "deploy", name, "-n", ns, "-o", "json"],
                         kubeconfig=args.kubeconfig, check=False, capture=True)
            if cp.returncode != 0:
                pending.append(f"{name}: chưa tồn tại trên cụm")
                continue
            obj = json.loads(cp.stdout)
            live = [c.get("image") for c in
                    obj["spec"]["template"]["spec"].get("containers", [])]
            if live != images:
                pending.append(f"{name}: đang chạy {live}, cần {images}")
                continue
            # Đủ pod sẵn sàng KHÔNG có nghĩa là bản mới đã lên. Đo được từ một lần thử
            # cố ý: đặt nhãn ảnh không tồn tại thì Kubernetes tạo pod mới, pod đó
            # ImagePullBackOff, còn 3 pod CŨ vẫn chạy nguyên. availableReplicas vẫn là
            # 3/3 nên phép kiểm cũ báo xanh — đúng cái sự cố mà bước này sinh ra để bắt.
            # Phải hỏi "bản mới đã thay xong bản cũ chưa", tức đúng câu hỏi mà
            # `kubectl rollout status` hỏi:
            st = obj.get("status") or {}
            need = (obj.get("spec") or {}).get("replicas", 1) or 1
            gen = (obj.get("metadata") or {}).get("generation", 0) or 0
            observed = st.get("observedGeneration", 0) or 0
            updated = st.get("updatedReplicas", 0) or 0
            avail = st.get("availableReplicas", 0) or 0
            total = st.get("replicas", 0) or 0
            if observed < gen:
                pending.append(f"{name}: Kubernetes chưa xử lý bản sửa mới nhất")
            elif updated < need:
                pending.append(
                    f"{name}: mới {updated}/{need} bản sao chạy phiên bản MỚI "
                    f"(còn {total - updated} bản sao cũ đang phục vụ)")
            elif total > updated:
                pending.append(
                    f"{name}: bản cũ chưa được thu hồi ({total - updated} bản sao thừa)")
            elif avail < need:
                pending.append(f"{name}: mới {avail}/{need} bản sao sẵn sàng")
        if not pending:
            log(f"tất cả {len(want)} Deployment trong {ns} đã chạy đúng ảnh vừa render")
            return
        if time.time() >= deadline:
            break
        time.sleep(10)

    # Hết giờ: in đúng thứ cần để hiểu vì sao, ngay tại chỗ.
    warn(f"sau {args.timeout}s vẫn chưa đạt trạng thái mong muốn trong {ns}")
    for line in pending:
        print(f"::error::{line}", file=sys.stderr, flush=True)
    kubectl(["get", "pods", "-n", ns], kubeconfig=args.kubeconfig, check=False)
    kubectl(["get", "events", "-n", ns, "--sort-by=.lastTimestamp"],
            kubeconfig=args.kubeconfig, check=False)
    raise SystemExit(
        f"{args.app}/{args.env}: manifest đã ghi và Fleet đã đồng bộ, nhưng cụm KHÔNG "
        "chạy đúng thứ vừa render. Xem danh sách pod và event ở trên."
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
                "`orchestrate.py vault-foundation --apply` with cluster-admin first."
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
orchestrate.py vault-onboard --app {app} --env {env} --print-policy \\
  | vault policy write {read_policy} -
orchestrate.py vault-onboard --app {app} --env {env} --print-policy --write \\
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


# =======================================================================================
# PHASE 6 — onboarding: một request, một máy trạng thái, chạy lại được
# =======================================================================================
# Mục 13 của kế hoạch. Điều cần giữ trong đầu khi đọc phần này:
#
# 1. MỖI BƯỚC KIỂM TRƯỚC KHI TẠO, và ghi state NGAY khi tạo xong. Một lần chạy hỏng ở
#    bước 7 rồi chạy lại phải TIẾP TỤC từ bước 7, không được tạo kho thứ hai, namespace
#    thứ hai hay một mật khẩu database mới đè lên cái database đang dùng.
# 2. TRẠNG THÁI NẰM NGOÀI TIẾN TRÌNH. Nó là một ConfigMap trong cụm (hoặc một file khi
#    chạy offline), nên một lần chạy khác — máy khác, người khác — nhìn thấy đúng thứ
#    lần trước để lại. Không dùng Secret: bản ghi này cố ý KHÔNG chứa giá trị bí mật nào.
# 3. KHÔNG BÁO READY SAI. Thiếu bí mật của bên thứ ba thì trạng thái là
#    WAITING_FOR_USER_SECRETS kèm đúng lệnh phải chạy — không phải một lần verify chờ hết
#    giờ rồi fail với "0/1 replicas ready", vốn gửi người trực đi soi image.
# 4. HAI NỬA QUYỀN, HAI CHỦ SỞ HỮU (mục 13.5). Thao tác GitHub chạy bằng danh tính người
#    dùng (`gh`); thao tác Vault cần token Vault RIÊNG. Không suy ra cái này từ cái kia:
#    quyền tạo repo không được kéo theo quyền viết policy trên Vault.
ONBOARD_API_VERSION = "idp.company/v1"
ONBOARD_KIND = "OnboardingRequest"

# Máy trạng thái ở mục 13.2. Thứ tự là thứ tự thật — engine đi tuần tự và không nhảy cóc.
ONBOARD_STATES = (
    "REQUESTED", "VALIDATING", "SCAFFOLDING_REPOSITORY", "BOOTSTRAPPING_PLATFORM",
    "CONFIGURING_VAULT", "PROVISIONING_DATABASE", "BUILDING_IMAGES", "DEPLOYING_STAGING",
    "VERIFYING_STAGING", "STAGING_READY", "PENDING_PROD_ACTIVATION", "PROVISIONING_PROD",
    "PENDING_PROD_APPROVAL", "VERIFYING_PROD", "READY",
)
# Nhánh tuỳ chọn: không phải lỗi, nhưng cũng KHÔNG phải READY.
ONBOARD_BRANCH_STATES = ("WAITING_FOR_USER_SECRETS", "PARTIALLY_READY", "FAILED_RETRYABLE")
# Vòng đời XOÁ (mục 13.4). Cố ý tách khỏi ONBOARD_STATES: xoá không phải "bước tiếp theo"
# của onboarding, nó là một workflow riêng có preview và có người duyệt.
ONBOARD_DELETE_STATES = ("DELETE_PLANNED", "PENDING_DELETE_APPROVAL", "DELETING", "DELETED")


class OnboardingPaused(Exception):
    """Dừng đúng chỗ, có trạng thái riêng, và chạy lại được — không phải lỗi.

    Hai chỗ dùng: thiếu bí mật của người dùng, và chờ người duyệt pull request prod. Cả
    hai đều là "đang chờ CON NGƯỜI", nên biến chúng thành lỗi sẽ dạy người vận hành bỏ
    qua lỗi của công cụ này.
    """

    def __init__(self, state: str, message: str):
        super().__init__(message)
        self.state = state
        self.message = message


def onboarding_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


# ------------------------------------------------------------------------ request (13.1)
def _req_block(doc: dict, key: str, where: str) -> dict:
    value = doc.get(key)
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise SystemExit(f"{where}: '{key}' phải là một mapping, đang là {type(value).__name__}.")
    return value


def _req_bool(value, key: str, where: str, default: bool) -> bool:
    if value is None:
        return default
    if not isinstance(value, bool):
        raise SystemExit(
            f"{where}: '{key}' phải là true/false, đang là {value!r}. Chuỗi \"false\" là "
            "một giá trị ĐÚNG trong YAML nên nó sẽ được hiểu là bật."
        )
    return value


def validate_onboarding_request(doc, where: str, catalog=None) -> dict:
    """Kiểm request onboarding và trả về bản đã chuẩn hoá (mục 13.1).

    Khoá lạ bị TỪ CHỐI chứ không bỏ qua. Một request là thứ người ta gõ tay một lần rồi
    quên; gõ nhầm `enviroments:` mà công cụ im lặng thì app được dựng thiếu prod và không
    ai biết cho tới lúc cần lên production.
    """
    if not isinstance(doc, dict):
        raise SystemExit(f"{where}: request phải là một YAML mapping.")
    known = {"apiVersion", "kind", "application", "stack", "database", "routing",
             "environments"}
    unknown = sorted(set(doc) - known)
    if unknown:
        raise SystemExit(
            f"{where}: khoá không nhận ra: {', '.join(unknown)}. "
            f"Được phép: {', '.join(sorted(known))}."
        )
    if doc.get("apiVersion") not in (None, ONBOARD_API_VERSION):
        raise SystemExit(f"{where}: apiVersion phải là {ONBOARD_API_VERSION!r}.")
    if doc.get("kind") not in (None, ONBOARD_KIND):
        raise SystemExit(f"{where}: kind phải là {ONBOARD_KIND!r}.")

    application = _req_block(doc, "application", where)
    name = validate_app_name(str(application.get("name") or ""))
    owner = str(application.get("owner") or "").strip()
    if not owner:
        raise SystemExit(
            f"{where}: application.owner là bắt buộc. Không có chủ sở hữu thì không ai "
            "nhận được cảnh báo, không ai duyệt được prod, và không ai xoá được app."
        )
    allowed_owners = CONFIG.get("onboarding.allowed_owners") or []
    if allowed_owners and owner not in allowed_owners:
        raise SystemExit(
            f"{where}: đội {owner!r} không nằm trong onboarding.allowed_owners của "
            "platform.env.yaml. Đây là chỗ kiểm quyền onboarding (mục 13.5) — sửa cấu "
            "hình, đừng sửa request."
        )

    stack_block = _req_block(doc, "stack", where)
    stack_id = str(stack_block.get("id") or "").strip()
    if not stack_id:
        raise SystemExit(f"{where}: stack.id là bắt buộc (xem `orchestrate.py stack-list`).")
    stack = load_stack(catalog or Path(__file__).resolve().parent, stack_id)
    published = str((stack.get("metadata") or {}).get("version"))
    wanted = str(stack_block.get("version") or "").strip()
    if not wanted:
        raise SystemExit(
            f"{where}: stack.version là bắt buộc. Bỏ trống nghĩa là 'phiên bản nào cũng "
            f"được', và app sẽ được sinh ra từ một bộ file khác nhau tuỳ ngày chạy. "
            f"Catalog này phát hành {stack_id} v{published}."
        )
    if wanted != published:
        raise SystemExit(
            f"{where}: xin stack {stack_id} v{wanted} nhưng catalog này phát hành "
            f"v{published}. Sửa request, hoặc dùng một catalog khác — đừng để hai bên lệch."
        )

    database = _req_block(doc, "database", where)
    has_capability = "database" in ((stack.get("spec") or {}).get("capabilities") or [])
    enabled = _req_bool(database.get("enabled"), "database.enabled", where, has_capability)
    if enabled != has_capability:
        raise SystemExit(
            f"{where}: database.enabled={enabled} nhưng stack {stack_id} "
            f"{'có' if has_capability else 'KHÔNG có'} capability `database`. Capability là "
            "thuộc tính của stack, không phải một công tắc theo app — chọn stack khác "
            "(`stack-list`) thay vì đổi cờ này."
        )
    profile = str(database.get("profile") or "application")
    if enabled and profile != "application":
        raise SystemExit(
            f"{where}: database.profile={profile!r} không được hỗ trợ. Platform này chỉ "
            "phát hành `application` — class cũ chỉ dùng để chạy thử và bị chặn ở prod."
        )

    routing = _req_block(doc, "routing", where)
    visibility = str(routing.get("visibility") or "internal")
    allowed_vis = CONFIG.get("onboarding.visibilities") or ["internal"]
    if visibility not in allowed_vis:
        raise SystemExit(
            f"{where}: routing.visibility={visibility!r} không nằm trong "
            f"onboarding.visibilities ({', '.join(allowed_vis)}). Catalog này chỉ có một "
            "Gateway; muốn thêm mức hiển thị khác thì thêm gateway + khai vào cấu hình, "
            "đừng nới ở đây."
        )

    envs_block = _req_block(doc, "environments", where)
    bad = sorted(set(envs_block) - set(ENVIRONMENTS))
    if bad:
        raise SystemExit(
            f"{where}: môi trường không tồn tại: {', '.join(bad)}. "
            f"Platform có đúng {', '.join(ENVIRONMENTS)}."
        )
    environments = {e: _req_bool(envs_block.get(e), f"environments.{e}", where, e == "staging")
                    for e in ENVIRONMENTS}
    if not environments["staging"]:
        raise SystemExit(
            f"{where}: environments.staging phải là true. Prod chỉ nhận ảnh ĐÃ verify ở "
            "staging, nên một app chỉ-prod không có đường nào hợp lệ để lên."
        )
    for env in ENVIRONMENTS:
        if environments[env] and enabled and not CONFIG.get(
                f"database_profiles.{env}.{profile}"):
            raise SystemExit(
                f"{where}: xin database ở {env} nhưng platform.env.yaml không có "
                f"database_profiles.{env}.{profile}."
            )

    return {
        "application": {"name": name, "owner": owner,
                        "description": str(application.get("description") or "")},
        "stack": {"id": stack_id, "version": published},
        "database": {"enabled": enabled, "profile": profile},
        "routing": {"visibility": visibility},
        "environments": environments,
    }


def load_onboarding_request(path, catalog=None) -> dict:
    p = Path(path)
    if not p.is_file():
        raise SystemExit(f"không thấy file request: {p}")
    try:
        doc = yaml.safe_load(p.read_text())
    except yaml.YAMLError as exc:
        raise SystemExit(f"{p}: YAML hỏng — {exc}") from None
    return validate_onboarding_request(doc, str(p), catalog)


def onboarding_idempotency_key(request: dict) -> str:
    """Băm của CHÍNH request đã chuẩn hoá (mục 13.4).

    Cùng một file chạy lại = cùng khoá = tiếp tục bản ghi cũ. Sửa request rồi chạy lại =
    khoá khác, và engine DỪNG thay vì âm thầm dựng lại một app đang chạy theo hình dạng
    mới. Đổi stack version của một app đang sống là một cuộc nâng cấp có pull request
    (`stack-upgrade`), không phải một lần onboarding thứ hai.
    """
    return hashlib.sha256(canonical_json(request).encode()).hexdigest()[:32]


def onboarding_request_id(request: dict) -> str:
    return f"ob-{request['application']['name']}-{onboarding_idempotency_key(request)[:8]}"


def onboarding_labels(request: dict, *, env: str = "") -> dict:
    """Nhãn mục 13.4 gắn lên mọi tài nguyên onboarding tạo ra.

    Có nhãn thì "cái này của ai, thuộc lần onboarding nào" trả lời được bằng một lệnh
    `kubectl get -l`, kể cả khi bản ghi state đã mất.
    """
    labels = {
        "app.kubernetes.io/part-of": "idp-platform",
        "idp.platform/application": request["application"]["name"],
        "idp.platform/stack-version": str(request["stack"]["version"]),
        "idp.platform/onboarding-request-id": onboarding_request_id(request),
    }
    if env:
        labels["idp.platform/environment"] = env
    return labels


# --------------------------------------------------------------- bản ghi state và audit
# Vì sao là ConfigMap chứ không phải Secret: bản ghi này CỐ Ý không chứa giá trị bí mật —
# chỉ tên đường dẫn Vault, tên kho, tên ảnh, trạng thái. Cất nó vào Secret sẽ dạy người
# đọc rằng "trong này có bí mật", và rồi sẽ có người viết bí mật thật vào.
#
# Vì sao nằm trong cụm chứ không phải trong repo cấu hình: một lần onboarding hỏng giữa
# chừng thường hỏng TRƯỚC khi repo cấu hình kịp tồn tại. State phải sống ở nơi có sẵn từ
# bước đầu tiên, và phải đọc được từ một máy khác — người mở lại việc dở dang hiếm khi là
# người bỏ dở nó.
class OnboardingStore:
    def read(self) -> dict | None:
        raise NotImplementedError

    def write(self, record: dict) -> None:
        raise NotImplementedError


class FileOnboardingStore(OnboardingStore):
    """Cho test và cho lần chạy khan (chưa có cụm). Cùng ngữ nghĩa, khác chỗ cất."""

    def __init__(self, path):
        self.path = Path(path)

    def read(self) -> dict | None:
        if not self.path.is_file() or not self.path.stat().st_size:
            return None
        return json.loads(self.path.read_text())

    def write(self, record: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(record, indent=2, sort_keys=True, ensure_ascii=False))


class ClusterOnboardingStore(OnboardingStore):
    def __init__(self, app: str, kubeconfig: str | None = None):
        pattern = CONFIG.get("onboarding.state_configmap_pattern") or "idp-onboarding-{app}"
        self.name = pattern.replace("{app}", app)
        self.namespace = state_ns()
        self.kubeconfig = kubeconfig

    def read(self) -> dict | None:
        cp = kubectl(["get", "configmap", self.name, "-n", self.namespace, "-o", "json"],
                     kubeconfig=self.kubeconfig, check=False, capture=True)
        if cp.returncode != 0:
            return None
        data = (json.loads(cp.stdout).get("data") or {}).get("record.json")
        return json.loads(data) if data else None

    def write(self, record: dict) -> None:
        ensure_namespace(self.namespace, self.kubeconfig)
        body = {
            "apiVersion": "v1",
            "kind": "ConfigMap",
            "metadata": {"name": self.name, "namespace": self.namespace,
                         "labels": record.get("labels") or {}},
            "data": {"record.json": json.dumps(record, indent=2, sort_keys=True,
                                               ensure_ascii=False)},
        }
        cp = kubectl(["apply", "-f", "-"], kubeconfig=self.kubeconfig,
                     stdin=json.dumps(body), check=False, capture=True)
        if cp.returncode != 0:
            raise SystemExit(f"không ghi được state onboarding: {(cp.stderr or '').strip()}")


def make_onboarding_store(app: str, args) -> OnboardingStore:
    path = getattr(args, "state_file", None)
    return FileOnboardingStore(path) if path else ClusterOnboardingStore(
        app, getattr(args, "kubeconfig", None))


def new_onboarding_record(request: dict) -> dict:
    return {
        "recordVersion": 1,
        "requestId": onboarding_request_id(request),
        "idempotencyKey": onboarding_idempotency_key(request),
        "request": request,
        "labels": onboarding_labels(request),
        "state": "REQUESTED",
        "steps": {},
        "outputs": {},
        "history": [{"at": onboarding_now(), "state": "REQUESTED"}],
        "createdAt": onboarding_now(),
        "updatedAt": onboarding_now(),
    }


def load_or_create_record(store: OnboardingStore, request: dict) -> dict:
    """Bản ghi cho request này, tạo nếu chưa có — và TỪ CHỐI nếu request đã đổi.

    Đây là chỗ chặn "bản sao thứ hai". Không có nó, sửa một dòng trong request rồi chạy
    lại sẽ dựng thêm kho, thêm namespace, thêm credential database bên cạnh app đang
    chạy — mà mọi bước đều báo thành công.
    """
    existing = store.read()
    if existing is None:
        record = new_onboarding_record(request)
        store.write(record)          # GHI NGAY, trước khi tạo bất cứ thứ gì bên ngoài
        log(f"tạo bản ghi onboarding {record['requestId']}")
        return record
    if existing.get("idempotencyKey") != onboarding_idempotency_key(request):
        raise SystemExit(
            f"app {request['application']['name']!r} đã có một lần onboarding đang chạy "
            f"({existing.get('requestId')}, trạng thái {existing.get('state')}) với một "
            "request KHÁC. Chạy tiếp bằng đúng file request cũ, hoặc — nếu thật sự muốn "
            "đổi stack/capability của một app đang sống — dùng `stack-upgrade` và một "
            "pull request. Onboarding lần hai không phải cách nâng cấp."
        )
    log(f"tiếp tục bản ghi onboarding {existing['requestId']} "
        f"(trạng thái {existing.get('state')})")
    return existing


def record_state(record: dict, state: str, store: OnboardingStore) -> None:
    if record.get("state") != state:
        record.setdefault("history", []).append({"at": onboarding_now(), "state": state})
        # Lịch sử chỉ để đọc khi truy vết, không phải audit log — cắt bớt để bản ghi không
        # phình vô hạn sau vài trăm lần retry.
        record["history"] = record["history"][-50:]
    record["state"] = state
    record["updatedAt"] = onboarding_now()
    store.write(record)


# ---------------------------------------------------------------- workflow CI của app
def app_ci_workflow_template(catalog, workloads: int) -> Path:
    """Mẫu nào: một service hay nhiều service.

    Chọn nhầm mẫu hỏng ngay ở bước build ("failed to read dockerfile"), nên nó được suy ra
    từ SỐ WORKLOAD thật trong kho chứ không phải do người điền.
    """
    name = "app-ci-mot-service.yaml" if workloads <= 1 else "app-ci-nhieu-service.yaml"
    path = Path(catalog) / "templates" / name
    if not path.is_file():
        raise SystemExit(f"không thấy mẫu CI {path}")
    return path


def render_app_ci_workflow(text: str, *, app: str, image: str, registry: str,
                           platform_repo: str) -> str:
    """Điền bốn dòng đánh dấu `<-- SỬA` trong khối env của mẫu.

    Bốn giá trị đó là TOẠ ĐỘ (registry nào, kho platform nào) cộng danh tính app, nên
    chúng tới từ platform.env.yaml. Nếu một dòng nào đó không được thay, hàm này DỪNG:
    một workflow còn `REGISTRY: harbor.vi-du.vn/idp` sẽ chạy, sẽ đỏ ở bước push, và thông
    báo lỗi sẽ nói về mạng chứ không nói về việc quên điền.
    """
    values = {"APP": app, "IMAGE_NAME": image, "REGISTRY": registry,
              "PLATFORM_REPO": platform_repo}
    # Bỏ khối hướng dẫn ở đầu mẫu ("chép file này vào…, sửa 4 dòng…"). Nó nói với NGƯỜI
    # đang chọn mẫu bằng tay; trong một file đã được sinh ra thì nó là lời khuyên sai —
    # bốn dòng đó đã được điền, và người đọc tiếp theo sẽ đi tìm thứ không còn ở đó.
    lines = text.splitlines()
    fences = [i for i, line in enumerate(lines) if line.startswith("# ====")]
    if len(fences) >= 2 and any("MẪU CI" in line for line in lines[:fences[1]]):
        lines = lines[fences[1] + 1:]
    # Nhánh kích hoạt CI phải là ĐÚNG hai nhánh mà platform coi là staging và prod.
    # Mẫu viết sẵn `[dev, main]` vì đó là quy ước mặc định; một công ty đổi
    # `environments.staging.config_branch` mà CI vẫn nghe `dev` thì onboarding đẩy code lên
    # một nhánh không có workflow nào chạy — không ảnh nào được build, và không có lỗi ở đâu.
    # GitHub phân giải khối `on:` tĩnh nên CI không tự hỏi được; điền lúc sinh là chỗ duy nhất.
    branches = [str(CONFIG.get(f"environments.{env}.config_branch")
                    or CONFIG.get("git.default_branch", "main")) for env in ENVIRONMENTS]
    out, filled = [], set()
    for line in lines:
        m = re.match(r"^(\s{2})([A-Z_]+):\s.*$", line)
        if m and m.group(2) in values:
            out.append(f"{m.group(1)}{m.group(2)}: {values[m.group(2)]}")
            filled.add(m.group(2))
            continue
        m = re.match(r"^(\s*)branches:\s*\[.*\]\s*$", line)
        if m:
            out.append(f"{m.group(1)}branches: [{', '.join(branches)}]")
            filled.add("branches")
            continue
        out.append(line)
    missing = sorted((set(values) | {"branches"}) - filled)
    if missing:
        raise SystemExit(
            f"mẫu CI không có dòng cho {', '.join(missing)} trong khối env — mẫu và bộ "
            "sinh đã lệch nhau. Sửa templates/app-ci-*.yaml, đừng nới chỗ này."
        )
    rendered = "\n".join(out) + "\n"
    if "SỬA" in rendered:
        raise SystemExit(
            "workflow sinh ra vẫn còn chỗ đánh dấu phải sửa tay. Mẫu đã đổi hình dạng và "
            "bộ sinh không theo kịp — sửa templates/app-ci-*.yaml hoặc "
            "render_app_ci_workflow, đừng giao cho đội ứng dụng một file nửa vời."
        )
    banner = (
        f"# CI của {app} — DO PLATFORM SINH RA lúc onboarding, từ "
        "templates/app-ci-*.yaml.\n"
        "#\n"
        "# Sửa được: thêm bước test, đổi nhãn runner, thêm job. Lần onboarding sau KHÔNG\n"
        "# ghi đè file này.\n"
        "# Đừng tự tính tên ảnh hay context build — hỏi platform (`image-plan --with-build`),\n"
        "# vì quy tắc đó phải giống hệt cái orchestrator dùng khi render manifest.\n"
    )
    return banner + rendered


APP_CI_REL = ".github/workflows/ci.yaml"


def ci_branch_warnings(catalog) -> list[str]:
    """Những gì CI của app sẽ THẤY, kiểm ngay lúc sinh file. Trả về danh sách cảnh báo.

    CI của app checkout platform ở NHÁNH MẶC ĐỊNH (`ref: main` trong mẫu), không phải ở
    nhánh bạn đang đứng — cố ý, để CI và orchestrator luôn dùng cùng một bản renderer
    (orchestrator cũng chỉ chạy được từ nhánh mặc định). Hệ quả ít ai nghĩ tới: onboard một
    app **từ một nhánh chưa merge** sẽ giao cho đội ứng dụng một workflow gọi những thứ
    nhánh mặc định chưa có.

    Đo được trên GitHub, chính vì thiếu cảnh báo này: CI của app fixture đỏ ở bước đầu với
    `unrecognized arguments: --with-build`, và thông báo đó không hề nhắc tới việc nhánh
    chưa được merge. Lần thứ hai thì chạy được nhưng tính tag `content` trong khi
    orchestrator tính `commit`, vì `platform.env.yaml` trên nhánh mặc định còn tắt cờ —
    hai tag khác nhau cho một commit, và Fleet apply một ảnh chưa ai đẩy lên.

    Đọc bằng `git show <nhánh>:<file>`, không gọi mạng: thứ CI nhận được chính là nội dung
    đã commit trên nhánh đó.
    """
    catalog = Path(catalog)
    branch = str(CONFIG.get("git.default_branch", "main") or "main")
    out: list[str] = []

    def committed(rel: str) -> str | None:
        cp = run(["git", "show", f"{branch}:{rel}"], cwd=catalog, check=False, capture=True)
        return cp.stdout if cp.returncode == 0 else None

    renderer = committed("orchestrate.py")
    if renderer is None:
        return [f"không đọc được nhánh '{branch}' của catalog để kiểm xem CI của app sẽ "
                f"chạy bằng bản orchestrate.py nào. Tự kiểm trước khi giao kho cho đội "
                f"ứng dụng."]
    if "--with-build" not in renderer:
        out.append(
            f"nhánh '{branch}' của kho platform CHƯA có `image-plan --with-build`, mà CI "
            f"sinh ra thì gọi nó. Workflow này sẽ ĐỎ ngay ở bước đầu ('unrecognized "
            f"arguments') cho tới khi nhánh phát triển được merge. Merge trước, rồi hãy "
            f"onboard app mới.")

    config_text = committed("platform.env.yaml")
    if config_text is not None:
        try:
            shipped = EnvConfig(yaml.safe_load(config_text) or {})
        except yaml.YAMLError:
            shipped = None
        if shipped is not None and not shipped.get("features.stack_onboarding", False) \
                and feature("stack_onboarding"):
            out.append(
                f"`features.stack_onboarding` đang BẬT ở cấu hình bạn chạy nhưng TẮT trong "
                f"platform.env.yaml trên nhánh '{branch}'. CI của app đọc file trên nhánh "
                f"đó, nên nó sẽ tính tag `content` trong khi orchestrator tính `commit`: "
                f"hai tag khác nhau cho một commit, và Fleet apply một ảnh chưa ai đẩy lên.")
    return out


def write_app_ci_workflow(app_dir, app: str, *, catalog=None, image: str = "",
                          force: bool = False) -> bool:
    """Sinh `.github/workflows/ci.yaml` cho kho ứng dụng. True nếu có ghi.

    KHÔNG ghi đè file đã có: sau lần đầu, file này thuộc về đội ứng dụng — họ được thêm
    bước test, và một lần onboarding chạy lại không có quyền xoá việc đó.
    """
    app_dir = Path(app_dir)
    dest = app_dir / APP_CI_REL
    if dest.exists() and not force:
        log(f"{APP_CI_REL} đã có -> giữ nguyên")
        return False
    catalog = Path(catalog or Path(__file__).resolve().parent)
    services = discover(app_dir)
    text = app_ci_workflow_template(catalog, len(services)).read_text()
    rendered = render_app_ci_workflow(
        text, app=app, image=image or app,
        registry=str(CONFIG.require("registry.path")),
        platform_repo=str(CONFIG.require("git.platform_repo")),
    )
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(rendered)
    log(f"sinh {APP_CI_REL} ({len(services)} workload)")
    for message in ci_branch_warnings(catalog):
        warn(message)
    return True


# ------------------------------------------------------------------------ Vault (13.5)
# Nửa Vault của onboarding. Nó KHÔNG dùng cùng danh tính với nửa GitHub, và điều đó là cố
# ý: quyền tạo repo không được suy ra thành quyền viết policy. Token tới từ môi trường
# (VAULT_TOKEN), không bao giờ từ cấu hình — một token trong file cấu hình là một token
# nằm trong git.
def vault_api(method: str, path: str, payload: dict | None = None,
              *, tolerate: tuple[int, ...] = (),
              content_type: str = "application/json") -> tuple[int, dict]:
    """Một lần gọi Vault. Trả (mã HTTP, body). Không bao giờ log body — nó chứa bí mật."""
    address = (os.environ.get("VAULT_ADDR") or "").rstrip("/")
    token = os.environ.get("VAULT_TOKEN")
    if not address or not token:
        raise SystemExit(
            "VAULT_ADDR và VAULT_TOKEN phải được đặt cho phần Vault của onboarding. "
            "Cố ý không đọc từ platform.env.yaml: `vault.address` là địa chỉ CỤM nhìn "
            "thấy (thường không tới được từ máy đang chạy lệnh), và token thì không bao "
            "giờ được nằm trong một file trong git. Xem `vault-onboard --print-policy` "
            "nếu người quản trị Vault muốn tự chạy phần này."
        )
    headers = {"X-Vault-Token": token, "Content-Type": content_type}
    if _vault_str("namespace"):
        headers["X-Vault-Namespace"] = _vault_str("namespace")
    data = json.dumps(payload).encode() if payload is not None else None
    request = urllib.request.Request(f"{address}/v1/{path}", data=data, headers=headers,
                                     method=method)
    log(f"vault {method} {path}")
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            body = response.read().decode() or "{}"
            return response.status, json.loads(body)
    except urllib.error.HTTPError as exc:
        if exc.code in tolerate:
            return exc.code, {}
        detail = exc.read().decode(errors="replace")[:300]
        raise SystemExit(f"Vault từ chối {method} {path} ({exc.code}): {detail}") from None
    except urllib.error.URLError as exc:
        raise SystemExit(f"không tới được Vault: {exc.reason}") from None


def vault_secret_key_names(app: str, env: str, name: str) -> set[str] | None:
    """TÊN các khoá trong một secret, hoặc None nếu đường dẫn chưa tồn tại.

    Chỉ tên, không bao giờ giá trị — hàm này tồn tại để trả lời "người dùng đã nạp bí mật
    chưa", và câu trả lời đó không cần biết bí mật là gì. Giá trị đọc về nằm trong biến
    cục bộ và không đi đâu cả; không log, không ghi vào state, không trả ra.
    """
    mount = _vault_str("kv_mount") or "kv"
    kv_type = (_vault_str("kv_type") or "kv-v2").lower()
    rel = vault_relative_path(app, env, name)
    path = f"{mount}/data/{rel}" if kv_type == "kv-v2" else f"{mount}/{rel}"
    status, body = vault_api("GET", path, tolerate=(404,))
    if status == 404:
        return None
    data = body.get("data") or {}
    if kv_type == "kv-v2":
        data = data.get("data") or {}
    return set(data)


def ensure_vault_app_access(app: str, env: str) -> dict:
    """Policy đọc/ghi + role kubernetes cho một app/env. Kiểm trước khi tạo.

    Đây là nửa mà `vault-onboard` cố ý chỉ IN ra: nó cần token quản trị Vault. Onboarding
    tự chạy được khi người chạy nó có token đó, và không làm gì cả khi mọi thứ đã có.
    """
    mount = _vault_str("auth_mount") or "kubernetes"
    role = vault_role_name(app, env)
    read_policy = vault_policy_name(app, env)
    write_policy = vault_policy_name(app, env, write=True)
    created = []

    for policy_name, write in ((read_policy, False), (write_policy, True)):
        status, _ = vault_api("GET", f"sys/policies/acl/{policy_name}", tolerate=(404,))
        if status == 404:
            vault_api("PUT", f"sys/policies/acl/{policy_name}",
                      {"policy": vault_policy(app, env, write=write)})
            created.append(f"policy {policy_name}")
        else:
            log(f"policy {policy_name} đã có -> giữ nguyên")

    status, _ = vault_api("GET", f"auth/{mount}/role/{role}", tolerate=(404,))
    if status == 404:
        vault_api("POST", f"auth/{mount}/role/{role}", {
            "bound_service_account_names": [vault_service_account(app, env)],
            "bound_service_account_namespaces": [app_namespace(app, env)],
            # CHỈ policy ĐỌC. VSO đăng nhập bằng role này; cấp thêm policy ghi ở đây là
            # cho operator quyền sửa mọi bí mật của app.
            "token_policies": [read_policy],
            "token_ttl": _vault_str("token_ttl") or "1h",
            "audience": _vault_str("auth_audience") or "vault",
        })
        created.append(f"role {role}")
    else:
        log(f"role {role} đã có -> giữ nguyên")
    return {"role": role, "readPolicy": read_policy, "writePolicy": write_policy,
            "created": created}


def ensure_vault_secret_keys(app: str, env: str, name: str, keys: dict[str, str]) -> list[str]:
    """Bảo đảm một secret có đủ các khoá, sinh giá trị cho khoá còn thiếu. Trả khoá đã tạo.

    `keys` là {tên khoá: giá trị hoặc "" để sinh ngẫu nhiên}. Khoá ĐÃ CÓ thì không đụng
    tới — ghi đè mật khẩu của một database đang chạy là cách chắc chắn nhất để app mất
    kết nối mà không ai hiểu vì sao.
    """
    mount = _vault_str("kv_mount") or "kv"
    kv_type = (_vault_str("kv_type") or "kv-v2").lower()
    rel = vault_relative_path(app, env, name)
    present = vault_secret_key_names(app, env, name)
    missing = [k for k in keys if present is None or k not in present]
    if not missing:
        log(f"{mount}/{rel} đã có đủ khoá {sorted(keys)} -> không ghi")
        return []

    alphabet = string.ascii_letters + string.digits
    payload = {}
    for key in missing:
        payload[key] = keys[key] or "".join(secrets.choice(alphabet) for _ in range(32))
    if kv_type == "kv-v2":
        path = f"{mount}/data/{rel}"
        body = {"data": payload}
    else:
        path = f"{mount}/{rel}"
        body = payload
    # PATCH khi secret đã tồn tại, POST khi chưa: patch không tạo được đường dẫn mới, còn
    # post lên một đường dẫn có sẵn sẽ XOÁ các khoá khác trong cùng secret.
    if present is None or kv_type != "kv-v2":
        vault_api("POST", path, body)
    else:
        # PATCH, không phải POST: secret này có thể đã mang khoá của người khác ghi trước
        # đó, và POST lên kv-v2 THAY THẾ toàn bộ phiên bản — tức xoá sạch những khoá kia.
        # kv-v2 chỉ chấp nhận patch với đúng Content-Type này.
        vault_api("PATCH", path, body,
                  content_type="application/merge-patch+json")
    log(f"đã ghi khoá {sorted(missing)} vào {mount}/{rel} — giá trị không được in ra")
    return missing


# ------------------------------------------------------------------- các bước (13.3)
@dataclass
class OnboardStep:
    key: str
    state: str
    fn: object
    doc: str


class OnboardContext:
    """Mọi thứ một bước cần, gom một chỗ, để bước nào cũng gọi được như nhau."""

    def __init__(self, request: dict, record: dict, store: OnboardingStore, args):
        self.request = request
        self.record = record
        self.store = store
        self.args = args
        self.app = request["application"]["name"]
        self.owner = request["application"]["owner"]
        self.catalog = Path(getattr(args, "catalog", None)
                            or Path(__file__).resolve().parent)
        self.work = Path(getattr(args, "work", None) or f"onboard-{self.app}")
        self.kubeconfig = getattr(args, "kubeconfig", None)

    # ---- tiện ích dùng chung
    def save(self) -> None:
        self.record["updatedAt"] = onboarding_now()
        self.store.write(self.record)

    def out(self, key: str, value) -> None:
        """Ghi một kết quả vào state NGAY. Bước sau đọc nó; retry cũng đọc nó."""
        self.record.setdefault("outputs", {})[key] = value
        self.save()

    @property
    def outputs(self) -> dict:
        return self.record.setdefault("outputs", {})

    @property
    def app_dir(self) -> Path:
        return self.work / "app"

    def org(self) -> str:
        return str(CONFIG.require("git.org"))

    def app_repo(self) -> str:
        pattern = CONFIG.get("git.app_repo_pattern") or "{app}"
        return f"{self.org()}/{pattern.replace('{app}', self.app)}"

    def config_repo(self) -> str:
        pattern = CONFIG.get("git.config_repo_pattern") or "{app}-config"
        return f"{self.org()}/{pattern.replace('{app}', self.app)}"

    def wants(self, env: str) -> bool:
        return bool(self.request["environments"].get(env))


def github_repo_url(slug: str) -> str | None:
    cp = run(["gh", "repo", "view", slug, "--json", "url", "--jq", ".url"],
             check=False, capture=True)
    return cp.stdout.strip() or None if cp.returncode == 0 else None


def _git(ctx_dir: Path, *argv: str, check: bool = True):
    return run(["git", *argv], cwd=ctx_dir, check=check, capture=True)


def step_validate(ctx: OnboardContext) -> None:
    """1 + 2 của mục 13.3: kiểm mọi thứ có thể kiểm TRƯỚC khi tạo bất cứ thứ gì.

    Cờ tính năng kiểm ở đây chứ không phải ở lúc render: một lần onboarding tạo repo,
    namespace và credential rồi mới phát hiện platform chưa bật tính năng là để lại một
    đống rác mà không ai dọn.
    """
    if not feature("stack_onboarding"):
        raise SystemExit(
            "features.stack_onboarding is off in platform.env.yaml. Onboarding sinh ra kho "
            "ứng dụng từ stack catalog, nên nó phải được bật trước — bật cho cả platform, "
            "một lần."
        )
    for name in ("application_values", "vault_secrets"):
        if not feature(name):
            raise SystemExit(
                f"features.{name} is off in platform.env.yaml. App sinh từ stack khai "
                "values theo môi trường và đọc bí mật qua Vault; thiếu cờ này thì lần "
                "render đầu tiên sẽ fail SAU khi repo và namespace đã được tạo."
            )
    if ctx.request["database"]["enabled"] and not feature("postgres_application"):
        raise SystemExit(
            "features.postgres_application is off in platform.env.yaml, nhưng stack này "
            "xin một database `class: application`."
        )

    # Toạ độ bắt buộc — hỏi SỚM, vì thiếu chúng thì bước 3 hoặc bước 8 mới hỏng.
    for key in ("git.org", "git.platform_repo", "registry.path"):
        CONFIG.require(key)

    for env in ENVIRONMENTS:
        if not ctx.wants(env):
            continue
        domain = CONFIG.get(f"environments.{env}.domain") or ""
        host = f"{ctx.app}.{domain}" if domain else ""
        if not host or len(host) > 253 or not re.match(
                r"^[a-z0-9]([a-z0-9.-]*[a-z0-9])?$", host):
            raise SystemExit(
                f"hostname suy ra cho {env} không hợp lệ: {host!r} "
                f"(environments.{env}.domain = {domain!r})."
            )
        ctx.outputs.setdefault("hostnames", {})[env] = host
    ctx.save()


def step_scaffold_repository(ctx: OnboardContext) -> None:
    """3 của mục 13.3: kho ứng dụng sinh từ stack, kèm CI của chính nó.

    Idempotent theo nghĩa mạnh: kho đã có mã nguồn thì KHÔNG sinh đè. Đội ứng dụng bắt đầu
    viết code ngay sau lần onboarding đầu tiên, và một lần retry không có quyền ghi đè
    việc của họ. `generate_stack` cũng không ghi đè từng file, nên hai lớp bảo vệ.
    """
    slug = ctx.app_repo()
    # Nhánh staging của kho ỨNG DỤNG dùng chung tên với nhánh staging của kho cấu hình.
    # Một giá trị, không hai: workflow CI sinh ra cũng nghe đúng tên này (xem
    # render_app_ci_workflow), nên đổi cấu hình là đổi cả hai đầu cùng lúc.
    branch = CONFIG.get("environments.staging.config_branch") or "dev"
    dest = ctx.app_dir
    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True)
    _git(dest, "init", "-q", "-b", branch)

    url = github_repo_url(slug)
    if url:
        log(f"kho ứng dụng {slug} đã có -> không tạo lại")
    else:
        description = ctx.request["application"]["description"] or f"Ứng dụng {ctx.app}"
        run(["gh", "repo", "create", slug, "--private", "--description", description])
        url = github_repo_url(slug)
        if not url:
            raise SystemExit(f"tạo kho {slug} xong nhưng không đọc lại được — dừng ở đây.")
    ctx.out("appRepo", url)          # ghi NGAY: retry phải thấy kho này đã tồn tại

    _git(dest, "remote", "add", "origin", url)
    fetched = _git(dest, "fetch", "-q", "origin", branch, check=False).returncode == 0
    if fetched:
        _git(dest, "checkout", "-q", "-B", branch, f"origin/{branch}")
        if list(dest.glob("*/score.yaml")) or (dest / "score.yaml").is_file():
            log(f"{slug}@{branch} đã có mã nguồn -> chỉ bổ sung thứ còn thiếu")

    generate_stack(ctx.catalog, ctx.request["stack"]["id"], ctx.app, dest,
                   owner=ctx.owner)
    write_app_ci_workflow(dest, ctx.app, catalog=ctx.catalog)

    _git(dest, "config", "user.name", CONFIG.get("git.committer_name", "idp-orchestrator"))
    _git(dest, "config", "user.email",
         CONFIG.get("git.committer_email", "idp-orchestrator@noreply.invalid"))
    _git(dest, "add", "-A")
    if _git(dest, "diff", "--cached", "--quiet", check=False).returncode != 0:
        _git(dest, "commit", "-qm",
             f"chore(idp): dựng {ctx.app} từ stack {ctx.request['stack']['id']} "
             f"v{ctx.request['stack']['version']}")
        _git(dest, "push", "-q", "origin", f"HEAD:{branch}")
        log(f"đẩy scaffold lên {slug}@{branch}")
    else:
        log("không có gì mới để commit -> kho đã ở đúng hình dạng")

    # Nhánh production của kho ỨNG DỤNG. Gieo một lần rồi thôi: sau đó nó chỉ đổi qua
    # pull request của đội ứng dụng.
    prod_branch = CONFIG.get("git.default_branch", "main")
    if _git(dest, "ls-remote", "--exit-code", "--heads", "origin", prod_branch,
            check=False).returncode != 0:
        _git(dest, "push", "-q", "origin", f"HEAD:{prod_branch}")
        log(f"gieo nhánh {prod_branch} của kho ứng dụng")

    sha = _git(dest, "rev-parse", "HEAD").stdout.strip()
    ctx.out("sha", sha)


def ensure_app_checkout(ctx: OnboardContext, at_sha: str = "") -> Path:
    """Bản checkout kho ứng dụng, dựng lại từ remote nếu cần. Trả về thư mục.

    Vì sao không dựa vào thư mục mà bước scaffold để lại: một lần retry hiếm khi chạy trên
    cùng cái máy đã bỏ dở. `--work` mới tinh thì không có bản checkout nào cả, và mọi bước
    sau đều cần nó.

    Hai chế độ, và khác biệt giữa chúng là một lỗi thật đang chờ xảy ra:
      * `at_sha` rỗng — lấy ĐỈNH nhánh. Dùng ở bước build: giữa lúc onboarding bỏ dở và
        lúc chạy lại, đội ứng dụng thường đã đẩy code lên. Deploy commit cũ ở đây là âm
        thầm bỏ qua việc của họ.
      * `at_sha` có giá trị — checkout ĐÚNG commit đó. Dùng ở bước deploy/verify: manifest
        phải trỏ tới ảnh vừa build, và nếu ai đó đẩy commit mới ngay giữa hai bước thì
        render theo đỉnh nhánh sẽ sinh ra một tham chiếu ảnh chưa ai đẩy lên.
    """
    dest = ctx.app_dir
    branch = CONFIG.get("environments.staging.config_branch") or "dev"
    url = ctx.outputs.get("appRepo") or github_repo_url(ctx.app_repo())
    if not url:
        raise SystemExit(f"chưa có kho ứng dụng {ctx.app_repo()} — bước scaffold chưa chạy?")
    if dest.exists():
        shutil.rmtree(dest)
    run(["git", "clone", "-q", "--branch", branch, url, str(dest)])
    if at_sha:
        _git(dest, "checkout", "-q", at_sha)
        return dest
    sha = _git(dest, "rev-parse", "HEAD").stdout.strip()
    if ctx.outputs.get("sha") and sha != ctx.outputs["sha"]:
        log(f"kho ứng dụng đã có commit mới ({ctx.outputs['sha'][:8]} -> {sha[:8]}) "
            "-> onboarding đi tiếp với commit mới nhất")
    ctx.out("sha", sha)
    return dest


def step_bootstrap_platform(ctx: OnboardContext) -> None:
    """4 của mục 13.3: kho cấu hình, hai nhánh, khung Fleet, workflow verify.

    Bọc `tools/tao-app-moi.sh` thay vì viết lại: script đó đã idempotent, đã được dùng
    thật, và nó cố tình chạy bằng danh tính NGƯỜI DÙNG (quyền tạo repo). Onboarding chỉ
    cung cấp toạ độ cho nó — không cái nào gắn cứng trong script.
    """
    script = ctx.catalog / "tools" / "tao-app-moi.sh"
    if not script.is_file():
        raise SystemExit(f"không thấy {script}")
    org, _, config_repo = ctx.config_repo().partition("/")
    env = dict(os.environ)
    env.update({
        "ORG": org,
        "APP": ctx.app,
        "CONFIG_REPO": config_repo,
        "PLATFORM_REPO": str(CONFIG.require("git.platform_repo")),
        "NS_PATTERN": str(CONFIG.get("kubernetes.namespace_pattern") or "{app}-{env}"),
        "VERIFY_RUNNER_LABEL": json.dumps(CONFIG.get("ci.verify_runner_label"))
        if isinstance(CONFIG.get("ci.verify_runner_label"), list)
        else str(CONFIG.get("ci.verify_runner_label") or "ubuntu-latest"),
    })
    log(f"$ bash {script}   (ORG={org} APP={ctx.app} CONFIG_REPO={config_repo})")
    cp = subprocess.run(["bash", str(script)], env=env, text=True)
    if cp.returncode != 0:
        raise SystemExit(f"tao-app-moi.sh hỏng (mã {cp.returncode}) — xem log ở trên.")
    ctx.out("configRepo", github_repo_url(ctx.config_repo()) or ctx.config_repo())
    ensure_app_repo_secrets(ctx)


# Bí mật cấp KHO mà CI của app cần. Không phải bí mật của Vault: chúng sống trong GitHub,
# thuộc về CI, và không bao giờ được đọc bởi platform. Chỉ có TÊN nằm ở đây.
APP_REPO_SECRETS = {
    # Token để CI checkout kho platform (hỏi tên ảnh) và dispatch deploy-request.
    "PLATFORM_DISPATCH_TOKEN": "APP_DISPATCH_TOKEN",
}


def ensure_app_repo_secrets(ctx: OnboardContext) -> None:
    """Đặt bí mật cấp kho cho CI của app — nếu người chạy cung cấp giá trị.

    Vì sao không tự sinh: token này là danh tính, không phải một chuỗi ngẫu nhiên. Platform
    không có quyền tạo nó, và cấp cho onboarding quyền đúc token là đúng thứ mục 13.5 cấm.

    Vì sao vẫn nằm ở đây: thiếu nó thì lần push đầu tiên của đội ứng dụng ĐỎ, và thông báo
    lỗi nói về `actions/checkout` chứ không nói "chưa ai đặt secret". Bản ghi onboarding
    phải nói ra điều đó, kể cả khi nó không tự làm được.
    """
    slug = ctx.app_repo().split("/", 1)[-1] if "/" in ctx.app_repo() else ctx.app_repo()
    cp = run(["gh", "secret", "list", "-R", ctx.app_repo(), "--json", "name",
              "--jq", ".[].name"], check=False, capture=True)
    present = set(cp.stdout.split()) if cp.returncode == 0 else set()
    pending = []
    for name, env_var in sorted(APP_REPO_SECRETS.items()):
        if name in present:
            log(f"secret {name} của {slug} đã có -> giữ nguyên")
            continue
        value = os.environ.get(env_var)
        if not value:
            pending.append(name)
            continue
        # Giá trị đi qua stdin, không qua tham số: tham số dòng lệnh nằm trong `ps` của
        # mọi user khác trên máy.
        run(["gh", "secret", "set", name, "-R", ctx.app_repo()], stdin=value)
        log(f"đặt secret {name} cho {ctx.app_repo()} (giá trị không được in ra)")
    ctx.out("pendingRepoSecrets", pending)
    if pending:
        warn(f"{ctx.app_repo()} còn thiếu secret {', '.join(pending)} — CI của đội ứng "
             f"dụng sẽ đỏ ở bước checkout platform cho tới khi có người đặt chúng "
             f"(`gh secret set <tên> -R {ctx.app_repo()}`, hoặc chạy lại onboarding với "
             f"{'/'.join(APP_REPO_SECRETS[n] for n in pending)} trong môi trường).")


def step_configure_vault(ctx: OnboardContext) -> None:
    """5 của mục 13.3: namespace, ServiceAccount, VaultAuth, policy và role Vault.

    Chỉ cho staging ở giai đoạn này. Prod nhận đúng bộ này khi được kích hoạt — mục 13.3
    nói rõ: không dựng sẵn tài nguyên production cho một app chưa ai bật production.
    """
    ensure_onboarding_environment(ctx, "staging")


def ensure_onboarding_environment(ctx: OnboardContext, env: str) -> None:
    ns = app_namespace(ctx.app, env)
    ensure_namespace(ns, ctx.kubeconfig)
    labels = [f"{k}={v}" for k, v in onboarding_labels(ctx.request, env=env).items()]
    kubectl(["label", "namespace", ns, "--overwrite", *labels],
            kubeconfig=ctx.kubeconfig, check=False, capture=True)
    _emit(vault_auth_manifests(ctx.app, env),
          argparse.Namespace(apply=True, kubeconfig=ctx.kubeconfig))
    _emit(verify_rbac_manifests(ctx.app, env),
          argparse.Namespace(apply=True, kubeconfig=ctx.kubeconfig))
    access = ensure_vault_app_access(ctx.app, env)
    ctx.outputs.setdefault("vault", {})[env] = access
    ctx.save()


def database_workloads(app_dir) -> list[str]:
    """Workload nào xin `postgres` class `application` — đọc từ chính Score của app."""
    found = []
    for svc in discover(Path(app_dir)):
        spec = yaml.safe_load(svc.path.read_text()) or {}
        for res in (spec.get("resources") or {}).values():
            if isinstance(res, dict) and res.get("type") == "postgres" \
                    and res.get("class") == "application":
                found.append(svc.workload)
                break
    return sorted(found)


def database_username(workload: str) -> str:
    """Đúng quy tắc provisioner `postgres.application` dùng cho `.State.username`.

    Hai chỗ tính một tên nghĩa là có ngày chúng lệch nhau; ở đây hậu quả là CNPG tạo
    database với owner khác với user mà VSO đồng bộ vào Secret, và app nhận
    "password authentication failed" trên một credential nhìn thì đúng. Test ghim hai bên
    lại với nhau.
    """
    return "app_" + workload.replace("-", "_")


def step_provision_database(ctx: OnboardContext) -> None:
    """6 của mục 13.3: sinh credential database và ghi thẳng vào Vault.

    Phải xong TRƯỚC khi Fleet apply: CNPG đọc chính Secret do VSO đồng bộ để tạo user.
    Thiếu nó thì Cluster đứng ở bootstrap, còn app thì crash-loop vì không kết nối được —
    hai triệu chứng chẳng cái nào nhắc tới Vault.
    """
    ensure_database_credentials(ctx, "staging")


def ensure_database_credentials(ctx: OnboardContext, env: str) -> None:
    if not ctx.request["database"]["enabled"]:
        log("stack này không xin database -> bỏ qua")
        return
    # Bước này cũng phải chạy được trên một máy chưa từng thấy app: nó đọc Score của app
    # để biết workload nào xin database, và bản checkout có thể chưa tồn tại.
    workloads = database_workloads(
        ensure_app_checkout(ctx, at_sha=ctx.outputs.get("sha", "")))
    if not workloads:
        log("không thấy workload nào khai postgres class application -> bỏ qua")
        return
    if len(workloads) > 1:
        raise SystemExit(
            f"{ctx.app}: {len(workloads)} workload cùng khai `postgres class application` "
            f"({', '.join(workloads)}), nhưng provisioner đọc credential từ MỘT đường dẫn "
            f"Vault cho cả app ({_vault_str('kv_mount')}/apps/{ctx.app}/{env}/"
            f"{CONFIG.get('database.credential_secret')}). Hai database dùng chung một "
            "credential là một sự cố đang chờ xảy ra — tách app, hoặc mở rộng contract."
        )
    name = str(CONFIG.get("database.credential_secret") or "database")
    username = database_username(workloads[0])
    created = ensure_vault_secret_keys(ctx.app, env, name,
                                       {"username": username, "password": ""})
    ctx.outputs.setdefault("database", {})[env] = {
        "workload": workloads[0],
        "username": username,
        "vaultPath": f"{_vault_str('kv_mount') or 'kv'}/"
                     f"{vault_relative_path(ctx.app, env, name)}",
        "credentialCreated": bool(created),
    }
    ctx.save()


def image_exists(ref: str, *, attempts: int = 3) -> bool:
    """Ảnh đã có trên registry chưa. Hỏi lại vài lần trước khi kết luận là CHƯA.

    Cùng lý do mẫu CI hỏi ba lần: một lỗi mạng thoáng qua bị hiểu thành "chưa có ảnh" thì
    ta build lại một ảnh đã tồn tại — vô hại nhưng tốn, và che mất lỗi thật.
    """
    for attempt in range(1, attempts + 1):
        if run(["docker", "manifest", "inspect", ref],
               check=False, capture=True).returncode == 0:
            return True
        if attempt < attempts:
            time.sleep(3)
    return False


def step_build_images(ctx: OnboardContext) -> None:
    """7 của mục 13.3: ảnh cho commit vừa đẩy.

    Hai nguồn, cùng một KẾ HOẠCH: `--images ci` chờ CI của kho ứng dụng đẩy ảnh lên, còn
    `--images local` tự build. Cả hai đều dùng đúng context/Dockerfile mà `image-plan
    --with-build` trả về, nên không có đường nào để hai bên build khác nhau.
    """
    app_dir = ensure_app_checkout(ctx)
    services = discover(app_dir)
    registry = str(CONFIG.require("registry.path"))
    tag = ctx.outputs["sha"]
    plan = plan_images(services, registry, ctx.app, tag, app_dir,
                       resolve_tag_strategy(app_dir, ""))
    specs = build_specs(app_dir, services, ctx.catalog)
    mode = getattr(ctx.args, "images", "local")

    missing = {w: ref for w, ref in plan.items() if not image_exists(ref)}
    for workload, ref in sorted(missing.items()):
        if mode == "ci":
            raise OnboardingPaused(
                "PARTIALLY_READY",
                f"ảnh {ref} chưa có trên registry. CI của {ctx.app_repo()} build nó khi "
                f"commit {tag[:8]} được đẩy lên; chạy lại lệnh này khi CI xong.",
            )
        spec = specs[workload]
        log(f"build {workload}: {ref} (context {spec['context']}, {spec['dockerfile']})")
        run(["docker", "build", "-f", str(app_dir / spec["dockerfile"]),
             "-t", ref, str(app_dir / spec["context"])])
        run(["docker", "push", ref])
    if not missing:
        log("mọi ảnh đã có trên registry -> không build lại")
    ctx.out("images", plan)


def clone_config_repo(ctx: OnboardContext, env: str) -> Path:
    """Bản checkout của kho cấu hình ở đúng nhánh của môi trường này."""
    branch = CONFIG.get(f"environments.{env}.config_branch") \
        or CONFIG.get("git.default_branch", "main")
    dest = ctx.work / f"config-{env}"
    if dest.exists():
        shutil.rmtree(dest)
    url = ctx.outputs.get("configRepo") or github_repo_url(ctx.config_repo())
    if not url:
        raise SystemExit(f"chưa có kho cấu hình {ctx.config_repo()}")
    run(["git", "clone", "-q", "--branch", branch, url, str(dest)])
    return dest


def deploy_environment(ctx: OnboardContext, env: str) -> Path:
    """Render + apply-secrets + commit + GitRepo cho một môi trường. Trả config dir.

    Cố ý gọi thẳng các lệnh có sẵn thay vì viết lại: đường deploy phải là MỘT, dù nó được
    kích hoạt từ onboarding hay từ `repository_dispatch` của CI. Hai bản hiện thực là hai
    hành vi, và cái ít dùng hơn sẽ mục đi trong im lặng.
    """
    config_dir = clone_config_repo(ctx, env)
    sha = ctx.outputs["sha"]
    ensure_app_checkout(ctx, at_sha=sha)
    work = ctx.work / f"render-{env}"
    render_args = argparse.Namespace(
        app=ctx.app, image=ctx.app, tag=sha, registry=str(CONFIG.require("registry.path")),
        tag_strategy="", env=env, catalog=str(ctx.catalog), app_dir=str(ctx.app_dir),
        work=str(work), out=str(config_dir / env / "manifests.yaml"),
        kubeconfig=ctx.kubeconfig, state_file=getattr(ctx.args, "render_state_file", None),
        no_state=False,
    )
    cmd_render(render_args)
    cmd_apply_secrets(argparse.Namespace(
        app=ctx.app, env=env, secrets=str(work / "secrets.yaml"),
        harbor_host=os.environ.get("REGISTRY_HOST") or CONFIG.get("registry.host"),
        harbor_user=os.environ.get("REGISTRY_USER"),
        harbor_pass=os.environ.get("REGISTRY_PASS"),
        kubeconfig=ctx.kubeconfig))
    return config_dir


def step_deploy_staging(ctx: OnboardContext) -> None:
    """8 + 9 của mục 13.3: render staging, ghi vào kho cấu hình, để Fleet kéo về."""
    config_dir = deploy_environment(ctx, "staging")
    cmd_commit(argparse.Namespace(
        config_dir=str(config_dir), app=ctx.app, env="staging",
        sha=ctx.outputs["sha"], app_dir=str(ctx.app_dir),
        catalog_ref=None, branch=None, via_pr=False))
    cmd_ensure_gitrepo(argparse.Namespace(
        app=ctx.app, env="staging", config_dir=str(config_dir),
        kubeconfig=ctx.kubeconfig, work=str(ctx.work)))
    ctx.out("manifests", {"staging": f"{ctx.outputs.get('configRepo')}"
                                     f"/blob/{CONFIG.get('environments.staging.config_branch')}"
                                     f"/staging/manifests.yaml"})


def third_party_secret_requirements(app_dir, app: str, env: str) -> list[dict]:
    """Bí mật mà NGƯỜI DÙNG phải tự nạp: mọi `secretRef` trong values của app.

    Credential database không nằm ở đây — platform tự sinh nó (bước 6). Phân biệt hai
    loại là điều làm nên khác nhau giữa "app đang chờ bạn dán API key" và "platform hỏng".
    """
    spec = load_application_values(Path(app_dir))
    if not spec:
        return []
    resolved = resolve_application_values(spec, env)
    wanted: dict[str, set[str]] = {}
    for key, value in resolved.items():
        if isinstance(value, dict) and "secretRef" in value:
            ref = value["secretRef"]
            wanted.setdefault(str(ref["name"]), set()).add(str(ref["key"]))
    out = []
    for name in sorted(wanted):
        present = vault_secret_key_names(app, env, name)
        missing = sorted(k for k in wanted[name] if present is None or k not in present)
        if missing:
            out.append({"secret": name, "keys": missing,
                        "path": f"{_vault_str('kv_mount') or 'kv'}/"
                                f"{vault_relative_path(app, env, name)}"})
    return out


def step_verify_staging(ctx: OnboardContext) -> None:
    """10 + 11 của mục 13.3: kiểm bí mật TRƯỚC, rồi mới chờ cụm.

    Thứ tự đó là toàn bộ điểm của WAITING_FOR_USER_SECRETS. Nếu chỉ chạy `verify`, một app
    thiếu API key sẽ chờ hết `initial_sync_timeout_seconds` rồi FAIL — đúng về mặt kỹ
    thuật, và hoàn toàn sai về mặt thông tin: không có gì hỏng, chỉ là chưa ai dán khoá vào.
    """
    app_dir = ensure_app_checkout(ctx, at_sha=ctx.outputs["sha"])
    missing = third_party_secret_requirements(app_dir, ctx.app, "staging")
    ctx.out("missingSecrets", missing)
    if missing:
        lines = [f"  orchestrate.py secret-set --app {ctx.app} --env staging "
                 f"--name {item['secret']} --key {key}"
                 for item in missing for key in item["keys"]]
        raise OnboardingPaused(
            "WAITING_FOR_USER_SECRETS",
            f"{ctx.app}/staging đã được deploy nhưng còn thiếu bí mật của bên thứ ba. "
            "Nạp chúng rồi chạy lại đúng lệnh onboarding này:\n" + "\n".join(lines),
        )
    config_dir = ctx.work / "config-staging"
    manifests = config_dir / "staging" / "manifests.yaml"
    if not manifests.is_file():
        raise SystemExit(f"không thấy {manifests} — bước deploy chưa chạy xong?")
    cmd_verify(argparse.Namespace(
        app=ctx.app, env="staging", manifests=str(manifests),
        kubeconfig=ctx.kubeconfig,
        timeout=config_int("onboarding.verify_timeout_seconds", 420)))
    urls = sorted({h for doc in load_all(manifests)
                   if doc.get("kind") == "HTTPRoute"
                   for h in (doc.get("spec") or {}).get("hostnames") or []})
    ctx.out("stagingUrls", [f"http://{h}" for h in urls])


def step_staging_ready(ctx: OnboardContext) -> None:
    """Bước cuối của nửa staging: nói ra trạng thái đúng, không nói READY."""
    state = "PENDING_PROD_ACTIVATION" if ctx.wants("prod") else "STAGING_READY"
    record_state(ctx.record, state, ctx.store)


ONBOARD_PLAN = [
    OnboardStep("validate", "VALIDATING", step_validate,
                "kiểm tên app, chủ sở hữu, stack version, hostname và quyền"),
    OnboardStep("scaffold-repository", "SCAFFOLDING_REPOSITORY", step_scaffold_repository,
                "kho ứng dụng từ stack, kèm .github/workflows/ci.yaml"),
    OnboardStep("bootstrap-platform", "BOOTSTRAPPING_PLATFORM", step_bootstrap_platform,
                "kho cấu hình, hai nhánh, khung Fleet, workflow verify"),
    OnboardStep("configure-vault", "CONFIGURING_VAULT", step_configure_vault,
                "namespace, ServiceAccount, VaultAuth, policy và role Vault"),
    OnboardStep("provision-database", "PROVISIONING_DATABASE", step_provision_database,
                "credential database sinh thẳng vào Vault"),
    OnboardStep("build-images", "BUILDING_IMAGES", step_build_images,
                "ảnh cho commit vừa đẩy"),
    OnboardStep("deploy-staging", "DEPLOYING_STAGING", step_deploy_staging,
                "render staging và ghi vào kho cấu hình cho Fleet"),
    OnboardStep("verify-staging", "VERIFYING_STAGING", step_verify_staging,
                "bí mật, database, rollout và route"),
    OnboardStep("staging-ready", "STAGING_READY", step_staging_ready,
                "chốt trạng thái staging"),
]


# --------------------------------------------------------------------- nửa production
def step_provision_prod(ctx: OnboardContext) -> None:
    """12a: tài nguyên prod chỉ được tạo KHI có người kích hoạt, không phải lúc tạo app."""
    if not ctx.wants("prod"):
        raise SystemExit(
            f"request của {ctx.app} khai environments.prod: false. Kích hoạt production là "
            "một thay đổi của request, không phải một cờ dòng lệnh."
        )
    if (ctx.record.get("steps") or {}).get("verify-staging", {}).get("status") != "done":
        raise SystemExit(
            f"{ctx.app} chưa qua VERIFYING_STAGING. Prod chạy ĐÚNG bộ ảnh mà staging đã "
            "được kiểm — chưa kiểm thì không có gì để đưa lên."
        )
    ensure_onboarding_environment(ctx, "prod")
    ensure_database_credentials(ctx, "prod")


def step_deploy_prod(ctx: OnboardContext) -> None:
    """12b: render prod, ÉP dùng ảnh của staging, rồi mở pull request.

    Hai điều được ghim ở đây, và cả hai đều là gate của phase:

    * `copy_images` chạy SAU render. Render prod tính lại tên ảnh từ `--tag`, và với
      `tagStrategy: content` mỗi workload có nhãn riêng — nên "prod dùng ảnh đã verify"
      không phải là một giá trị mà là cả một bộ. Chép từ manifest staging là cách duy
      nhất đúng khi repo có nhiều service.
    * `via_pr=True` không điều kiện. Nhánh prod của kho cấu hình CÓ THỂ chưa bật bảo vệ —
      trên một cụm thử thì gần như chắc chắn là chưa — và nếu để logic đoán, prod sẽ được
      push thẳng. Đây là chỗ con người phải nhìn thấy diff.
    """
    config_dir = clone_config_repo(ctx, "prod")
    staging_branch = CONFIG.get("environments.staging.config_branch") or "dev"
    staging_manifests = ctx.work / "staging-manifests.yaml"
    cp = run(["git", "show", f"origin/{staging_branch}:staging/manifests.yaml"],
             cwd=config_dir, check=False, capture=True)
    if cp.returncode != 0:
        raise SystemExit(
            f"không đọc được staging/manifests.yaml trên nhánh {staging_branch} của kho "
            "cấu hình — prod không có nguồn ảnh nào để sao chép."
        )
    staging_manifests.write_text(cp.stdout)

    deploy_environment(ctx, "prod")
    prod_manifests = config_dir / "prod" / "manifests.yaml"
    moved = copy_images(staging_manifests, prod_manifests, ctx.app)
    log(f"prod lấy {moved} ảnh từ staging" if moved
        else "prod đã chạy đúng bộ ảnh của staging")
    ctx.out("prodImages", {
        d["metadata"]["name"]: [c.get("image") for c in
                                d["spec"]["template"]["spec"]["containers"]]
        for d in load_all(prod_manifests) if d.get("kind") == "Deployment"})

    url = cmd_commit(argparse.Namespace(
        config_dir=str(config_dir), app=ctx.app, env="prod", sha=ctx.outputs["sha"],
        app_dir=str(ctx.app_dir), catalog_ref=None, branch=None, via_pr=True))
    if url:
        ctx.out("prodPullRequest", url)
        raise OnboardingPaused(
            "PENDING_PROD_APPROVAL",
            f"prod của {ctx.app} đang chờ duyệt: {url}\n"
            "Merge pull request đó rồi chạy lại lệnh này để platform kiểm cụm prod.",
        )
    log("nhánh prod đã mang đúng manifest này -> pull request trước đó đã được merge")


def step_verify_prod(ctx: OnboardContext) -> None:
    config_dir = ctx.work / "config-prod"
    if not (config_dir / "prod" / "manifests.yaml").is_file():
        config_dir = clone_config_repo(ctx, "prod")
    manifests = config_dir / "prod" / "manifests.yaml"
    if not manifests.is_file():
        raise OnboardingPaused(
            "PENDING_PROD_APPROVAL",
            "nhánh prod của kho cấu hình chưa có prod/manifests.yaml — pull request chưa "
            "được merge. Không có gì để kiểm.",
        )
    missing = third_party_secret_requirements(ctx.app_dir, ctx.app, "prod")
    ctx.out("missingSecretsProd", missing)
    if missing:
        lines = [f"  orchestrate.py secret-set --app {ctx.app} --env prod "
                 f"--name {item['secret']} --key {key}"
                 for item in missing for key in item["keys"]]
        raise OnboardingPaused(
            "WAITING_FOR_USER_SECRETS",
            "prod còn thiếu bí mật của bên thứ ba. Bí mật KHÔNG được sao chép từ staging "
            "sang prod — đó là chủ ý:\n" + "\n".join(lines))
    cmd_ensure_gitrepo(argparse.Namespace(
        app=ctx.app, env="prod", config_dir=str(config_dir),
        kubeconfig=ctx.kubeconfig, work=str(ctx.work)))
    cmd_verify(argparse.Namespace(
        app=ctx.app, env="prod", manifests=str(manifests), kubeconfig=ctx.kubeconfig,
        timeout=config_int("onboarding.verify_timeout_seconds", 420)))
    urls = sorted({h for doc in load_all(manifests) if doc.get("kind") == "HTTPRoute"
                   for h in (doc.get("spec") or {}).get("hostnames") or []})
    ctx.out("prodUrls", [f"http://{h}" for h in urls])


def step_prod_ready(ctx: OnboardContext) -> None:
    record_state(ctx.record, "READY", ctx.store)


ONBOARD_PROD_PLAN = [
    OnboardStep("provision-prod", "PROVISIONING_PROD", step_provision_prod,
                "namespace, Vault và credential database của prod"),
    OnboardStep("deploy-prod", "PENDING_PROD_APPROVAL", step_deploy_prod,
                "render prod bằng ảnh của staging, mở pull request"),
    OnboardStep("verify-prod", "VERIFYING_PROD", step_verify_prod,
                "Fleet, bí mật, database và rollout của prod"),
    OnboardStep("prod-ready", "READY", step_prod_ready, "chốt trạng thái"),
]


# --------------------------------------------------------------------------- engine
def run_onboarding(ctx: OnboardContext, plan=None, *, stop_after: str = "") -> dict:
    """Chạy các bước theo thứ tự, bỏ qua bước đã xong, dừng có trạng thái khi phải chờ.

    Ba tính chất, mỗi cái đều là một gate của phase:
      * bước đã `done` thì KHÔNG chạy lại — retry không sinh bản sao;
      * `OnboardingPaused` là dừng có trạng thái, không phải lỗi — WAITING_FOR_USER_SECRETS
        và PENDING_PROD_APPROVAL đều không bao giờ trở thành READY;
      * bước hỏng ghi FAILED_RETRYABLE kèm lý do rồi ném tiếp — lần chạy sau bắt đầu lại
        đúng từ bước đó, chứ không phải từ đầu.
    """
    forced = set(getattr(ctx.args, "force_step", None) or [])
    unknown = sorted(forced - {s.key for s in (plan or ONBOARD_PLAN)})
    if unknown:
        raise SystemExit(f"--force-step: không có bước nào tên {', '.join(unknown)}")
    for step in (plan or ONBOARD_PLAN):
        status = (ctx.record.get("steps") or {}).get(step.key, {}).get("status")
        if status == "done" and step.key not in forced:
            log(f"bước {step.key} đã xong -> bỏ qua")
            continue
        record_state(ctx.record, step.state, ctx.store)
        log(f"==> [{step.state}] {step.key}: {step.doc}")
        try:
            step.fn(ctx)
        except OnboardingPaused as paused:
            ctx.record.setdefault("steps", {})[step.key] = {
                "status": "waiting", "at": onboarding_now(), "note": paused.message}
            record_state(ctx.record, paused.state, ctx.store)
            warn(paused.message)
            return ctx.record
        except SystemExit as exc:
            ctx.record.setdefault("steps", {})[step.key] = {
                "status": "failed", "at": onboarding_now(), "error": str(exc)}
            record_state(ctx.record, "FAILED_RETRYABLE", ctx.store)
            raise
        ctx.record.setdefault("steps", {})[step.key] = {
            "status": "done", "at": onboarding_now()}
        ctx.save()
        if stop_after and stop_after == step.key:
            log(f"dừng sau bước {step.key} theo yêu cầu")
            break
    return ctx.record


def onboarding_summary(record: dict) -> str:
    """11 của mục 13.3: trả về đúng thứ người vừa tạo app cần đọc."""
    out = [f"onboarding {record.get('requestId')}: {record.get('state')}"]
    for step in ONBOARD_PLAN + ONBOARD_PROD_PLAN:
        info = (record.get("steps") or {}).get(step.key)
        if info:
            mark = {"done": "OK ", "waiting": "CHỜ", "failed": "HỎNG"}.get(
                info.get("status"), "?")
            out.append(f"  [{mark}] {step.key}")
    outputs = record.get("outputs") or {}
    for label, key in (("kho ứng dụng", "appRepo"), ("kho cấu hình", "configRepo"),
                       ("pull request prod", "prodPullRequest")):
        if outputs.get(key):
            out.append(f"  {label}: {outputs[key]}")
    for label, key in (("staging", "stagingUrls"), ("prod", "prodUrls")):
        for url in outputs.get(key) or []:
            out.append(f"  {label}: {url}")
    for env, info in sorted((outputs.get("database") or {}).items()):
        out.append(f"  database {env}: user {info['username']} "
                   f"(credential ở {info['vaultPath']})")
    for item in outputs.get("missingSecrets") or []:
        out.append(f"  CÒN THIẾU: {item['path']} khoá {', '.join(item['keys'])}")
    return "\n".join(out)


def cmd_onboard(args) -> None:
    request = load_onboarding_request(args.request, getattr(args, "catalog", None))
    store = make_onboarding_store(request["application"]["name"], args)
    record = load_or_create_record(store, request)
    ctx = OnboardContext(request, record, store, args)
    ctx.work.mkdir(parents=True, exist_ok=True)
    run_onboarding(ctx, stop_after=getattr(args, "stop_after", "") or "")
    print(onboarding_summary(record))


def _record_context(args) -> OnboardContext:
    store = make_onboarding_store(args.app, args)
    record = store.read()
    if record is None:
        raise SystemExit(
            f"không có bản ghi onboarding nào cho {args.app!r}. Chạy `onboard --request` "
            "trước, hoặc kiểm lại --state-file/--kubeconfig đang trỏ đúng chỗ."
        )
    return OnboardContext(record["request"], record, store, args)


def cmd_onboard_status(args) -> None:
    ctx = _record_context(args)
    if getattr(args, "json", False):
        print(json.dumps(ctx.record, indent=2, sort_keys=True, ensure_ascii=False))
        return
    print(onboarding_summary(ctx.record))


def cmd_onboard_activate_prod(args) -> None:
    """12 của mục 13.3. Lệnh RIÊNG, không phải một cờ của `onboard` — và đó là chủ ý:
    đưa một app lên production là một quyết định, không phải một bước tiếp theo."""
    ctx = _record_context(args)
    allowed = ("STAGING_READY", "PENDING_PROD_ACTIVATION", "PROVISIONING_PROD",
               "PENDING_PROD_APPROVAL", "VERIFYING_PROD", "READY",
               "WAITING_FOR_USER_SECRETS", "FAILED_RETRYABLE")
    if ctx.record.get("state") not in allowed:
        raise SystemExit(
            f"{ctx.app} đang ở trạng thái {ctx.record.get('state')} — staging chưa xong. "
            "Chạy `onboard` cho tới STAGING_READY trước."
        )
    ctx.work.mkdir(parents=True, exist_ok=True)
    # Prod render từ ĐÚNG commit staging đã được verify, không phải từ đỉnh nhánh.
    ensure_app_checkout(ctx, at_sha=ctx.record["outputs"]["sha"])
    run_onboarding(ctx, ONBOARD_PROD_PLAN, stop_after=getattr(args, "stop_after", "") or "")
    print(onboarding_summary(ctx.record))


# --------------------------------------------------------------------------------------
# offboard — xoá một app (mục 13.4)
# --------------------------------------------------------------------------------------
# Ba tính chất mà mục 13.4 đòi, và vì sao từng cái tồn tại:
#
#   PREVIEW    — mặc định lệnh này KHÔNG xoá gì. Nó in ra chính xác những gì sẽ bị xoá và
#                những gì sẽ được GIỮ LẠI kèm lý do. Một lệnh xoá mà phải chạy mới biết nó
#                làm gì là một lệnh không ai dám chạy ở prod, nên rốt cuộc người ta xoá tay
#                — và xoá tay mới là thứ xoá nhầm.
#   APPROVAL   — `--execute` đòi gõ lại đúng tên app; `prod` đòi thêm `--approved-by`, và
#                tên đó được ghi vào bản ghi state để sau này còn tra được ai đã duyệt.
#   LIFECYCLE  — không phải cái gì cũng xoá cùng lúc. Backup của database KHÔNG bị đụng
#                tới (retention của kho object lo việc đó); bí mật trong Vault mặc định chỉ
#                xoá mềm; kho Git thì không xoá bao giờ.
#
# Và tính chất thứ tư, thứ khiến ba cái trên đáng tin: KHÔNG XOÁ NHẦM CỦA ĐỘI KHÁC. Mọi
# tài nguyên phải TỰ CHỨNG MINH nó thuộc app này trước khi bị chạm tới. Suy từ tên là
# không đủ — `{app}-{env}` là một quy ước, không phải một bằng chứng.
def offboard_targets(app: str, env: str, kubeconfig) -> tuple[list[dict], list[dict]]:
    """Trả về (sẽ xoá, sẽ giữ). Không gọi lệnh xoá nào."""
    remove: list[dict] = []
    keep: list[dict] = []
    ns = app_namespace(app, env)

    cp = kubectl(["get", "namespace", ns, "-o", "json"],
                 kubeconfig=kubeconfig, check=False, capture=True)
    if cp.returncode == 0:
        # BẰNG CHỨNG, KHÔNG PHẢI TÊN. Namespace do platform tạo không mang nhãn, nên tên
        # đúng quy ước là tất cả những gì ta có ở mức namespace — và nó KHÔNG đủ. Nên ta
        # hỏi thứ bên trong: nếu có bất kỳ tài nguyên nào tự khai thuộc một application
        # KHÁC, thì đây là namespace dùng chung và ta không được xoá nó.
        others = sorted(_foreign_applications(ns, app, kubeconfig))
        if others:
            keep.append({"kind": "Namespace", "name": ns,
                         "why": f"có tài nguyên của application khác: {', '.join(others)}"})
        else:
            remove.append({"kind": "Namespace", "name": ns,
                           "why": "chứa toàn bộ workload/Cluster/Secret của app này"})
    else:
        keep.append({"kind": "Namespace", "name": ns, "why": "không tồn tại"})

    # GitRepo của Fleet: LIỆT-KÊ-RỒI-KHỚP theo `spec.repo`, không đoán theo tên. Đoán tên
    # là cách nhanh nhất để xoá GitRepo của một đội đặt tên trùng quy ước.
    fleet_ns = CONFIG.get("kubernetes.fleet_namespace", "fleet-local") or "fleet-local"
    want = onboarding_config_repo_url(app)
    cp = kubectl(["get", "gitrepo", "-n", fleet_ns, "-o", "json"],
                 kubeconfig=kubeconfig, check=False, capture=True)
    if cp.returncode == 0:
        for obj in (json.loads(cp.stdout or "{}").get("items") or []):
            spec, meta = obj.get("spec") or {}, obj.get("metadata") or {}
            same = re.sub(r"\.git$", "", spec.get("repo", "")) == re.sub(r"\.git$", "", want)
            entry = {"kind": "GitRepo", "name": f"{fleet_ns}/{meta.get('name')}",
                     "why": f"trỏ vào {spec.get('repo')}"}
            if same and env in (spec.get("paths") or [env]):
                remove.append(entry)
            elif same:
                keep.append({**entry, "why": f"cùng kho nhưng paths={spec.get('paths')} "
                                             f"— không phải môi trường {env}"})

    # Bí mật trong Vault: đúng tiền tố apps/<app>/<env>/, tức đúng ranh giới mà policy của
    # app đã vẽ ra. Không quét rộng hơn, vì rộng hơn là chạm sang app khác.
    remove.append({"kind": "VaultPrefix",
                   "name": f"{CONFIG.get('vault.kv_mount') or 'kv'}/"
                           f"{vault_prefix_for(app, env)}",
                   "why": "tiền tố bí mật của riêng app/env này"})

    # Những thứ CỐ Ý không xoá.
    keep.append({"kind": "DatabaseBackup", "name": CONFIG.get("database.backup.object_store_url") or "(chưa cấu hình)",
                 "why": "xoá app không được xoá đường phục hồi; retention của kho object "
                        "quyết định, không phải lệnh này"})
    keep.append({"kind": "GitRepository", "name": want,
                 "why": "kho Git giữ lịch sử triển khai; hãy archive, đừng xoá"})
    keep.append({"kind": "VaultPolicy",
                 "name": (CONFIG.get("vault.policy_template") or "idp-{application}-{environment}")
                         .replace("{application}", app).replace("{environment}", env),
                 "why": "policy/role Vault do Vault Ops sở hữu — gỡ bằng quy trình của họ"})
    return remove, keep


def _foreign_applications(ns: str, app: str, kubeconfig) -> set[str]:
    """Nhãn `idp.platform/application` khác `app` xuất hiện trong namespace này."""
    found: set[str] = set()
    kinds = "deploy,statefulset,cronjob,job,service,configmap,secret,pvc"
    cp = kubectl(["get", kinds, "-n", ns, "-o", "json"],
                 kubeconfig=kubeconfig, check=False, capture=True)
    if cp.returncode != 0:
        return found
    for obj in (json.loads(cp.stdout or "{}").get("items") or []):
        owner = ((obj.get("metadata") or {}).get("labels") or {}).get("idp.platform/application")
        if owner and owner != app:
            found.add(owner)
    return found


def onboarding_config_repo_url(app: str) -> str:
    org = CONFIG.get("git.org") or ""
    pattern = CONFIG.get("git.config_repo_pattern") or "{app}-config"
    return f"https://github.com/{org}/{pattern.replace('{app}', app)}"


def cmd_offboard(args) -> None:
    app, env = validate_secret_name(args.app), validate_environment(args.env)
    remove, keep = offboard_targets(app, env, args.kubeconfig)

    print(f"\n=== KẾ HOẠCH XOÁ {app}/{env} ===\n")
    print("SẼ XOÁ:")
    for t in remove:
        print(f"  - {t['kind']:14} {t['name']}\n      vì: {t['why']}")
    print("\nSẼ GIỮ:")
    for t in keep:
        print(f"  - {t['kind']:14} {t['name']}\n      vì: {t['why']}")
    blocked = [t for t in keep if t["kind"] == "Namespace" and "application khác" in t["why"]]
    print()

    store = make_onboarding_store(app, args)
    record = store.read()

    if not args.execute:
        print("Đây là BẢN XEM TRƯỚC — chưa có gì bị xoá.")
        print(f"Chạy thật:  offboard --app {app} --env {env} --execute --confirm {app}"
              + (" --approved-by <tên>" if env == "prod" else ""))
        if record is not None:
            record["state"] = "DELETE_PLANNED"
            record.setdefault("history", []).append(
                {"at": onboarding_now(), "state": "DELETE_PLANNED", "env": env})
            store.write(record)
        return

    # ---- từ đây là hành động thật ----
    if args.confirm != app:
        raise SystemExit(
            f"--confirm phải là đúng tên app ({app!r}), nhận được {args.confirm!r}. "
            "Gõ lại tên là rào chắn cuối cùng trước một thao tác không hoàn tác được.")
    if env == "prod" and not args.approved_by:
        raise SystemExit(
            "xoá ở prod đòi --approved-by <tên người duyệt>. Tên đó được ghi vào bản ghi "
            "state, nên sau này còn tra được ai đã đồng ý.")
    if blocked:
        raise SystemExit(
            f"namespace {app_namespace(app, env)} có tài nguyên của application khác "
            f"({blocked[0]['why']}). Từ chối xoá: xoá app này sẽ kéo theo của đội khác.")

    if record is not None:
        record["state"] = "DELETING"
        record.setdefault("deletion", {}).update(
            {"env": env, "approvedBy": args.approved_by or "", "at": onboarding_now(),
             "purgeSecrets": bool(args.purge_secrets)})
        record.setdefault("history", []).append(
            {"at": onboarding_now(), "state": "DELETING", "env": env})
        store.write(record)

    done = []
    for t in remove:
        if t["kind"] == "GitRepo":
            ns_, name_ = t["name"].split("/", 1)
            kubectl(["delete", "gitrepo", name_, "-n", ns_, "--ignore-not-found"],
                    kubeconfig=args.kubeconfig, check=True, capture=True)
        elif t["kind"] == "Namespace":
            # Sau GitRepo, luôn luôn. Xoá namespace trước thì Fleet thấy bundle thiếu tài
            # nguyên và dựng lại tất cả trong lúc ta đang xoá — hai bên đánh nhau, và
            # Fleet thắng.
            kubectl(["delete", "namespace", t["name"], "--ignore-not-found", "--wait=false"],
                    kubeconfig=args.kubeconfig, check=True, capture=True)
        elif t["kind"] == "VaultPrefix":
            _offboard_vault(app, env, purge=args.purge_secrets)
        done.append(f"{t['kind']}:{t['name']}")
        log(f"đã xoá {t['kind']} {t['name']}")

    if record is not None:
        record["state"] = "DELETED"
        record["deletion"]["removed"] = done
        record.setdefault("history", []).append(
            {"at": onboarding_now(), "state": "DELETED", "env": env})
        store.write(record)
    print(f"\n{app}/{env}: đã xoá {len(done)} nhóm tài nguyên. Kho Git và backup còn nguyên.")


def _offboard_vault(app: str, env: str, *, purge: bool) -> None:
    """Xoá bí mật của app/env. Mặc định XOÁ MỀM.

    kv-v2 phân biệt `data` (xoá mềm, phục hồi được) với `metadata` (xoá hẳn, không lấy
    lại được). Mặc định mềm là có chủ ý: xoá nhầm một app rồi phát hiện sau vài giờ là
    chuyện có thật, và khi đó thứ khó dựng lại nhất chính là bí mật — mọi thứ khác đều
    render lại được từ Git.
    """
    address = (os.environ.get("VAULT_ADDR") or "").rstrip("/")
    token = os.environ.get("VAULT_TOKEN")
    if not address or not token:
        warn("bỏ qua phần Vault: chưa đặt VAULT_ADDR/VAULT_TOKEN. Bí mật của app VẪN CÒN.")
        return
    mount = CONFIG.get("vault.kv_mount") or "kv"
    prefix = vault_prefix_for(app, env)
    headers = {"X-Vault-Token": token}
    if CONFIG.get("vault.namespace"):
        headers["X-Vault-Namespace"] = CONFIG.get("vault.namespace")

    def call(method: str, url: str):
        try:
            with urllib.request.urlopen(
                    urllib.request.Request(url, headers=headers, method=method),
                    timeout=30) as response:
                return json.loads(response.read() or b"{}")
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return {}
            raise SystemExit(f"Vault từ chối {method} {url} ({exc.code})") from None
        except urllib.error.URLError as exc:
            raise SystemExit(f"không tới được Vault: {exc.reason}") from None

    listed = call("LIST", f"{address}/v1/{mount}/metadata/{prefix}")
    names = ((listed.get("data") or {}).get("keys") or [])
    for name in names:
        leaf = f"{prefix}/{name}".rstrip("/")
        kind = "metadata" if purge else "data"
        call("DELETE", f"{address}/v1/{mount}/{kind}/{leaf}")
        log(f"{'xoá hẳn' if purge else 'xoá mềm'} {mount}/{leaf}")
    if not names:
        log(f"không có bí mật nào dưới {mount}/{prefix}")


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
        # Danh sách in ra dạng JSON. JSON là YAML hợp lệ, nên ["a","b"] dán thẳng vào
        # `runs-on:` được — dùng cho nhãn máy chạy gồm nhiều nhãn. In str(list) của Python
        # sẽ ra dấu nháy đơn, YAML đọc được nhưng trông lạ và dễ bị sửa nhầm.
        print(json.dumps(value) if isinstance(value, list) else value)
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

    `--with-build` adds the build recipe (context + Dockerfile) and therefore changes the
    JSON shape. It is a NEW flag rather than a new default because every app already
    running has a copy of the old CI template that reads `.[workload]` as a string; making
    the richer shape the default would break all of them at once, on the next push.
    """
    app_dir = Path(args.app_dir)
    services = discover(app_dir)
    plan = plan_images(services, args.registry, args.image, args.tag, app_dir,
                       resolve_tag_strategy(app_dir, args.tag_strategy))
    if getattr(args, "with_build", False):
        specs = build_specs(app_dir, services, getattr(args, "catalog", None))
        plan = {w: dict(specs[w], image=ref) for w, ref in plan.items()}
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
        # Default is EMPTY, not "content": empty means "nobody said", which lets
        # `.idp/stack.yaml` have an opinion (see resolve_tag_strategy). An app with no stack
        # file still lands on "content", so nothing already deployed changes.
        p.add_argument(
            "--tag-strategy", choices=("", "commit", "content"), default="",
            help="content: mỗi workload mang mã băm THƯ MỤC của chính nó. commit: mọi "
                 "workload dùng --tag. Bỏ trống: đọc .idp/stack.yaml, mặc định content.",
        )
        # Optional for `promote --mode tag-only`, which rewrites an existing manifest and
        # needs no catalog, app checkout or scratch dir.
        # Xác nhận tường minh rằng database mới được phép RỖNG khi đổi class. Không có
        # mặc định "cứ chạy đi": xem check_postgres_class_migration.
        p.add_argument("--accept-empty-database", action="store_true",
                       help="khi đổi `class` của một postgres đã có state: chấp nhận dựng "
                            "database mới RỖNG và bỏ lại dữ liệu cũ trên PVC cũ")
        p.add_argument("--catalog", required=paths_required, help="checkout of the idp catalog")
        p.add_argument("--app-dir", required=paths_required, help="checkout of the app repo")
        p.add_argument("--work", required=paths_required, help="scratch dir for this render")
        p.add_argument("--kubeconfig")
        add_state_flags(p)

    p = sub.add_parser("verify", help="chờ cụm thực sự chạy đúng thứ vừa render")
    p.add_argument("--app", required=True)
    p.add_argument("--env", required=True, choices=("staging", "prod"))
    p.add_argument("--manifests", required=True)
    p.add_argument("--kubeconfig")
    p.add_argument("--timeout", type=int, default=300)
    p.set_defaults(func=cmd_verify)

    p = sub.add_parser("ensure-gitrepo",
                       help="tạo GitRepo của Fleet nếu chưa có (không bao giờ ghi đè)")
    p.add_argument("--app", required=True)
    p.add_argument("--env", required=True, choices=("staging", "prod"))
    p.add_argument("--config-dir", required=True)
    p.add_argument("--kubeconfig")
    p.add_argument("--work")
    p.set_defaults(func=cmd_ensure_gitrepo)

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
    p.add_argument("--tag-strategy", choices=("", "commit", "content"), default="")
    p.add_argument("--with-build", action="store_true",
                   help="kèm context và Dockerfile của từng workload (đổi hình dạng JSON)")
    p.add_argument("--catalog", default=str(Path(__file__).resolve().parent),
                   help="checkout của kho platform, để đọc buildContext của stack")
    p.set_defaults(func=cmd_image_plan)

    # ---- stack catalog. `--catalog` defaults to this file's own checkout: that is where
    # the templates live, and it is code location, not an infrastructure value.
    def add_catalog_flag(p):
        p.add_argument("--catalog", default=str(Path(__file__).resolve().parent),
                       help="checkout của kho platform (nơi có templates/stacks/)")

    p = sub.add_parser("stack-list", help="các stack mà catalog này phát hành")
    add_catalog_flag(p)
    p.set_defaults(func=cmd_stack_list)

    p = sub.add_parser("stack-new", help="dựng một kho ứng dụng mới từ một stack")
    p.add_argument("--stack", required=True, help="id của stack, vd node-fullstack")
    p.add_argument("--app", required=True)
    p.add_argument("--out", required=True, help="thư mục kho ứng dụng sẽ được dựng vào")
    p.add_argument("--owner", default="", help="đội sở hữu, ghi vào .idp/stack.yaml")
    p.add_argument("--catalog-ref", default="",
                   help="ref catalog để ghim vào platform.lock của app mới")
    p.add_argument("--force", action="store_true",
                   help="ghi đè file đã có (mặc định: giữ nguyên, nên chạy lại được)")
    add_catalog_flag(p)
    p.set_defaults(func=cmd_stack_new)

    p = sub.add_parser("stack-validate",
                       help="kho ứng dụng còn khớp với stack nó khai không")
    p.add_argument("--app-dir", required=True)
    add_catalog_flag(p)
    p.set_defaults(func=cmd_stack_validate)

    p = sub.add_parser("stack-upgrade",
                       help="in diff giữa kho ứng dụng và phiên bản stack hiện tại")
    p.add_argument("--app-dir", required=True)
    p.add_argument("--app", default="", help="ghi đè metadata.application nếu file thiếu")
    p.add_argument("--write", action="store_true",
                   help="ghi thay đổi vào working tree (vẫn phải tự mở pull request)")
    p.add_argument("--all", action="store_true",
                   help="diff cả mã nguồn, không chỉ file do platform sở hữu")
    p.add_argument("--work", default="", help="thư mục tạm để render bản mới")
    add_catalog_flag(p)
    p.set_defaults(func=cmd_stack_upgrade)

    p = sub.add_parser("preflight")
    p.add_argument("--require-cluster", action="store_true")
    p.add_argument("--require-score-compose", action="store_true",
                   help="also demand score-compose at its pinned version (stack CI, local dev)")
    p.add_argument("--require-vault", action="store_true",
                   help="also demand VSO at its pinned version plus VaultConnection/VaultAuthGlobal")
    p.add_argument("--kubeconfig")
    p.set_defaults(func=cmd_preflight)

    p = sub.add_parser("vault-foundation",
                       help="in VaultConnection + VaultAuthGlobal (một bộ cho mỗi cụm)")
    p.add_argument("--apply", action="store_true", help="kubectl apply thay vì chỉ in ra")
    p.add_argument("--kubeconfig")
    p.set_defaults(func=cmd_vault_foundation)

    p = sub.add_parser("vault-onboard",
                       help="ServiceAccount + VaultAuth cho một app/env, kèm policy và role Vault")
    p.add_argument("--app", required=True)
    p.add_argument("--env", required=True, choices=("staging", "prod"))
    p.add_argument("--print-policy", action="store_true", help="chỉ in HCL của policy")
    p.add_argument("--write", action="store_true",
                   help="với --print-policy: in policy GHI (dành cho người/onboarding, không cho VSO)")
    p.add_argument("--apply", action="store_true", help="kubectl apply phần Kubernetes")
    p.add_argument("--kubeconfig")
    p.set_defaults(func=cmd_vault_onboard)

    # `idp-secret set` trong kế hoạch. Dành cho NGƯỜI, không cho CI: nó cần Vault token có
    # policy ghi. Giá trị chỉ vào qua nhập ẩn hoặc stdin — không có cờ --value, vì tham số
    # dòng lệnh nằm trong history và trong `ps` của mọi user khác trên máy.
    p = sub.add_parser("secret-set",
                       help="ghi một khoá bí mật vào Vault ở đúng đường dẫn platform suy ra")
    p.add_argument("--app", required=True)
    p.add_argument("--env", required=True, choices=("staging", "prod"))
    p.add_argument("--name", required=True, help="tên secret logic, vd: stripe")
    p.add_argument("--key", required=True, help="khoá bên trong secret đó, vd: api_key")
    p.add_argument("--stdin", action="store_true", help="đọc giá trị từ stdin thay vì nhập ẩn")
    p.add_argument("--generate", action="store_true",
                   help="sinh ngẫu nhiên (dùng cho credential do PLATFORM sở hữu, vd mật "
                        "khẩu database). Giá trị không bao giờ được in ra.")
    p.add_argument("--replace", action="store_true",
                   help="ghi đè toàn bộ secret (mặc định chỉ vá đúng khoá này)")
    p.set_defaults(func=cmd_secret_set)

    p = sub.add_parser("offboard",
                       help="xoá một app: mặc định chỉ XEM TRƯỚC (mục 13.4)")
    p.add_argument("--app", required=True)
    p.add_argument("--env", required=True, choices=("staging", "prod"))
    p.add_argument("--execute", action="store_true",
                   help="thật sự xoá. Không có cờ này thì chỉ in kế hoạch.")
    p.add_argument("--confirm", default="",
                   help="gõ lại đúng tên app; bắt buộc khi có --execute")
    p.add_argument("--approved-by", default="",
                   help="ai duyệt việc xoá này; bắt buộc ở prod, ghi vào bản ghi state")
    p.add_argument("--purge-secrets", action="store_true",
                   help="xoá HẲN bí mật trong Vault thay vì xoá mềm (không lấy lại được)")
    p.add_argument("--state-file",
                   help="đọc/ghi bản ghi onboarding trong file thay vì ConfigMap")
    p.add_argument("--kubeconfig")
    p.set_defaults(func=cmd_offboard)

    p = sub.add_parser("rotate-db-credential",
                       help="xoay vòng mật khẩu database theo đúng thứ tự Vault -> VSO -> "
                            "CNPG -> pod, kiểm từng bước")
    p.add_argument("--app", required=True)
    p.add_argument("--env", required=True, choices=("staging", "prod"))
    p.add_argument("--kubeconfig")
    p.set_defaults(func=cmd_rotate_db_credential)

    p = sub.add_parser("verify-rbac",
                       help="in danh tính chỉ-đọc dùng để verify (không có quyền đọc Secret)")
    p.add_argument("--app", required=True)
    p.add_argument("--env", required=True, choices=("staging", "prod"))
    p.add_argument("--apply", action="store_true")
    p.add_argument("--kubeconfig")
    p.set_defaults(func=cmd_verify_rbac)

    # ---- onboarding (Phase 6). `--work` giữ nguyên giữa các lần chạy là CÓ Ý: bản
    # checkout kho ứng dụng và kho cấu hình nằm ở đó, và lần retry dùng lại chúng.
    def add_resume_flags(p):
        p.add_argument("--stop-after", default="",
                       help="dừng sau bước này (xem tên bước trong `onboard-status`)")
        p.add_argument("--force-step", action="append", default=[],
                       help="chạy lại một bước đã `done` (mọi bước đều kiểm-trước-khi-"
                            "tạo, nên chạy lại là an toàn). Lặp cờ này cho nhiều bước.")

    def add_onboard_state_flags(p):
        p.add_argument("--state-file",
                       help="giữ bản ghi onboarding trong file thay vì ConfigMap trong cụm")
        p.add_argument("--kubeconfig")

    p = sub.add_parser("onboard",
                       help="chạy máy trạng thái onboarding cho một request (mục 13)")
    p.add_argument("--request", required=True, help="file YAML theo mục 13.1")
    p.add_argument("--work", help="thư mục làm việc, mặc định onboard-<app>")
    p.add_argument("--images", choices=("local", "ci"), default="local",
                   help="local: tự build và đẩy ảnh; ci: chờ CI của kho ứng dụng đẩy")
    add_resume_flags(p)
    add_catalog_flag(p)
    add_onboard_state_flags(p)
    p.set_defaults(func=cmd_onboard)

    p = sub.add_parser("onboard-status", help="trạng thái và kết quả của một lần onboarding")
    p.add_argument("--app", required=True)
    p.add_argument("--json", action="store_true", help="in nguyên bản ghi")
    add_onboard_state_flags(p)
    p.set_defaults(func=cmd_onboard_status)

    p = sub.add_parser("onboard-activate-prod",
                       help="kích hoạt production: dùng ảnh đã verify ở staging, qua pull request")
    p.add_argument("--app", required=True)
    p.add_argument("--work", help="thư mục làm việc của lần onboarding trước")
    add_resume_flags(p)
    add_catalog_flag(p)
    add_onboard_state_flags(p)
    p.set_defaults(func=cmd_onboard_activate_prod)

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
