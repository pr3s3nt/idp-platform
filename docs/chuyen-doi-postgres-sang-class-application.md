# Chuyển một app đang chạy sang `class: application`

Tài liệu này nói thẳng một điều mà bảng so sánh trong kế hoạch không nói: **đổi `class`
của một resource `postgres` KHÔNG di chuyển dữ liệu.** Nếu bạn chỉ sửa ba dòng trong
`score.yaml` rồi deploy, bạn sẽ có một database mới tinh và rỗng, còn dữ liệu cũ nằm lại
trên một ổ đĩa không còn ai trỏ tới — trong khi mọi thứ báo xanh.

Phần 1 giải thích vì sao. Phần 2 là đường đi có giữ dữ liệu. Phần 3 là đường đi khi bạn
thật sự không cần giữ gì.

---

## 1. Vì sao đổi class lại là một database khác

score-k8s định danh một resource bằng chuỗi `<type>.<class>#<workload>.<tên>`. Class nằm
trong khoá định danh, nên:

```text
postgres.default#api.db        <- trước
postgres.application#api.db    <- sau
```

là **hai resource khác nhau**. Resource mới nhận một `Guid` mới, và mọi tên object đều
suy ra từ Guid đó. Bản ghi state cũ không bị xoá; nó nằm lại trong file state vĩnh viễn,
kèm cả mật khẩu dạng thô mà provisioner cũ đã ghi vào đó.

Đo trên harness, từ một app legacy có 4 dòng dữ liệu thật:

| | trước (`class` cũ) | sau (`class: application`) |
|---|---|---|
| `PGHOST` | `pg-api-54f63de0` | `pg-api-be0342e7-rw` |
| `PGDATABASE` | `db-haKaonqu` | `app_api` |
| `PGUSER` | `user-IUvGqfQK` | `app_api` |
| `PGPASSWORD` | `secretKeyRef: pg-api-54f63de0` | `secretKeyRef: pg-api-be0342e7-cred` |

Cluster mới lên `Ready` với **0 bảng**. Fleet prune StatefulSet cũ vì nó không còn trong
bundle — nhưng **PVC thì không bị xoá theo**: PVC sinh từ `volumeClaimTemplate` không nằm
trong `ownerReference` của StatefulSet, nên nó ở lại trạng thái `Bound`, giữ nguyên dữ
liệu, và không còn pod nào gắn vào. Đo được: xoá StatefulSet xong, PVC
`pv-data-pg-api-54f63de0-0` vẫn `Bound`; apply lại StatefulSet thì 4 dòng cũ trở lại
nguyên vẹn.

Nói cách khác: **dữ liệu không mất, nhưng app không còn thấy nó**, và không có một cảnh
báo nào ở bất kỳ đâu — không phải trong render, không phải trong Fleet, không phải trong
`verify`. Đó là lý do platform nay **dừng render** khi phát hiện tình huống này
(`check_postgres_class_migration`), thay vì để bạn phát hiện lúc mở app lên xem.

---

## 2. Đường có giữ dữ liệu — `bootstrap.initdb.import`

CNPG import bằng `pg_dump`/`pg_restore` từ một database bên ngoài lúc khởi tạo cluster.
Đây là đường đã được chạy thật trên harness và giữ đúng dữ liệu.

**Điều kiện:** database cũ phải còn chạy và còn kết nối được từ trong cụm; major version
của nguồn và đích phải tương thích (`pg_dump` từ 17 sang 17 — nếu lệch, kiểm trước).

### Bước 1 — ghi lại toạ độ của database cũ

```bash
# Từ file state đang dùng, đọc bản ghi của resource CŨ:
python3 - <<'EOF'
import yaml
s = yaml.safe_load(open("state.yaml"))
for uid, r in (s.get("resources") or {}).items():
    if uid.startswith("postgres.") and not uid.startswith("postgres.application#"):
        print(uid, "->", {k: v for k, v in (r.get("state") or {}).items() if k != "password"})
EOF
```

Bạn cần ba giá trị: `service` (host), `database`, `username`. Mật khẩu KHÔNG cần đọc ra —
nó đã nằm sẵn trong Secret cùng tên với `service`, dưới khoá `password`.

### Bước 2 — chụp một bản sao lưu trước khi làm gì cả

Đường này không ghi vào database cũ, nhưng vẫn phải có đường lùi:

```bash
kubectl -n <ns> exec <pod-postgres-cũ> -- \
  pg_dump -U <username> -d <database> -Fc -f /tmp/truoc-migrate.dump
kubectl -n <ns> cp <pod-postgres-cũ>:/tmp/truoc-migrate.dump ./truoc-migrate.dump
```

### Bước 3 — chuẩn bị credential mới trong Vault

Database mới lấy user/password từ Vault, không phải từ state:

```bash
python3 orchestrate.py --env-config platform.env.yaml \
  secret-set --app <app> --env <env> --name database --key password --generate --replace
python3 orchestrate.py --env-config platform.env.yaml \
  secret-set --app <app> --env <env> --name database --key username --stdin <<< "app_<workload>"
```

### Bước 4 — render với class mới, chấp nhận cluster rỗng, rồi CHƯA apply

```bash
python3 orchestrate.py --env-config platform.env.yaml render \
  --app <app> --env <env> ... --accept-empty-database \
  --out ./config/<env>/manifests.yaml
```

Cờ `--accept-empty-database` ở đây chỉ để render đi qua guard; bạn sẽ thêm khối import vào
manifest trước khi nó tới cụm.

### Bước 5 — thêm khối import vào `Cluster` vừa render

Sửa `Cluster` trong `manifests.yaml`, thêm `bootstrap.initdb.import` và `externalClusters`:

```yaml
  bootstrap:
    initdb:
      database: app_api          # giữ nguyên giá trị renderer đã sinh
      owner: app_api             # giữ nguyên
      secret:
        name: pg-api-be0342e7-cred
      import:
        type: microservice       # import ĐÚNG MỘT database và đổi tên nó thành `database:`
        databases: ["db-haKaonqu"]   # tên database CŨ
        source:
          externalCluster: pg-cu
  externalClusters:
    - name: pg-cu
      connectionParameters:
        host: pg-api-54f63de0    # Service của StatefulSet cũ
        user: user-IUvGqfQK      # username CŨ
        dbname: db-haKaonqu      # database CŨ
      password:
        name: pg-api-54f63de0    # Secret cũ
        key: password
```

`type: microservice` nhập một database và đổi tên nó thành tên đích, đồng thời chuyển
quyền sở hữu sang owner mới. Đó chính là thứ ta cần: tên database và tên user đổi, nội
dung giữ nguyên.

### Bước 6 — apply và đối chiếu

```bash
kubectl -n <ns> apply -f ./config/<env>/manifests.yaml
kubectl -n <ns> get cluster.postgresql.cnpg.io -w      # chờ "Cluster in healthy state"
```

Đối chiếu bằng một phép đo, không bằng cảm giác:

```bash
# trên database CŨ
kubectl -n <ns> exec <pod-cũ> -- psql -U <user-cũ> -d <db-cũ> \
  -tAc "SELECT count(*)||' | '||md5(string_agg(<cột>,',' ORDER BY id)) FROM <bảng>;"
# trên database MỚI
kubectl -n <ns> exec <cluster-mới>-1 -c postgres -- psql -U postgres -d app_api \
  -tAc "SELECT count(*)||' | '||md5(string_agg(<cột>,',' ORDER BY id)) FROM <bảng>;"
```

Hai chuỗi phải giống hệt nhau. Đo trên harness: `4 | baf60ff7089bb14d4165fd78da14a6da` ở
cả hai bên, và app đăng nhập được vào database mới bằng credential lấy từ Vault.

### Bước 7 — bỏ khối import, và chỉ dọn khi đã chắc

`import` chỉ có tác dụng lúc bootstrap; để lại trong manifest thì lần render sau sẽ ghi đè
nó mất, nên **render lại bình thường** (không cần `--accept-empty-database` nữa — state đã
có khoá `postgres.application#...`, guard tự im) và commit bản đó.

Dữ liệu cũ: **giữ ít nhất một chu kỳ backup**. StatefulSet cũ đã bị prune, nhưng PVC còn
đó. Khi đã chắc chắn:

```bash
kubectl -n <ns> get pvc                       # tìm pv-data-<tên-cũ>-0
kubectl -n <ns> delete pvc pv-data-<tên-cũ>-0
```

Bản ghi state cũ (`postgres.default#...`) cũng nên xoá khỏi file state sau đó — nó chứa
mật khẩu dạng thô của provisioner cũ và không còn tài nguyên nào tương ứng.

---

## 3. Đường không giữ dữ liệu — nói thẳng là huỷ rồi dựng lại

Nếu database này thật sự không có gì đáng giữ (môi trường thử, dữ liệu sinh lại được từ
seed), thì đường đi là **huỷ rồi dựng lại**, và tài liệu này gọi đúng tên nó chứ không gọi
là "migrate":

```bash
python3 orchestrate.py --env-config platform.env.yaml render \
  --app <app> --env <env> ... --accept-empty-database
```

Chuyện xảy ra, đầy đủ:

- Một CNPG `Cluster` mới, **rỗng**, tên khác, database khác, user khác.
- `PGHOST`/`PGDATABASE`/`PGUSER`/`PGPASSWORD` của app đều đổi. App đọc chúng từ biến môi
  trường nên không phải sửa code — nhưng nếu app có migration lúc khởi động thì nó sẽ chạy
  lại từ đầu trên một database trắng.
- StatefulSet cũ bị Fleet prune. **PVC cũ KHÔNG bị xoá** và vẫn tính vào hạn mức lưu trữ
  của namespace. Bạn phải tự xoá nó.
- Bản ghi state cũ ở lại trong file state, kèm mật khẩu dạng thô.

Nếu đọc xong ba gạch đầu dòng đó mà vẫn thấy ổn thì đường này đúng cho bạn.

---

## 4. Những gì KHÔNG đổi khi chuyển class

Để cân bằng: phần contract thì đúng là không phải sửa gì.

- App vẫn khai ba dòng `type: postgres` + `class: application` và nhận **đúng bộ output
  cũ** (`host`/`port`/`database`/`username`/`password`).
- Không phải sửa code app, không phải đổi tên biến môi trường.
- `password` vẫn đi qua `secretKeyRef`, không bao giờ vào git.

Cái đổi là **danh tính của database**, và đó là thứ tài liệu này tồn tại để nói ra.

---

## 5. Liên quan

- `docs/adr/0008-provider-database-va-credential.md` — vì sao class mới nằm ở class riêng.
- `docs/adr/0005-database-profile.md` — profile staging/prod.
- `docs/runbook/` — `database-provisioning-backup-that-bai.md` cho sự cố backup/restore.
