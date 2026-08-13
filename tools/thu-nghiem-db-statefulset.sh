#!/usr/bin/env bash
# Runtime harness cho backend database=statefulset (profile công ty) trên cụm kind.
#
# VÌ SAO CÓ FILE NÀY: pytest chỉ chứng minh render đúng. Câu "StatefulSet chạy được" chỉ
# đúng khi nó lên thật trên một cụm thật, PVC Bound thật, chạy được SQL thật, và dữ liệu
# CÒN LẠI sau khi xoá pod. Script này lái đúng luồng đó bằng chính idpctl render.
#
# ĐÂY LÀ HARNESS — KHÔNG chạy ở công ty. Nó ánh xạ toạ độ công ty (StorageClass
# rook-ceph-block, ảnh Harbor, credential qua Vault/VSO) sang tương đương LOCAL của cụm
# kind, đúng theo nguyên tắc "overlay runtime tạm thời" — profile công ty KHÔNG bị sửa.
#   StorageClass rook-ceph-block   -> storage-class (local-path của kind)
#   ảnh Harbor postgres            -> postgres:17-alpine (nạp sẵn vào node kind)
#   credential Vault -> VSO -> Secret -> tạo thẳng Secret basic-auth (VSO test riêng)
#
#   ./tools/thu-nghiem-db-statefulset.sh [--context kind-staging] [--keep]
set -euo pipefail

CONTEXT=kind-staging
KEEP=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --context) CONTEXT="$2"; shift 2 ;;
    --keep) KEEP=1; shift ;;
    -h|--help) sed -n '2,20p' "$0"; exit 0 ;;
    *) echo "tham số lạ: $1" >&2; exit 2 ;;
  esac
done

APP=stsdemo
ENVN=staging
NS="${APP}-${ENVN}"
IMAGE="postgres:17-alpine"       # ánh xạ local của ảnh Harbor
SC_LOCAL="storage-class"         # ánh xạ local của rook-ceph-block
CLUSTER_NAME="${CONTEXT#kind-}"  # kind-staging -> staging
WORK="$(mktemp -d)"
K() { kubectl --context "$CONTEXT" "$@"; }

cleanup() {
  if [[ "$KEEP" == "0" ]]; then
    echo "==> dọn: xoá namespace $NS"
    K delete ns "$NS" --ignore-not-found --wait=false >/dev/null 2>&1 || true
  else
    echo "==> --keep: giữ lại namespace $NS để soi"
  fi
  rm -rf "$WORK"
}
trap cleanup EXIT

echo "==> chờ namespace $NS của lần chạy trước biến mất hẳn (nếu có)"
K wait --for=delete "ns/$NS" --timeout=120s >/dev/null 2>&1 || true

echo "==> nạp ảnh $IMAGE vào node kind ($CLUSTER_NAME)"
kind load docker-image "$IMAGE" --name "$CLUSTER_NAME" >/dev/null 2>&1 || \
  echo "   (bỏ qua: kind load lỗi — có thể ảnh đã có sẵn trên node)"

echo "==> render backend=statefulset qua overlay runtime (profile công ty KHÔNG bị sửa)"
python3 - "$WORK/overlay.yaml" "$SC_LOCAL" <<'PY'
import sys, yaml
out, sc = sys.argv[1], sys.argv[2]
d = yaml.safe_load(open("platform.env.company.yaml"))
d.setdefault("features", {})["postgres_application"] = True
# --- runtime mapping (chỉ harness) ---
d.setdefault("kubernetes", {})["storage_class"] = sc
d["database"]["storage_class"] = ""
d["database"]["image_repository"] = "postgres"           # + :engine_version
d["database_profiles"]["staging"]["application"]["engine_version"] = "17-alpine"
open(out, "w").write(yaml.safe_dump(d, sort_keys=False))
print("overlay ->", out)
PY

mkdir -p "$WORK/app"
cat > "$WORK/app/score.yaml" <<EOF
apiVersion: score.dev/v1b1
metadata:
  name: api
containers:
  main:
    image: .
service:
  ports:
    http:
      port: 8080
      targetPort: 8080
resources:
  db:
    type: postgres
    class: application
EOF

python3 idpctl --env-config "$WORK/overlay.yaml" render \
  --app "$APP" --tag runtime --registry local.test/idp \
  --catalog . --app-dir "$WORK/app" --work "$WORK/work" \
  --state-file "$WORK/state.yaml" --env "$ENVN" --out "$WORK/out.yaml" >/dev/null

# Tên cluster/secret suy từ manifest render (không đoán).
CRED=$(python3 -c "
import yaml
for d in yaml.safe_load_all(open('$WORK/work/manifests.yaml')):
    if d and d['kind']=='StatefulSet':
        for e in d['spec']['template']['spec']['containers'][0]['env']:
            if e['name']=='POSTGRES_PASSWORD':
                print(e['valueFrom']['secretKeyRef']['name']); break
")
read -r STS PGUSER PGDB < <(python3 -c "
import yaml
sts=next(d for d in yaml.safe_load_all(open('$WORK/work/manifests.yaml')) if d and d['kind']=='StatefulSet')
env={e['name']: e.get('value') for e in sts['spec']['template']['spec']['containers'][0]['env']}
print(sts['metadata']['name'], env['POSTGRES_USER'], env['POSTGRES_DB'])
")
echo "   StatefulSet=$STS  credSecret=$CRED  user=$PGUSER  db=$PGDB"

echo "==> tạo namespace + Secret basic-auth (ánh xạ VSO: giá trị test, KHÔNG phải secret thật)"
K create ns "$NS" --dry-run=client -o yaml | K apply -f - >/dev/null
K -n "$NS" create secret generic "$CRED" \
  --type=kubernetes.io/basic-auth \
  --from-literal=username="$PGUSER" \
  --from-literal=password="testpass-$(date +%s)" \
  --dry-run=client -o yaml | K apply -f - >/dev/null

echo "==> apply manifests (bỏ VaultStaticSecret — đã thay bằng Secret ở trên)"
python3 -c "
import yaml,sys
docs=[d for d in yaml.safe_load_all(open('$WORK/out.yaml')) if d and d['kind']!='VaultStaticSecret']
# gỡ imagePullSecrets: ảnh đã nạp local, không cần secret kéo ảnh trong harness
for d in docs:
    if d['kind']=='StatefulSet':
        d['spec']['template']['spec'].pop('imagePullSecrets',None)
        for c in d['spec']['template']['spec']['containers']:
            c['imagePullPolicy']='IfNotPresent'
sys.stdout.write(yaml.safe_dump_all(docs))
" | K -n "$NS" apply -f - >/dev/null

echo "==> chờ StatefulSet Ready (tối đa 180s)"
K -n "$NS" rollout status "statefulset/$STS" --timeout=180s

echo "==> PVC phải Bound"
K -n "$NS" get pvc -o wide
PHASE=$(K -n "$NS" get pvc -o jsonpath='{.items[0].status.phase}')
[[ "$PHASE" == "Bound" ]] || { echo "PVC không Bound (phase=$PHASE)"; exit 1; }

echo "==> Service <cluster>-rw phải tồn tại (host mà app nhận)"
K -n "$NS" get svc "${STS}-rw"

POD="${STS}-0"
echo "==> SQL thật: tạo bảng + ghi dữ liệu"
K -n "$NS" exec "$POD" -- psql -U "$PGUSER" -d "$PGDB" -c \
  "CREATE TABLE IF NOT EXISTS t(id int); INSERT INTO t VALUES (42);"
BEFORE=$(K -n "$NS" exec "$POD" -- psql -U "$PGUSER" -d "$PGDB" -tAc "SELECT count(*) FROM t;")
echo "   số dòng trước khi restart: $BEFORE"

echo "==> xoá pod, chờ StatefulSet tạo lại"
K -n "$NS" delete pod "$POD" --wait=true
K -n "$NS" rollout status "statefulset/$STS" --timeout=180s

echo "==> KIỂM PERSISTENCE: dữ liệu phải còn sau khi pod tạo lại từ PVC cũ"
AFTER=$(K -n "$NS" exec "$POD" -- psql -U "$PGUSER" -d "$PGDB" -tAc "SELECT count(*) FROM t;")
echo "   số dòng sau khi restart: $AFTER"
[[ "$AFTER" == "$BEFORE" && "$AFTER" -ge 1 ]] || { echo "PERSISTENCE HỎNG: $BEFORE -> $AFTER"; exit 1; }

echo
echo "==> PASS: StatefulSet Ready · PVC Bound · SQL chạy · dữ liệu BỀN qua restart."
