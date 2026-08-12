#!/usr/bin/env bash
# Đồng bộ CODE của nền tảng này sang một repo ĐỘC LẬP (ví dụ Git self-hosted của công ty).
#
# VÌ SAO CHÉP FILE CHỨ KHÔNG MERGE GIT: repo công ty là một repo riêng, lịch sử không liên
# thông với repo này, nên `git merge`/`git push` không dùng được. Đồng bộ = chép ĐÚNG bộ file
# mà git theo dõi.
#
# VÌ SAO KHÔNG `cp -r`: `cp -r` kéo theo `.git/` (đè lịch sử repo công ty), `__pycache__`,
# `.claude/`, thư mục scratch onboard/work — rác không thuộc mã nguồn. `git archive` chỉ xuất
# ĐÚNG các file được git theo dõi ở ref chỉ định, kèm dotfile (`.github/`, `.gitignore`), và
# tự loại mọi thứ trên.
#
# VÌ SAO CHỪA platform.env.yaml: đây là nơi DUY NHẤT chứa toạ độ hạ tầng (org, registry, vault,
# gateway, storage class...). Trên GHES, runner đọc đúng file này. Chép đè lên nó = mang giá
# trị sandbox đè lên giá trị công ty -> deploy nhầm hạ tầng. Mặc định script CHỪA nó ra và chỉ
# IN diff để bạn merge tay (thêm KEY mới, giữ VALUE công ty).
#
# Dùng:
#   tools/dong-bo-sang-cong-ty.sh /duong/dan/repo-cong-ty [ref]
#     ref = nhánh/commit nguồn, mặc định HEAD (nhánh đang checkout).
#
# Cờ:
#   --with-env-config   chép luôn platform.env.yaml (mặc định KHÔNG — chỉ dùng cho lần đầu khi
#                       repo công ty chưa có file này và bạn sẽ điền toạ độ ngay sau đó).
#   --dry-run           chỉ liệt kê sẽ ghi những gì, không ghi thật.
set -euo pipefail

WITH_ENV_CONFIG=0
DRY_RUN=0
POS=()
for a in "$@"; do
  case "$a" in
    --with-env-config) WITH_ENV_CONFIG=1 ;;
    --dry-run)         DRY_RUN=1 ;;
    -h|--help)         grep '^#' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    --*)               echo "cờ lạ: $a" >&2; exit 2 ;;
    *)                 POS+=("$a") ;;
  esac
done

TARGET="${POS[0]:-}"
REF="${POS[1]:-HEAD}"
PROTECTED="platform.env.yaml"   # file toạ độ, chừa ra trừ khi --with-env-config

[ -n "$TARGET" ] || { echo "thiếu đường dẫn repo công ty. Xem: $0 --help" >&2; exit 2; }

SRC="$(git rev-parse --show-toplevel)"
SREF="$(git -C "$SRC" rev-parse --short "$REF")"
co() { echo; echo "==> $*"; }

co "Nguồn : $SRC @ $REF ($SREF)"
echo "    Đích  : $TARGET"

# --- kiểm đích là một git repo ------------------------------------------------------------
if [ ! -d "$TARGET/.git" ] && ! git -C "$TARGET" rev-parse --git-dir >/dev/null 2>&1; then
  echo "!! $TARGET không phải một git repo. Tạo/clone repo công ty rồi checkout nhánh mới trước." >&2
  exit 1
fi
TBRANCH="$(git -C "$TARGET" symbolic-ref --short HEAD 2>/dev/null || echo '(detached)')"
echo "    Nhánh đích hiện tại: $TBRANCH"
if [ "$TBRANCH" = "main" ] || [ "$TBRANCH" = "master" ]; then
  echo "!! Đang đứng ở '$TBRANCH' của repo công ty. Hãy checkout một NHÁNH MỚI (off main) rồi chạy lại." >&2
  exit 1
fi

# --- danh sách exclude --------------------------------------------------------------------
EXCLUDES=()
[ "$WITH_ENV_CONFIG" -eq 1 ] || EXCLUDES+=("--exclude=$PROTECTED")

# --- dry-run: chỉ liệt kê -----------------------------------------------------------------
if [ "$DRY_RUN" -eq 1 ]; then
  co "DRY-RUN — các file SẼ được ghi vào $TARGET:"
  git -C "$SRC" archive "$REF" | tar -t "${EXCLUDES[@]}" | sed 's/^/    /'
  [ "$WITH_ENV_CONFIG" -eq 1 ] || echo "    (CHỪA $PROTECTED — thêm --with-env-config nếu muốn chép cả nó)"
  exit 0
fi

# --- chép thật ----------------------------------------------------------------------------
co "Chép file tracked của nhánh nguồn vào repo công ty"
git -C "$SRC" archive "$REF" | tar -x "${EXCLUDES[@]}" -C "$TARGET"
# đếm ĐÚNG file: bỏ các mục thư mục (tar liệt kê thư mục với đuôi '/').
N="$(git -C "$SRC" archive "$REF" | tar -t "${EXCLUDES[@]}" | grep -c '[^/]$' || true)"
echo "    đã ghi $N file"

# --- báo cáo platform.env.yaml (nếu chừa) -------------------------------------------------
if [ "$WITH_ENV_CONFIG" -eq 0 ]; then
  co "platform.env.yaml — ĐÃ CHỪA. Bạn phải merge TAY (thêm key mới, giữ value công ty)."
  if [ -f "$TARGET/$PROTECTED" ]; then
    echo "    diff (nguồn '<' vs công ty '>') — dòng '<' là thứ nguồn có mà công ty có thể còn thiếu:"
    if diff <(git -C "$SRC" show "$REF:$PROTECTED") "$TARGET/$PROTECTED" > /tmp/envdiff.$$ 2>/dev/null; then
      echo "    (hai bên giống hệt — không cần merge)"
    else
      sed 's/^/      /' /tmp/envdiff.$$
    fi
    rm -f /tmp/envdiff.$$
  else
    echo "    Repo công ty CHƯA có $PROTECTED. Lần đầu: chạy lại với --with-env-config rồi ĐIỀN"
    echo "    toạ độ công ty (org, registry, vault.address, ingress.*, storage_class...),"
    echo "    hoặc dựa vào platform.env.company.yaml làm mẫu."
  fi
fi

# --- báo cáo file chỉ-có-ở-đích (ứng viên đã bị nguồn xoá) --------------------------------
co "File CÓ ở repo công ty mà KHÔNG có ở nguồn (soi xem có cần xoá tay không):"
comm -23 \
  <(git -C "$TARGET" ls-files | sort) \
  <(git -C "$SRC" ls-tree -r --name-only "$REF" | sort) \
  | grep -vE "^$PROTECTED$" | sed 's/^/    /' || true
echo "    (rỗng = không có gì lệch; git archive KHÔNG tự xoá file, nên phải xoá tay nếu cần)"

# --- nhắc bước tiếp ----------------------------------------------------------------------
cat <<EOF

==> XONG PHẦN CHÉP. Ở repo công ty giờ hãy:
  1. git status / git diff   — soi thay đổi.
  2. Merge tay platform.env.yaml (nếu có diff ở trên): thêm KEY mới, GIỮ value công ty.
  3. Khai trên GHES (KHÔNG phải file): biến RUNNER_LABEL / CI_RUNNER_LABEL; secret
     APP_ID+APP_PRIVATE_KEY (hoặc BOT_TOKEN), KUBECONFIG_STAGING/PROD, REGISTRY_HOST/USER/PASS.
  4. python3 -m pytest test_orchestrate.py -q   — lớp 1 phải xanh trước khi commit.
  5. git add -A && git commit && mở pull request.

Chi tiết: HUONG-DAN-DONG-BO-CONG-TY.md
EOF
