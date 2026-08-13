# 1. Thiếu bí mật trong Vault

## Triệu chứng

- Pod đứng ở `CreateContainerConfigError`.
- `verify` fail với `bí mật chưa được VSO đồng bộ sau …s`.
- Onboarding dừng ở trạng thái `WAITING_FOR_USER_SECRETS`.

## Xác nhận

```bash
kubectl -n <app>-<env> get vaultstaticsecret \
  -o custom-columns=NAME:.metadata.name,SYNCED:.status.conditions[0].status,MSG:.status.conditions[0].message
```

Dấu hiệu riêng của tình huống này trong `message`:

```text
err=empty response from Vault, path="kv/apps/<app>/<env>/<tên>"
```

`empty response` nghĩa là **đường dẫn không có gì**, khác hẳn `permission denied` (xem
[runbook 2](vault-tu-choi-quyen.md)). Đường dẫn in ra trong thông điệp là đường dẫn thật
platform đang đọc — dùng đúng nó, đừng gõ lại từ trí nhớ.

## Nguyên nhân thường gặp

| Nguyên nhân | Nhận ra bằng |
|---|---|
| Chưa ai ghi giá trị | `vault kv get` trả 404 |
| Ghi nhầm môi trường | Có ở `staging`, không có ở `prod` — bí mật **không** tự chảy từ staging sang prod, đó là chủ ý |
| Ghi nhầm tên khoá | Đường dẫn tồn tại nhưng thiếu đúng khoá app đang xin |
| Vault dev mode vừa restart | Toàn bộ mount biến mất — xem [runbook 3](vso-xac-thuc-hong.md) |

## Xử lý

Ghi giá trị bằng đúng lệnh của platform, để đường dẫn không thể lệch:

```bash
export VAULT_ADDR=... VAULT_TOKEN=...     # token có policy GHI, không phải token của VSO
python3 idpctl --env-config platform.env.yaml \
  secret-set --app <app> --env <env> --name <tên> --key <khoá> --stdin
```

- Lần đầu tạo một secret: thêm `--replace` (patch không tạo được bản đầu tiên).
- Mật khẩu do platform sở hữu (vd database): dùng `--generate`, giá trị không bao giờ
  được in ra.
- **Không** có cờ `--value`: tham số dòng lệnh nằm trong shell history và trong `ps` của
  mọi user khác trên máy.

## Xác minh đã xong

```bash
kubectl -n <app>-<env> get vaultstaticsecret <tên> \
  -o jsonpath='{.status.conditions[?(@.type=="SecretSynced")].status}'   # phải là True
kubectl -n <app>-<env> get pods                                          # pod rời khỏi CreateContainerConfigError
```

Nếu là onboarding dở dang, chạy lại đúng lệnh `onboard` cũ — các bước đã xong bị bỏ qua,
không tạo bản sao thứ hai.

## Nếu vẫn hỏng

- `message` đổi sang `permission denied` → [runbook 2](vault-tu-choi-quyen.md).
- Condition không đổi trong nhiều phút: VSO đang backoff. Đo được trên harness, quá trình
  tự lành mất tới **~2,5 phút** sau khi Vault trở lại. Đừng xoá CR để giục.
- `VaultStaticSecret` không tồn tại trên cụm → Fleet chưa apply bản render mới; kiểm
  GitRepo trước, chưa cần động tới Vault.
