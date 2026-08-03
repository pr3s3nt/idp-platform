#!/usr/bin/env bash
# Mint một installation token của GitHub App bằng openssl + curl, KHÔNG dùng action nào.
#
# VÌ SAO CÓ FILE NÀY
# actions/create-github-app-token không nằm trong bộ action đi kèm GitHub Enterprise Server.
# Đo trực tiếp trên GHES của công ty:
#   Error: Unable to resolve action `actions/create-github-app-token@v1`,
#          repository not found on this server.
# Workflow hỏng ngay ở bước lấy token, trước khi làm được bất cứ việc gì.
#
# Script này làm đúng việc của action đó bằng openssl + curl + python3 — đều có sẵn trên
# runner (workflow vốn đã cần python3 cho orchestrate.py).
#
# CHẠY TRÊN CẢ HAI LOẠI GITHUB
# Không ghim địa chỉ API. GitHub Actions tự đặt GITHUB_API_URL:
#   github.com -> https://api.github.com
#   GHES       -> https://github.cong-ty.vn/api/v3
# Nhờ vậy cùng một workflow chạy được ở sandbox lẫn ở công ty, không phải rẽ nhánh.
#
# Dùng ngoài workflow:
#   APP_ID=123 APP_PRIVATE_KEY="$(cat key.pem)" OWNER=cong-ty ./mint-app-token.sh
set -euo pipefail

: "${APP_ID:?thiếu APP_ID}"
: "${APP_PRIVATE_KEY:?thiếu APP_PRIVATE_KEY}"
API="${API:-${GITHUB_API_URL:-https://api.github.com}}"
API="${API%/}"

b64url() { openssl base64 -A | tr '+/' '-_' | tr -d '='; }

die() { echo "mint-app-token: $*" >&2; exit 1; }

# Gọi API và TRẢ LỖI RÕ RÀNG. Bản đầu tiên chỉ đọc trường .token rồi kiểm rỗng, nên mọi
# nguyên nhân — sai khoá, App chưa cài, GHES chặn — đều hiện ra như nhau: một chuỗi rỗng.
# Ở đây phần thân lỗi của GitHub được in nguyên văn.
api() {
  local method="$1" path="$2" body code
  body=$(curl -sS -X "$method" \
    -H "Authorization: Bearer $jwt" \
    -H "Accept: application/vnd.github+json" \
    -H "X-GitHub-Api-Version: 2022-11-28" \
    -w $'\n%{http_code}' "$API$path") || die "không gọi được $API$path"
  code="${body##*$'\n'}"; body="${body%$'\n'*}"
  [ "$code" -ge 200 ] && [ "$code" -lt 300 ] || die "$method $path -> HTTP $code: $body"
  printf '%s' "$body"
}

# ------------------------------------------------------------------ 1. ký JWT của App
# iat lùi 60 giây: GitHub từ chối JWT có thời điểm ở tương lai, mà đồng hồ máy chạy CI lệch
# vài giây là chuyện thường. exp tối đa 10 phút theo quy định của GitHub — để 9 phút.
now=$(date +%s)
header='{"alg":"RS256","typ":"JWT"}'
payload=$(printf '{"iat":%d,"exp":%d,"iss":"%s"}' "$((now - 60))" "$((now + 540))" "$APP_ID")

signing_input="$(printf %s "$header" | b64url).$(printf %s "$payload" | b64url)"
key_file=$(mktemp); trap 'rm -f "$key_file"' EXIT
chmod 600 "$key_file"
printf '%s\n' "$APP_PRIVATE_KEY" > "$key_file"
openssl rsa -in "$key_file" -noout >/dev/null 2>&1 \
  || die "APP_PRIVATE_KEY không phải khoá RSA đọc được (còn nguyên xuống dòng của file .pem chứ?)"
signature=$(printf %s "$signing_input" | openssl dgst -sha256 -sign "$key_file" -binary | b64url)
jwt="${signing_input}.${signature}"

# --------------------------------------------------- 2. tìm installation của đúng chủ sở hữu
# Bản đầu lấy installation ĐẦU TIÊN. Sai khi App được cài ở nhiều nơi: token sẽ mang quyền
# trên tổ chức khác, và lỗi chỉ hiện ra muộn dưới dạng 404 khi ghi vào repo. Action gốc nhận
# tham số `owner`, nên ở đây cũng tra theo chủ sở hữu.
# Lấy kết quả ra biến TRƯỚC khi phân tích. Nối thẳng `api | python3` thì khi api thất bại,
# python3 vẫn chạy với đầu vào rỗng và phun traceback ra màn hình — trông như hỏng nặng
# trong khi thực ra chỉ là "chủ sở hữu này không phải tổ chức, thử kiểu còn lại".
try_installation() {
  local out
  out=$(api GET "$1" 2>/dev/null) || return 1
  printf '%s' "$out" | python3 -c 'import json,sys
try:
    print(json.load(sys.stdin).get("id", ""))
except Exception:
    pass'
}

if [ -z "${INSTALLATION_ID:-}" ] && [ -n "${OWNER:-}" ]; then
  # Chủ sở hữu có thể là tổ chức hoặc tài khoản cá nhân — thử lần lượt, im lặng.
  INSTALLATION_ID=$(try_installation "/orgs/$OWNER/installation" || true)
  [ -n "$INSTALLATION_ID" ] \
    || INSTALLATION_ID=$(try_installation "/users/$OWNER/installation" || true)
fi

if [ -z "${INSTALLATION_ID:-}" ]; then
  INSTALLATION_ID=$(api GET "/app/installations" \
    | python3 -c '
import json, sys
d = json.load(sys.stdin)
if len(d) > 1:
    sys.stderr.write("mint-app-token: App được cài ở %d nơi, không có OWNER để chọn -> lấy cái đầu\n" % len(d))
print(d[0]["id"] if d else "")')
fi
[ -n "$INSTALLATION_ID" ] || die "App $APP_ID chưa được cài ở đâu cả (hoặc khoá không khớp App này)"

# ------------------------------------------------------------ 3. đổi JWT lấy token cài đặt
token=$(api POST "/app/installations/$INSTALLATION_ID/access_tokens" \
  | python3 -c 'import json,sys; print(json.load(sys.stdin).get("token",""))')
[ -n "$token" ] || die "gọi được API nhưng không có token trong phản hồi"

# Che token khỏi log TRƯỚC khi nó có cơ hội lọt ra, rồi đưa vào output của step để phần
# workflow phía sau dùng y như output của action gốc.
if [ -n "${GITHUB_OUTPUT:-}" ]; then
  echo "::add-mask::$token"
  echo "token=$token" >> "$GITHUB_OUTPUT"
  echo "installation-id=$INSTALLATION_ID" >> "$GITHUB_OUTPUT"
  echo "==> đã mint token cho installation $INSTALLATION_ID qua $API"
else
  printf '%s\n' "$token"
fi
