# 4. Database dựng hoặc backup thất bại

## 4A. Cluster không lên `Ready`

### Xác nhận

```bash
kubectl -n <app>-<env> get cluster.postgresql.cnpg.io -o wide
kubectl -n <app>-<env> get cluster.postgresql.cnpg.io <tên> -o jsonpath='{.status.conditions}' ; echo
kubectl -n <app>-<env> get pods,pvc
```

### Nguyên nhân thường gặp

| Nguyên nhân | Nhận ra bằng |
|---|---|
| Credential chưa được VSO đồng bộ | `initdb` không chạy; `VaultStaticSecret` `False` → [runbook 1](thieu-bi-mat-vault.md) |
| Secret sai kiểu | CNPG bỏ qua **trong im lặng** và khởi tạo với user mặc định. Phải là `kubernetes.io/basic-auth` |
| PVC `Pending` | `storage_class` sai tên — PVC treo vĩnh viễn, không có lỗi rõ ràng |
| Ảnh postgres không kéo được | `ImagePullBackOff` trên pod `-1` |

### Xử lý

Sửa nguyên nhân rồi để CNPG tự dựng lại. Đừng xoá `Cluster` để "thử lại" khi nó đã từng
`Ready` — xoá `Cluster` là xoá cả PVC dữ liệu.

---

## 4B. Backup hỏng — trường hợp nguy hiểm nhất của cả nền tảng

### Vì sao nguy hiểm

`Cluster` báo `Ready`. Condition `ContinuousArchiving` báo `True` với thông điệp
*"Continuous archiving is working"*. WAL nằm thật trong kho object. **Và không phục hồi
được gì.**

Đo trên harness: một `Cluster` ở đúng trạng thái trên, khi dựng lại bằng
`bootstrap.recovery` từ chính kho đó, chết ngay với:

```text
error: while restoring cluster: no target backup found
```

Nguyên nhân: `barmanObjectStore` **chỉ bật WAL archiving**. Nó không chụp base backup nào.
WAL không có base backup thì phục hồi được đúng không gì cả.

### Kiểm duy nhất đáng tin

```bash
kubectl -n <app>-<env> get cluster.postgresql.cnpg.io <tên> \
  -o jsonpath='{.status.firstRecoverabilityPoint}{"\n"}{.status.lastSuccessfulBackup}{"\n"}'
```

**`firstRecoverabilityPoint` rỗng = KHÔNG phục hồi được**, bất kể `Ready` và
`ContinuousArchiving` nói gì. Đây là trường mà cảnh báo phải theo dõi, và là trường mà
`verify` của platform chờ.

### Xác nhận thêm

```bash
kubectl -n <app>-<env> get scheduledbackup.postgresql.cnpg.io,backup.postgresql.cnpg.io
```

Không có `ScheduledBackup` nào → cụm được render bằng catalog cũ. Render lại và apply.

### Nguyên nhân thường gặp

| Nguyên nhân | Nhận ra bằng |
|---|---|
| Không có `ScheduledBackup` | không có object nào; `firstRecoverabilityPoint` rỗng |
| Thiếu `endpointURL` với kho không phải AWS | barman gọi `s3.amazonaws.com`; `ContinuousArchiving` chuyển `False` sau một lúc |
| Secret credential kho object sai/thiếu | Backup `phase: failed`, log pod backup có 403 |
| Secret credential **khác namespace** với Cluster | CNPG không đọc chéo namespace |
| Cron 5 trường thay vì 6 | backup chạy **mỗi giờ** thay vì mỗi ngày — không lỗi, chỉ tốn tiền |

### Xử lý

```bash
# ép một base backup ngay, không chờ lịch:
kubectl -n <app>-<env> create -f - <<'YAML'
apiVersion: postgresql.cnpg.io/v1
kind: Backup
metadata: {generateName: khan-cap-}
spec:
  cluster: {name: <tên-cluster>}
YAML
kubectl -n <app>-<env> get backup.postgresql.cnpg.io -w
```

### Xác minh đã xong

`firstRecoverabilityPoint` có giá trị, và `Backup` gần nhất ở `phase: completed`.

---

## 4C. Diễn tập phục hồi — việc phải làm trước khi cho app đầu tiên lên prod

Backup chưa từng được phục hồi thì không phải backup. Dựng một `Cluster` **mới** (đừng
đụng cái đang chạy) từ kho object:

```bash
kubectl -n <app>-<env> apply -f - <<'YAML'
apiVersion: postgresql.cnpg.io/v1
kind: Cluster
metadata: {name: <tên>-dientap}
spec:
  instances: 1
  imageName: <cùng ảnh với cluster gốc>
  enableSuperuserAccess: false
  storage: {size: <đủ chứa>, storageClass: <lớp lưu trữ>}
  bootstrap:
    recovery: {source: nguon-backup}
  externalClusters:
    - name: nguon-backup
      barmanObjectStore:
        serverName: <TÊN CLUSTER GỐC>
        destinationPath: <database.backup.object_store_url>
        endpointURL: <database.backup.endpoint_url>
        s3Credentials:
          accessKeyId: {name: <credentials_secret>, key: ACCESS_KEY_ID}
          secretAccessKey: {name: <credentials_secret>, key: ACCESS_SECRET_KEY}
YAML
```

Đối chiếu bằng phép đo, không bằng cảm giác:

```bash
kubectl -n <app>-<env> exec <tên>-dientap-1 -c postgres -- \
  psql -U postgres -d <database> -tAc \
  "SELECT count(*)||' | '||md5(string_agg(<cột>,',' ORDER BY id)) FROM <bảng>;"
```

Chuỗi này phải khớp cluster gốc. Đo trên harness, kể cả với dữ liệu ghi **sau** lần base
backup: khớp — tức WAL replay cũng chạy. Xoá cluster diễn tập sau khi đo xong.

## Nếu vẫn hỏng

- `no target backup found` sau khi đã có `ScheduledBackup`: kiểm `serverName` — nó phải là
  tên **Cluster gốc**, không phải tên cluster đang dựng.
- CNPG ≥ 1.31 sẽ bỏ hẳn `barmanObjectStore` gốc (cụm hiện tại đã cảnh báo deprecated).
  Khi nâng cấp phải chuyển sang Barman Cloud Plugin — đây là một thay đổi catalog, không
  phải một sự cố.
