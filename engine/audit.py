"""Deployment audit store: history, failure reporting and platform KPIs.

Why this is one module and not a service. The platform already deploys real apps; the
history store must be an OBSERVER that can never take a deploy down. So every entry point
here is guarded (see ``_guarded``): with ``audit.enabled: false`` — the default — nothing
imports a driver or opens a socket, and with ``required: false`` a database outage logs a
warning and lets the deploy continue. The deploy path calls a handful of ``idpctl audit-*``
subcommands; all SQL lives here, never in workflow YAML.

What it deliberately never stores. No connection string (only the NAME of the env var that
carries it), no token, password, kubeconfig or Kubernetes Secret value. Every message and
error is passed through ``redact`` before it reaches a column, and the snapshot reads
cluster state read-only — it records a VaultStaticSecret's NAME and sync conditions, never
the destination Secret's contents.

Identity is derived, not random. A deployment's identity is a stable hash of
``repository + run id + run attempt + app + environment + workflow``. A GitHub Actions
re-run of the same attempt maps onto the same row (idempotent); a NEW attempt is a new,
distinguishable deployment. That is what keeps a retry from double-counting in the KPIs.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from . import context as _ctx
from .context import CONFIG, log, warn, kubectl, canonical_json

# --------------------------------------------------------------------------------------
# settings
# --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class AuditSettings:
    enabled: bool
    required: bool
    database_url_env: str
    connect_timeout: int
    retention_days: int
    notify_failure: bool
    notification_mode: str


def settings() -> AuditSettings:
    """Read the ``audit`` block. Coordinates come from platform.env.yaml, never code."""
    return AuditSettings(
        enabled=bool(CONFIG.get("audit.enabled", False)),
        required=bool(CONFIG.get("audit.required", False)),
        database_url_env=str(CONFIG.get("audit.database_url_env") or "AUDIT_DATABASE_URL"),
        connect_timeout=_ctx.config_int("audit.connect_timeout_seconds", 5),
        retention_days=_ctx.config_int("audit.retention_days", 365),
        notify_failure=bool(CONFIG.get("audit.notify_failure", True)),
        notification_mode=str(CONFIG.get("audit.notification_mode") or "commit-comment"),
    )


# --------------------------------------------------------------------------------------
# redaction — everything crossing into a column or a comment goes through here first
# --------------------------------------------------------------------------------------
# The audit trail is only trustworthy if it can never become a NEW place a secret leaks.
# These patterns are intentionally broad: over-redaction costs a little readability, while
# under-redaction writes a token to a table people query casually. When in doubt, redact.
_REDACTIONS: tuple[tuple[re.Pattern, str], ...] = (
    # A libpq/DSN password field: `password=...` up to the next space or end.
    (re.compile(r"(?i)\bpassword\s*=\s*\S+"), "password=<redacted>"),
    # userinfo in any URL: scheme://user:pass@host -> scheme://<redacted>@host
    (re.compile(r"://[^\s/@:]+:[^\s/@]+@"), "://<redacted>@"),
    # A whole postgres DSN URL, creds or not — it names the history host, keep it out.
    (re.compile(r"(?i)\bpostgres(?:ql)?://\S+"), "postgres://<redacted>"),
    # GitHub tokens of every current shape, and classic 40-hex PATs after a token= hint.
    (re.compile(r"\b(?:ghp|ghs|gho|ghu|ghr)_[A-Za-z0-9]{20,}"), "<redacted-token>"),
    (re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}"), "<redacted-token>"),
    (re.compile(r"(?i)\b(authorization|bearer|token)\b\s*[:=]?\s*[A-Za-z0-9._\-]{16,}"),
     r"\1 <redacted-token>"),
    # AWS-style access key ids.
    (re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"), "<redacted-key>"),
    # key=value / "key": "value" for obviously sensitive keys.
    (re.compile(r"(?i)\b(pass(?:word|wd)?|secret|token|api[_-]?key|access[_-]?key|"
                r"secret[_-]?key|private[_-]?key)\b(\s*[:=]\s*)(\"?)[^\s\",}]+\3"),
     r"\1\2<redacted>"),
    # A long base64 run (kubeconfig, cert, key material). The lookahead REQUIRES at least
    # one '+' or '/' in the run, so a plain 64-hex image digest — which has neither — is
    # never mistaken for secret material and survives verbatim.
    (re.compile(r"\b(?=[A-Za-z0-9+/]*[+/])[A-Za-z0-9+/]{60,}={0,2}"), "<redacted-blob>"),
)


def redact(text) -> str:
    """Strip secrets from any string before it is stored or shown. Never raises."""
    if text is None:
        return ""
    out = str(text)
    for pattern, replacement in _REDACTIONS:
        out = pattern.sub(replacement, out)
    # Keep rows small and predictable; a runaway error message is not history worth keeping.
    return out[:4000]


# --------------------------------------------------------------------------------------
# failure classification — stage first, a few structured overrides second
# --------------------------------------------------------------------------------------
CATEGORIES = (
    "CONFIGURATION", "SECRET_OR_VAULT", "REGISTRY_OR_IMAGE", "DATABASE_OR_STORAGE",
    "GATEWAY_OR_ROUTE", "FLEET_CONVERGENCE", "KUBERNETES_ROLLOUT", "GIT_OR_PERMISSION",
    "RUNNER_OR_TOOL", "UNKNOWN",
)

# Canonical stages the deploy/promote/verify flows report, mapped to their usual cause.
STAGE_CATEGORY = {
    "preflight": "RUNNER_OR_TOOL",
    "checkout": "GIT_OR_PERMISSION",
    "render": "CONFIGURATION",
    "apply_secrets": "SECRET_OR_VAULT",
    "commit_config": "GIT_OR_PERMISSION",
    "fleet_register": "FLEET_CONVERGENCE",
    "fleet_converge": "FLEET_CONVERGENCE",
    "vault_sync": "SECRET_OR_VAULT",
    "database_ready": "DATABASE_OR_STORAGE",
    "rollout_verify": "KUBERNETES_ROLLOUT",
    "tip_guard": "FLEET_CONVERGENCE",
}

# Message signatures that beat the stage default — a rollout that fails on an image pull is
# a REGISTRY problem, not a generic KUBERNETES_ROLLOUT one, and the fix is different.
_MESSAGE_SIGNS: tuple[tuple[re.Pattern, str], ...] = (
    (re.compile(r"(?i)imagepullbackoff|errimagepull|pull access denied|manifest unknown|"
                r"not found: manifest|no such image|unauthorized.*registry"), "REGISTRY_OR_IMAGE"),
    (re.compile(r"(?i)vaultstaticsecret|vault |vso |secret.*not.*sync|kv/data"), "SECRET_OR_VAULT"),
    (re.compile(r"(?i)httproute|resolvedrefs|gateway|listener|sectionname|no matching parent"),
     "GATEWAY_OR_ROUTE"),
    (re.compile(r"(?i)pvc|persistentvolume|storageclass|not bound|cnpg|postgres|database"),
     "DATABASE_OR_STORAGE"),
    (re.compile(r"(?i)gitrepo|fleet|bundle|not ready.*fleet"), "FLEET_CONVERGENCE"),
    (re.compile(r"(?i)permission denied|403|not found on this server|refusing to|protected branch|"
                r"could not read Username|authentication failed"), "GIT_OR_PERMISSION"),
    (re.compile(r"(?i)command not found|no such file or directory|version mismatch|not on PATH"),
     "RUNNER_OR_TOOL"),
)


def classify(stage: str | None, message: str | None = None,
             explicit: str | None = None) -> str:
    """Best-effort failure category. Not an AI log reader — stage plus a few signatures."""
    if explicit:
        up = explicit.strip().upper()
        return up if up in CATEGORIES else "UNKNOWN"
    text = message or ""
    for pattern, category in _MESSAGE_SIGNS:
        if pattern.search(text):
            return category
    return STAGE_CATEGORY.get((stage or "").strip(), "UNKNOWN")


# --------------------------------------------------------------------------------------
# identity — a deployment id that is stable across a same-attempt re-run
# --------------------------------------------------------------------------------------
@dataclass(frozen=True)
class Identity:
    repository: str
    run_id: str
    run_attempt: str
    app: str
    environment: str
    workflow: str

    @property
    def dedup_key(self) -> str:
        return hashlib.sha256("\x1f".join([
            self.repository, self.run_id, str(self.run_attempt),
            self.app, self.environment, self.workflow,
        ]).encode()).hexdigest()


def identity_from(app: str, environment: str, *, repository: str | None = None,
                  run_id: str | None = None, run_attempt: str | None = None,
                  workflow: str | None = None) -> Identity:
    """Build the identity, defaulting each field from the GitHub Actions environment.

    A hand-replay on a runner passes them explicitly; inside Actions they come from the
    standard variables so the workflow never has to spell the identity out twice.
    """
    return Identity(
        repository=repository or os.environ.get("GITHUB_REPOSITORY", "") or "local",
        run_id=str(run_id or os.environ.get("GITHUB_RUN_ID", "") or "local"),
        run_attempt=str(run_attempt or os.environ.get("GITHUB_RUN_ATTEMPT", "") or "1"),
        app=app,
        environment=environment,
        workflow=workflow or os.environ.get("GITHUB_WORKFLOW", "") or "manual",
    )


# --------------------------------------------------------------------------------------
# connection + guard
# --------------------------------------------------------------------------------------
def _database_url(cfg: AuditSettings) -> str:
    url = os.environ.get(cfg.database_url_env, "")
    if not url:
        # The NAME is safe to print; the value never is.
        raise RuntimeError(
            f"audit is enabled but ${cfg.database_url_env} is empty — the connection "
            "string must come from that environment variable."
        )
    return url


def _connect(cfg: AuditSettings):
    """Open a connection with a short timeout. psycopg is imported lazily on purpose.

    With audit off (the default) this module is imported but never reaches here, so a
    runner that has never installed a Postgres driver keeps rendering and deploying. The
    import cost and the dependency are paid only by an install that switched audit ON.
    """
    try:
        import psycopg  # noqa: PLC0415  (lazy: see docstring)
    except ModuleNotFoundError as exc:  # pragma: no cover - environment specific
        raise RuntimeError(
            "audit is enabled but the 'psycopg' driver is not installed on this runner "
            "(pip install 'psycopg[binary]')."
        ) from exc
    # statement_timeout guards against a slow query wedging a deploy; connect_timeout
    # guards against an unreachable host doing the same.
    conn = psycopg.connect(
        _database_url(cfg),
        connect_timeout=cfg.connect_timeout,
        autocommit=False,
        application_name="idpctl-audit",
        options=f"-c statement_timeout={max(cfg.connect_timeout, 5) * 1000}",
    )
    return conn


def _guarded(work, *, default=None):
    """Run ``work(conn)`` under the fail-open/fail-closed contract.

    Disabled -> return ``default`` immediately, no driver, no socket. Enabled and it
    fails -> raise only when ``audit.required``; otherwise warn and return ``default`` so
    the deploy is never taken down by its own audit trail. The exception text is redacted
    because a connection error can echo the DSN.
    """
    cfg = settings()
    if not cfg.enabled:
        return default
    try:
        conn = _connect(cfg)
    except Exception as exc:  # noqa: BLE001 - deliberately broad, then re-raised or warned
        return _handle_failure(cfg, exc, default)
    try:
        with conn:
            result = work(conn)
        return result
    except Exception as exc:  # noqa: BLE001
        return _handle_failure(cfg, exc, default)
    finally:
        try:
            conn.close()
        except Exception:  # pragma: no cover
            pass


def _handle_failure(cfg: AuditSettings, exc: Exception, default):
    msg = redact(str(exc))
    if cfg.required:
        raise SystemExit(
            f"audit.required is true and the audit store failed: {msg}. "
            "Set audit.required=false to let deploys proceed on an audit outage."
        )
    warn(f"audit store unavailable, continuing (fail-open): {msg}")
    return default


# --------------------------------------------------------------------------------------
# schema + migrations — versioned, safe to run repeatedly
# --------------------------------------------------------------------------------------
# One list; append new tuples for future changes. Every statement is IF NOT EXISTS so a
# re-run — or a run after the tracking table was lost — is a no-op, not an error. UTC is
# the only time zone that touches these tables (timestamptz + now() at UTC).
_MIGRATIONS: tuple[tuple[int, str], ...] = (
    (1, """
    CREATE TABLE IF NOT EXISTS applications (
        id            BIGSERIAL PRIMARY KEY,
        application   TEXT NOT NULL,
        repository    TEXT NOT NULL,
        owner         TEXT,
        stack_id      TEXT,
        stack_version TEXT,
        first_seen    TIMESTAMPTZ NOT NULL DEFAULT now(),
        last_seen     TIMESTAMPTZ NOT NULL DEFAULT now(),
        UNIQUE (repository, application)
    );

    CREATE TABLE IF NOT EXISTS deployments (
        id               BIGSERIAL PRIMARY KEY,
        dedup_key        TEXT NOT NULL UNIQUE,
        application_id   BIGINT REFERENCES applications(id),
        application      TEXT NOT NULL,
        environment      TEXT NOT NULL,
        workflow         TEXT,
        trigger          TEXT,
        repository       TEXT,
        github_run_id    TEXT,
        run_attempt      INTEGER NOT NULL DEFAULT 1,
        actor            TEXT,
        runner_label     TEXT,
        runner_name      TEXT,
        run_url          TEXT,
        app_sha          TEXT,
        platform_sha     TEXT,
        queued_at        TIMESTAMPTZ,
        started_at       TIMESTAMPTZ,
        ended_at         TIMESTAMPTZ,
        status           TEXT NOT NULL DEFAULT 'running',
        failure_stage    TEXT,
        failure_category TEXT,
        created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
        updated_at       TIMESTAMPTZ NOT NULL DEFAULT now()
    );
    CREATE INDEX IF NOT EXISTS ix_deployments_app_env
        ON deployments (application, environment);
    CREATE INDEX IF NOT EXISTS ix_deployments_started
        ON deployments (started_at);
    CREATE INDEX IF NOT EXISTS ix_deployments_status
        ON deployments (status);

    CREATE TABLE IF NOT EXISTS deployment_events (
        id            BIGSERIAL PRIMARY KEY,
        deployment_id BIGINT NOT NULL REFERENCES deployments(id) ON DELETE CASCADE,
        seq           INTEGER NOT NULL,
        event_key     TEXT NOT NULL,
        occurred_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
        stage         TEXT NOT NULL,
        status        TEXT NOT NULL,
        duration_ms   BIGINT,
        category      TEXT,
        message       TEXT,
        metadata      JSONB,
        UNIQUE (deployment_id, event_key)
    );
    CREATE INDEX IF NOT EXISTS ix_events_deployment
        ON deployment_events (deployment_id, seq);
    CREATE INDEX IF NOT EXISTS ix_events_stage
        ON deployment_events (stage);

    CREATE TABLE IF NOT EXISTS application_snapshots (
        id              BIGSERIAL PRIMARY KEY,
        deployment_id   BIGINT NOT NULL UNIQUE REFERENCES deployments(id) ON DELETE CASCADE,
        application_id  BIGINT REFERENCES applications(id),
        application     TEXT NOT NULL,
        environment     TEXT NOT NULL,
        app_sha         TEXT,
        platform_sha    TEXT,
        config_repo     TEXT,
        config_commit   TEXT,
        namespace       TEXT,
        database_backend TEXT,
        gateway         TEXT,
        listener        TEXT,
        capability_flags JSONB,
        manifest_digest TEXT,
        captured_at     TIMESTAMPTZ NOT NULL DEFAULT now()
    );

    CREATE TABLE IF NOT EXISTS deployed_resources (
        id              BIGSERIAL PRIMARY KEY,
        deployment_id   BIGINT NOT NULL REFERENCES deployments(id) ON DELETE CASCADE,
        snapshot_id     BIGINT REFERENCES application_snapshots(id) ON DELETE SET NULL,
        api_version     TEXT,
        kind            TEXT NOT NULL,
        namespace       TEXT,
        name            TEXT NOT NULL,
        image_reference TEXT,
        image_digest    TEXT,
        desired_state   TEXT,
        ready_state     TEXT,
        storage_class   TEXT,
        pvc             TEXT,
        gateway_parent  TEXT,
        gateway_listener TEXT,
        metadata        JSONB,
        captured_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
        UNIQUE (deployment_id, kind, namespace, name)
    );
    CREATE INDEX IF NOT EXISTS ix_resources_deployment
        ON deployed_resources (deployment_id);
    """),
)


def migrate(conn) -> list[int]:
    """Apply every migration not yet recorded. Returns the versions applied this call."""
    applied: list[int] = []
    with conn.cursor() as cur:
        cur.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations ("
            " version INTEGER PRIMARY KEY,"
            " applied_at TIMESTAMPTZ NOT NULL DEFAULT now())"
        )
        cur.execute("SELECT version FROM schema_migrations")
        done = {row[0] for row in cur.fetchall()}
        for version, sql in _MIGRATIONS:
            if version in done:
                continue
            cur.execute(sql)
            cur.execute("INSERT INTO schema_migrations (version) VALUES (%s)", (version,))
            applied.append(version)
    return applied


def run_migrations() -> list[int] | None:
    """CLI entry: migrate under the guard. None when audit is disabled."""
    return _guarded(migrate)


# --------------------------------------------------------------------------------------
# timestamp helpers — DB is UTC, always
# --------------------------------------------------------------------------------------
def _now() -> datetime:
    return datetime.now(timezone.utc)


def _ts(value: str | None) -> datetime | None:
    """Parse an ISO-8601 timestamp, or 'now', or nothing. Result is tz-aware UTC."""
    if value in (None, "", "now"):
        return _now() if value == "now" else None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt.astimezone(timezone.utc) if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


# --------------------------------------------------------------------------------------
# writes
# --------------------------------------------------------------------------------------
def _upsert_application(cur, ident: Identity, *, owner=None, stack_id=None,
                        stack_version=None) -> int | None:
    """Insert-or-touch the stable application identity, returning its id.

    Identity can be upserted (last_seen moves, first_seen is preserved); deployment
    HISTORY never is. COALESCE keeps a later row from blanking an owner an earlier one knew.
    """
    cur.execute(
        """
        INSERT INTO applications (application, repository, owner, stack_id, stack_version)
        VALUES (%s, %s, %s, %s, %s)
        ON CONFLICT (repository, application) DO UPDATE SET
            last_seen     = now(),
            owner         = COALESCE(EXCLUDED.owner, applications.owner),
            stack_id      = COALESCE(EXCLUDED.stack_id, applications.stack_id),
            stack_version = COALESCE(EXCLUDED.stack_version, applications.stack_version)
        RETURNING id
        """,
        (ident.app, ident.repository, owner, stack_id, stack_version),
    )
    row = cur.fetchone()
    return row[0] if row else None


def start_deployment(ident: Identity, *, trigger=None, actor=None, run_url=None,
                     app_sha=None, platform_sha=None, runner_label=None, runner_name=None,
                     queued_at=None, started_at="now", owner=None, stack_id=None,
                     stack_version=None) -> dict | None:
    """Create the deployment row idempotently. Re-running the same attempt reuses it.

    ON CONFLICT touches only ``updated_at`` — it never rewinds status, timestamps or a
    recorded failure — so a same-attempt re-run of this command cannot overwrite what the
    first run of the attempt already learned. A DIFFERENT run attempt has a different
    dedup_key and is a distinct deployment, which is what keeps first-attempt success rate
    honest.
    """
    def work(conn):
        with conn.cursor() as cur:
            app_id = _upsert_application(cur, ident, owner=owner, stack_id=stack_id,
                                         stack_version=stack_version)
            cur.execute(
                """
                INSERT INTO deployments (
                    dedup_key, application_id, application, environment, workflow, trigger,
                    repository, github_run_id, run_attempt, actor, runner_label, runner_name,
                    run_url, app_sha, platform_sha, queued_at, started_at, status)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'running')
                ON CONFLICT (dedup_key) DO UPDATE SET updated_at = now()
                RETURNING id, (xmax = 0) AS inserted
                """,
                (ident.dedup_key, app_id, ident.app, ident.environment, ident.workflow,
                 trigger, ident.repository, ident.run_id, int(ident.run_attempt), actor,
                 runner_label, runner_name, run_url, app_sha, platform_sha,
                 _ts(queued_at), _ts(started_at)),
            )
            dep_id, inserted = cur.fetchone()
        return {"deployment_id": dep_id, "dedup_key": ident.dedup_key,
                "created": bool(inserted), "application_id": app_id}

    return _guarded(work)


def record_event(ident: Identity, *, stage: str, status: str, category=None, message=None,
                 duration_ms=None, metadata=None, seq=None, key=None) -> dict | None:
    """Append one timeline event. Idempotent per (deployment, event_key).

    The event_key defaults to ``stage:status`` so replaying the same stage outcome during a
    same-attempt re-run collapses onto one row instead of inflating the timeline. The
    message is redacted; metadata must already be non-sensitive and is stored as JSONB.
    """
    event_key = key or f"{stage}:{status}"
    resolved_category = classify(stage, message, category) if status in (
        "failure", "cancelled") else category

    def work(conn):
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM deployments WHERE dedup_key = %s",
                        (ident.dedup_key,))
            row = cur.fetchone()
            if not row:
                # Event before start: create a minimal deployment so nothing is dropped.
                start_row = _ensure_deployment(cur, ident)
                dep_id = start_row
            else:
                dep_id = row[0]
            next_seq = seq
            if next_seq is None:
                cur.execute(
                    "SELECT COALESCE(MAX(seq), 0) + 1 FROM deployment_events "
                    "WHERE deployment_id = %s", (dep_id,))
                next_seq = cur.fetchone()[0]
            meta_json = json.dumps(_sanitize_metadata(metadata)) if metadata else None
            cur.execute(
                """
                INSERT INTO deployment_events (
                    deployment_id, seq, event_key, stage, status, duration_ms, category,
                    message, metadata)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb)
                ON CONFLICT (deployment_id, event_key) DO NOTHING
                RETURNING id
                """,
                (dep_id, next_seq, event_key, stage, status, duration_ms,
                 resolved_category, redact(message) if message else None, meta_json),
            )
            inserted = cur.fetchone() is not None
        return {"deployment_id": dep_id, "event_key": event_key, "inserted": inserted,
                "category": resolved_category}

    return _guarded(work)


def _ensure_deployment(cur, ident: Identity) -> int:
    app_id = _upsert_application(cur, ident)
    cur.execute(
        """
        INSERT INTO deployments (dedup_key, application_id, application, environment,
            workflow, repository, github_run_id, run_attempt, started_at, status)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s, now(), 'running')
        ON CONFLICT (dedup_key) DO UPDATE SET updated_at = now()
        RETURNING id
        """,
        (ident.dedup_key, app_id, ident.app, ident.environment, ident.workflow,
         ident.repository, ident.run_id, int(ident.run_attempt)),
    )
    return cur.fetchone()[0]


def finish_deployment(ident: Identity, *, status: str, failure_stage=None,
                      failure_category=None, ended_at="now", message=None) -> dict | None:
    """Close the deployment. Only the current deployment's own lifecycle row is written.

    This transitions running -> success/failure/cancelled for THIS deployment; it never
    touches an older deployment's row and never deletes an event. On a failure the
    category is resolved from the stage/message if not given explicitly.
    """
    category = None
    if status in ("failure", "cancelled"):
        category = classify(failure_stage, message, failure_category)

    def work(conn):
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE deployments SET
                    status = %s,
                    ended_at = %s,
                    failure_stage = COALESCE(%s, failure_stage),
                    failure_category = COALESCE(%s, failure_category),
                    updated_at = now()
                WHERE dedup_key = %s
                RETURNING id
                """,
                (status, _ts(ended_at), failure_stage, category, ident.dedup_key),
            )
            row = cur.fetchone()
            if not row:
                dep_id = _ensure_deployment(cur, ident)
                cur.execute(
                    "UPDATE deployments SET status=%s, ended_at=%s, failure_stage=%s, "
                    "failure_category=%s, updated_at=now() WHERE id=%s",
                    (status, _ts(ended_at), failure_stage, category, dep_id))
            else:
                dep_id = row[0]
        return {"deployment_id": dep_id, "status": status, "failure_category": category}

    return _guarded(work)


def stored_category(ident: Identity) -> str | None:
    """The failure_category already recorded for this deployment, if any.

    Lets the notification reuse the SAME category ``finish_deployment`` computed from the
    structured failure, so the commit comment and the DB row never disagree. Guarded, so a
    missing/disabled store just yields None and the caller falls back to classify().
    """
    def work(conn):
        with conn.cursor() as cur:
            cur.execute("SELECT failure_category FROM deployments WHERE dedup_key = %s",
                        (ident.dedup_key,))
            row = cur.fetchone()
            return row[0] if row else None

    return _guarded(work)


def _sanitize_metadata(meta):
    """Redact string values inside caller-supplied metadata before it is stored as JSONB."""
    if isinstance(meta, dict):
        return {k: _sanitize_metadata(v) for k, v in meta.items()}
    if isinstance(meta, list):
        return [_sanitize_metadata(v) for v in meta]
    if isinstance(meta, str):
        return redact(meta)
    return meta


# --------------------------------------------------------------------------------------
# snapshot — read-only capture of what actually landed
# --------------------------------------------------------------------------------------
def _manifest_resources(docs: list[dict]) -> list[dict]:
    """Pull the resources and their shapes out of the rendered manifest (no cluster read)."""
    out: list[dict] = []
    for doc in docs:
        kind = doc.get("kind")
        if not kind:
            continue
        meta = doc.get("metadata") or {}
        spec = doc.get("spec") or {}
        entry = {
            "api_version": doc.get("apiVersion"),
            "kind": kind,
            "namespace": meta.get("namespace"),
            "name": meta.get("name"),
            "image_reference": None,
            "storage_class": None,
            "pvc": None,
            "gateway_parent": None,
            "gateway_listener": None,
            "metadata": {},
        }
        containers = (spec.get("template", {}).get("spec", {}).get("containers")
                      if isinstance(spec.get("template"), dict) else None)
        if containers:
            entry["image_reference"] = containers[0].get("image")
        if kind == "HTTPRoute":
            parents = spec.get("parentRefs") or []
            if parents:
                entry["gateway_parent"] = parents[0].get("name")
                entry["gateway_listener"] = parents[0].get("sectionName")
        if kind in ("PersistentVolumeClaim",):
            entry["storage_class"] = spec.get("storageClassName")
            entry["pvc"] = meta.get("name")
        if kind == "Cluster":  # CNPG
            entry["storage_class"] = (spec.get("storage") or {}).get("storageClass")
        out.append(entry)
    return out


def _kubectl_json(args, kubeconfig):
    cp = kubectl(args, kubeconfig=kubeconfig, check=False, capture=True)
    if cp.returncode != 0:
        return None
    try:
        return json.loads(cp.stdout)
    except (ValueError, TypeError):
        return None


def collect_snapshot(app: str, env: str, manifests: str, *, kubeconfig=None,
                     config_repo=None, config_commit=None, app_sha=None,
                     platform_sha=None, read_cluster=True) -> dict:
    """Build the snapshot payload read-only. Never opens a Secret.

    RBAC gaps are recorded as ``unknown``/warning in metadata, never as ``absent`` — an
    unreadable resource is not a missing one, and the snapshot must not claim otherwise.
    This function does NO database work; ``save_snapshot`` persists what it returns.
    """
    ns = _ctx.app_namespace(app, env)
    docs = _ctx.load_all(Path(manifests)) if manifests and Path(manifests).is_file() else []
    resources = _manifest_resources(docs)
    manifest_digest = hashlib.sha256(
        canonical_json([{"apiVersion": d.get("apiVersion"), "kind": d.get("kind"),
                         "metadata": {"name": (d.get("metadata") or {}).get("name")},
                         "spec": d.get("spec")} for d in docs]).encode()).hexdigest()

    gateway = listener = None
    for r in resources:
        if r["kind"] == "HTTPRoute":
            gateway, listener = r["gateway_parent"], r["gateway_listener"]
            break

    snap = {
        "application": app,
        "environment": env,
        "namespace": ns,
        "app_sha": app_sha,
        "platform_sha": platform_sha or os.environ.get("PLATFORM_SHA") or None,
        "config_repo": config_repo,
        "config_commit": config_commit,
        "database_backend": _ctx.database_backend(),
        "gateway": gateway,
        "listener": listener,
        "capability_flags": {k: bool(CONFIG.get(f"features.{k}", False)) for k in (
            "application_values", "vault_secrets", "postgres_application",
            "stack_onboarding")},
        "manifest_digest": manifest_digest,
        "resources": resources,
        "warnings": [],
    }

    if read_cluster:
        _enrich_from_cluster(snap, ns, kubeconfig)
    return snap


def _enrich_from_cluster(snap: dict, ns: str, kubeconfig) -> None:
    """Best-effort live read: image digests, VaultStaticSecret + Fleet conditions.

    Every read is check=False; a permission error becomes an ``unknown`` warning rather
    than a failure, because the snapshot is an observer and must not fail a deploy that the
    verify step already passed.
    """
    for r in snap["resources"]:
        if r["kind"] not in ("Deployment", "StatefulSet"):
            continue
        obj = _kubectl_json(["get", r["kind"].lower(), r["name"], "-n", ns, "-o", "json"],
                            kubeconfig)
        if obj is None:
            snap["warnings"].append(f"{r['kind']}/{r['name']}: unknown (not readable)")
            r["metadata"]["live"] = "unknown"
            continue
        status = obj.get("status") or {}
        spec = obj.get("spec") or {}
        r["desired_state"] = str(spec.get("replicas", ""))
        r["ready_state"] = str(status.get("readyReplicas", status.get("availableReplicas", "")))
        # Live image digest, if the container status exposes an imageID (repo@sha256:...).
        for cs in status.get("containerStatuses", []) if isinstance(status, dict) else []:
            image_id = cs.get("imageID") or ""
            if "@sha256:" in image_id:
                r["image_digest"] = image_id.split("@", 1)[1]
                break
    # VaultStaticSecret: record name + sync conditions ONLY. Never read the destination.
    vss = _kubectl_json(["get", "vaultstaticsecret", "-n", ns, "-o", "json"], kubeconfig)
    if vss is not None:
        for item in vss.get("items", []):
            name = (item.get("metadata") or {}).get("name")
            conditions = [{"type": c.get("type"), "status": c.get("status"),
                           "reason": c.get("reason")}
                          for c in (item.get("status") or {}).get("conditions", [])]
            snap["resources"].append({
                "api_version": item.get("apiVersion"), "kind": "VaultStaticSecret",
                "namespace": ns, "name": name, "image_reference": None,
                "storage_class": None, "pvc": None, "gateway_parent": None,
                "gateway_listener": None, "ready_state": None, "desired_state": None,
                "metadata": {"conditions": conditions}})
    elif snap["capability_flags"].get("vault_secrets"):
        snap["warnings"].append("VaultStaticSecret: unknown (not readable)")
    # Fleet GitRepo readiness + commit, from the state namespace.
    state_ns = CONFIG.get("kubernetes.state_namespace")
    gitrepos = _kubectl_json(["get", "gitrepo", "-n", state_ns, "-o", "json"], kubeconfig)
    if gitrepos is not None:
        for item in gitrepos.get("items", []):
            gstatus = item.get("status") or {}
            snap.setdefault("fleet", []).append({
                "name": (item.get("metadata") or {}).get("name"),
                "commit": gstatus.get("commit"),
                "readyBundles": gstatus.get("readyClusters"),
            })


def save_snapshot(ident: Identity, snap: dict) -> dict | None:
    """Persist a snapshot + its resources for a deployment. Re-capture updates in place."""
    def work(conn):
        with conn.cursor() as cur:
            app_id = _upsert_application(cur, ident)
            dep_id = _ensure_deployment(cur, ident)
            cur.execute(
                """
                INSERT INTO application_snapshots (
                    deployment_id, application_id, application, environment, app_sha,
                    platform_sha, config_repo, config_commit, namespace, database_backend,
                    gateway, listener, capability_flags, manifest_digest)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s)
                ON CONFLICT (deployment_id) DO UPDATE SET
                    app_sha = EXCLUDED.app_sha,
                    platform_sha = EXCLUDED.platform_sha,
                    config_repo = EXCLUDED.config_repo,
                    config_commit = EXCLUDED.config_commit,
                    namespace = EXCLUDED.namespace,
                    database_backend = EXCLUDED.database_backend,
                    gateway = EXCLUDED.gateway,
                    listener = EXCLUDED.listener,
                    capability_flags = EXCLUDED.capability_flags,
                    manifest_digest = EXCLUDED.manifest_digest,
                    captured_at = now()
                RETURNING id
                """,
                (dep_id, app_id, snap["application"], snap["environment"], snap.get("app_sha"),
                 snap.get("platform_sha"), snap.get("config_repo"), snap.get("config_commit"),
                 snap.get("namespace"), snap.get("database_backend"), snap.get("gateway"),
                 snap.get("listener"), json.dumps(snap.get("capability_flags") or {}),
                 snap.get("manifest_digest")),
            )
            snap_id = cur.fetchone()[0]
            # Also stamp platform_sha onto the deployment if it learned it now.
            if snap.get("platform_sha"):
                cur.execute("UPDATE deployments SET platform_sha = COALESCE(platform_sha, %s) "
                            "WHERE id = %s", (snap["platform_sha"], dep_id))
            for r in snap.get("resources", []):
                if not r.get("name") or not r.get("kind"):
                    continue
                meta = _sanitize_metadata(r.get("metadata") or {})
                cur.execute(
                    """
                    INSERT INTO deployed_resources (
                        deployment_id, snapshot_id, api_version, kind, namespace, name,
                        image_reference, image_digest, desired_state, ready_state,
                        storage_class, pvc, gateway_parent, gateway_listener, metadata)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb)
                    ON CONFLICT (deployment_id, kind, namespace, name) DO UPDATE SET
                        snapshot_id = EXCLUDED.snapshot_id,
                        image_reference = EXCLUDED.image_reference,
                        image_digest = COALESCE(EXCLUDED.image_digest, deployed_resources.image_digest),
                        desired_state = EXCLUDED.desired_state,
                        ready_state = EXCLUDED.ready_state,
                        storage_class = EXCLUDED.storage_class,
                        pvc = EXCLUDED.pvc,
                        gateway_parent = EXCLUDED.gateway_parent,
                        gateway_listener = EXCLUDED.gateway_listener,
                        metadata = EXCLUDED.metadata,
                        captured_at = now()
                    """,
                    (dep_id, snap_id, r.get("api_version"), r.get("kind"),
                     r.get("namespace") or snap.get("namespace"), r.get("name"),
                     r.get("image_reference"), r.get("image_digest"), r.get("desired_state"),
                     r.get("ready_state"), r.get("storage_class"), r.get("pvc"),
                     r.get("gateway_parent"), r.get("gateway_listener"), json.dumps(meta)),
                )
        return {"snapshot_id": snap_id, "deployment_id": dep_id,
                "resources": len(snap.get("resources", [])),
                "warnings": snap.get("warnings", [])}

    return _guarded(work)


# --------------------------------------------------------------------------------------
# KPI report
# --------------------------------------------------------------------------------------
def _pctl_expr(column: str) -> str:
    return (f"percentile_cont(0.5) WITHIN GROUP (ORDER BY {column}) AS p50, "
            f"percentile_cont(0.95) WITHIN GROUP (ORDER BY {column}) AS p95")


def report(*, date_from=None, date_to=None, app=None, environment=None) -> dict | None:
    """Operational KPI baseline for the platform. Not an end-user metric — there are no
    end users yet; this measures the deploy pipeline itself."""
    where = ["1=1"]
    params: list = []
    if date_from:
        where.append("started_at >= %s")
        params.append(_ts(date_from))
    if date_to:
        where.append("started_at <= %s")
        params.append(_ts(date_to))
    if app:
        where.append("application = %s")
        params.append(app)
    if environment:
        where.append("environment = %s")
        params.append(environment)
    clause = " AND ".join(where)

    def work(conn):
        with conn.cursor() as cur:
            cur.execute(f"""
                SELECT
                    count(*) AS total,
                    count(*) FILTER (WHERE status = 'success') AS succeeded,
                    count(*) FILTER (WHERE status = 'failure') AS failed,
                    count(*) FILTER (WHERE status = 'cancelled') AS cancelled,
                    count(*) FILTER (WHERE run_attempt = 1) AS first_attempts,
                    count(*) FILTER (WHERE run_attempt = 1 AND status = 'success')
                        AS first_attempt_success,
                    {_pctl_expr("EXTRACT(EPOCH FROM (ended_at - started_at))")}
                FROM deployments WHERE {clause} AND ended_at IS NOT NULL
                """, params)
            row = cur.fetchone()
            total, succeeded, failed, cancelled, first_attempts, first_ok, e2e_p50, e2e_p95 = row

            # Total including still-running (no ended_at) for the raw count KPI.
            cur.execute(f"SELECT count(*) FROM deployments WHERE {clause}", params)
            total_all = cur.fetchone()[0]

            durations = {}
            for label, stage in (("rollout", "rollout_verify"), ("fleet", "fleet_converge")):
                cur.execute(f"""
                    SELECT {_pctl_expr("duration_ms")}
                    FROM deployment_events e JOIN deployments d ON d.id = e.deployment_id
                    WHERE e.stage = %s AND e.duration_ms IS NOT NULL AND {clause}
                    """, [stage, *params])
                p50, p95 = cur.fetchone()
                durations[label] = {"p50_ms": _num(p50), "p95_ms": _num(p95)}

            cur.execute(f"""
                SELECT failure_category, failure_stage, count(*)
                FROM deployments
                WHERE status IN ('failure','cancelled') AND {clause}
                GROUP BY failure_category, failure_stage ORDER BY count(*) DESC
                """, params)
            failures = [{"category": c, "stage": s, "count": n}
                        for c, s, n in cur.fetchall()]

            cur.execute(f"""
                SELECT application, environment, count(*) AS deploys, max(started_at) AS last
                FROM deployments WHERE {clause}
                GROUP BY application, environment ORDER BY application, environment
                """, params)
            managed = [{"application": a, "environment": e, "deployments": n,
                        "last_deploy_at": _iso(t)} for a, e, n, t in cur.fetchall()]

        completed = (succeeded or 0) + (failed or 0) + (cancelled or 0)
        return {
            "total_deployments": total_all,
            "completed_deployments": completed,
            "succeeded": succeeded or 0,
            "failed": failed or 0,
            "cancelled": cancelled or 0,
            "success_rate": _rate(succeeded, completed),
            "first_attempt_success_rate": _rate(first_ok, first_attempts),
            "e2e_duration_seconds": {"p50": _num(e2e_p50), "p95": _num(e2e_p95)},
            "rollout_duration_ms": durations["rollout"],
            "fleet_convergence_ms": durations["fleet"],
            "failures_by_category": failures,
            "managed": managed,
        }

    return _guarded(work)


def _num(value):
    return round(float(value), 3) if value is not None else None


def _rate(part, whole):
    return round((part or 0) / whole, 4) if whole else None


def _iso(value):
    return value.astimezone(timezone.utc).isoformat() if isinstance(value, datetime) else None


def format_report(data: dict) -> str:
    """Human-readable table of the KPI dict."""
    if not data:
        return "audit disabled or unavailable — no report."
    lines = ["Platform deployment KPI (operational baseline)", "=" * 46]
    lines.append(f"total deployments        : {data['total_deployments']}")
    lines.append(f"completed                : {data['completed_deployments']} "
                 f"(ok {data['succeeded']} / fail {data['failed']} / cancel {data['cancelled']})")
    lines.append(f"success rate             : {_pct(data['success_rate'])}")
    lines.append(f"first-attempt success    : {_pct(data['first_attempt_success_rate'])}")
    e2e = data["e2e_duration_seconds"]
    lines.append(f"e2e duration (s)         : p50 {e2e['p50']}  p95 {e2e['p95']}")
    ro = data["rollout_duration_ms"]
    lines.append(f"rollout duration (ms)    : p50 {ro['p50_ms']}  p95 {ro['p95_ms']}")
    fl = data["fleet_convergence_ms"]
    lines.append(f"fleet convergence (ms)   : p50 {fl['p50_ms']}  p95 {fl['p95_ms']}")
    lines.append("failures by category/stage:")
    if data["failures_by_category"]:
        for f in data["failures_by_category"]:
            lines.append(f"  - {f['category']}/{f['stage']}: {f['count']}")
    else:
        lines.append("  (none)")
    lines.append("managed applications:")
    if data["managed"]:
        for m in data["managed"]:
            lines.append(f"  - {m['application']}/{m['environment']}: "
                         f"{m['deployments']} deploys, last {m['last_deploy_at']}")
    else:
        lines.append("  (none)")
    return "\n".join(lines)


def _pct(value):
    return f"{value * 100:.1f}%" if value is not None else "n/a"


# --------------------------------------------------------------------------------------
# failure notification — comment on the app commit that triggered the deploy
# --------------------------------------------------------------------------------------
# 2-4 things worth checking, per category. Short and actionable; whoever is paged should be
# able to start without reading the run log first.
_SUGGESTIONS = {
    "CONFIGURATION": [
        "Kiểm placeholder %%...%% chưa khai trong platform.env.yaml (render dừng ngay khi thiếu).",
        "So khối `environments.<env>` với giá trị app mong đợi (replicas, domain).",
        "Chạy `idpctl doctor --no-cluster` để đối chiếu feature/backend với config.",
    ],
    "SECRET_OR_VAULT": [
        "Kiểm VaultStaticSecret ở namespace app: condition SecretSynced=True chưa?",
        "Đường dẫn Vault có đúng `apps/<app>/<env>/<name>` và role đã onboard chưa?",
        "Xác nhận VSO đang chạy và VaultConnection/VaultAuthGlobal tồn tại.",
    ],
    "REGISTRY_OR_IMAGE": [
        "Ảnh `<registry>/<image>:<sha>` đã được CI build & push chưa?",
        "imagePullSecret có trong namespace và trỏ đúng registry không?",
        "Xem pod: ImagePullBackOff/ErrImagePull thường là sai tag hoặc thiếu quyền pull.",
    ],
    "DATABASE_OR_STORAGE": [
        "PVC đã Bound chưa? StorageClass có tồn tại trên cụm không?",
        "CNPG Cluster / StatefulSet postgres đã Ready chưa?",
        "Secret database đã sync để app kết nối được chưa?",
    ],
    "GATEWAY_OR_ROUTE": [
        "HTTPRoute có Accepted=True và ResolvedRefs=True không?",
        "`ingress.section_name` có khớp listener của Gateway (web vs websecure) không?",
        "Gateway `<name>`@`<namespace>` có PROGRAMMED=True không?",
    ],
    "FLEET_CONVERGENCE": [
        "GitRepo của Fleet đã trỏ đúng commit mới nhất chưa?",
        "Bundle có Ready không? Xem `kubectl get gitrepo,bundle` ở namespace state.",
        "Manifest đã được merge vào nhánh môi trường này đọc chưa (không kẹt ở PR)?",
    ],
    "KUBERNETES_ROLLOUT": [
        "Xem pod và event trong namespace app (CrashLoopBackOff? readiness fail?).",
        "updatedReplicas/observedGeneration đã bắt kịp generation mới chưa?",
        "Log container có lỗi khởi động (thiếu env, sai config) không?",
    ],
    "GIT_OR_PERMISSION": [
        "Token bot còn quyền ghi repo cấu hình / mở PR không?",
        "Nhánh môi trường có branch protection chặn push thẳng không?",
        "GH_HOST/GITHUB_API_URL có trỏ đúng máy chủ GitHub (public hay GHES) không?",
    ],
    "RUNNER_OR_TOOL": [
        "Runner self-hosted còn online và đúng nhãn không?",
        "score-k8s/score-compose có đúng version ghim trong platform.env.yaml không?",
        "Công cụ cần thiết (kubectl, vault, gh) có trên PATH của runner không?",
    ],
    "UNKNOWN": [
        "Mở run log ở link phía trên để xem stage nào dừng.",
        "Chạy lại luồng bằng tay qua `idpctl` để tái hiện đúng bước lỗi.",
    ],
}


def github_api_base() -> str:
    """API root that works on public GitHub and GHES alike, from the runtime environment.

    Actions sets GITHUB_API_URL directly (the public API root, or ``https://HOST/api/v3``
    on GHES). We never hard-code a host — a GHES install must work with no code change,
    only its own env.
    """
    api = os.environ.get("GITHUB_API_URL")
    if api:
        return api.rstrip("/")
    server = os.environ.get("GITHUB_SERVER_URL", "https://github.com").rstrip("/")
    return "https://api.github.com" if server == "https://github.com" else f"{server}/api/v3"


def marker(ident: Identity) -> str:
    return (f"<!-- idp-deployment:{ident.run_id}:{ident.run_attempt}:"
            f"{ident.app}:{ident.environment} -->")


def build_comment(ident: Identity, *, stage: str, category: str, run_url: str,
                  platform_sha: str | None = None) -> str:
    """The commit-comment body. Marker first so a re-run can find and update it."""
    suggestions = _SUGGESTIONS.get(category, _SUGGESTIONS["UNKNOWN"])
    lines = [
        marker(ident),
        f"### ❌ Deploy thất bại — `{ident.app}` → `{ident.environment}`",
        "",
        f"- **Stage lỗi:** `{stage or 'unknown'}`",
        f"- **Phân loại:** `{category}`",
        f"- **Run:** {run_url}" if run_url else "- **Run:** (không có URL)",
    ]
    if platform_sha:
        lines.append(f"- **Platform SHA:** `{platform_sha}`")
    lines += ["", "**Gợi ý kiểm tra:**"]
    lines += [f"{i}. {s}" for i, s in enumerate(suggestions, 1)]
    return "\n".join(lines)


def _http_request(method: str, url: str, token: str, payload: dict | None = None) -> dict:
    """One place doing GitHub REST. Separated so tests can monkeypatch it cleanly."""
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("X-GitHub-Api-Version", "2022-11-28")
    if data is not None:
        req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=15) as resp:  # noqa: S310 - api base is trusted
        body = resp.read().decode() or "{}"
    return json.loads(body) if body.strip() else {}


def notify_failure(ident: Identity, *, stage: str, category: str, commit_sha: str,
                   run_url: str, token: str | None = None, platform_sha: str | None = None,
                   repository: str | None = None) -> dict:
    """Comment the failure onto the app commit; update in place on a same-attempt re-run.

    Anti-spam: a hidden marker keyed by run/attempt/app/env. If a comment with that marker
    exists we PATCH it instead of adding another. If commenting is impossible we emit an
    Actions ::warning:: and, when the audit DB is reachable, record a notify-failure event
    — but we NEVER mask the underlying deploy failure, and never post a secret.
    """
    cfg = settings()
    result = {"posted": False, "updated": False, "reason": None}
    if not cfg.notify_failure:
        result["reason"] = "notify_failure disabled"
        return result
    repo = repository or ident.repository or os.environ.get("GITHUB_REPOSITORY", "")
    token = token or os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN") or ""
    body = build_comment(ident, stage=stage, category=category, run_url=run_url,
                         platform_sha=platform_sha)
    if not (repo and token and commit_sha):
        _fallback(ident, "thiếu repo/token/commit để bình luận", stage, category)
        result["reason"] = "missing repo, token or commit"
        return result
    base = github_api_base()
    try:
        listing = _http_request(
            "GET", f"{base}/repos/{repo}/commits/{commit_sha}/comments?per_page=100", token)
        existing = next((c for c in listing if isinstance(c, dict)
                         and marker(ident) in (c.get("body") or "")), None)
        if existing:
            _http_request("PATCH", f"{base}/repos/{repo}/comments/{existing['id']}", token,
                          {"body": body})
            result.update(posted=True, updated=True, comment_id=existing["id"])
        else:
            created = _http_request(
                "POST", f"{base}/repos/{repo}/commits/{commit_sha}/comments", token,
                {"body": body})
            result.update(posted=True, comment_id=created.get("id"))
    except Exception as exc:  # noqa: BLE001 - notification must never raise into the deploy
        _fallback(ident, redact(str(exc)), stage, category)
        result["reason"] = redact(str(exc))
    return result


def _fallback(ident: Identity, why: str, stage: str, category: str) -> None:
    """When commenting is impossible: warn loudly and, if the DB is up, leave a trace."""
    warn(f"không bình luận được lỗi deploy cho {ident.app}/{ident.environment} ({why}); "
         "lỗi deploy gốc vẫn giữ nguyên")
    record_event(ident, stage="notify_failure", status="warning", category=category,
                 message=f"notification fallback: {why}",
                 metadata={"stage": stage})
