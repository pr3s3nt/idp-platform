#!/usr/bin/env bash
# Dựng Vault + Vault Secrets Operator cho HARNESS LOCAL (Phase 2).
#
# CHỈ DÙNG CHO MÁY DEV. Vault dựng ở đây chạy dev mode: in-memory, unseal sẵn, token gốc
# biết trước. Công ty đã có Vault thật rồi — ở đó bạn KHÔNG chạy script này, bạn chỉ điền
# `vault.*` trong platform.env.company.yaml rồi chạy phần cuối (`vault-foundation`).
#
# Mọi toạ độ đọc từ platform.env.yaml qua `orchestrate.py config --get`. Không có giá trị
# hạ tầng nào viết cứng trong file này — đổi Vault/mount/namespace là sửa config, không
# sửa script.
#
#   ./tools/dung-vault-harness.sh                       # cụm kubectl đang trỏ tới
#   ./tools/dung-vault-harness.sh --context kind-staging
#
# Chạy lại được nhiều lần (idempotent).
set -euo pipefail

ENV_CONFIG="platform.env.yaml"
CONTEXT=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --env-config) ENV_CONFIG="$2"; shift 2 ;;
    --context)    CONTEXT="$2";    shift 2 ;;
    -h|--help)    sed -n '2,18p' "$0"; exit 0 ;;
    *) echo "tham số lạ: $1" >&2; exit 2 ;;
  esac
done

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
KCTX=(); [[ -n "$CONTEXT" ]] && KCTX=(--context "$CONTEXT")
k() { kubectl "${KCTX[@]}" "$@"; }
cfg() { python3 "$HERE/orchestrate.py" --env-config "$HERE/$ENV_CONFIG" config --get "$1" 2>/dev/null; }

ADDRESS=$(cfg vault.address)
OPERATOR_NS=$(cfg vault.operator_namespace)
OPERATOR_VERSION=$(cfg vault.operator_version)
KV_MOUNT=$(cfg vault.kv_mount)
KV_TYPE=$(cfg vault.kv_type)
AUTH_MOUNT=$(cfg vault.auth_mount)

# Vault chạy ở namespace nào suy ra TỪ vault.address, để một chỗ khai báo duy nhất.
# http://vault.<ns>.svc.cluster.local:8200 -> release "vault", namespace "<ns>"
host=${ADDRESS#*://}; host=${host%%:*}
RELEASE=${host%%.*}; rest=${host#*.}; VAULT_NS=${rest%%.*}
if [[ -z "$RELEASE" || -z "$VAULT_NS" || "$RELEASE" == "$host" ]]; then
  echo "vault.address=$ADDRESS không có dạng <release>.<namespace>.svc… — script harness"
  echo "chỉ dựng được Vault TRONG cụm. Vault ngoài cụm thì bỏ qua bước 1, tự cấu hình."
  exit 1
fi

echo "==> cụm:        $(k config current-context)"
echo "==> Vault:      $RELEASE trong ns/$VAULT_NS  ($ADDRESS)"
echo "==> VSO:        $OPERATOR_VERSION trong ns/$OPERATOR_NS"
echo "==> KV:         $KV_MOUNT ($KV_TYPE), auth mount: $AUTH_MOUNT"

# Token gốc của dev-mode. KHÔNG phải bí mật thật: Vault dev mode mất sạch dữ liệu mỗi lần
# pod khởi động lại, nên nó không bảo vệ thứ gì. Đặt biến môi trường để đổi.
DEV_ROOT_TOKEN="${VAULT_DEV_ROOT_TOKEN:-root}"

echo
echo "===== 1. Vault (dev mode — CHỈ harness) ====="
helm repo add hashicorp https://helm.releases.hashicorp.com >/dev/null 2>&1 || true
helm repo update hashicorp >/dev/null
helm upgrade --install "$RELEASE" hashicorp/vault \
  ${CONTEXT:+--kube-context "$CONTEXT"} \
  --namespace "$VAULT_NS" --create-namespace \
  --set server.dev.enabled=true \
  --set "server.dev.devRootToken=$DEV_ROOT_TOKEN" \
  --set injector.enabled=false \
  --wait --timeout 6m
k -n "$VAULT_NS" wait --for=condition=Ready "pod/${RELEASE}-0" --timeout=180s

vault_exec() {
  k -n "$VAULT_NS" exec -i "${RELEASE}-0" -- \
    env "VAULT_TOKEN=$DEV_ROOT_TOKEN" VAULT_ADDR=http://127.0.0.1:8200 "$@"
}

echo
echo "===== 2. KV engine, kubernetes auth, audit log ====="
version_flag=""; [[ "$KV_TYPE" == "kv-v2" ]] && version_flag="-version=2" || version_flag="-version=1"
vault_exec vault secrets enable -path="$KV_MOUNT" $version_flag kv 2>&1 | tail -1 || true
vault_exec vault auth enable -path="$AUTH_MOUNT" kubernetes 2>&1 | tail -1 || true
# VSO gửi ServiceAccount token của app; Vault xác thực nó với API server của chính cụm.
vault_exec vault write "auth/$AUTH_MOUNT/config" \
  kubernetes_host=https://kubernetes.default.svc:443 2>&1 | tail -1
# Audit log là yêu cầu ở mục 7.5 của kế hoạch. Ở harness ghi ra stdout của pod; công ty
# phải ghi ra file/syslog có lưu trữ.
vault_exec vault audit enable file file_path=stdout 2>&1 | tail -1 || true

echo
echo "===== 3. Vault Secrets Operator $OPERATOR_VERSION ====="
helm upgrade --install vault-secrets-operator hashicorp/vault-secrets-operator \
  ${OPERATOR_VERSION:+--version "$OPERATOR_VERSION"} \
  ${CONTEXT:+--kube-context "$CONTEXT"} \
  --namespace "$OPERATOR_NS" --create-namespace \
  --wait --timeout 6m

echo
echo "===== 4. VaultConnection + VaultAuthGlobal (sinh từ platform.env.yaml) ====="
python3 "$HERE/orchestrate.py" --env-config "$HERE/$ENV_CONFIG" vault-foundation --apply

echo
echo "===== 5. Kiểm lại bằng chính preflight của platform ====="
python3 "$HERE/orchestrate.py" --env-config "$HERE/$ENV_CONFIG" \
  preflight --require-cluster --require-vault

cat <<EOF

XONG. Onboard một app vào Vault:

  python3 orchestrate.py vault-onboard --app <app> --env staging --apply   # phía Kubernetes
  python3 orchestrate.py vault-onboard --app <app> --env staging           # in phần Vault

Phần Vault (policy + role) do người quản trị Vault chạy bằng token CỦA HỌ. Trên harness:

  kubectl -n $VAULT_NS exec -i ${RELEASE}-0 -- env VAULT_TOKEN=\$VAULT_DEV_ROOT_TOKEN \\
    VAULT_ADDR=http://127.0.0.1:8200 vault policy write <tên> -
EOF
