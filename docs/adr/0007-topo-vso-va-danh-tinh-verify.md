# ADR 0007 — Hình trạng VSO: mỗi app một danh tính, verify là danh tính riêng

Trạng thái: Accepted — 2026-08-10

## Bối cảnh

ADR 0002 đã chốt Vault là kho bí mật duy nhất và VSO là thứ đọc nó. Còn lại một câu hỏi
chưa trả lời: **VSO đăng nhập Vault với tư cách ai, và ai được đọc kết quả.**

Cách nhanh nhất là dựng một `VaultAuth` dùng chung cho cả cụm, bound tới ServiceAccount
`default` của mỗi namespace, rồi cấp cho nó policy đọc `kv/apps/*`. Nó chạy được ngay và
sai theo cách không có gì báo: mọi app dùng chung một danh tính thì policy theo tiền tố
không còn ranh giới nào để thực thi, và một app đọc secret của app khác là một dòng YAML.

Câu hỏi thứ hai ít rõ hơn: sau khi deploy, cái gì trả lời "app đã chạy chưa?". Nếu nó
dùng cùng kubeconfig với bước deploy thì nó đọc được mọi Secret trong namespace — bao gồm
đúng những giá trị mà toàn bộ thiết kế này tồn tại để CI không nhìn thấy.

## Quyết định

**1. Mỗi (app, environment) một ServiceAccount, một Vault role, một policy theo tiền tố.**

```text
ServiceAccount idp-<app>          trong namespace <app>-<env>
Vault role     idp-<app>-<env>    bound (namespace, serviceAccount) — đúng một cặp
policy         idp-<app>-<env>-read   trên kv/data/apps/<app>/<env>/*
```

ServiceAccount cố tình KHÔNG phải `default`: role bind theo cặp (namespace,
serviceAccount), nên bind vào `default` là cấp cho mọi pod trong namespace — kể cả pod
không thuộc app — quyền lấy secret của app.

Policy đọc và policy ghi là **hai policy khác tên**. VSO chỉ nhận policy đọc. Quyền ghi
thuộc về con người và công cụ onboarding, và ở `prod` thì theo chính sách phê duyệt của
công ty.

**2. `vault.path_template` bắt buộc chứa cả `{application}` lẫn `{environment}` và kết
thúc bằng `{name}`.** Đây là ràng buộc được kiểm tra, không phải quy ước. Policy sinh ra
là policy theo TIỀN TỐ; thiếu `{application}` thì mọi app chung một tiền tố, thiếu
`{environment}` thì staging đọc được credential của prod. `{name}` phải đứng cuối vì đó
là đoạn duy nhất do app khai — nó là chỗ duy nhất dấu `*` được phép nằm.

**3. `vaultConnectionRef` trong `VaultAuthGlobal` luôn ghi kèm namespace.**

Đo trên VSO 1.5.0: một ref không kèm namespace được phân giải theo namespace của resource
ĐANG THAM CHIẾU — tức namespace của app, không phải namespace của VaultAuthGlobal. Hệ quả
là mọi `VaultAuth` fail với `VaultConnection "default" not found`, và chỗ báo lỗi là log
của controller chứ không phải lúc `kubectl apply`. Vì vậy platform sinh dạng đầy đủ
`<operator-namespace>/<connection-name>`.

**4. Verify dùng một danh tính riêng, không có quyền đọc Secret.**

`idpctl verify-rbac` sinh ServiceAccount + Role + RoleBinding chỉ có
`get/list/watch` trên: VSO CR, Deployment/StatefulSet/ReplicaSet, Pod/pod log/Service/
Event, PVC, Job, HTTPRoute. Không có `secrets`.

Không có phương án nửa vời: Kubernetes RBAC không có verb nào cho xem TÊN và KEY của
Secret mà che GIÁ TRỊ. `get secrets` chính là `đọc mọi secret trong namespace`. Mà verify
không cần: nó chỉ cần biết VSO báo `Ready`.

**5. CI không bao giờ giữ Vault token.** `vault-onboard` **in ra** lệnh cho người quản trị
Vault chạy bằng token của họ, thay vì tự chạy. Một Vault token trong CI đọc được đúng
những gì policy cho phép — có nó thì việc "platform chỉ sinh tham chiếu" mất hết ý nghĩa.

## Hệ quả

Tích cực:

- Ranh giới giữa hai app là thứ Vault thực thi, không phải thứ code nhớ kiểm tra. Kiểm
  chứng được bằng một lệnh: trỏ `VaultStaticSecret` của app A vào tiền tố của app B thì
  nhận 403.
- Sự cố lộ kubeconfig verify không lộ secret nào.
- Tên role/policy/ServiceAccount đều là template trong `platform.env.yaml`, nên công ty
  giữ được quy ước đặt tên sẵn có mà không phải sửa code.

Cái giá:

- Onboarding một app là hai nửa của hai chủ sở hữu (Kubernetes: platform; policy/role:
  Vault Ops). Chậm hơn một bước tự động hoàn toàn. Đó là chủ ý: bên cấp quyền đọc secret
  phải là bên quản trị Vault.
- Mỗi app thêm ba object trong Vault (2 policy + 1 role). Với hàng trăm app thì cần công
  cụ, và công cụ đó chính là `vault-onboard`.
- Verify chạy bằng kubeconfig hẹp nghĩa là phải phát hành thêm một kubeconfig. Ai chạy
  verify bằng kubeconfig rộng vẫn chạy được — platform sinh ra cái hẹp để việc dùng cái
  rộng là một lựa chọn có ý thức, không phải mặc định.

## Đã cân nhắc và loại

**Một `VaultAuth` dùng chung cấp cụm.** Ít object hơn hẳn. Nhưng danh tính dùng chung thì
policy theo tiền tố không thực thi được gì — nó chỉ còn là tài liệu.

**`VaultStaticSecret` trỏ thẳng `VaultAuthGlobal`.** CRD cho phép. Nhưng nó bỏ qua danh
tính theo namespace và xác thực bằng bất cứ thứ gì global đang khai — đúng lỗ hổng ở trên,
qua một cửa khác. Vì vậy `VaultStaticSecret` chỉ được trỏ `VaultAuth` trong namespace của
chính nó.

**Cho CI một Vault token chỉ-đọc để tự verify secret đã tồn tại.** Tiện khi debug. Nhưng
"chỉ đọc" ở đây nghĩa là đọc được giá trị bí mật, tức là đưa CI vào đúng vị trí mà ADR
0002 loại bỏ. Chẩn đoán phải dựa vào condition/reason của VSO CR, thứ không chứa giá trị.

**Dùng `ClusterRole` cho verify.** Một binding cho mọi namespace, tiện quản lý. Nhưng để
trả lời "deploy CỦA TÔI đã lên chưa" thì không cần đọc object của app khác.
