# Đăng ký GitHub App cho IDP Orchestrator

> Thay thế PAT cá nhân đang làm 5 việc cùng lúc bằng một danh tính máy có phạm vi hẹp.
> Thời gian: khoảng 10 phút.

---

## Vì sao (tóm tắt)

Hiện `APP_REPOS_TOKEN`, `CONFIG_REPO_TOKEN`, `PLATFORM_DISPATCH_TOKEN`, `REGISTRY_PASS`
và credential của Fleet **đều là cùng một PAT** có `admin:org`, `delete_repo`. App thay
được 3 trong 5 cái đầu, bằng token sống 1 giờ, chỉ trên repo được cài.

Quan trọng nhất: GitHub **chặn tự duyệt PR của chính mình**. PR triển khai hiện do PAT của
bạn tạo, nên nếu bạn cũng là người duyệt thì **không ai duyệt được** — cổng duyệt prod tự
khoá. App làm tác giả thì bất kỳ ai cũng duyệt được.

---

## Bước 1 — Tạo App

Vào: **https://github.com/settings/apps/new**

> Với org của công ty sau này thì vào:
> `https://github.com/organizations/<TÊN-ORG>/settings/apps/new`

Điền đúng như bảng:

| Trường | Giá trị | Ghi chú |
|---|---|---|
| **GitHub App name** | `idp-orchestrator-pr3s3nt` | Phải **duy nhất toàn GitHub**. Trùng thì thêm hậu tố |
| **Homepage URL** | `https://github.com/pr3s3nt/idp-platform` | Không quan trọng, điền gì cũng được |
| **Webhook → Active** | ☐ **BỎ TICK** | Quan trọng. Ta không dùng webhook; để bật thì GitHub cứ gửi sự kiện vào hư không |
| **Where can this App be installed?** | `Only on this account` | |

Các trường còn lại để trống.

## Bước 2 — Cấp quyền

Kéo xuống mục **Permissions → Repository permissions**. Chỉ cần **3 quyền**, để nguyên mặc
định `No access` cho tất cả những mục khác:

| Quyền | Mức | Dùng để làm gì |
|---|---|---|
| **Contents** | `Read and write` | Đọc repo app khi checkout; ghi manifest vào repo config; và gửi `repository_dispatch` từ CI app sang platform |
| **Pull requests** | `Read and write` | Mở PR vào nhánh production của repo config |
| **Metadata** | `Read-only` | GitHub tự tick, bắt buộc |

**KHÔNG cần** và đừng cấp: Actions, Administration, Secrets, Workflows, Packages,
Members, bất cứ thứ gì khác.

> Nếu sau này muốn App tự sửa file trong `.github/workflows/` thì mới cần thêm
> `Workflows: write`. Hiện không cần — orchestrator chỉ ghi manifest.

Bấm **Create GitHub App**.

## Bước 3 — Lấy App ID

Sau khi tạo, trang cài đặt App hiện **App ID** ở gần đầu (một con số, ví dụ `1234567`).

→ **Ghi lại số này**, gửi thẳng cho tôi trong chat cũng được. Nó **không phải bí mật**.

## Bước 4 — Sinh private key

Vẫn ở trang đó, kéo xuống cuối mục **Private keys** → bấm **Generate a private key**.
Trình duyệt sẽ tải về một file `.pem`.

→ File này **LÀ BÍ MẬT**. Đừng dán vào chat. Lưu vào máy bằng lệnh sau, chạy trong
terminal của bạn:

```bash
umask 077 && mv ~/Downloads/*.private-key.pem ~/.idp-app-key.pem && ls -l ~/.idp-app-key.pem
```

(Sửa lại đường dẫn nếu trình duyệt tải về chỗ khác. Trên Windows tải về thì đường dẫn
trong WSL là `/mnt/c/Users/<tên>/Downloads/...`)

Tôi sẽ đọc file đó qua stdin để mint token, **không bao giờ in nội dung ra**.

## Bước 5 — Cài App vào các repo

Ở menu bên trái chọn **Install App** → bấm **Install** cạnh tài khoản `pr3s3nt`.

Chọn **Only select repositories**, rồi tick đúng những repo sau:

```
idp-platform
idp-helloworld              idp-helloworld-config
idp-sample-nginx            idp-sample-nginx-config
idp-sample-pg               idp-sample-pg-config
idp-sample-boutique         idp-sample-boutique-config
idp-boutique                idp-boutique-config
```

Bấm **Install**.

## Bước 6 — Báo tôi

Nhắn cho tôi:

1. **App ID** (con số ở bước 3)
2. Xác nhận đã lưu private key vào `~/.idp-app-key.pem`

Tôi sẽ tự lấy Installation ID qua API, không cần bạn tìm.

---

## Sau đó tôi làm gì

| Việc | Chi tiết |
|---|---|
| Thêm secret vào `idp-platform` | `APP_ID`, `APP_PRIVATE_KEY` |
| Sửa workflow | Dùng `actions/create-github-app-token` mint token mỗi lần chạy, thay `CONFIG_REPO_TOKEN` và `APP_REPOS_TOKEN` |
| Sửa danh tính commit | `committer_email` đổi sang email chính thức của App, thay cho địa chỉ tạm hiện tại |
| Chuyển Fleet sang deploy key | **App không dùng được cho Fleet** — token App hết hạn sau 1 giờ mà Fleet poll 15 giây/lần và không tự làm mới được. Fleet cần deploy key chỉ-đọc, dài hạn |
| Kiểm chứng | Chạy lại vòng deploy staging + prod PR, xác nhận commit ghi đúng tên App |

---

## Việc riêng cho môi trường công ty (chưa làm ở sandbox)

Sau khi có App trên org công ty, vào branch protection của repo config:

- Nhánh `dev` (staging): **thêm App vào danh sách bypass** → staging ghi thẳng, không PR
- Nhánh `main` (production): **KHÔNG thêm App** → buộc qua PR, đúng yêu cầu của bạn

Đây là chỗ App hơn hẳn tài khoản người: cấp bypass cho một người là người đó có bypass ở
mọi nơi họ làm việc; cấp cho App thì chính xác tới từng nhánh.

---

## Những thứ App KHÔNG thay thế

| Việc | Vẫn phải dùng |
|---|---|
| Fleet đọc repo config | **Deploy key** chỉ-đọc, mỗi repo một key |
| Đẩy/kéo image | **Robot account của Harbor** (hiện đang dùng nhầm PAT cá nhân) |
| Truy cập cụm Kubernetes | kubeconfig / service account |
