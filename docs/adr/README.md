# Architecture Decision Records

Mỗi file ở đây ghi lại MỘT quyết định đã chốt, kèm lý do và cái giá phải trả.

Vì sao cần: những quyết định dưới đây đều có ít nhất một lựa chọn thay thế trông hợp lý
hơn ở cái nhìn đầu tiên. Không ghi lại lý do thì sáu tháng nữa sẽ có người "dọn dẹp" đúng
thứ đang giữ cho hệ thống không hỏng — và họ sẽ có lý, vì lý do thật đã biến mất.

| ADR | Quyết định | Trạng thái |
|---|---|---|
| [0001](0001-application-values-v1.md) | `ApplicationValues v1` là contract cấu hình theo môi trường | Accepted |
| [0002](0002-vault-only-secret-store.md) | Vault là kho bí mật duy nhất; đồng bộ bằng VSO | Accepted |
| [0003](0003-hai-moi-truong-staging-prod.md) | Đúng hai tên môi trường: `staging` và `prod` | Accepted |
| [0004](0004-placeholder-matrix.md) | Placeholder chỉ hợp lệ ở 4 vị trí, theo allowlist | Accepted |
| [0005](0005-database-profile.md) | Staging/prod cùng contract database, khác profile | Accepted |
| [0006](0006-ghim-phien-ban-toolchain.md) | Ghim phiên bản `score-k8s`/`score-compose`/VSO | Accepted |
| [0007](0007-topo-vso-va-danh-tinh-verify.md) | Mỗi app một danh tính Vault; verify không được đọc Secret | Accepted |
| [0008](0008-provider-database-va-credential.md) | CloudNativePG cho `class: application`; credential database đi qua Vault | Accepted |
| [0009](0009-stack-catalog-va-phat-trien-local.md) | Stack là phép cộng component; `make dev` sinh từ chính Score | Accepted |

Định dạng: Bối cảnh → Quyết định → Hệ quả → Đã cân nhắc và loại.
