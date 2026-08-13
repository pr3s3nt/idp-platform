#!/usr/bin/env bash
# Runtime E2E cho Vault/VSO trên harness kind.
#
# Bao phủ: foundation, policy/role theo app+env, secret-set, VSO sync, workload nhận biến,
# lọc _raw, từ chối prefix app khác, verify RBAC, Vault outage, bootstrap lại và hội tụ.
# Vault harness chạy dev mode nên outage bằng restart sẽ xoá dữ liệu Vault đang có.
# CHỈ chạy trên cụm thử nghiệm.
set -euo pipefail

CONTEXT=kind-staging
while [[ $# -gt 0 ]]; do
  case "$1" in
    --context) CONTEXT="$2"; shift 2 ;;
    -h|--help) sed -n '2,10p' "$0"; exit 0 ;;
    *) echo "tham số lạ: $1" >&2; exit 2 ;;
  esac
done

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
APP=vault-main
ENVN=staging
NS="$APP-$ENVN"
VAULT_NS=vault
VAULT_POD=vault-0
VSO_NS=vault-secrets-operator-system
DEV_ROOT_TOKEN="${VAULT_DEV_ROOT_TOKEN:-root}"
WORK="$(mktemp -d)"
PF_PID=""
VAULT_PORT=""
VAULT_WAS_STOPPED=0
K() { kubectl --context "$CONTEXT" "$@"; }
O() { python3 "$HERE/idpctl" --env-config "$HERE/platform.env.yaml" "$@"; }
V() {
  K -n "$VAULT_NS" exec -i "$VAULT_POD" -- \
    env "VAULT_TOKEN=$DEV_ROOT_TOKEN" VAULT_ADDR=http://127.0.0.1:8200 vault "$@"
}
start_port_forward() {
  if [[ -n "$PF_PID" ]]; then
    kill "$PF_PID" >/dev/null 2>&1 || true
    wait "$PF_PID" >/dev/null 2>&1 || true
  fi
  : >"$WORK/port-forward.log"
  # Để kubectl chọn cổng local rảnh. Sau một outage, forward cũ có thể chưa nhả cổng ngay
  # dù process đã nhận SIGTERM; dùng một số cố định biến race đó thành lỗi recovery giả.
  K -n "$VAULT_NS" port-forward svc/vault :8200 >"$WORK/port-forward.log" 2>&1 &
  PF_PID=$!
  VAULT_PORT=""
  for _ in $(seq 1 50); do
    kill -0 "$PF_PID" >/dev/null 2>&1 || break
    VAULT_PORT=$(sed -n 's/.*127\.0\.0\.1:\([0-9][0-9]*\) .*/\1/p' \
      "$WORK/port-forward.log" | head -1)
    [[ -n "$VAULT_PORT" ]] && return 0
    sleep .2
  done
  echo "không mở được port-forward tới Vault:" >&2
  sed -n '1,20p' "$WORK/port-forward.log" >&2
  exit 1
}

cleanup_vault_objects() {
  K -n "$VAULT_NS" get pod "$VAULT_POD" >/dev/null 2>&1 || return 0
  V kv metadata delete "kv/apps/$APP/$ENVN/probe" >/dev/null 2>&1 || true
  V delete "auth/kubernetes/role/idp-$APP-$ENVN" >/dev/null 2>&1 || true
  V policy delete "idp-$APP-$ENVN-read" >/dev/null 2>&1 || true
  V policy delete "idp-$APP-$ENVN-write" >/dev/null 2>&1 || true
}

cleanup() {
  [[ -n "$PF_PID" ]] && kill "$PF_PID" >/dev/null 2>&1 || true
  if [[ "$VAULT_WAS_STOPPED" == 1 ]]; then
    K -n "$VAULT_NS" scale statefulset/vault --replicas=1 >/dev/null 2>&1 || true
    K -n "$VAULT_NS" wait --for=condition=Ready pod/vault-0 --timeout=120s >/dev/null 2>&1 || true
  fi
  cleanup_vault_objects
  K delete namespace "$NS" --ignore-not-found --wait=false >/dev/null 2>&1 || true
  rm -rf "$WORK"
}
trap cleanup EXIT

CURRENT=$(kubectl config current-context)
[[ "$CURRENT" == "$CONTEXT" ]] || {
  echo "current context là $CURRENT; dung-vault-harness hiện apply foundation vào current context." >&2
  echo "Chuyển current context sang $CONTEXT rồi chạy lại để không bootstrap nhầm cụm." >&2
  exit 1
}

echo "==> bootstrap Vault/VSO foundation bằng source main hiện tại"
"$HERE/tools/dung-vault-harness.sh" --context "$CONTEXT" >/dev/null
O preflight --require-cluster --require-vault >/dev/null

echo "==> tạo namespace + danh tính VaultAuth riêng cho $APP/$ENVN"
K create namespace "$NS" --dry-run=client -o yaml | K apply -f - >/dev/null
O vault-onboard --app "$APP" --env "$ENVN" --apply >/dev/null

configure_app_vault() {
  O vault-onboard --app "$APP" --env "$ENVN" --print-policy | \
    V policy write "idp-$APP-$ENVN-read" - >/dev/null
  O vault-onboard --app "$APP" --env "$ENVN" --print-policy --write | \
    V policy write "idp-$APP-$ENVN-write" - >/dev/null
  V write "auth/kubernetes/role/idp-$APP-$ENVN" \
    "bound_service_account_names=idp-$APP" \
    "bound_service_account_namespaces=$NS" \
    "policies=idp-$APP-$ENVN-read" audience=vault ttl=1h >/dev/null
}
configure_app_vault

echo "==> ghi secret ngẫu nhiên qua chính lệnh secret-set (không in giá trị)"
SECRET_VALUE="$(openssl rand -hex 24)"
start_port_forward
SET_LOG=$(printf '%s' "$SECRET_VALUE" | \
  VAULT_ADDR="http://127.0.0.1:$VAULT_PORT" VAULT_TOKEN="$DEV_ROOT_TOKEN" \
  O secret-set --app "$APP" --env "$ENVN" --name probe --key api_key --stdin --replace 2>&1)
[[ "$SET_LOG" != *"$SECRET_VALUE"* ]] || { echo "secret-set làm lộ giá trị" >&2; exit 1; }
printf '%s\n' "$SET_LOG"

mkdir -p "$WORK/app/.score-values" "$WORK/render"
cat > "$WORK/app/score.yaml" <<'YAML'
apiVersion: score.dev/v1b1
metadata:
  name: vault-main
containers:
  app:
    image: .
    variables:
      VAULT_PROBE: "${resources.config.VAULT_PROBE}"
resources:
  config:
    type: environment
YAML
cat > "$WORK/app/.score-values/values.yaml" <<'YAML'
apiVersion: idp.company/v1
kind: ApplicationValues
spec:
  application: {}
  environments:
    staging:
      VAULT_PROBE:
        secretRef:
          name: probe
          key: api_key
YAML

echo "==> render VaultStaticSecret + Deployment từ source main"
O render --app "$APP" --image nginx --tag 1.27-alpine --env "$ENVN" \
  --registry docker.io/library --catalog "$HERE" --app-dir "$WORK/app" \
  --work "$WORK/render" --out "$WORK/out.yaml" --state-file "$WORK/state.yaml" >/dev/null

# Docker Hub là public; bỏ pull secret khỏi fixture để bài này chỉ đo Vault/VSO.
python3 - "$WORK/out.yaml" <<'PY'
import sys, yaml
p=sys.argv[1]
docs=[d for d in yaml.safe_load_all(open(p)) if d]
for d in docs:
    if d.get("kind") == "Deployment":
        d["spec"]["template"]["spec"].pop("imagePullSecrets", None)
open(p,"w").write(yaml.safe_dump_all(docs, sort_keys=False))
PY
[[ ! -e "$WORK/render/secrets.yaml" || ! -s "$WORK/render/secrets.yaml" ]] || {
  echo "render sinh Kubernetes Secret chứa giá trị — vi phạm" >&2; exit 1;
}
if grep -R -F "$SECRET_VALUE" "$WORK/app" "$WORK/render" "$WORK/out.yaml" >/dev/null; then
  echo "giá trị secret lọt vào file render" >&2; exit 1
fi

DEST=$(python3 - "$WORK/out.yaml" <<'PY'
import sys,yaml
v=next(d for d in yaml.safe_load_all(open(sys.argv[1])) if d and d.get("kind")=="VaultStaticSecret")
print(v["spec"]["destination"]["name"])
PY
)
VSS=$(python3 - "$WORK/out.yaml" <<'PY'
import sys,yaml
v=next(d for d in yaml.safe_load_all(open(sys.argv[1])) if d and d.get("kind")=="VaultStaticSecret")
print(v["metadata"]["name"])
PY
)

echo "==> apply và để orchestrator verify VSO sync trước rollout"
K -n "$NS" apply -f "$WORK/out.yaml" >/dev/null
O verify --app "$APP" --env "$ENVN" --manifests "$WORK/out.yaml" --timeout 180 >/dev/null

SYNCED_VALUE=$(K -n "$NS" get secret "$DEST" -o jsonpath='{.data.api_key}' | base64 -d)
[[ "$SYNCED_VALUE" == "$SECRET_VALUE" ]] || { echo "Secret đồng bộ sai giá trị" >&2; exit 1; }
KEYS=$(K -n "$NS" get secret "$DEST" -o json | python3 -c \
  'import json,sys; print(" ".join(sorted((json.load(sys.stdin).get("data") or {}).keys())))')
[[ "$KEYS" == api_key ]] || { echo "destination có key thừa: $KEYS" >&2; exit 1; }
POD=$(K -n "$NS" get pod -l app.kubernetes.io/name=vault-main -o jsonpath='{.items[0].metadata.name}')
POD_VALUE=$(K -n "$NS" exec "$POD" -- printenv VAULT_PROBE)
[[ "$POD_VALUE" == "$SECRET_VALUE" ]] || { echo "container không nhận đúng secret" >&2; exit 1; }
echo "   Synced · chỉ có api_key (không _raw) · container nhận đúng giá trị"

echo "==> negative gate: cùng VaultAuth không được đọc prefix app khác"
python3 - "$WORK/out.yaml" "$WORK/denied.yaml" <<'PY'
import sys,yaml
v=next(d for d in yaml.safe_load_all(open(sys.argv[1])) if d and d.get("kind")=="VaultStaticSecret")
v["metadata"]["name"]="vault-main-denied"
v["metadata"].setdefault("annotations",{})["idp.platform/vault-path"]="apps/otherapp/staging/probe"
v["spec"]["path"]="apps/otherapp/staging/probe"
v["spec"]["destination"]["name"]="vault-main-denied"
v["spec"].pop("rolloutRestartTargets",None)
open(sys.argv[2],"w").write(yaml.safe_dump(v,sort_keys=False))
PY
K -n "$NS" apply -f "$WORK/denied.yaml" >/dev/null
DENIED=0
for _ in $(seq 1 30); do
  S=$(K -n "$NS" get vaultstaticsecret vault-main-denied -o jsonpath='{.status.conditions[0].status}' 2>/dev/null || true)
  M=$(K -n "$NS" get vaultstaticsecret vault-main-denied -o jsonpath='{.status.conditions[0].message}' 2>/dev/null || true)
  if [[ "$S" == False && "$M" == *"permission denied"* ]]; then DENIED=1; break; fi
  sleep 2
done
[[ "$DENIED" == 1 ]] || { echo "prefix khác không bị từ chối rõ ràng" >&2; exit 1; }
! K -n "$NS" get secret vault-main-denied >/dev/null 2>&1 || {
  echo "VSO tạo Secret dù Vault từ chối" >&2; exit 1;
}
K -n "$NS" delete vaultstaticsecret vault-main-denied >/dev/null
echo "   permission denied đúng prefix; không tạo destination Secret"

echo "==> verify RBAC: đọc status được, đọc Secret và namespace khác không được"
O verify-rbac --app "$APP" --env "$ENVN" --apply >/dev/null
VERIFY_SA=$(K -n "$NS" get serviceaccount -o name | sed 's#serviceaccount/##' | grep verify | head -1)
SUBJECT="system:serviceaccount:$NS:$VERIFY_SA"
[[ $(K auth can-i get secrets -n "$NS" --as="$SUBJECT") == no ]]
[[ $(K auth can-i get vaultstaticsecrets.secrets.hashicorp.com -n "$NS" --as="$SUBJECT") == yes ]]
[[ $(K auth can-i get pods -n sample-nginx-staging --as="$SUBJECT") == no ]]
echo "   Secret=no · VaultStaticSecret=yes · namespace khác=no"

echo "==> outage: dừng Vault, app đang chạy vẫn giữ secret đã nạp"
K -n "$VAULT_NS" scale statefulset/vault --replicas=0 >/dev/null
VAULT_WAS_STOPPED=1
K -n "$VAULT_NS" wait --for=delete pod/vault-0 --timeout=120s >/dev/null
[[ $(K -n "$NS" get deploy vault-main -o jsonpath='{.status.availableReplicas}') == 1 ]]
POD_VALUE=$(K -n "$NS" exec "$POD" -- printenv VAULT_PROBE)
[[ "$POD_VALUE" == "$SECRET_VALUE" ]]
K -n "$NS" annotate vaultstaticsecret "$VSS" idp.platform/outage-probe="$(date +%s)" --overwrite >/dev/null
OUTAGE_SEEN=0
for _ in $(seq 1 30); do
  S=$(K -n "$NS" get vaultstaticsecret "$VSS" -o jsonpath='{.status.conditions[0].status}' 2>/dev/null || true)
  M=$(K -n "$NS" get vaultstaticsecret "$VSS" -o jsonpath='{.status.conditions[0].message}' 2>/dev/null || true)
  if [[ "$S" == False && "$M" == *"connection refused"* ]]; then OUTAGE_SEEN=1; break; fi
  sleep 2
done
if [[ "$OUTAGE_SEEN" != 1 ]]; then
  # Một số reconcile giữ condition cũ nhưng controller vẫn báo lỗi nguồn. Chấp nhận bằng
  # chứng log trực tiếp, không báo pass mù.
  LOGS=$(K -n "$VSO_NS" logs deploy/vault-secrets-operator-controller-manager -c manager --since=2m 2>/dev/null || true)
  [[ "$LOGS" == *"connection refused"* ]] || { echo "VSO không ghi nhận Vault outage" >&2; exit 1; }
fi
echo "   app vẫn Running; VSO ghi nhận connection refused"

echo "==> xoá destination trong lúc Vault sập, rồi bootstrap Vault và hội tụ lại"
K -n "$NS" delete secret "$DEST" >/dev/null
K -n "$VAULT_NS" scale statefulset/vault --replicas=1 >/dev/null
K -n "$VAULT_NS" wait --for=condition=Ready pod/vault-0 --timeout=120s >/dev/null
VAULT_WAS_STOPPED=0
"$HERE/tools/dung-vault-harness.sh" --context "$CONTEXT" >/dev/null
O vault-onboard --app "$APP" --env "$ENVN" --apply >/dev/null
configure_app_vault
start_port_forward
printf '%s' "$SECRET_VALUE" | \
  VAULT_ADDR="http://127.0.0.1:$VAULT_PORT" VAULT_TOKEN="$DEV_ROOT_TOKEN" \
  O secret-set --app "$APP" --env "$ENVN" --name probe --key api_key --stdin --replace >/dev/null
K -n "$NS" annotate vaultstaticsecret "$VSS" idp.platform/recovery-probe="$(date +%s)" --overwrite >/dev/null
for _ in $(seq 1 60); do
  S=$(K -n "$NS" get vaultstaticsecret "$VSS" -o jsonpath='{.status.conditions[0].status}' 2>/dev/null || true)
  R=$(K -n "$NS" get vaultstaticsecret "$VSS" -o jsonpath='{.status.conditions[0].reason}' 2>/dev/null || true)
  K -n "$NS" get secret "$DEST" >/dev/null 2>&1 && [[ "$S" == True && "$R" == Synced ]] && break
  sleep 2
done
SYNCED_VALUE=$(K -n "$NS" get secret "$DEST" -o jsonpath='{.data.api_key}' | base64 -d)
[[ "$SYNCED_VALUE" == "$SECRET_VALUE" ]] || { echo "recovery sync sai" >&2; exit 1; }
O verify --app "$APP" --env "$ENVN" --manifests "$WORK/out.yaml" --timeout 180 >/dev/null
echo "   Vault bootstrap lại · VSO Synced · destination được tạo lại · verify pass"

echo "==> ownerReference: xoá VaultStaticSecret phải thu hồi destination Secret"
OWNER=$(K -n "$NS" get secret "$DEST" -o jsonpath='{.metadata.ownerReferences[0].kind}')
[[ "$OWNER" == VaultStaticSecret ]] || { echo "owner=$OWNER, mong đợi VaultStaticSecret"; exit 1; }
K -n "$NS" delete vaultstaticsecret "$VSS" >/dev/null
K -n "$NS" wait --for=delete "secret/$DEST" --timeout=60s >/dev/null

echo
echo "==> PASS: Vault/VSO E2E · prefix isolation · no-secret RBAC · outage continuity · recovery · owner cleanup."
