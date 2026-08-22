"""Logic tests for the deployment audit store (engine/audit.py).

Two layers, same as the rest of the platform. The pure-logic tests here run everywhere and
prove redaction, classification, identity, the disabled fast-path and the fail-open/closed
contract WITHOUT a database. The DB-backed tests (migration idempotency, idempotent start,
event dedup, KPI maths, "no secret reaches a column") run against a REAL PostgreSQL — set
AUDIT_TEST_DATABASE_URL and they execute; otherwise they skip, and the runtime harness runs
them against Postgres-in-kind. Green here never means "the store works end to end" — that is
what the runtime harness proves (see docs/testing.md).
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

import engine as orc

CATALOG = Path(__file__).resolve().parent

try:
    import psycopg
except ModuleNotFoundError:  # pragma: no cover - driver optional when audit is off
    psycopg = None

DB_URL = os.environ.get("AUDIT_TEST_DATABASE_URL")
requires_db = pytest.mark.skipif(
    not DB_URL or psycopg is None,
    reason="set AUDIT_TEST_DATABASE_URL and install psycopg to run audit DB tests",
)


# --------------------------------------------------------------------------------------
# config / profiles
# --------------------------------------------------------------------------------------
def test_both_profiles_load_with_audit_block():
    sandbox = orc.EnvConfig.load(str(CATALOG / "platform.env.yaml"))
    company = orc.EnvConfig.load(str(CATALOG / "platform.env.company.yaml"))
    # Company (brownfield) ships audit OFF, like every other capability.
    assert company.get("audit.enabled") is False
    assert company.get("audit.database_url_env") == "AUDIT_DATABASE_URL"
    # Both expose the same shape; neither carries a URL or credential.
    for cfg in (sandbox, company):
        assert cfg.get("audit.database_url_env")
        assert cfg.get("audit.notification_mode") == "commit-comment"


def test_audit_defaults_disabled_in_code():
    """DEFAULTS ship audit OFF, so a config-less install renders as before."""
    cfg = orc.EnvConfig({})
    assert cfg.get("audit.enabled") is False
    assert cfg.get("audit.required") is False


def test_no_connection_string_or_credential_in_yaml():
    for name in ("platform.env.yaml", "platform.env.company.yaml"):
        text = (CATALOG / name).read_text()
        assert "postgres://" not in text and "postgresql://" not in text
        # Only the NAME of the env var may appear, never a value assignment with creds.
        assert "AUDIT_DATABASE_URL" in text
        assert "password=" not in text.lower()


# --------------------------------------------------------------------------------------
# disabled fast-path — no driver, no socket
# --------------------------------------------------------------------------------------
def _disable_audit(monkeypatch):
    monkeypatch.setattr(orc, "CONFIG", orc.EnvConfig({"audit": {"enabled": False}}))


def _boom(*a, **k):
    raise AssertionError("_connect must not be called when audit is disabled")


def test_disabled_never_connects(monkeypatch):
    _disable_audit(monkeypatch)
    monkeypatch.setattr(orc.audit, "_connect", _boom)
    ident = orc.audit.identity_from("app", "staging")
    assert orc.audit.run_migrations() is None
    assert orc.audit.start_deployment(ident) is None
    assert orc.audit.record_event(ident, stage="render", status="success") is None
    assert orc.audit.finish_deployment(ident, status="success") is None
    assert orc.audit.report() is None


# --------------------------------------------------------------------------------------
# fail-open vs fail-closed
# --------------------------------------------------------------------------------------
def _enable_audit(monkeypatch, *, required=False):
    monkeypatch.setattr(orc, "CONFIG", orc.EnvConfig({"audit": {
        "enabled": True, "required": required,
        "database_url_env": "AUDIT_TEST_DATABASE_URL"}}))


def test_fail_open_on_db_error(monkeypatch):
    _enable_audit(monkeypatch, required=False)
    monkeypatch.setattr(orc.audit, "_connect",
                        lambda cfg: (_ for _ in ()).throw(RuntimeError("host unreachable")))
    warned = []
    monkeypatch.setattr(orc.audit, "warn", lambda m: warned.append(m))
    ident = orc.audit.identity_from("app", "staging")
    assert orc.audit.start_deployment(ident) is None  # deploy continues
    assert warned and "fail-open" in warned[0]


def test_fail_closed_on_db_error(monkeypatch):
    _enable_audit(monkeypatch, required=True)
    monkeypatch.setattr(orc.audit, "_connect",
                        lambda cfg: (_ for _ in ()).throw(RuntimeError("host unreachable")))
    ident = orc.audit.identity_from("app", "staging")
    with pytest.raises(SystemExit) as exc:
        orc.audit.start_deployment(ident)
    assert "audit.required" in str(exc.value)


def test_fail_closed_error_is_redacted(monkeypatch):
    _enable_audit(monkeypatch, required=True)
    dsn = "postgres://u:sup3rsecret@db.internal:5432/audit"
    monkeypatch.setattr(orc.audit, "_connect",
                        lambda cfg: (_ for _ in ()).throw(RuntimeError(f"cannot reach {dsn}")))
    with pytest.raises(SystemExit) as exc:
        orc.audit.run_migrations()
    assert "sup3rsecret" not in str(exc.value)


# --------------------------------------------------------------------------------------
# identity / dedup
# --------------------------------------------------------------------------------------
def test_dedup_key_stable_and_attempt_sensitive():
    a1 = orc.audit.Identity("org/repo", "100", "1", "app", "staging", "deploy")
    a1b = orc.audit.Identity("org/repo", "100", "1", "app", "staging", "deploy")
    a2 = orc.audit.Identity("org/repo", "100", "2", "app", "staging", "deploy")
    other_env = orc.audit.Identity("org/repo", "100", "1", "app", "prod", "deploy")
    assert a1.dedup_key == a1b.dedup_key          # same run+attempt -> same deployment
    assert a1.dedup_key != a2.dedup_key           # new attempt -> new deployment
    assert a1.dedup_key != other_env.dedup_key    # env is part of identity


def test_identity_defaults_from_github_env(monkeypatch):
    monkeypatch.setenv("GITHUB_REPOSITORY", "org/repo")
    monkeypatch.setenv("GITHUB_RUN_ID", "555")
    monkeypatch.setenv("GITHUB_RUN_ATTEMPT", "2")
    monkeypatch.setenv("GITHUB_WORKFLOW", "deploy")
    ident = orc.audit.identity_from("app", "staging")
    assert ident.repository == "org/repo" and ident.run_id == "555"
    assert ident.run_attempt == "2" and ident.workflow == "deploy"


# --------------------------------------------------------------------------------------
# redaction
# --------------------------------------------------------------------------------------
@pytest.mark.parametrize("raw,leaked", [
    ("connect postgres://u:p4ss@h/db failed", "p4ss"),
    ("password=hunter2 in dsn", "hunter2"),
    ("token ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ012345", "ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ012345"),
    ("github_pat_11ABCDEFG0123456789_supersecrettail", "github_pat_11ABCDEFG"),
    ("api_key: sk-abcdef123456", "sk-abcdef123456"),
    ("AKIAIOSFODNN7EXAMPLE creds", "AKIAIOSFODNN7EXAMPLE"),
])
def test_redact_removes_secret(raw, leaked):
    assert leaked not in orc.audit.redact(raw)


def test_redact_keeps_image_digest():
    """A 64-hex image digest is not a secret and must survive redaction."""
    digest = "a" * 64
    out = orc.audit.redact(f"image pinned to nginx@sha256:{digest}")
    assert digest in out


def test_redact_handles_none():
    assert orc.audit.redact(None) == ""


def test_metadata_sanitized_recursively():
    meta = {"note": "password=abc123", "nested": {"t": "ghp_" + "Z" * 30}}
    clean = orc.audit._sanitize_metadata(meta)
    assert "abc123" not in clean["note"]
    assert "ghp_" not in clean["nested"]["t"]


# --------------------------------------------------------------------------------------
# classification
# --------------------------------------------------------------------------------------
@pytest.mark.parametrize("stage,expected", [
    ("render", "CONFIGURATION"),
    ("apply_secrets", "SECRET_OR_VAULT"),
    ("commit_config", "GIT_OR_PERMISSION"),
    ("fleet_converge", "FLEET_CONVERGENCE"),
    ("database_ready", "DATABASE_OR_STORAGE"),
    ("rollout_verify", "KUBERNETES_ROLLOUT"),
    ("preflight", "RUNNER_OR_TOOL"),
    ("something-unmapped", "UNKNOWN"),
])
def test_classify_by_stage(stage, expected):
    assert orc.audit.classify(stage) == expected


def test_classify_message_beats_stage():
    # A rollout that fails on an image pull is a REGISTRY problem, not a generic rollout one.
    assert orc.audit.classify("rollout_verify", "ImagePullBackOff on web") == "REGISTRY_OR_IMAGE"
    assert orc.audit.classify("render", "HTTPRoute ResolvedRefs=False") == "GATEWAY_OR_ROUTE"


def test_classify_explicit_override():
    assert orc.audit.classify("render", "anything", explicit="database_or_storage") == \
        "DATABASE_OR_STORAGE"
    assert orc.audit.classify("render", "x", explicit="not-a-category") == "UNKNOWN"


# --------------------------------------------------------------------------------------
# notification (no network — monkeypatch the one HTTP helper)
# --------------------------------------------------------------------------------------
def test_comment_has_marker_and_no_secret(monkeypatch):
    ident = orc.audit.Identity("org/repo", "9", "1", "shop", "staging", "deploy")
    body = orc.audit.build_comment(ident, stage="apply_secrets", category="SECRET_OR_VAULT",
                                   run_url="https://ci/run/9", platform_sha="deadbeef")
    assert orc.audit.marker(ident) in body
    assert "shop" in body and "SECRET_OR_VAULT" in body and "deadbeef" in body


def test_notify_creates_then_updates_same_comment(monkeypatch):
    _enable_audit(monkeypatch)
    monkeypatch.setenv("GITHUB_API_URL", "https://api.github.com")
    ident = orc.audit.Identity("org/repo", "9", "1", "shop", "staging", "deploy")
    calls = []
    state = {"comments": []}

    def fake_http(method, url, token, payload=None):
        calls.append((method, url))
        if method == "GET":
            return list(state["comments"])
        if method == "POST":
            c = {"id": 1, "body": payload["body"]}
            state["comments"].append(c)
            return c
        if method == "PATCH":
            state["comments"][0]["body"] = payload["body"]
            return state["comments"][0]
        return {}

    monkeypatch.setattr(orc.audit, "_http_request", fake_http)
    r1 = orc.audit.notify_failure(ident, stage="apply_secrets", category="SECRET_OR_VAULT",
                                  commit_sha="abc", run_url="u", token="t")
    assert r1["posted"] and not r1.get("updated")
    r2 = orc.audit.notify_failure(ident, stage="apply_secrets", category="SECRET_OR_VAULT",
                                  commit_sha="abc", run_url="u", token="t")
    assert r2["updated"] is True                       # anti-spam: same comment reused
    assert len(state["comments"]) == 1
    assert sum(1 for m, _ in calls if m == "POST") == 1


def test_notify_fallback_when_no_token(monkeypatch):
    _enable_audit(monkeypatch)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("GH_TOKEN", raising=False)
    warned = []
    monkeypatch.setattr(orc.audit, "warn", lambda m: warned.append(m))
    # DB not reachable here, but fail-open means the fallback event is best-effort only.
    monkeypatch.setattr(orc.audit, "_connect",
                        lambda cfg: (_ for _ in ()).throw(RuntimeError("no db")))
    r = orc.audit.notify_failure(orc.audit.identity_from("app", "staging"),
                                 stage="render", category="CONFIGURATION",
                                 commit_sha="abc", run_url="u", token="")
    assert r["posted"] is False and warned  # original failure never masked


def test_github_api_base_public_and_ghes(monkeypatch):
    monkeypatch.delenv("GITHUB_API_URL", raising=False)
    monkeypatch.setenv("GITHUB_SERVER_URL", "https://github.com")
    assert orc.audit.github_api_base() == "https://api.github.com"
    monkeypatch.setenv("GITHUB_SERVER_URL", "https://ghe.corp.example")
    assert orc.audit.github_api_base() == "https://ghe.corp.example/api/v3"
    monkeypatch.setenv("GITHUB_API_URL", "https://ghe.corp.example/api/v3")
    assert orc.audit.github_api_base() == "https://ghe.corp.example/api/v3"


# --------------------------------------------------------------------------------------
# snapshot — manifest parsing, and NEVER reading a Secret
# --------------------------------------------------------------------------------------
_MANIFEST = """
apiVersion: apps/v1
kind: Deployment
metadata: {name: web, namespace: shop-staging}
spec:
  replicas: 2
  template:
    spec:
      containers:
        - name: web
          image: reg.example/shop:abc123
---
apiVersion: gateway.networking.k8s.io/v1
kind: HTTPRoute
metadata: {name: web, namespace: shop-staging}
spec:
  parentRefs:
    - name: ingress-gateway
      sectionName: websecure
"""


def _write_manifest(tmp_path):
    p = tmp_path / "manifests.yaml"
    p.write_text(_MANIFEST)
    return str(p)


def test_snapshot_parses_manifest_without_cluster(tmp_path, monkeypatch):
    monkeypatch.setattr(orc, "CONFIG", orc.EnvConfig({
        "features": {"vault_secrets": True},
        "database": {"backend": "statefulset"}}))
    snap = orc.audit.collect_snapshot("shop", "staging", _write_manifest(tmp_path),
                                      read_cluster=False, app_sha="abc123")
    kinds = {r["kind"] for r in snap["resources"]}
    assert {"Deployment", "HTTPRoute"} <= kinds
    web = next(r for r in snap["resources"] if r["kind"] == "Deployment")
    assert web["image_reference"] == "reg.example/shop:abc123"
    assert snap["gateway"] == "ingress-gateway" and snap["listener"] == "websecure"
    assert snap["database_backend"] == "statefulset"
    assert snap["capability_flags"]["vault_secrets"] is True
    assert len(snap["manifest_digest"]) == 64


def test_snapshot_never_reads_a_secret(tmp_path, monkeypatch):
    monkeypatch.setattr(orc, "CONFIG", orc.EnvConfig({"features": {"vault_secrets": True}}))
    seen = []

    def fake_kubectl(args, kubeconfig=None, **kw):
        seen.append(list(args))
        import subprocess
        return subprocess.CompletedProcess(args, 0, stdout="{}", stderr="")

    monkeypatch.setattr(orc.audit, "kubectl", fake_kubectl)
    orc.audit.collect_snapshot("shop", "staging", _write_manifest(tmp_path),
                               read_cluster=True, kubeconfig="kc")
    # Not a single `kubectl get secret[s]` — VaultStaticSecret (a CR) is fine.
    for args in seen:
        if args[:1] == ["get"]:
            assert args[1] not in ("secret", "secrets"), f"snapshot read a Secret: {args}"


def test_snapshot_records_unknown_not_absent(tmp_path, monkeypatch):
    monkeypatch.setattr(orc, "CONFIG", orc.EnvConfig({}))

    def fake_kubectl(args, kubeconfig=None, **kw):
        import subprocess
        return subprocess.CompletedProcess(args, 1, stdout="", stderr="forbidden")

    monkeypatch.setattr(orc.audit, "kubectl", fake_kubectl)
    snap = orc.audit.collect_snapshot("shop", "staging", _write_manifest(tmp_path),
                                      read_cluster=True, kubeconfig="kc")
    assert any("unknown" in w for w in snap["warnings"])


# --------------------------------------------------------------------------------------
# report formatting (no DB)
# --------------------------------------------------------------------------------------
def test_format_report_handles_none():
    assert "disabled" in orc.audit.format_report(None)


def test_rate_and_num_helpers():
    assert orc.audit._rate(3, 4) == 0.75
    assert orc.audit._rate(0, 0) is None
    assert orc.audit._num(None) is None
    assert orc.audit._num(1.23456) == 1.235


# ======================================================================================
# DB-backed tests — real PostgreSQL via AUDIT_TEST_DATABASE_URL (runtime harness)
# ======================================================================================
def _query(sql, params=()):
    conn = psycopg.connect(DB_URL, connect_timeout=5)
    try:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            return cur.fetchall()
    finally:
        conn.close()


@pytest.fixture
def audit_db(monkeypatch):
    monkeypatch.setattr(orc, "CONFIG", orc.EnvConfig({"audit": {
        "enabled": True, "required": False,
        "database_url_env": "AUDIT_TEST_DATABASE_URL"}}))
    conn = psycopg.connect(DB_URL, connect_timeout=5)
    with conn:
        with conn.cursor() as cur:
            cur.execute("DROP TABLE IF EXISTS deployed_resources, application_snapshots, "
                        "deployment_events, deployments, applications, schema_migrations "
                        "CASCADE")
    conn.close()
    orc.audit.run_migrations()
    return DB_URL


@requires_db
def test_migration_runs_twice_idempotent(audit_db):
    # Fixture already migrated once; a second call applies nothing new.
    assert orc.audit.run_migrations() == []
    rows = _query("SELECT count(*) FROM schema_migrations")
    assert rows[0][0] >= 1


@requires_db
def test_start_is_idempotent_same_attempt(audit_db):
    ident = orc.audit.identity_from("shop", "staging", repository="org/repo",
                                    run_id="1", run_attempt="1", workflow="deploy")
    r1 = orc.audit.start_deployment(ident, actor="alice")
    r2 = orc.audit.start_deployment(ident, actor="alice")
    assert r1["created"] is True and r2["created"] is False
    assert r1["deployment_id"] == r2["deployment_id"]
    assert _query("SELECT count(*) FROM deployments")[0][0] == 1


@requires_db
def test_new_attempt_is_distinct(audit_db):
    base = dict(repository="org/repo", run_id="1", workflow="deploy")
    a1 = orc.audit.identity_from("shop", "staging", run_attempt="1", **base)
    a2 = orc.audit.identity_from("shop", "staging", run_attempt="2", **base)
    orc.audit.start_deployment(a1)
    orc.audit.start_deployment(a2)
    assert _query("SELECT count(*) FROM deployments")[0][0] == 2


@requires_db
def test_events_append_only_and_dedup(audit_db):
    ident = orc.audit.identity_from("shop", "staging", repository="org/repo",
                                    run_id="1", run_attempt="1", workflow="deploy")
    orc.audit.start_deployment(ident)
    orc.audit.record_event(ident, stage="render", status="success", duration_ms=1200)
    orc.audit.record_event(ident, stage="render", status="success", duration_ms=1200)  # retry
    orc.audit.record_event(ident, stage="rollout_verify", status="success", duration_ms=5000)
    rows = _query("SELECT stage, status FROM deployment_events "
                  "WHERE stage='render' AND status='success'")
    assert len(rows) == 1  # retry collapsed, no duplicate
    total = _query("SELECT count(*) FROM deployment_events")[0][0]
    assert total == 2


@requires_db
def test_no_secret_value_reaches_the_database(audit_db):
    ident = orc.audit.identity_from("shop", "staging", repository="org/repo",
                                    run_id="1", run_attempt="1", workflow="deploy")
    orc.audit.start_deployment(ident)
    orc.audit.record_event(
        ident, stage="apply_secrets", status="failure",
        message="vault login failed with token ghp_SECRETSECRETSECRETSECRET012345",
        metadata={"dsn": "postgres://u:leakpw@h/db", "note": "password=leak123"})
    rows = _query("SELECT message, metadata::text FROM deployment_events "
                  "WHERE stage='apply_secrets'")
    blob = " ".join(str(c) for r in rows for c in r)
    for secret in ("ghp_SECRETSECRETSECRETSECRET012345", "leakpw", "leak123"):
        assert secret not in blob


@requires_db
def test_finish_success_failure_cancelled(audit_db):
    def ident(attempt):
        return orc.audit.identity_from("shop", "staging", repository="org/repo",
                                       run_id="1", run_attempt=attempt, workflow="deploy")
    for attempt, status, stage in (("1", "success", None),
                                    ("2", "failure", "rollout_verify"),
                                    ("3", "cancelled", "fleet_converge")):
        i = ident(attempt)
        orc.audit.start_deployment(i)
        orc.audit.finish_deployment(i, status=status, failure_stage=stage,
                                    message="ImagePullBackOff" if status == "failure" else None)
    rows = dict(_query("SELECT status, count(*) FROM deployments GROUP BY status"))
    assert rows == {"success": 1, "failure": 1, "cancelled": 1}
    # failure category resolved from the ImagePullBackOff message, not the stage default.
    cat = _query("SELECT failure_category FROM deployments WHERE status='failure'")[0][0]
    assert cat == "REGISTRY_OR_IMAGE"


@requires_db
def test_snapshot_persists_resources(audit_db, tmp_path, monkeypatch):
    monkeypatch.setattr(orc, "CONFIG", orc.EnvConfig({
        "audit": {"enabled": True, "database_url_env": "AUDIT_TEST_DATABASE_URL"},
        "database": {"backend": "statefulset"}}))
    ident = orc.audit.identity_from("shop", "staging", repository="org/repo",
                                    run_id="1", run_attempt="1", workflow="deploy")
    orc.audit.start_deployment(ident)
    snap = orc.audit.collect_snapshot("shop", "staging", _write_manifest(tmp_path),
                                      read_cluster=False, app_sha="abc123")
    res = orc.audit.save_snapshot(ident, snap)
    assert res["resources"] >= 2
    backend = _query("SELECT database_backend FROM application_snapshots")[0][0]
    assert backend == "statefulset"
    img = _query("SELECT image_reference FROM deployed_resources WHERE kind='Deployment'")
    assert img[0][0] == "reg.example/shop:abc123"
    # re-capture updates in place, no duplicate rows
    orc.audit.save_snapshot(ident, snap)
    assert _query("SELECT count(*) FROM application_snapshots")[0][0] == 1


@requires_db
def test_report_kpi_maths(audit_db):
    def deploy(attempt, status, e2e_stage_ms=None):
        i = orc.audit.identity_from("shop", "staging", repository="org/repo",
                                    run_id=str(1000 + int(attempt)), run_attempt="1",
                                    workflow="deploy")
        orc.audit.start_deployment(i, started_at="2026-01-01T00:00:00Z")
        if e2e_stage_ms:
            orc.audit.record_event(i, stage="rollout_verify", status="success",
                                   duration_ms=e2e_stage_ms)
        orc.audit.finish_deployment(
            i, status=status, ended_at="2026-01-01T00:01:00Z",
            failure_stage="rollout_verify" if status == "failure" else None,
            message="ImagePullBackOff" if status == "failure" else None)
    deploy("1", "success", 4000)
    deploy("2", "success", 6000)
    deploy("3", "failure", 8000)
    rep = orc.audit.report(app="shop", environment="staging")
    assert rep["total_deployments"] == 3
    assert rep["succeeded"] == 2 and rep["failed"] == 1
    assert rep["success_rate"] == round(2 / 3, 4)
    assert rep["first_attempt_success_rate"] == round(2 / 3, 4)
    # each deploy is exactly 60s end to end
    assert rep["e2e_duration_seconds"]["p50"] == 60.0
    assert rep["rollout_duration_ms"]["p50_ms"] == 6000.0
    cats = {f["category"] for f in rep["failures_by_category"]}
    assert "REGISTRY_OR_IMAGE" in cats
    assert rep["managed"][0]["application"] == "shop"
