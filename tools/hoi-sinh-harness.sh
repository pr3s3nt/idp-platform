#!/usr/bin/env bash
# Hồi sinh harness sau khi máy/WSL khởi động lại.
#
# VÌ SAO CẦN SCRIPT NÀY (triệu chứng phiên sau sẽ gặp):
#   - `kubectl` mọi context kind-* trả "connection refused" trên 127.0.0.1:<port>.
#   - `docker ps -a` thấy *-control-plane ở trạng thái "Exited (128)".
# NGUYÊN NHÂN: cụm kind = container Docker chạy Kubernetes BÊN TRONG. Khi WSL boot,
# Docker cố tự khởi động lại container kind QUÁ SỚM — lúc lớp systemd/cgroup của WSL
# chưa sẵn sàng cấp "cgroup scope" — nên runc chết ngay (exit 128). Đây là đua lúc boot,
# KHÔNG phải cụm hỏng: dữ liệu (etcd/PV) vẫn nằm trong filesystem container, chỉ *dừng*.
# Khi máy đã up một lúc, systemd/cgroup đã sẵn sàng → `docker start` lại là chạy.
#
# VÀ MỘT HỆ QUẢ RIÊNG CỦA VAULT: Vault harness chạy dev mode = giữ mọi thứ TRONG RAM.
# Container kind restart → Vault mất SẠCH KV mount + policy + role + secret → mọi
# VaultStaticSecret rơi SecretSynced=False → app dùng secret đứng NotReady. Nên sau khi
# cụm lên lại, phải DỰNG LẠI Vault foundation (bước 3) rồi seed lại secret TỪNG app.
#
# Script này idempotent — chạy lại bao nhiêu lần cũng được (cụm đang chạy thì bước 1 no-op).
#
#   ./tools/hoi-sinh-harness.sh                 # start cả 3 cụm + dựng lại Vault trên staging
#   ./tools/hoi-sinh-harness.sh --no-vault      # chỉ start cụm, KHÔNG đụng Vault
#   ./tools/hoi-sinh-harness.sh --vault-context kind-prod
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# Toạ độ hạ tầng LOCAL của harness. Đây KHÔNG phải giá trị công ty (luật số 1 không áp
# dụng): tên container kind + tên container registry là sự thật của MÁY DEV này, do kind/
# script dựng-cụm đặt ra, không phải cấu hình mang sang công ty khác. Vault foundation thì
# vẫn đọc toạ độ từ platform.env.yaml qua dung-vault-harness.sh.
KIND_CONTAINERS=(staging-control-plane prod-control-plane v2-control-plane)
REGISTRY_CONTAINER="registry"
CONTEXTS=(kind-staging kind-prod kind-v2)
VAULT_CONTEXT="kind-staging"   # cụm verify chính; Vault foundation dựng ở đây
DO_VAULT=1

while [[ $# -gt 0 ]]; do
  case "$1" in
    --no-vault)      DO_VAULT=0; shift ;;
    --vault-context) VAULT_CONTEXT="$2"; shift 2 ;;
    -h|--help)       sed -n '2,26p' "$0"; exit 0 ;;
    *) echo "tham số lạ: $1" >&2; exit 2 ;;
  esac
done

echo "===== 1. Khởi động lại container kind + registry (idempotent) ====="
# `docker start` no-op nếu đang chạy. Chạy registry trước — pod pull ảnh từ nó.
docker start "$REGISTRY_CONTAINER" >/dev/null 2>&1 || echo "  (registry: không start được — kiểm 'docker ps -a')"
for c in "${KIND_CONTAINERS[@]}"; do
  if docker start "$c" >/dev/null 2>&1; then
    echo "  started/đang chạy: $c"
  else
    echo "  BỎ QUA (không có container $c) — cụm này có thể đã bị 'kind delete'." >&2
  fi
done

echo
echo "===== 2. Chờ API server từng cụm sẵn sàng (/readyz) ====="
# Container "Up" != Kubernetes sẵn sàng: etcd/apiserver/kubelet bên trong cần vài chục giây.
for ctx in "${CONTEXTS[@]}"; do
  printf "  %-14s " "$ctx"
  ok=""
  for _ in $(seq 1 40); do
    if kubectl --context "$ctx" get --raw='/readyz' >/dev/null 2>&1; then ok=1; break; fi
    sleep 3
  done
  if [[ -n "$ok" ]]; then
    echo "READY ($(kubectl --context "$ctx" get nodes --no-headers 2>/dev/null | awk '{print $1"="$2}' | tr '\n' ' '))"
  else
    echo "CHƯA sẵn sàng sau 120s — xem 'docker logs ${ctx#kind-}-control-plane'"
  fi
done

echo
echo "===== 3. Chờ pod re-sync + hạ tầng nền tảng trên $VAULT_CONTEXT ====="
# Ngay sau restart hàng loạt pod báo "Unknown" (kubelet chưa report lại) rồi tự phục hồi.
for _ in $(seq 1 20); do
  unknown=$(kubectl --context "$VAULT_CONTEXT" get pods -A --no-headers 2>/dev/null | grep -c Unknown || true)
  [[ "${unknown:-1}" == "0" ]] && break
  sleep 3
done
echo "  pod Unknown còn lại: ${unknown:-?}"
kubectl --context "$VAULT_CONTEXT" get pods -A --no-headers 2>/dev/null \
  | grep -iE 'fleet-controller|cnpg|minio|traefik|vault-secrets-operator|/vault-0' \
  | awk '{print "  "$1"/"$2"\t"$4}'

if [[ "$DO_VAULT" == "1" ]]; then
  echo
  echo "===== 4. Dựng lại Vault foundation trên $VAULT_CONTEXT (dev mode mất data khi restart) ====="
  "$HERE/tools/dung-vault-harness.sh" --context "$VAULT_CONTEXT"
else
  echo
  echo "===== 4. BỎ QUA Vault (--no-vault) ====="
  echo "  LƯU Ý: nếu Vault đã restart, mọi VaultStaticSecret vẫn SecretSynced=False tới khi"
  echo "  bạn chạy: ./tools/dung-vault-harness.sh --context $VAULT_CONTEXT"
fi

echo
echo "===== XONG ====="
echo "Harness lõi đã sống: cụm + registry + Fleet + CNPG + gateway (đủ cho luồng deploy mới)."
if [[ "$DO_VAULT" == "1" ]]; then
  echo "Vault foundation đã dựng lại — NHƯNG policy/role/secret TỪNG app thì Vault dev mode đã"
  echo "xoá. App cũ dùng secret vẫn NotReady tới khi seed lại RIÊNG từng app:"
  echo "  python3 idpctl vault-onboard --app <app> --env staging --apply"
  echo "  python3 idpctl secret-set ...        # theo lệnh onboard/HUONG-DAN in ra"
fi
echo "Kiểm nhanh 'còn sống không':  kubectl get nodes && python3 idpctl --env-config platform.env.yaml preflight --require-cluster --require-vault"
