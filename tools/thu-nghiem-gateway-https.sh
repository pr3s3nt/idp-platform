#!/usr/bin/env bash
# Verify company-shape routing qua HTTPS trên harness ĐÃ nâng cấp tại chỗ.
#
# Tiền đề: đã chạy `./tools/dung-harness-cong-ty.sh --up` (thêm listener `websecure` vào
# CHÍNH `ingress-gateway`, giới hạn host *.harness-https.local). Script này render một route
# theo profile công ty (sectionName=websecure, scheme=https), deploy lên gateway THẬT, rồi
# chứng minh: HTTPRoute Accepted=True + ResolvedRefs=True trên listener HTTPS, curl HTTPS 200.
#
# HARNESS — teardown tự động (xoá ns test) trừ --keep. KHÔNG đụng 24 route đang chạy (host
# khác *.harness-https.local nên không liên quan listener này).
#
#   ./tools/thu-nghiem-gateway-https.sh [--context kind-staging] [--keep]
set -euo pipefail

CONTEXT=kind-staging
KEEP=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --context) CONTEXT="$2"; shift 2 ;;
    --keep) KEEP=1; shift ;;
    -h|--help) sed -n '2,16p' "$0"; exit 0 ;;
    *) echo "tham số lạ: $1" >&2; exit 2 ;;
  esac
done

GWNS=traefik
GW=ingress-gateway
LISTENER=websecure
HOST="echo.harness-https.local"          # khớp *.harness-https.local của listener
NS=echo-https-staging
IMAGE="nginx:1.27-alpine"
WORK="$(mktemp -d)"
K() { kubectl --context "$CONTEXT" "$@"; }

cleanup() {
  [[ -n "${PF_PID:-}" ]] && kill "$PF_PID" >/dev/null 2>&1 || true
  [[ "$KEEP" == "0" ]] && K delete ns "$NS" --ignore-not-found --wait=false >/dev/null 2>&1 || true
  rm -rf "$WORK"
}
trap cleanup EXIT

echo "==> kiểm tiền đề: listener $LISTENER có trên $GWNS/$GW chưa"
HAS=$(K -n "$GWNS" get gateway "$GW" -o json | python3 -c "
import json,sys
print(any(l.get('name')=='$LISTENER' for l in (json.load(sys.stdin).get('spec') or {}).get('listeners') or []))")
[[ "$HAS" == "True" ]] || { echo "THIẾU listener $LISTENER — chạy: ./tools/dung-harness-cong-ty.sh --up"; exit 1; }

echo "==> lấy CA thật của listener (từ Secret harness-https-tls) để curl verify TLS đàng hoàng"
K -n "$GWNS" get secret harness-https-tls -o jsonpath='{.data.tls\.crt}' | base64 -d > "$WORK/ca.crt"

echo "==> render route qua overlay (profile công ty; gateway->$GW, sectionName=websecure, https)"
python3 - "$WORK/overlay.yaml" "$GW" "$GWNS" <<'PY'
import sys, yaml
out, gw, gwns = sys.argv[1:4]
d = yaml.safe_load(open("platform.env.company.yaml"))
ing = d.setdefault("ingress", {})
ing["gateway_name"] = gw            # runtime mapping -> gateway harness thật (đã có websecure)
ing["gateway_namespace"] = gwns
# section_name=websecure, route_scheme=https GIỮ NGUYÊN từ profile công ty
open(out, "w").write(yaml.safe_dump(d, sort_keys=False))
PY

mkdir -p "$WORK/app"
cat > "$WORK/app/score.yaml" <<EOF
apiVersion: score.dev/v1b1
metadata:
  name: echo
containers:
  main:
    image: .
service:
  ports:
    http:
      port: 80
      targetPort: 80
resources:
  web:
    type: route
    params:
      host: $HOST
      port: 80
      path: /
EOF

python3 orchestrate.py --env-config "$WORK/overlay.yaml" render \
  --app echo --tag runtime --registry local.test/idp \
  --catalog . --app-dir "$WORK/app" --work "$WORK/work" \
  --state-file "$WORK/state.yaml" --env staging --out "$WORK/out.yaml" >/dev/null

echo "==> xác nhận HTTPRoute render ra sectionName=$LISTENER"
python3 -c "
import yaml
r=next(d for d in yaml.safe_load_all(open('$WORK/out.yaml')) if d and d['kind']=='HTTPRoute')
p=r['spec']['parentRefs'][0]; assert p.get('sectionName')=='$LISTENER', p
print('   parentRefs:', p)
"

echo "==> deploy app (ánh xạ ảnh -> $IMAGE) + HTTPRoute vào $NS"
K create ns "$NS" --dry-run=client -o yaml | K apply -f - >/dev/null
python3 -c "
import yaml,sys
docs=[d for d in yaml.safe_load_all(open('$WORK/out.yaml')) if d]
for d in docs:
    if d['kind']=='Deployment':
        d['spec']['template']['spec'].pop('imagePullSecrets',None)
        for c in d['spec']['template']['spec']['containers']:
            c['image']='$IMAGE'; c['imagePullPolicy']='IfNotPresent'
sys.stdout.write(yaml.safe_dump_all(docs))
" | K -n "$NS" apply -f - >/dev/null
K -n "$NS" rollout status deploy/echo --timeout=120s

echo "==> KIỂM điều kiện HTTPRoute trên listener $LISTENER"
sleep 3
ROUTE=$(K -n "$NS" get httproute -o jsonpath='{.items[0].metadata.name}')
K -n "$NS" get httproute "$ROUTE" -o json | python3 -c "
import json,sys
parents=(json.load(sys.stdin).get('status') or {}).get('parents') or []
acc=ref=False
for p in parents:
    for c in p.get('conditions') or []:
        print('   ', p['parentRef'].get('sectionName'), c['type'], c['status'])
        acc=acc or (c['type']=='Accepted' and c['status']=='True')
        ref=ref or (c['type']=='ResolvedRefs' and c['status']=='True')
assert acc and ref, 'HTTPRoute chưa Accepted/ResolvedRefs'
print('   => Accepted & ResolvedRefs OK')
"

echo "==> curl HTTPS qua listener $LISTENER (port-forward traefik 443, verify bằng CA thật)"
K -n "$GWNS" port-forward svc/traefik 18443:443 >/dev/null 2>&1 &
PF_PID=$!
sleep 3
CODE=$(curl -s -o /dev/null -w "%{http_code}" --cacert "$WORK/ca.crt" \
  --resolve "$HOST:18443:127.0.0.1" "https://$HOST:18443/")
echo "   HTTP status qua HTTPS gateway: $CODE"
[[ "$CODE" == "200" ]] || { echo "curl HTTPS không trả 200"; exit 1; }

echo
echo "==> PASS: route công ty-shape đi qua HTTPS trên ingress-gateway THẬT · Accepted+ResolvedRefs · curl 200."
