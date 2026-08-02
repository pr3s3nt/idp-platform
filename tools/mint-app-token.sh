#!/usr/bin/env bash
# Mint một installation token của GitHub App bằng openssl + curl, KHÔNG dùng action nào.
#
# Vì sao có file này: actions/create-github-app-token không nằm trong bộ action đi kèm
# GitHub Enterprise Server. Trên GHES không bật GitHub Connect thì workflow sẽ hỏng ngay ở
# bước đó. Script này làm đúng việc của action đó, bằng công cụ có sẵn trên mọi runner.
#
# Dùng:
#   APP_ID=123 APP_PRIVATE_KEY="$(cat key.pem)" API=https://api.github.com \
#     ./mint-app-token.sh > token.txt
#
# Trên GHES: API=https://github.cong-ty.vn/api/v3
set -euo pipefail

: "${APP_ID:?thiếu APP_ID}"
: "${APP_PRIVATE_KEY:?thiếu APP_PRIVATE_KEY}"
API="${API:-https://api.github.com}"

b64url() { openssl base64 -A | tr '+/' '-_' | tr -d '='; }

# iat lùi 60 giây: GitHub từ chối JWT có iat ở tương lai, và đồng hồ runner lệch vài giây
# là chuyện thường. exp tối đa 10 phút theo quy định của GitHub — để 9 phút cho an toàn.
now=$(date +%s)
header='{"alg":"RS256","typ":"JWT"}'
payload=$(printf '{"iat":%d,"exp":%d,"iss":"%s"}' "$((now - 60))" "$((now + 540))" "$APP_ID")

signing_input="$(printf %s "$header" | b64url).$(printf %s "$payload" | b64url)"
key_file=$(mktemp); trap 'rm -f "$key_file"' EXIT
printf '%s\n' "$APP_PRIVATE_KEY" > "$key_file"
signature=$(printf %s "$signing_input" | openssl dgst -sha256 -sign "$key_file" -binary | b64url)
jwt="${signing_input}.${signature}"

# Một App cài ở nhiều nơi thì có nhiều installation. Lấy cái đầu tiên nếu không chỉ định —
# đúng với mô hình một App cho một tổ chức.
if [ -z "${INSTALLATION_ID:-}" ]; then
  INSTALLATION_ID=$(curl -sS -H "Authorization: Bearer $jwt" \
    -H "Accept: application/vnd.github+json" "$API/app/installations" \
    | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d[0]["id"] if d else "")')
fi
[ -n "$INSTALLATION_ID" ] || { echo "không tìm thấy installation nào của App $APP_ID" >&2; exit 1; }

token=$(curl -sS -X POST -H "Authorization: Bearer $jwt" \
  -H "Accept: application/vnd.github+json" \
  "$API/app/installations/$INSTALLATION_ID/access_tokens" \
  | python3 -c 'import json,sys; print(json.load(sys.stdin).get("token",""))')
[ -n "$token" ] || { echo "không lấy được installation token" >&2; exit 1; }
printf '%s\n' "$token"
