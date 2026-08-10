# ADR 0005 — Staging và prod cùng contract database, khác profile

Trạng thái: Accepted — 2026-08-10

## Bối cảnh

Provisioner `postgres` hiện tại dựng StatefulSet 1 replica, PVC 1Gi, không HA, không
backup, và lưu mật khẩu trong Score state. Nó vừa đủ cho demo và cho phát triển local.

Nó không dùng được cho production. Nhưng cám dỗ lớn nhất không phải là dùng nó ở prod — mà
là dùng MỘT provisioner khác ở prod. Khi đó staging không còn kiểm chứng được prod: khác
phiên bản engine, khác cách xác thực, khác cơ chế migration. Staging xanh không nói lên
điều gì về prod.

## Quyết định

App khai đúng một thứ, giống hệt nhau ở mọi môi trường:

```yaml
resources:
  db:
    type: postgres
    class: application
```

Và nhận đúng một bộ output ở mọi môi trường: `host`, `port`, `database`, `username`,
`password`.

Khác biệt nằm HẾT trong `database_profiles` của `platform.env.yaml`, app không thấy:

| Bắt buộc GIỐNG nhau | Được phép KHÁC nhau |
|---|---|
| PostgreSQL major version | Số instance |
| Extension và schema convention | CPU / RAM / dung lượng |
| Luồng xác thực và Vault | High availability |
| TLS và network policy | Retention backup, PITR |
| Cơ chế migration | Monitoring và SLO |
| Tập output của resource | |

`class: application` phải do database provider/operator production-grade hiện thực, và:

- Lấy profile theo environment từ platform config.
- Credential do onboarding sinh và ghi THẲNG vào Vault; CI không nhìn thấy.
- Output `password` là encoded secret reference, không phải plaintext.
- Có condition Ready để `verify` chờ.
- Có contract backup/restore rõ ràng.

Provisioner StatefulSet cũ chuyển sang `class: development` và **fail khi render `prod`**.

## Hệ quả

Tích cực:

- Staging thật sự kiểm chứng được prod, vì hai bên chỉ khác kích thước.
- Nâng cấp engine version là một thay đổi ở platform config, đi qua staging trước.
- Mật khẩu không còn nằm trong Score state.

Cái giá:

- Staging tốn tài nguyên hơn hẳn StatefulSet 1Gi hiện tại. Đây là cái giá của việc staging
  có ý nghĩa; profile staging vẫn nhỏ hơn prod nhiều (1 instance, 10Gi, không HA).
- Phải chọn và vận hành một database operator. Đó là Phase 4, không phải việc của Phase 0.
- Khi mang vào công ty, profile prod phải do DBA chốt lại — số instance, dung lượng và
  retention trong file config chỉ là ĐỀ XUẤT.

## Đã cân nhắc và loại

**Dùng provisioner StatefulSet hiện tại cho cả hai, chỉ tăng resource ở prod.** Không có
HA, không có backup, không có restore đã kiểm chứng, mật khẩu nằm trong state. Tăng
resource không sửa được điều nào trong số đó.

**StatefulSet ở staging, managed service ở prod.** Rẻ, và phá đúng thứ staging tồn tại để
làm. Mọi lỗi liên quan tới khác biệt engine/auth/migration sẽ được phát hiện lần đầu ở
production.

**Cho app tự khai instance/storage trong `score.yaml`.** Đẩy quyết định về dung lượng và
HA cho người ít thông tin nhất, và biến hạn mức tài nguyên thành thứ app tự cấp cho mình.

**Xoá provisioner cũ.** Nó vẫn hữu ích cho demo và cho `score-compose` local. Hạ class và
chặn ở prod đủ để nó không bị dùng nhầm.
