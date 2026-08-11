#!/usr/bin/env bash
# Chạy các CẢNH BÁO phía cụm ở docs/canh-bao.md và thoát khác 0 nếu có cái nào đang kêu.
#
# Vì sao có file này: một đặc tả cảnh báo mà chưa ai chạy thì chỉ là văn. Script này biến
# từng biểu thức trong docs/canh-bao.md thành một lệnh chạy được, nên khi biểu thức sai
# (sai tên trường, sai kiểu) thì biết ngay, chứ không phải biết vào ngày có sự cố thật.
#
# Đây KHÔNG phải hệ giám sát. Nó không có lịch sử, không có `for:`, nên nó đánh giá điều
# kiện tại một thời điểm — ngưỡng THỜI GIAN trong docs/canh-bao.md vẫn phải do hệ giám sát
# của công ty áp. Dùng script này để kiểm tình trạng hiện tại và để kiểm chính các biểu
# thức đó.
#
#   ./tools/kiem-suc-khoe.sh                 # toàn cụm
#   ./tools/kiem-suc-khoe.sh --namespace X   # một namespace
set -uo pipefail

NS_FLAG=(-A)
while [[ $# -gt 0 ]]; do
  case "$1" in
    --namespace|-n) NS_FLAG=(-n "$2"); shift 2 ;;
    -h|--help) sed -n '2,17p' "$0"; exit 0 ;;
    *) echo "tham số lạ: $1" >&2; exit 2 ;;
  esac
done

PROBLEMS=0
note() { echo "  $*"; PROBLEMS=$((PROBLEMS + 1)); }
section() { echo; echo "== $* =="; }

section "A1  VaultStaticSecret không đồng bộ (docs/canh-bao.md#a1)"
out=$(kubectl get vaultstaticsecret "${NS_FLAG[@]}" -o json 2>/dev/null | python3 -c "
import json,sys
try: items=json.load(sys.stdin)['items']
except Exception: sys.exit(0)
for i in items:
    conds=(i.get('status') or {}).get('conditions') or []
    c=next((c for c in conds if c['type']=='SecretSynced'), None)
    # status, KHÔNG phải reason: một CR đang hỏng vẫn có reason='Synced'
    if not c or c['status']!='True':
        print('%s/%s %s' % (i['metadata']['namespace'], i['metadata']['name'],
                            (c or {}).get('message','(chưa có status)')[:90]))
")
[[ -n "$out" ]] && while IFS= read -r l; do note "$l"; done <<< "$out" || echo "  ok"

section "B1  Database KHÔNG phục hồi được (docs/canh-bao.md#b1)"
out=$(kubectl get cluster.postgresql.cnpg.io "${NS_FLAG[@]}" -o json 2>/dev/null | python3 -c "
import json,sys
try: items=json.load(sys.stdin)['items']
except Exception: sys.exit(0)
for c in items:
    if not ((c.get('spec') or {}).get('backup') or {}).get('barmanObjectStore'):
        continue
    st=c.get('status') or {}
    if not st.get('firstRecoverabilityPoint'):
        print('%s/%s: có kho object nhưng chưa có base backup nào — bootstrap.recovery sẽ '
              'fail với \"no target backup found\"' % (c['metadata']['namespace'], c['metadata']['name']))
")
[[ -n "$out" ]] && while IFS= read -r l; do note "$l"; done <<< "$out" || echo "  ok"

section "B3  CNPG Cluster không Ready (docs/canh-bao.md#b3)"
out=$(kubectl get cluster.postgresql.cnpg.io "${NS_FLAG[@]}" -o json 2>/dev/null | python3 -c "
import json,sys
try: items=json.load(sys.stdin)['items']
except Exception: sys.exit(0)
for c in items:
    conds=(c.get('status') or {}).get('conditions') or []
    r=next((x for x in conds if x['type']=='Ready'), None)
    if not r or r['status']!='True':
        print('%s/%s phase=%s' % (c['metadata']['namespace'], c['metadata']['name'],
                                  (c.get('status') or {}).get('phase','?')))
")
[[ -n "$out" ]] && while IFS= read -r l; do note "$l"; done <<< "$out" || echo "  ok"

section "B4  Xoay vòng credential dở dang (docs/canh-bao.md#b4)"
out=$(kubectl get cluster.postgresql.cnpg.io "${NS_FLAG[@]}" -o json 2>/dev/null | python3 -c "
import json,subprocess,sys
try: items=json.load(sys.stdin)['items']
except Exception: sys.exit(0)
for c in items:
    ns=c['metadata']['namespace']
    st=((c.get('status') or {}).get('managedRolesStatus') or {}).get('passwordStatus') or {}
    for role in (((c.get('spec') or {}).get('managed') or {}).get('roles') or []):
        sec=(role.get('passwordSecret') or {}).get('name')
        if not sec: continue
        rv=subprocess.run(['kubectl','get','secret',sec,'-n',ns,'-o',
                           'jsonpath={.metadata.resourceVersion}'],
                          capture_output=True,text=True).stdout.strip()
        seen=(st.get(role['name']) or {}).get('resourceVersion')
        if rv and seen and rv!=seen:
            # Trạng thái này KHÔNG có triệu chứng cho tới lần restart pod kế tiếp.
            print('%s/%s role=%s: Secret ở %s nhưng CNPG mới áp %s — chạy '
                  'rotate-db-credential' % (ns, c['metadata']['name'], role['name'], rv, seen))
")
[[ -n "$out" ]] && while IFS= read -r l; do note "$l"; done <<< "$out" || echo "  ok"

section "C1/C2  Fleet bundle Modified hoặc NotReady (docs/canh-bao.md#c1)"
out=$(kubectl get bundle -A -o json 2>/dev/null | python3 -c "
import json,sys
try: items=json.load(sys.stdin)['items']
except Exception: sys.exit(0)
for b in items:
    for r in (b.get('status') or {}).get('resources') or []:
        if r.get('state') in ('Modified','NotReady'):
            print('%s/%s %s/%s %s' % (b['metadata']['namespace'], b['metadata']['name'],
                                      r.get('kind'), r.get('name'),
                                      (r.get('message') or '')[:70]))
")
[[ -n "$out" ]] && while IFS= read -r l; do note "$l"; done <<< "$out" || echo "  ok"

section "F1  Rollout quá giờ (docs/canh-bao.md#f1)"
out=$(kubectl get deploy "${NS_FLAG[@]}" -o json 2>/dev/null | python3 -c "
import json,sys
try: items=json.load(sys.stdin)['items']
except Exception: sys.exit(0)
for d in items:
    m,s=d['metadata'],(d.get('status') or {})
    want=(d.get('spec') or {}).get('replicas',1)
    # updatedReplicas/observedGeneration, KHÔNG phải availableReplicas: pod cũ vẫn
    # available trong lúc bản mới không lên nổi.
    if s.get('observedGeneration',0) < m.get('generation',0) or s.get('updatedReplicas',0) < want:
        print('%s/%s updated=%s/%s gen=%s/%s' % (m['namespace'], m['name'],
              s.get('updatedReplicas',0), want, s.get('observedGeneration',0), m.get('generation',0)))
")
[[ -n "$out" ]] && while IFS= read -r l; do note "$l"; done <<< "$out" || echo "  ok"

echo
if [[ "$PROBLEMS" -gt 0 ]]; then
  echo "==> $PROBLEMS cảnh báo đang kêu. Runbook: docs/runbook/"
  exit 1
fi
echo "==> không có cảnh báo nào."
