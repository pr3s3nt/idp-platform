#!/usr/bin/env bash
# Dựng KHO OBJECT cho HARNESS LOCAL — nơi backup của database prod đi tới.
#
# Vì sao cần: render `prod` với `class: application` bị CHẶN khi
# `database.backup.object_store_url` rỗng (xem check_database_classes). Đó là fail-closed
# có chủ ý — một database production không phục hồi được thì không đáng gọi là chạy. Nên
# harness cũng phải có một kho object THẬT, không phải một URL giả cho qua cửa.
#
# Ở công ty: KHÔNG chạy script này. Công ty đã có kho object (S3, Ceph, MinIO của hạ tầng).
# Chỉ điền `database.backup.*` trong platform.env.company.yaml — kể cả `endpoint_url` nếu
# kho đó không phải AWS S3.
#
# Chạy lại được nhiều lần. Mọi toạ độ đọc từ platform.env.yaml.
#
#   ./tools/dung-object-store-harness.sh --context kind-staging
set -euo pipefail

ENV_CONFIG="platform.env.yaml"
CONTEXT=""
NS="object-store"
IMAGE="mirror.gcr.io/minio/minio:latest"
while [[ $# -gt 0 ]]; do
  case "$1" in
    --env-config) ENV_CONFIG="$2"; shift 2 ;;
    --context)    CONTEXT="$2";    shift 2 ;;
    --namespace)  NS="$2";         shift 2 ;;
    --image)      IMAGE="$2";      shift 2 ;;
    -h|--help)    sed -n '2,16p' "$0"; exit 0 ;;
    *) echo "tham số lạ: $1" >&2; exit 2 ;;
  esac
done

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
KCTX=(); [[ -n "$CONTEXT" ]] && KCTX=(--context "$CONTEXT")
k() { kubectl "${KCTX[@]}" "$@"; }
# --env-config nhận cả đường dẫn tuyệt đối lẫn tương đối so với gốc kho. Ghép cứng
# "$HERE/$ENV_CONFIG" sẽ hỏng với đường dẫn tuyệt đối, và hỏng theo kiểu tệ nhất: cfg()
# trả về rỗng, `set -e` giết script ngay ở phép gán đầu tiên, không in ra một chữ nào.
case "$ENV_CONFIG" in /*) CFG_PATH="$ENV_CONFIG" ;; *) CFG_PATH="$HERE/$ENV_CONFIG" ;; esac
[[ -f "$CFG_PATH" ]] || { echo "không thấy $CFG_PATH" >&2; exit 2; }
# `|| true`: một khoá rỗng KHÔNG phải lỗi (endpoint_url rỗng nghĩa là AWS S3), nhưng
# `config --get` thoát 1 khi khoá không tồn tại và set -e sẽ giết script tại chỗ gán.
cfg() { python3 "$HERE/idpctl" --env-config "$CFG_PATH" config --get "$1" 2>/dev/null || true; }

CRED_SECRET="$(cfg database.backup.credentials_secret)"
STORAGE_CLASS="$(cfg kubernetes.storage_class)"
BUCKET="idp-backup"
# Thông tin đăng nhập của kho harness. Sinh MỘT LẦN rồi giữ trong chính Secret đó: chạy
# lại script mà đổi khoá thì mọi Cluster đang archive WAL sẽ hỏng giữa chừng.
ROOT_USER="idp-harness"

echo "==> namespace $NS"
k get namespace "$NS" >/dev/null 2>&1 || k create namespace "$NS"

if k -n "$NS" get secret minio-root >/dev/null 2>&1; then
  echo "    thông tin đăng nhập đã có -> giữ nguyên"
else
  k -n "$NS" create secret generic minio-root \
    --from-literal=MINIO_ROOT_USER="$ROOT_USER" \
    --from-literal=MINIO_ROOT_PASSWORD="$(head -c 24 /dev/urandom | base64 | tr -d '/+=' | head -c 24)"
  echo "    sinh thông tin đăng nhập mới (không in ra)"
fi

echo "==> MinIO"
k -n "$NS" apply -f - <<YAML
apiVersion: v1
kind: PersistentVolumeClaim
metadata: {name: minio-data, namespace: $NS}
spec:
  accessModes: [ReadWriteOnce]
  storageClassName: $STORAGE_CLASS
  resources: {requests: {storage: 10Gi}}
---
apiVersion: apps/v1
kind: Deployment
metadata: {name: minio, namespace: $NS}
spec:
  replicas: 1
  selector: {matchLabels: {app: minio}}
  template:
    metadata: {labels: {app: minio}}
    spec:
      containers:
        - name: minio
          image: $IMAGE
          args: ["server", "/data", "--console-address", ":9001"]
          envFrom: [{secretRef: {name: minio-root}}]
          ports: [{containerPort: 9000}, {containerPort: 9001}]
          volumeMounts: [{name: data, mountPath: /data}]
          readinessProbe:
            httpGet: {path: /minio/health/ready, port: 9000}
            initialDelaySeconds: 5
      volumes:
        - name: data
          persistentVolumeClaim: {claimName: minio-data}
---
apiVersion: v1
kind: Service
metadata: {name: minio, namespace: $NS}
spec:
  selector: {app: minio}
  ports:
    - {name: s3, port: 9000, targetPort: 9000}
    - {name: console, port: 9001, targetPort: 9001}
YAML
k -n "$NS" rollout status deploy/minio --timeout=180s

echo "==> bucket $BUCKET"
# `mc` chạy trong chính ảnh minio, nên không cần thêm công cụ nào trên máy.
k -n "$NS" run mc-init --rm -i --restart=Never --image="$IMAGE" \
  --env="MC_HOST_h=http://$(k -n "$NS" get secret minio-root -o jsonpath='{.data.MINIO_ROOT_USER}' | base64 -d):$(k -n "$NS" get secret minio-root -o jsonpath='{.data.MINIO_ROOT_PASSWORD}' | base64 -d)@minio.$NS.svc.cluster.local:9000" \
  --command -- sh -c "mc mb -p h/$BUCKET && mc ls h/" >/dev/null 2>&1 || true
echo "    bucket sẵn sàng"

if [[ -z "$CRED_SECRET" ]]; then
  echo
  echo "==> database.backup.credentials_secret đang RỖNG trong $ENV_CONFIG."
  echo "    CNPG cần một Secret trong NAMESPACE CỦA APP với hai khoá"
  echo "    ACCESS_KEY_ID / ACCESS_SECRET_KEY. Đặt tên nó vào khoá đó rồi chạy lại."
  exit 0
fi

echo "==> phát $CRED_SECRET vào các namespace app đang có"
# Secret credential phải nằm CÙNG namespace với Cluster — CNPG không đọc chéo namespace.
for ns in $(k get ns -o name | sed 's|namespace/||'); do
  case "$ns" in *-staging|*-prod) ;; *) continue ;; esac
  k -n "$ns" get secret "$CRED_SECRET" >/dev/null 2>&1 && { echo "    $ns: đã có"; continue; }
  k -n "$ns" create secret generic "$CRED_SECRET" \
    --from-literal=ACCESS_KEY_ID="$(k -n "$NS" get secret minio-root -o jsonpath='{.data.MINIO_ROOT_USER}' | base64 -d)" \
    --from-literal=ACCESS_SECRET_KEY="$(k -n "$NS" get secret minio-root -o jsonpath='{.data.MINIO_ROOT_PASSWORD}' | base64 -d)" >/dev/null
  echo "    $ns: đã tạo"
done

cat <<MSG

==> XONG. Điền vào $ENV_CONFIG:

  database:
    backup:
      object_store_url: "s3://$BUCKET/"
      endpoint_url: "http://minio.$NS.svc.cluster.local:9000"
      credentials_secret: "$CRED_SECRET"

Kho object của HARNESS: một bản sao, không HA, không sao lưu ngoài cụm. Nó tồn tại để
đường prod chạy được thật, không phải để giữ dữ liệu.
MSG
