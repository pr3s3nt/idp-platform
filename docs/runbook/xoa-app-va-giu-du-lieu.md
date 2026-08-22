# 8. Xoá app và giữ dữ liệu

> **Lệnh `offboard` đã bị GỠ** (commit `c5d28ac`). Không còn `idpctl offboard`, không còn
> `--execute/--confirm/--approved-by/--purge-secrets`. Việc dọn app giờ làm **thủ công** —
> runbook này giữ nguyên các rào an toàn mà `offboard` từng ép, bạn phải tự kiểm.

## Nguyên tắc: cái gì xoá, cái gì GIỮ

| | Số phận | Vì sao |
|---|---|---|
| Namespace `<app>-<env>` | **xoá** | chứa workload, Cluster, Secret của app |
| `GitRepo` của Fleet | **xoá TRƯỚC namespace** | ngược lại Fleet dựng lại thứ ta đang xoá, và Fleet thắng |
| Bí mật Vault `apps/<app>/<env>/*` | **xoá mềm** | phục hồi được bằng `vault kv undelete` |
| **Backup database** | **giữ** | xoá app không được xoá đường phục hồi; retention kho object quyết định |
| **Kho Git** (app + cấu hình) | **giữ** | giữ lịch sử triển khai — hãy *archive*, đừng xoá |
| **Policy/role Vault** | **giữ** | Vault Ops sở hữu; gỡ bằng quy trình của họ |
| PVC database cũ (nếu từng đổi class) | **giữ** | có thể đã orphan ngoài namespace; xem bước 4 |

## Bước 1 — xem trước sẽ xoá gì (thay `offboard` in kế hoạch)

Mọi tài nguyên platform tạo đều mang nhãn `idp.platform/application`. Liệt kê để biết phạm vi:

```bash
APP=<app>; ENV=<env>
kubectl get all,gitrepo,cluster.postgresql.cnpg.io,vaultstaticsecret -A \
  -l idp.platform/application="$APP"
kubectl -n fleet-local get gitrepo -o custom-columns=NAME:.metadata.name,REPO:.spec.repo | grep "$APP"
```

## Bước 2 — kiểm hai điều trước khi xoá

1. **Backup database còn trong hạn và đã thử phục hồi chưa?**

   ```bash
   kubectl -n "$APP-$ENV" get cluster.postgresql.cnpg.io <tên> \
     -o jsonpath='{.status.firstRecoverabilityPoint}{" .. "}{.status.lastSuccessfulBackup}{"\n"}'
   ```

   `firstRecoverabilityPoint` rỗng = **không có gì để phục hồi** — xoá lúc này là mất dữ liệu
   vĩnh viễn dù kho object đầy WAL. Xem [runbook 4](database-provisioning-backup-that-bai.md).

2. **RÀO CHẮN: không xoá nhầm của đội khác.** `offboard` từng tự từ chối khi namespace chứa
   tài nguyên mang nhãn `idp.platform/application` của app khác. Giờ **bạn phải tự kiểm** —
   tên namespace đúng quy ước `{app}-{env}` KHÔNG phải bằng chứng sở hữu:

   ```bash
   kubectl -n "$APP-$ENV" get all -L idp.platform/application \
     | grep -v "$APP" | grep -v '^NAME'      # phải RỖNG; có dòng lạ = dừng lại, tách ra trước
   ```

   `GitRepo` khớp theo `spec.repo`, không theo tên — đối chiếu `spec.repo` đúng `idp-<app>-config`.

## Bước 3 — xoá (đúng thứ tự: GitRepo trước, namespace sau)

```bash
# 3a. Xoá GitRepo TRƯỚC để Fleet ngừng đồng bộ (nếu không nó dựng lại ngay).
kubectl -n fleet-local delete gitrepo <tên-gitrepo-của-app>

# 3b. Xoá namespace (kéo theo workload, Cluster CNPG, Secret trong ns).
kubectl delete ns "$APP-$ENV"

# 3c. Xoá MỀM bí mật Vault (phục hồi được). KHÔNG destroy trừ khi tuân thủ bắt buộc.
#     Đường dẫn theo vault.path_template = apps/<app>/<env>/<name>.
kubectl -n vault exec -i vault-0 -- env VAULT_TOKEN=$VAULT_DEV_ROOT_TOKEN \
  VAULT_ADDR=http://127.0.0.1:8200 vault kv delete kv/apps/$APP/$ENV/<name>
```

> prod: đây là thao tác không hoàn tác. Trước khi chạy, xác nhận có người duyệt và ghi lại
> **ai duyệt** (offboard cũ ép `--approved-by`; giờ tự ghi vào PR/ticket dọn dẹp).

## Bước 4 — dọn phần còn lại, SAU một chu kỳ backup

Chỉ làm khi chắc chắn không quay lại:

```bash
kubectl -n "$APP-$ENV" get pvc                    # PVC mồ côi (nếu từng đổi class database)
gh repo archive <org>/<app>                       # ARCHIVE kho Git, đừng xoá
gh repo archive <org>/idp-<app>-config
# xoá HẲN bí mật nếu chính sách tuân thủ đòi (không hoàn tác):
kubectl -n vault exec -i vault-0 -- env VAULT_TOKEN=$VAULT_DEV_ROOT_TOKEN \
  VAULT_ADDR=http://127.0.0.1:8200 vault kv metadata delete kv/apps/$APP/$ENV/<name>
# gỡ policy/role Vault — việc của Vault Ops:
vault delete auth/kubernetes/role/idp-$APP-$ENV
vault policy delete idp-$APP-$ENV-read
vault policy delete idp-$APP-$ENV-write
```

## Xác minh đã xong

```bash
kubectl get ns | grep "$APP"                       # không còn (hoặc Terminating)
kubectl -n fleet-local get gitrepo | grep "$APP"   # không còn
kubectl get all -A -l idp.platform/application="$APP"   # rỗng
```

Và kiểm thứ **phải còn** thì vẫn còn: thư mục backup của app trong kho object, kho Git đã archive.

## Phục hồi khi xoá nhầm

Trong hạn xoá mềm của bí mật:

```bash
kubectl -n vault exec -i vault-0 -- env VAULT_TOKEN=$VAULT_DEV_ROOT_TOKEN \
  VAULT_ADDR=http://127.0.0.1:8200 vault kv undelete -versions=<n> kv/apps/$APP/$ENV/<name>
```

Namespace/workload/manifest dựng lại từ kho cấu hình (lý do không xoá kho). Dữ liệu database
dựng lại từ kho object theo [runbook 4](database-provisioning-backup-that-bai.md) mục 4C. Đây
chính là lý do ba thứ đó cố ý nằm ngoài danh sách xoá.
