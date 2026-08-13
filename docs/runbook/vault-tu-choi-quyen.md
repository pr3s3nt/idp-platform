# 2. Vault từ chối quyền

## Triệu chứng

`VaultStaticSecret` không đồng bộ, `message` chứa:

```text
Code: 403. Errors:
* permission denied
```

## Xác nhận

```bash
kubectl -n <app>-<env> get vaultstaticsecret -o json | \
  python3 -c "import json,sys
for i in json.load(sys.stdin)['items']:
    c=i['status']['conditions'][0]
    print(i['metadata']['name'], c['status'], (c.get('message') or '')[:120])"
```

Phân biệt ba lỗi rất giống nhau:

| Thông điệp | Nghĩa | Đi tiếp |
|---|---|---|
| `empty response from Vault` | đường dẫn rỗng | [runbook 1](thieu-bi-mat-vault.md) |
| `403 permission denied` | policy không cho đọc tiền tố đó | tài liệu này |
| `400 invalid role name` | role không tồn tại trong Vault | [runbook 3](vso-xac-thuc-hong.md) |

## Nguyên nhân thường gặp

1. **App đọc sai tiền tố.** Policy chỉ cấp `apps/<app>/<env>/*`. Một app cố đọc tiền tố
   của app khác nhận đúng 403 này — đó là thiết kế, không phải lỗi. Đã đo ở Phase 2.
2. **Chưa onboard app vào Vault** cho môi trường đang chạy. Onboard `staging` không tự
   cấp `prod`.
3. **Policy bị ghi đè** bởi một lần chạy khác với nội dung cũ.
4. **Sai `auth_role_template`/`policy_template`** so với quy ước Vault của công ty.

## Xử lý

In ra chính xác policy platform mong đợi, rồi so với cái đang có:

```bash
python3 idpctl --env-config platform.env.yaml \
  vault-onboard --app <app> --env <env> --print-policy
vault policy read idp-<app>-<env>-read
```

Cấp lại phần Kubernetes và phần Vault:

```bash
python3 idpctl --env-config platform.env.yaml \
  vault-onboard --app <app> --env <env> --apply        # ServiceAccount + VaultAuth
python3 idpctl --env-config platform.env.yaml \
  vault-onboard --app <app> --env <env>                # in phần Vault Ops phải chạy
```

Phần Vault (policy + role) do **người quản trị Vault** chạy bằng token của họ. VSO không
bao giờ được cấp policy GHI — nó chỉ đọc.

## Xác minh đã xong

```bash
kubectl -n <app>-<env> get vaultauth app-vault \
  -o jsonpath='{.status.conditions[?(@.type=="Ready")].status}'      # True
kubectl -n <app>-<env> get vaultstaticsecret \
  -o jsonpath='{.items[*].status.conditions[0].status}'              # toàn True
```

Kiểm luôn rào chắn còn nguyên — một app **không** được đọc tiền tố của app khác:

```bash
python3 idpctl --env-config platform.env.yaml \
  verify-rbac --app <app> --env <env>
```

## Nếu vẫn hỏng

- 403 chỉ xảy ra với **một** secret trong nhiều: sai tên secret logic, không phải sai
  policy — policy cấp theo tiền tố nên nó cấp cả cụm hoặc không cấp gì.
- Vault Enterprise: kiểm `vault.namespace` trong `platform.env.yaml`. Sai namespace cho ra
  403 giống hệt sai policy.
