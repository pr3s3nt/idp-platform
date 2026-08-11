# 8. Xoá app và giữ dữ liệu

Workflow: `offboard` (mục 13.4). Mặc định nó **không xoá gì** — nó in kế hoạch.

## Bước 1 — xem trước, luôn luôn

```bash
python3 orchestrate.py --env-config platform.env.yaml offboard --app <app> --env <env>
```

In ra hai danh sách: **SẼ XOÁ** và **SẼ GIỮ**, mỗi mục kèm lý do. Đọc cả hai. Danh sách
"sẽ giữ" quan trọng ngang danh sách kia — nó cho biết cái gì còn lại để dọn sau, và cái gì
cố ý không bao giờ bị đụng tới.

## Bước 2 — kiểm hai điều trước khi cho phép

1. **Backup của database còn trong hạn không, và đã từng phục hồi thử chưa?**

   ```bash
   kubectl -n <app>-<env> get cluster.postgresql.cnpg.io <tên> \
     -o jsonpath='{.status.firstRecoverabilityPoint}{" .. "}{.status.lastSuccessfulBackup}{"\n"}'
   ```

   `firstRecoverabilityPoint` rỗng nghĩa là **không có gì để phục hồi** — xoá app lúc này
   là mất dữ liệu vĩnh viễn dù kho object đầy WAL. Xem [runbook 4](database-provisioning-backup-that-bai.md).

2. **Có ai còn dùng database này không?** Một app khác trỏ vào cùng Service là chuyện có
   thật trong brownfield.

## Bước 3 — xoá

```bash
python3 orchestrate.py --env-config platform.env.yaml offboard \
  --app <app> --env <env> --execute --confirm <app>
# prod:
  ... --execute --confirm <app> --approved-by <tên người duyệt>
```

- `--confirm` phải là **đúng tên app**. Gõ lại tên là rào chắn cuối trước một thao tác
  không hoàn tác được.
- `prod` bắt buộc `--approved-by`; tên đó vào bản ghi state để sau còn tra được.
- `--purge-secrets` xoá **hẳn** bí mật trong Vault. Mặc định là xoá mềm — đừng dùng cờ này
  trừ khi có yêu cầu tuân thủ bắt buộc.

## Cái gì bị xoá, cái gì không

| | Số phận | Vì sao |
|---|---|---|
| Namespace `<app>-<env>` | **xoá** | chứa workload, Cluster, Secret của app |
| `GitRepo` của Fleet | **xoá trước namespace** | ngược lại thì Fleet dựng lại thứ ta đang xoá, và Fleet thắng |
| Bí mật Vault `apps/<app>/<env>/*` | **xoá mềm** | phục hồi được bằng `vault kv undelete` |
| **Backup database** | **giữ** | xoá app không được xoá đường phục hồi; retention của kho object quyết định |
| **Kho Git** (app + cấu hình) | **giữ** | giữ lịch sử triển khai — hãy *archive*, đừng xoá |
| **Policy/role Vault** | **giữ** | Vault Ops sở hữu; gỡ bằng quy trình của họ |
| PVC của database cũ (nếu từng đổi class) | **giữ** | không nằm trong namespace bị xoá nếu đã orphan; xem bên dưới |

## Rào chắn: không xoá nhầm của đội khác

Trước khi xoá namespace, `offboard` quét bên trong và **từ chối** nếu thấy bất kỳ tài
nguyên nào mang nhãn `idp.platform/application` của một app khác:

```text
namespace <app>-<env> có tài nguyên của application khác (doi-thanh-toan).
Từ chối xoá: xoá app này sẽ kéo theo của đội khác.
```

Tên namespace đúng quy ước **không phải** bằng chứng sở hữu — `{app}-{env}` là quy ước.
Nếu gặp thông báo này, đừng ép: tách tài nguyên của đội kia ra trước.

Tương tự, `GitRepo` được khớp theo `spec.repo`, không theo tên.

## Bước 4 — dọn phần còn lại, sau một chu kỳ backup

Chỉ làm khi đã chắc chắn không cần quay lại:

```bash
# PVC mồ côi (nếu app từng đổi class database)
kubectl -n <app>-<env> get pvc
# archive kho Git thay vì xoá
gh repo archive <org>/<app>
gh repo archive <org>/idp-<app>-config
# xoá hẳn bí mật nếu chính sách tuân thủ đòi
... offboard --app <app> --env <env> --execute --confirm <app> --purge-secrets
# gỡ policy/role Vault — việc của Vault Ops
vault delete auth/kubernetes/role/idp-<app>-<env>
vault policy delete idp-<app>-<env>-read
vault policy delete idp-<app>-<env>-write
```

## Xác minh đã xong

```bash
kubectl get ns | grep <app>                       # không còn (hoặc Terminating)
kubectl -n fleet-local get gitrepo | grep <app>   # không còn
kubectl get all -A -l idp.platform/application=<app>
python3 orchestrate.py --env-config platform.env.yaml onboard-status --app <app>   # DELETED
```

Và kiểm rằng thứ **phải còn** thì vẫn còn:

```bash
# thư mục backup của app vẫn nằm trong kho object
```

## Phục hồi khi xoá nhầm

Trong vòng thời gian bí mật còn xoá mềm:

```bash
vault kv undelete -mount=kv -versions=<n> apps/<app>/<env>/<tên>
```

Namespace, workload và manifest dựng lại được từ kho cấu hình (đó là lý do không xoá kho).
Dữ liệu database dựng lại từ kho object theo [runbook 4](database-provisioning-backup-that-bai.md)
mục 4C. Đây chính là lý do ba thứ đó cố ý không nằm trong danh sách xoá.
