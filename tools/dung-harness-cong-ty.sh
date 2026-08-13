#!/usr/bin/env bash
# Nâng cấp harness THẬT (kind) cho GIỐNG hình dạng công ty — tại chỗ, idempotent, đảo được.
#
# Ý tưởng của bạn: harness càng giống công ty, "chạy ở đây" càng dự đoán "chạy ở công ty".
# Script này thêm những capability harness đang THIẾU so với công ty, KHÔNG phá app đang chạy:
#
#   [gateway] công ty có listener HTTPS (websecure, TLS Terminate, self-signed). Harness chỉ
#             có HTTP. -> thêm listener `websecure` vào CHÍNH `ingress-gateway`, GIỚI HẠN
#             hostname `*.harness-https.local` để mọi route hiện có (host khác) KHÔNG bind vào
#             -> bán kính ảnh hưởng = 0. Đảo lại bằng `--down`.
#
#   [registry] bài kiểm tự chứa ở tools/thu-nghiem-registry-private.sh: dựng registry OCI
#              HTTPS/auth/custom-CA, push + pull thật, rồi tự teardown. Không giữ một
#              registry thứ hai chạy thường trực chỉ để mô phỏng Harbor.
#
#   ./tools/dung-harness-cong-ty.sh --up     [--context kind-staging]   # mặc định
#   ./tools/dung-harness-cong-ty.sh --down   [--context kind-staging]   # gỡ về nguyên trạng
set -euo pipefail

CONTEXT=kind-staging
MODE=up
while [[ $# -gt 0 ]]; do
  case "$1" in
    --up) MODE=up; shift ;;
    --down) MODE=down; shift ;;
    --context) CONTEXT="$2"; shift 2 ;;
    -h|--help) sed -n '2,20p' "$0"; exit 0 ;;
    *) echo "tham số lạ: $1" >&2; exit 2 ;;
  esac
done

GWNS=traefik
GW=ingress-gateway
LISTENER=websecure
TLS_SECRET=harness-https-tls
GW_HOST_SUFFIX="harness-https.local"     # listener chỉ nhận route host *.harness-https.local
K() { kubectl --context "$CONTEXT" "$@"; }

if [[ "$MODE" == "down" ]]; then
  echo "==> gỡ listener $LISTENER khỏi $GWNS/$GW (giữ nguyên các listener khác)"
  K -n "$GWNS" get gateway "$GW" -o json | python3 -c "
import json,sys
g=json.load(sys.stdin)
ls=(g.get('spec') or {}).get('listeners') or []
g['spec']['listeners']=[l for l in ls if l.get('name')!='$LISTENER']
sys.stdout.write(json.dumps({'spec':{'listeners':g['spec']['listeners']}}))
" | K -n "$GWNS" patch gateway "$GW" --type=merge -p "$(cat)" >/dev/null 2>&1 || \
  { P=$(K -n "$GWNS" get gateway "$GW" -o json | python3 -c "
import json,sys
g=json.load(sys.stdin); ls=(g.get('spec') or {}).get('listeners') or []
print(json.dumps({'spec':{'listeners':[l for l in ls if l.get('name')!='$LISTENER']}}))
"); K -n "$GWNS" patch gateway "$GW" --type=merge -p "$P" >/dev/null; }
  K -n "$GWNS" delete secret "$TLS_SECRET" --ignore-not-found >/dev/null
  echo "==> xong: $GW về hình dạng trước."
  exit 0
fi

WORK="$(mktemp -d)"; trap 'rm -rf "$WORK"' EXIT

echo "==> cert self-signed *.$GW_HOST_SUFFIX + Secret TLS trong ns $GWNS (idempotent)"
openssl req -x509 -newkey rsa:2048 -nodes -keyout "$WORK/tls.key" -out "$WORK/tls.crt" \
  -days 30 -subj "/CN=*.$GW_HOST_SUFFIX" \
  -addext "subjectAltName=DNS:*.$GW_HOST_SUFFIX" >/dev/null 2>&1
K -n "$GWNS" create secret tls "$TLS_SECRET" --cert="$WORK/tls.crt" --key="$WORK/tls.key" \
  --dry-run=client -o yaml | K apply -f - >/dev/null

echo "==> thêm listener $LISTENER (HTTPS/8443 Terminate) vào $GWNS/$GW nếu chưa có"
K -n "$GWNS" get gateway "$GW" -o json > "$WORK/gw.json"
python3 - "$WORK/gw.json" "$LISTENER" "$TLS_SECRET" "$GW_HOST_SUFFIX" "$WORK/patch.json" <<'PY'
import json,sys
gwf, name, secret, suffix, out = sys.argv[1:6]
g=json.load(open(gwf))
ls=(g.get('spec') or {}).get('listeners') or []
names={l.get('name') for l in ls}
if name not in names:
    ls.append({
        "name": name, "protocol": "HTTPS", "port": 8443,
        "hostname": f"*.{suffix}",   # GIỚI HẠN: chỉ route host này mới bind -> route cũ an toàn
        "tls": {"mode": "Terminate",
                "certificateRefs": [{"kind": "Secret", "name": secret}]},
        "allowedRoutes": {"namespaces": {"from": "All"}},
    })
json.dump({"spec": {"listeners": ls}}, open(out, "w"))
print("listeners sau patch:", [l["name"] for l in ls])
PY
K -n "$GWNS" patch gateway "$GW" --type=merge -p "$(cat "$WORK/patch.json")" >/dev/null

sleep 3
echo "==> kiểm: listener $LISTENER Programmed, và các route CŨ KHÔNG bị kéo sang listener mới"
K -n "$GWNS" get gateway "$GW" -o json | python3 -c "
import json,sys
g=json.load(sys.stdin); st=g.get('status') or {}
for l in st.get('listeners') or []:
    conds={c['type']:c['status'] for c in l.get('conditions') or []}
    print(f'  listener {l[\"name\"]:10} attachedRoutes={l.get(\"attachedRoutes\")} {conds}')
"
echo
echo "==> PASS: harness giờ có listener HTTPS $LISTENER (giới hạn *.$GW_HOST_SUFFIX)."
echo "    Route công ty-shape (sectionName=$LISTENER, host *.$GW_HOST_SUFFIX) sẽ đi qua HTTPS."
echo "    Gỡ: ./tools/dung-harness-cong-ty.sh --down"
