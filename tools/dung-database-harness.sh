#!/usr/bin/env bash
# Cài database operator cho HARNESS LOCAL (Phase 4) — provider của `class: application`.
#
# Ở công ty: nếu đã có dịch vụ database do DBA vận hành thì KHÔNG chạy script này. Chỉ điền
# `database.*` trong platform.env.company.yaml, và nếu dùng provider khác thì thay
# `provisioners/postgres-application.provisioners.yaml` bằng bản của provider đó — hợp đồng
# với app (host/port/database/username/password) giữ nguyên nên app không phải sửa gì.
#
# Mọi toạ độ đọc từ platform.env.yaml. Chạy lại được nhiều lần.
#
#   ./tools/dung-database-harness.sh --context kind-staging
set -euo pipefail

ENV_CONFIG="platform.env.yaml"
CONTEXT=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --env-config) ENV_CONFIG="$2"; shift 2 ;;
    --context)    CONTEXT="$2";    shift 2 ;;
    -h|--help)    sed -n '2,12p' "$0"; exit 0 ;;
    *) echo "tham số lạ: $1" >&2; exit 2 ;;
  esac
done

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
KCTX=(); [[ -n "$CONTEXT" ]] && KCTX=(--context "$CONTEXT")
k() { kubectl "${KCTX[@]}" "$@"; }
cfg() { python3 "$HERE/orchestrate.py" --env-config "$HERE/$ENV_CONFIG" config --get "$1" 2>/dev/null; }

PROVIDER=$(cfg database.provider)
OPERATOR_NS=$(cfg database.operator_namespace)
OPERATOR_VERSION=$(cfg database.operator_version)
IMAGE_REPO=$(cfg database.image_repository)

if [[ "$PROVIDER" != "cloudnative-pg" ]]; then
  echo "database.provider = '$PROVIDER', không phải cloudnative-pg."
  echo "Script harness này chỉ cài CloudNativePG. Provider khác thì cài theo tài liệu của"
  echo "họ rồi thay provisioner postgres-application — app không phải sửa gì."
  exit 1
fi

echo "==> cụm:      $(k config current-context)"
echo "==> provider: $PROVIDER $OPERATOR_VERSION trong ns/$OPERATOR_NS"
echo "==> ảnh pg:   $IMAGE_REPO:<engine_version của profile>"

helm repo add cnpg https://cloudnative-pg.github.io/charts >/dev/null 2>&1 || true
helm repo update cnpg >/dev/null
helm upgrade --install cnpg cnpg/cloudnative-pg \
  ${OPERATOR_VERSION:+--version "$OPERATOR_VERSION"} \
  ${CONTEXT:+--kube-context "$CONTEXT"} \
  --namespace "$OPERATOR_NS" --create-namespace \
  --wait --timeout 8m

echo
echo "===== CRD đã có ====="
k get crd | grep cnpg.io | awk '{print "  " $1}'

cat <<EOF

XONG. Một app dùng database:

  1. score.yaml:   resources: {db: {type: postgres, class: application}}
  2. onboard Vault (Phase 2) rồi sinh credential — mật khẩu KHÔNG ai nhìn thấy:

     python3 orchestrate.py secret-set --app <app> --env staging \\
       --name \$(python3 orchestrate.py config --get database.credential_secret) \\
       --key username --stdin --replace   <<< "app_<workload>"
     python3 orchestrate.py secret-set --app <app> --env staging \\
       --name \$(python3 orchestrate.py config --get database.credential_secret) \\
       --key password --generate

  3. render + apply như bình thường; \`verify\` chờ Cluster báo Ready.

LƯU Ý HARNESS: chưa có kho object nên \`database.backup.object_store_url\` để rỗng, và
render \`prod\` với class application sẽ BỊ CHẶN — đúng như thiết kế. Muốn thử prod thì
dựng kho object (MinIO/S3) rồi điền trường đó.
EOF
