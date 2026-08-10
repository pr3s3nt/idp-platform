# ADR 0008 — CloudNativePG cho `class: application`, credential đi qua Vault

Trạng thái: Accepted — 2026-08-10

## Bối cảnh

ADR 0005 đã chốt HỢP ĐỒNG: app khai `type: postgres, class: application` và nhận cùng một
bộ output ở mọi môi trường, khác biệt nằm hết trong `database_profiles`. Nó không nói ai
thực thi hợp đồng đó, và không nói mật khẩu từ đâu ra.

Provisioner `postgres` đang có tạo một StatefulSet một bản sao, PVC 1Gi, không HA, không
backup — và **sinh mật khẩu trong lúc render**, nghĩa là mật khẩu NẰM TRONG Score state.
State đó là một Kubernetes Secret dùng chung cho cả platform, nên mật khẩu database của
mọi app nằm chung một chỗ mà không ai xoay vòng.

## Quyết định

**1. Provider là một operator, mặc định CloudNativePG, và tên nó nằm trong config.**
`database.provider` + `database.operator_version`. Công ty có dịch vụ DBA riêng thì thay
`provisioners/postgres-application.provisioners.yaml` bằng bản của họ; hợp đồng với app
không đổi nên **app không phải sửa gì**.

**2. Mật khẩu KHÔNG do platform sinh lúc render.** Luồng đúng một chiều:

```text
onboarding sinh credential  →  ghi thẳng Vault (apps/<app>/<env>/database)
   →  VSO đồng bộ ra Secret kiểu kubernetes.io/basic-auth
      →  CNPG dùng CHÍNH Secret đó ở bootstrap.initdb.secret để tạo user
      →  app đọc CHÍNH Secret đó qua encodeSecretRef
```

Một credential, một nguồn sự thật, không có bản sao nào ở nơi khác. Score state chỉ giữ tên
cluster, tên database và username — không có gì bí mật. Đó là khác biệt kiểm chứng được
giữa class mới và class cũ, và là một test.

`secret-set --generate` sinh giá trị rồi ghi thẳng Vault, **không in ra, không trả về**:
mật khẩu database là thứ không ai cần nhìn thấy, kể cả người tạo nó.

**3. Class cũ bị chặn ở `prod`, nhưng chỉ khi platform đã bật `features.postgres_application`.**

Chặn ngay lập tức là phá lời hứa brownfield: đang có app chạy `type: postgres` không class
và render prod hằng ngày. Nên guard gắn với cờ tính năng — bật cờ là tuyên bố "cụm này đã
có provider thật", và từ lúc đó dùng nhầm bản demo ở prod là lỗi cứng.

**4. Render `prod` bị từ chối khi chưa cấu hình kho object cho backup.** Fail-closed.
Một database production không phục hồi được thì không phải "đang chạy", chỉ là "chưa hỏng".
`staging` không cần — mất staging là dựng lại.

**5. `verify` chờ condition `Ready` của Cluster, không đếm pod.** Cluster ba bản sao có pod
chạy từ sớm trong khi replica chưa join; app kết nối lúc đó gặp "the database system is
starting up", trông hệt một lỗi cấu hình.

## Hệ quả

Tích cực:

- Mật khẩu database ra khỏi Score state hoàn toàn, và xoay vòng đi cùng cơ chế đã có ở
  Phase 3 thay vì là một cơ chế thứ hai.
- Staging và prod dùng cùng engine, cùng luồng xác thực, cùng output — nên staging còn là
  bằng chứng về prod.
- Đổi provider là đổi một file catalog, không phải đổi app.

Cái giá:

- Thêm một operator phải vận hành và nâng cấp trên cụm.
- Onboarding một app có database phải ghi credential vào Vault TRƯỚC khi render, nếu không
  cluster không bootstrap được. Thông báo lỗi của `verify` nói thẳng điều đó.
- Không dựng được kho object trên harness WSL2 ⇒ backup/restore chỉ kiểm chứng được ở công
  ty. Platform bù lại bằng cách **chặn** prod khi thiếu cấu hình, thay vì im lặng cho qua.

## Đã cân nhắc và loại

**Nâng cấp provisioner cũ tại chỗ thay vì thêm class.** Mọi app đang dùng `type: postgres`
sẽ đổi hành vi trong im lặng — và với database, "đổi hành vi" nghĩa là dữ liệu cũ nằm ở
StatefulSet cũ còn app trỏ sang cluster mới rỗng.

**Để CNPG tự sinh mật khẩu** (bỏ `bootstrap.initdb.secret`). Đơn giản hơn, nhưng khi đó
credential sinh ra trong cụm và Vault không biết gì về nó — mất đúng thứ ADR 0002 dựng lên:
một chỗ duy nhất để xoay vòng và kiểm toán.

**Bật `enableSuperuserAccess`.** Tiện lúc chẩn đoán. Nhưng nó tạo thêm một credential nữa
mà không ai xoay vòng, để đổi lấy một tiện lợi mà `kubectl exec` đã cho.
