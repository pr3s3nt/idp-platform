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
import hashlib
import json
import re
import shutil
import subprocess
import sys
import time
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


def write_environment_provisioner(resolved: dict, dest: Path, *, app: str, env: str) -> Path:
    """Materialise a provisioner for `type: environment` carrying this app's values.

    Generated per render rather than shipped in the catalog because the values ARE the
    app's, and the catalog is shared and version-pinned. It lands in the work directory
    next to the resolved catalog, so a failed render leaves behind exactly the files
    score-k8s was handed.
    """
    literals = {}
    for key, value in sorted(resolved.items()):
        if isinstance(value, dict) and "secretRef" in value:
            # Phase 3 turns these into encodeSecretRef outputs backed by a VaultStaticSecret.
            # Until then, refusing beats rendering a workload with the variable missing.
            if not feature("vault_secrets"):
                raise SystemExit(
                    f"{VALUES_REL}: {key!r} is a secretRef, but features.vault_secrets is "
                    "off for this platform. Enable it (and install the Vault Secrets "
                    "Operator) or use a literal value."
                )
            raise SystemExit(f"internal: secretRef output for {key!r} is not implemented yet")
        literals[key] = value

    body = yaml.safe_dump(literals, sort_keys=True, default_flow_style=False,
                          allow_unicode=True) if literals else "{}\n"
    doc = (
        f"# GENERATED by orchestrate.py for {app}/{env} — do not edit, do not commit.\n"
        f"# Source: {VALUES_REL}. Values are literals only; nothing here is a secret.\n"
        "- uri: template://platform/environment\n"
        "  type: environment\n"
        f"  description: ApplicationValues for {app} in {env}\n"
        "  outputs: |\n"
        + "".join(f"    {line}\n" for line in _go_template_safe(body).splitlines())
    )
    dest.write_text(doc)
    log(f"generated environment provisioner with {len(literals)} value(s) -> {dest}")
    return dest


# --------------------------------------------------------------------------------------
# Vault foundation
# --------------------------------------------------------------------------------------
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
    consumers = 0
    for service, doc, alias in aliases:
        if alias is None:
            continue
        where = f"{service.path.name} ({service.workload})"
        consumers += 1
        check_file_secrets(doc, resolved, where=where)
        used |= check_referenced_keys(doc, alias, resolved, where=where)

    if not consumers:
        warn(f"{VALUES_REL} defines {len(resolved)} value(s) for {env}, but no workload "
             "declares a `type: environment` resource, so none of them reach a container.")
        return []
    if unused := sorted(set(resolved) - used):
        # A warning, not an error: a key can legitimately serve only one of several
        # environments, or be staged ahead of the code that will read it.
        warn(f"{VALUES_REL}: value(s) not referenced by any workload in {env}: {unused}")

    return [write_environment_provisioner(
        resolved, catalog_dir / "generated.environment.provisioners.yaml", app=app, env=env)]


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
    want: dict[str, list[str]] = {}
    for doc in load_all(Path(args.manifests)):
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
    p.add_argument("--tag-strategy", choices=("commit", "content"), default="content")
    p.set_defaults(func=cmd_image_plan)

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

    p = sub.add_parser("verify-rbac",
                       help="in danh tính chỉ-đọc dùng để verify (không có quyền đọc Secret)")
    p.add_argument("--app", required=True)
    p.add_argument("--env", required=True, choices=("staging", "prod"))
    p.add_argument("--apply", action="store_true")
    p.add_argument("--kubeconfig")
    p.set_defaults(func=cmd_verify_rbac)

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
