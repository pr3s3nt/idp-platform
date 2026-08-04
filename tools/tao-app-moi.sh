#!/usr/bin/env bash
# Dựng toàn bộ phần chuẩn bị cho một ứng dụng mới.
#
# CHẠY BẰNG TÀI KHOẢN CỦA BẠN, không phải tài khoản bot. Đó là chủ ý: việc tạo repo và
# mời cộng tác viên cần quyền cao, mà cấp quyền đó cho bot nghĩa là bot tạo được repo ở
# bất kỳ đâu trong tổ chức. Người chạy một lần thì không phải mở rộng gì cả.
#
# Idempotent: chạy lại nhiều lần không sao, cái gì có rồi thì bỏ qua.
#
# Dùng:
#   ORG=example-org APP=thanh-toan BOT=tai-khoan-bot \
#     PLATFORM_REPO=example-org/idp-platform ./tao-app-moi.sh
set -euo pipefail

: "${ORG:?thiếu ORG}"
: "${APP:?thiếu APP}"
: "${PLATFORM_REPO:?thiếu PLATFORM_REPO — ví dụ example-org/idp-platform}"
BOT="${BOT:-}"
CONFIG_REPO="${CONFIG_REPO:-$APP-config}"
# KHÔNG viết ${NS_PATTERN:-{app}-{env}}: bash cắt biểu thức ở dấu } ĐẦU TIÊN, nên giá trị
# mặc định bị hỏng và thừa một } ở cuối. Đã đo: namespace ra "app-staging-{env}}".
NS_PATTERN="${NS_PATTERN:-}"
[ -n "$NS_PATTERN" ] || NS_PATTERN='{app}-{env}'

# Chạy trong workflow thì git chưa có thông tin đăng nhập. gh có token rồi nên nhờ nó
# cài credential helper — không phải nhét token vào URL, thứ dễ lọt vào log và vào remote.
if [ -n "${GH_TOKEN:-}${GH_ENTERPRISE_TOKEN:-}" ] && [ -n "${CI:-}" ]; then
  gh auth setup-git >/dev/null 2>&1 || true
fi

ns() {
  local s="$NS_PATTERN"
  s="${s//\{app\}/$APP}"
  s="${s//\{env\}/$1}"
  printf '%s' "$s"
}
co()  { echo; echo "==> $*"; }

co "Ứng dụng: $APP   Kho cấu hình: $ORG/$CONFIG_REPO"
echo "    namespace: $(ns staging) / $(ns prod)"

# ------------------------------------------------------------------ 1. kho cấu hình
if gh repo view "$ORG/$CONFIG_REPO" >/dev/null 2>&1; then
  co "Kho $ORG/$CONFIG_REPO đã có -> bỏ qua"
else
  co "Tạo kho $ORG/$CONFIG_REPO"
  gh repo create "$ORG/$CONFIG_REPO" --private --description "Manifest do IDP sinh cho $APP"
fi

# ------------------------------------------------ 2. gieo hai nhánh dev và main
# Fleet coi mỗi thư mục có fleet.yaml là một Bundle riêng và lấy defaultNamespace trong đó
# làm nơi đặt tài nguyên. Platform cũng tự sinh file này khi render, nhưng gieo sẵn ở đây
# thì Fleet có cái để đọc ngay từ đầu, không phải chờ lần deploy đầu tiên.
TMP=$(mktemp -d); trap 'rm -rf "$TMP"' EXIT
git clone -q "$(gh repo view "$ORG/$CONFIG_REPO" --json sshUrl -q .sshUrl 2>/dev/null \
  || gh repo view "$ORG/$CONFIG_REPO" --json url -q .url)" "$TMP/cfg" 2>/dev/null \
  || git clone -q "$(gh repo view "$ORG/$CONFIG_REPO" --json url -q .url)" "$TMP/cfg"
cd "$TMP/cfg"

seed_branch() {
  local br="$1" env="$2"
  if git ls-remote --exit-code --heads origin "$br" >/dev/null 2>&1; then
    echo "    nhánh $br đã có -> bỏ qua"; return
  fi
  git checkout -q --orphan "$br" 2>/dev/null || git checkout -q -B "$br"
  git rm -rq --cached . 2>/dev/null || true
  rm -rf ./*  2>/dev/null || true
  mkdir -p "$env"
  cat > "$env/fleet.yaml" <<EOF
# Fleet coi thư mục này là một Bundle. defaultNamespace quyết định tài nguyên đặt ở đâu.
namespace: $(ns "$env")
defaultNamespace: $(ns "$env")
EOF
  cat > README.md <<EOF
# $CONFIG_REPO

Manifest do IDP sinh ra. **Đừng sửa tay** — lần deploy sau sẽ ghi đè.

| Nhánh | Môi trường | Thư mục |
|---|---|---|
| \`dev\` | staging | \`staging/\` |
| \`main\` | production | \`prod/\` |
EOF
  git add -A
  git -c user.name="$(git config user.name || echo idp)" \
      -c user.email="$(git config user.email || echo idp@local)" \
      commit -qm "khởi tạo nhánh $br cho môi trường $env"
  git push -q origin "$br"
  echo "    tạo nhánh $br (thư mục $env/)"
}

co "Gieo hai nhánh"
seed_branch dev staging
seed_branch main prod

# ---------------------------------------------- 3. workflow verify của kho cấu hình
co "Cài workflow verify vào kho cấu hình"
for br in dev main; do
  git checkout -q "$br" && git pull -q origin "$br" 2>/dev/null || true
  mkdir -p .github/workflows
  curl -sfL "https://raw.githubusercontent.com/$PLATFORM_REPO/main/templates/config-repo-verify.yaml" \
    -o .github/workflows/verify.yaml 2>/dev/null \
    || gh api "repos/$PLATFORM_REPO/contents/templates/config-repo-verify.yaml" \
         --jq '.content' | base64 -d > .github/workflows/verify.yaml
  sed -i "s|__APP__|$APP|; s|__PLATFORM_REPO__|$PLATFORM_REPO|" .github/workflows/verify.yaml
  if git diff --quiet .github/workflows/verify.yaml 2>/dev/null && \
     git ls-files --error-unmatch .github/workflows/verify.yaml >/dev/null 2>&1; then
    echo "    $br: đã có, không đổi"
  else
    git add .github/workflows/verify.yaml
    git -c user.name="$(git config user.name || echo idp)" \
        -c user.email="$(git config user.email || echo idp@local)" \
        commit -qm "ci: gọi platform kiểm cụm khi nhánh này đổi" && git push -q origin "$br"
    echo "    $br: đã cài"
  fi
done

# ------------------------------------------------------------------ 4. mời bot
if [ -n "$BOT" ]; then
  co "Mời $BOT vào hai kho (quyền write)"
  for R in "$APP" "$CONFIG_REPO"; do
    gh api -X PUT "repos/$ORG/$R/collaborators/$BOT" -f permission=push >/dev/null 2>&1 \
      && echo "    $R: đã mời" || echo "    $R: KHÔNG mời được (repo chưa tồn tại?)"
  done
  echo "    ⚠️  Bot phải CHẤP NHẬN lời mời thì mới vào được."
else
  co "Bỏ qua bước mời bot (không đặt biến BOT)"
fi

# ------------------------------------------------------------------ 5. còn lại
cat <<EOF

==> XONG PHẦN TỰ ĐỘNG. Còn ba việc phải làm tay:

  1. Đặt secret:
       gh secret set PLATFORM_DISPATCH_TOKEN -R $ORG/$APP
       gh secret set PLATFORM_DISPATCH_TOKEN -R $ORG/$CONFIG_REPO
       gh secret set HARBOR_PASSWORD         -R $ORG/$APP

  2. Bật bảo vệ nhánh 'main' của $ORG/$CONFIG_REPO, bắt buộc pull request + người duyệt.
     CỐ Ý để người làm: đây là điểm kiểm soát duy nhất của con người trong cả luồng.

  3. Đăng ký runner cho $ORG/$APP (job push cần máy vào được registry nội bộ).

Xong ba việc đó thì push lên nhánh dev của $ORG/$APP là app lên staging.
GitRepo của Fleet platform tự tạo, không phải làm gì.
EOF
