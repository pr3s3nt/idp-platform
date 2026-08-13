# 3. VSO xác thực hỏng

Bao gồm cả trường hợp **Vault khởi động lại và mất sạch cấu hình** — trên harness dev mode
đây là tình huống thường gặp nhất.

## Triệu chứng

- `VaultStaticSecret` không đồng bộ; log của VSO đầy `Failed to get NewClientWithLogin`.
- Thông điệp: `400 invalid role name "idp-<app>-<env>"` hoặc `connection refused`.
- **App đang chạy vẫn hoàn toàn bình thường.** Đây là phần nguy hiểm nhất của tình huống
  này, xem mục "Vì sao không ai thấy" bên dưới.

## Xác nhận

```bash
kubectl -n vault-secrets-operator-system logs deploy/vault-secrets-operator-controller-manager \
  --tail=50 | grep -i error | tail -5
kubectl -n <app>-<env> get vaultauth app-vault -o jsonpath='{.status}' ; echo
# Vault còn mount nào không:
vault secrets list ; vault auth list
```

## Vì sao không ai thấy

Đo trên harness sau khi pod Vault restart (dev mode, mất toàn bộ mount + auth):

| Thứ | Trạng thái |
|---|---|
| Pod ứng dụng | **Running**, không restart |
| Secret đích trong cụm | **còn nguyên** — VSO không xoá khi mất kết nối |
| Database | vẫn phục vụ, dữ liệu đủ |
| `VaultStaticSecret.status.conditions[SecretSynced]` | `status: False` — nhưng `reason` vẫn là chuỗi `"Synced"` |
| `Healthy` / `Ready` | `False` lúc mất kết nối, quay lại `True` khi Vault trở lại **dù đồng bộ vẫn hỏng** |

Hai bài học:

1. **Cảnh báo phải đọc `status`, không đọc `reason`.** `reason: "Synced"` là tên của loại
   điều kiện, không phải kết quả.
2. **`Ready=True` không có nghĩa là đồng bộ đang chạy.** Ba condition có thể bất đồng, và
   cái đúng là `SecretSynced`.

## Xử lý

### Nếu Vault mất cấu hình (dev/harness)

```bash
./tools/dung-vault-harness.sh --context <context>       # dựng lại mount + auth + VSO CR
# rồi onboard lại từng app đang chạy:
python3 idpctl --env-config platform.env.yaml vault-onboard --app <app> --env <env> --apply
#   + phần policy/role do Vault Ops chạy (lệnh in ra bởi lệnh trên, không có --apply)
# rồi ghi lại bí mật — dev mode không giữ dữ liệu:
python3 idpctl --env-config platform.env.yaml secret-set ... --replace
```

Muốn giữ nguyên mật khẩu database đang chạy (để CNPG không phải đổi mật khẩu role), ghi
lại **đúng giá trị đang có trong Secret** thay vì sinh mới:

```bash
kubectl -n <app>-<env> get secret <cluster>-cred -o jsonpath='{.data.password}' | base64 -d \
  | python3 idpctl --env-config platform.env.yaml \
      secret-set --app <app> --env <env> --name database --key password --stdin --replace
```

### Nếu Vault vẫn còn cấu hình (công ty)

Kiểm theo thứ tự — mỗi bước loại trừ bước sau:

1. `VaultConnection` có đúng `address` không, và VSO có tới được không.
2. `VaultAuth` trong namespace app: `Ready=True`?
3. Role trong Vault có tồn tại và bound đúng ServiceAccount/namespace không:
   `vault read auth/<mount>/role/idp-<app>-<env>`.
4. `auth_audience` trong config có khớp audience của role không.

## Xác minh đã xong

```bash
python3 idpctl --env-config platform.env.yaml preflight --require-cluster --require-vault
kubectl -n <app>-<env> get vaultstaticsecret \
  -o jsonpath='{range .items[*]}{.metadata.name}{"="}{.status.conditions[0].status}{"\n"}{end}'
```

**Kiên nhẫn ở đây là một bước, không phải một lời khuyên.** Đo trên harness: sau khi Vault
trở lại, một `VaultStaticSecret` có từ trước sự cố mất **~2,5 phút** mới tự đồng bộ lại,
vì VSO backoff và `message` đứng yên trong suốt thời gian đó (cùng một giá trị `horizon`).
Một CR tạo **sau** khi Vault trở lại thì đồng bộ ngay. Đừng kết luận "hỏng vĩnh viễn" từ
một điều kiện đứng yên hai phút.

## Nếu vẫn hỏng

- Restart controller thì an toàn:
  `kubectl -n vault-secrets-operator-system rollout restart deploy/vault-secrets-operator-controller-manager`.
  (Đo được: một mình việc này **không** rút ngắn backoff — nó chỉ hữu ích khi controller
  thật sự kẹt.)
- **KHÔNG xoá `VaultStaticSecret`** để làm mới: Secret đích bị thu hồi theo
  `ownerReference`, và pod đang chạy mất nguồn biến môi trường ngay lập tức.
- Lệch phiên bản VSO so với `vault.operator_version`: `preflight --require-vault` báo.
