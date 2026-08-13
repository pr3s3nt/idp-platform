#!/usr/bin/env bash
# Runtime harness cho registry kiểu công ty: HTTPS + custom CA + auth + imagePullSecret.
#
# Script dựng một OCI registry nhẹ trên Docker network của kind, sinh CA/credential ngẫu
# nhiên trong thư mục tạm, push một image thật, rồi render workload bằng chính
# orchestrate.py từ platform.env.company.yaml qua overlay runtime. Nó chứng minh cả hai
# nửa supply chain: Docker/runner push được và kubelet pull được bằng imagePullSecret.
# Mọi tài nguyên, CA và credential test đều được dọn khi kết thúc.
#
#   ./tools/thu-nghiem-registry-private.sh [--context kind-staging]
set -euo pipefail

CONTEXT=kind-staging
while [[ $# -gt 0 ]]; do
  case "$1" in
    --context) CONTEXT="$2"; shift 2 ;;
    -h|--help) sed -n '2,13p' "$0"; exit 0 ;;
    *) echo "tham số lạ: $1" >&2; exit 2 ;;
  esac
done

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CLUSTER_NAME="${CONTEXT#kind-}"
NODE="${CLUSTER_NAME}-control-plane"
REG_CONTAINER="idp-registry-harness-${CLUSTER_NAME}"
APP=regdemo
ENVN=staging
NS="${APP}-${ENVN}"
WORK="$(mktemp -d)"
REG_ADDR=""
HOST_CERT_DIR=""
NODE_CERT_DIR=""
IMAGE_REF=""
K() { kubectl --context "$CONTEXT" "$@"; }

cleanup() {
  echo "==> dọn registry fixture"
  K delete namespace "$NS" --ignore-not-found --wait=false >/dev/null 2>&1 || true
  if [[ -n "$REG_ADDR" ]]; then
    docker logout "$REG_ADDR" >/dev/null 2>&1 || true
  fi
  if [[ -n "$IMAGE_REF" ]]; then
    docker image rm "$IMAGE_REF" >/dev/null 2>&1 || true
  fi
  docker rm -f "$REG_CONTAINER" >/dev/null 2>&1 || true
  if [[ -n "$NODE_CERT_DIR" ]]; then
    docker exec "$NODE" rm -f "$NODE_CERT_DIR/ca.crt" "$NODE_CERT_DIR/hosts.toml" \
      >/dev/null 2>&1 || true
    docker exec "$NODE" rmdir "$NODE_CERT_DIR" >/dev/null 2>&1 || true
  fi
  if [[ -n "$HOST_CERT_DIR" ]]; then
    sudo rm -f "$HOST_CERT_DIR/ca.crt" >/dev/null 2>&1 || true
    sudo rmdir "$HOST_CERT_DIR" >/dev/null 2>&1 || true
  fi
  rm -rf "$WORK"
}
trap cleanup EXIT

docker network inspect kind >/dev/null
docker inspect "$NODE" >/dev/null
docker rm -f "$REG_CONTAINER" >/dev/null 2>&1 || true

# Chọn một IP chưa dùng ở cuối subnet IPv4 của network kind. Image ref dùng IP để không
# phải sửa /etc/hosts của máy hay node.
REG_IP=$(docker network inspect kind | python3 -c '
import ipaddress, json, sys
d=json.load(sys.stdin)[0]
subnet=next(x["Subnet"] for x in d["IPAM"]["Config"] if ":" not in x["Subnet"])
net=ipaddress.ip_network(subnet)
used={str(ipaddress.ip_interface(c["IPv4Address"]).ip)
      for c in (d.get("Containers") or {}).values() if c.get("IPv4Address")}
for offset in range(10, 250):
    candidate=str(net.broadcast_address-offset)
    if candidate not in used:
        print(candidate); break
else:
    raise SystemExit("không tìm được IP trống trên Docker network kind")
')
REG_ADDR="${REG_IP}:5443"
HOST_CERT_DIR="/etc/docker/certs.d/${REG_ADDR}"
NODE_CERT_DIR="/etc/containerd/certs.d/${REG_ADDR}"
IMAGE_REF="${REG_ADDR}/idp/private-nginx:runtime"

[[ "$REG_IP" =~ ^[0-9]+(\.[0-9]+){3}$ ]] || { echo "IP registry không hợp lệ"; exit 1; }
[[ ! -e "$HOST_CERT_DIR" ]] || { echo "$HOST_CERT_DIR đã tồn tại; không ghi đè"; exit 1; }

mkdir -p "$WORK/certs" "$WORK/auth" "$WORK/data" "$WORK/app" "$WORK/render"
REG_USER="harness-$(openssl rand -hex 4)"
REG_PASS="$(openssl rand -base64 30 | tr -d '\n')"

echo "==> sinh CA + certificate test cho registry HTTPS $REG_ADDR"
openssl req -x509 -newkey rsa:2048 -nodes \
  -keyout "$WORK/certs/ca.key" -out "$WORK/certs/ca.crt" -days 1 \
  -subj "/CN=IDP registry harness CA" >/dev/null 2>&1
openssl req -new -newkey rsa:2048 -nodes \
  -keyout "$WORK/certs/tls.key" -out "$WORK/certs/tls.csr" \
  -subj "/CN=$REG_IP" -addext "subjectAltName=IP:$REG_IP" >/dev/null 2>&1
openssl x509 -req -in "$WORK/certs/tls.csr" \
  -CA "$WORK/certs/ca.crt" -CAkey "$WORK/certs/ca.key" -CAcreateserial \
  -out "$WORK/certs/tls.crt" -days 1 -copy_extensions copy >/dev/null 2>&1

echo "==> sinh htpasswd ngẫu nhiên (không in credential)"
docker run --rm --entrypoint htpasswd httpd:2.4-alpine \
  -Bbn "$REG_USER" "$REG_PASS" > "$WORK/auth/htpasswd"

echo "==> dựng registry private trên Docker network kind"
docker run -d --name "$REG_CONTAINER" --network kind --ip "$REG_IP" \
  -v "$WORK/certs:/certs:ro" -v "$WORK/auth:/auth:ro" \
  -v "$WORK/data:/var/lib/registry" \
  -e REGISTRY_HTTP_ADDR=0.0.0.0:5443 \
  -e REGISTRY_HTTP_TLS_CERTIFICATE=/certs/tls.crt \
  -e REGISTRY_HTTP_TLS_KEY=/certs/tls.key \
  -e REGISTRY_AUTH=htpasswd \
  -e REGISTRY_AUTH_HTPASSWD_REALM='IDP harness registry' \
  -e REGISTRY_AUTH_HTPASSWD_PATH=/auth/htpasswd \
  registry:2 >/dev/null

for _ in $(seq 1 30); do
  if curl -fsS --cacert "$WORK/certs/ca.crt" -u "$REG_USER:$REG_PASS" \
      "https://$REG_ADDR/v2/" >/dev/null 2>&1; then
    break
  fi
  sleep 1
done
curl -fsS --cacert "$WORK/certs/ca.crt" -u "$REG_USER:$REG_PASS" \
  "https://$REG_ADDR/v2/" >/dev/null
UNAUTH_CODE=$(curl -sS --cacert "$WORK/certs/ca.crt" -o /dev/null -w '%{http_code}' \
  "https://$REG_ADDR/v2/")
[[ "$UNAUTH_CODE" == 401 ]] || { echo "registry không bắt auth (HTTP $UNAUTH_CODE)"; exit 1; }

echo "==> cài custom CA cho Docker daemon và containerd của node kind"
sudo mkdir -p "$HOST_CERT_DIR"
sudo install -m 0644 "$WORK/certs/ca.crt" "$HOST_CERT_DIR/ca.crt"
docker exec "$NODE" mkdir -p "$NODE_CERT_DIR"
docker cp "$WORK/certs/ca.crt" "$NODE:$NODE_CERT_DIR/ca.crt" >/dev/null
printf 'server = "https://%s"\n\n[host."https://%s"]\n  capabilities = ["pull", "resolve"]\n  ca = "%s/ca.crt"\n' \
  "$REG_ADDR" "$REG_ADDR" "$NODE_CERT_DIR" | \
  docker exec -i "$NODE" sh -c "umask 022; tee '$NODE_CERT_DIR/hosts.toml' >/dev/null"

echo "==> runner đăng nhập và push image thật qua TLS đã verify"
printf '%s' "$REG_PASS" | docker login "$REG_ADDR" -u "$REG_USER" --password-stdin >/dev/null
docker image inspect nginx:1.27-alpine >/dev/null 2>&1 || docker pull nginx:1.27-alpine >/dev/null
docker tag nginx:1.27-alpine "$IMAGE_REF"
docker push "$IMAGE_REF" >/dev/null

echo "==> negative gate: không có imagePullSecret phải bị registry từ chối"
K create namespace "$NS" >/dev/null
K -n "$NS" run no-registry-secret --image="$IMAGE_REF" --image-pull-policy=Always \
  --restart=Never >/dev/null
AUTH_FAILURE=0
for _ in $(seq 1 20); do
  MESSAGE=$(K -n "$NS" get events --field-selector involvedObject.name=no-registry-secret \
    --sort-by=.lastTimestamp -o jsonpath='{range .items[*]}{.message}{"\n"}{end}' 2>/dev/null || true)
  if printf '%s' "$MESSAGE" | grep -qiE \
      'unauthorized|failed to authorize|authorization failed|authentication required|no basic auth credentials'; then
    AUTH_FAILURE=1; break
  fi
  sleep 2
done
[[ "$AUTH_FAILURE" == 1 ]] || { echo "pod thiếu secret không báo lỗi auth rõ ràng"; echo "$MESSAGE"; exit 1; }
K -n "$NS" delete pod no-registry-secret --wait=false >/dev/null

echo "==> render workload từ profile công ty qua overlay runtime tạm"
python3 - "$WORK/overlay.yaml" "$REG_ADDR" <<'PY'
import sys, yaml
out, registry = sys.argv[1:3]
d = yaml.safe_load(open("platform.env.company.yaml"))
d["registry"]["host"] = registry
d["registry"]["path"] = f"{registry}/idp"
open(out, "w").write(yaml.safe_dump(d, sort_keys=False))
PY
printf '%s\n' \
  'apiVersion: score.dev/v1b1' \
  'metadata:' \
  '  name: regdemo' \
  'containers:' \
  '  web:' \
  '    image: .' > "$WORK/app/score.yaml"

python3 orchestrate.py --env-config "$WORK/overlay.yaml" render \
  --app "$APP" --image private-nginx --tag runtime --registry "$REG_ADDR/idp" \
  --catalog . --app-dir "$WORK/app" --work "$WORK/render" \
  --state-file "$WORK/state.yaml" --env "$ENVN" --out "$WORK/out.yaml" >/dev/null
python3 - "$WORK/out.yaml" <<'PY'
import sys, yaml
d=next(x for x in yaml.safe_load_all(open(sys.argv[1])) if x and x.get("kind")=="Deployment")
assert d["spec"]["template"]["spec"]["imagePullSecrets"] == [{"name":"registry-pull"}]
print("   manifest imagePullSecrets=registry-pull")
PY

echo "==> orchestrator tạo registry Secret, kubelet pull image private và rollout"
if ! APPLY_OUTPUT=$(python3 orchestrate.py --env-config "$WORK/overlay.yaml" apply-secrets \
  --app "$APP" --env "$ENVN" --secrets "$WORK/render/secrets.yaml" \
  --harbor-host "$REG_ADDR" --harbor-user "$REG_USER" --harbor-pass "$REG_PASS" 2>&1); then
  printf '%s\n' "${APPLY_OUTPUT//$REG_PASS/<redacted>}" >&2
  exit 1
fi
if [[ "$APPLY_OUTPUT" == *"$REG_PASS"* ]]; then
  echo "FATAL: apply-secrets làm lộ registry password trong log" >&2
  exit 1
fi
[[ "$APPLY_OUTPUT" == *'--docker-password=<redacted>'* ]] || {
  echo "apply-secrets không để lại bằng chứng redaction mong đợi" >&2; exit 1;
}
printf '%s\n' "$APPLY_OUTPUT"
K -n "$NS" apply -f "$WORK/out.yaml" >/dev/null
K -n "$NS" rollout status deployment/regdemo --timeout=120s
python3 orchestrate.py --env-config "$WORK/overlay.yaml" verify \
  --app "$APP" --env "$ENVN" --manifests "$WORK/out.yaml" --timeout 120 >/dev/null

LIVE_IMAGE=$(K -n "$NS" get deployment regdemo -o jsonpath='{.spec.template.spec.containers[0].image}')
[[ "$LIVE_IMAGE" == "$IMAGE_REF" ]] || { echo "image live lệch: $LIVE_IMAGE"; exit 1; }
echo
echo "==> PASS: TLS/custom CA · auth bắt buộc · runner push · thiếu Secret bị từ chối · imagePullSecret pull · workload Ready."
