# Runbook vận hành

Tám tình huống ở mục 17 của kế hoạch. Mỗi runbook có cùng bố cục:

**Triệu chứng** → **Xác nhận** (lệnh cụ thể) → **Nguyên nhân thường gặp** → **Xử lý** →
**Xác minh đã xong** → **Nếu vẫn hỏng**.

Các runbook này được viết từ những gì **đo được trên harness**, không phải từ tài liệu
thượng nguồn. Chỗ nào là hành vi đã quan sát thấy thì có ghi rõ.

| # | Tình huống | File |
|---|---|---|
| 1 | Thiếu bí mật trong Vault | [thieu-bi-mat-vault.md](thieu-bi-mat-vault.md) |
| 2 | Vault từ chối quyền | [vault-tu-choi-quyen.md](vault-tu-choi-quyen.md) |
| 3 | VSO xác thực hỏng | [vso-xac-thuc-hong.md](vso-xac-thuc-hong.md) |
| 4 | Database dựng/backup thất bại | [database-provisioning-backup-that-bai.md](database-provisioning-backup-that-bai.md) |
| 5 | Fleet drift / không reconcile | [fleet-drift.md](fleet-drift.md) |
| 6 | Onboarding dở dang và retry | [onboarding-do-dang.md](onboarding-do-dang.md) |
| 7 | Rollback nâng cấp stack | [rollback-nang-cap-stack.md](rollback-nang-cap-stack.md) |
| 8 | Xoá app và giữ dữ liệu | [xoa-app-va-giu-du-lieu.md](xoa-app-va-giu-du-lieu.md) |

Ngưỡng cảnh báo và cách gắn vào hệ giám sát: [`../canh-bao.md`](../canh-bao.md).

## Nguyên tắc chung khi cầm một sự cố của nền tảng này

1. **Đọc condition, đừng đọc `reason`.** Đo được trên harness: một `VaultStaticSecret`
   đang hỏng đồng bộ vẫn có `reason: "Synced"` trong khi `status: "False"`. `reason` là
   tên của loại điều kiện, không phải kết quả.
2. **App đang chạy hiếm khi hỏng cùng lúc với nền tảng.** Secret đã đồng bộ nằm lại trong
   cụm, biến môi trường đã nạp nằm lại trong pod. Nên "Vault sập" thường KHÔNG có triệu
   chứng ở phía người dùng — nó chỉ nổ ra ở lần restart pod kế tiếp, vì một lý do không
   liên quan. Đừng chờ app đỏ mới coi là sự cố.
3. **Đừng xoá CR để "làm mới".** Xoá một `VaultStaticSecret` sẽ thu hồi luôn Secret đích
   theo `ownerReference`, và pod đang chạy mất nguồn biến môi trường. Restart controller
   thì an toàn, xoá CR thì không.
