# Cấu hình

Hai tầng cấu hình tách bạch: **nền tảng** (`platform.env.yaml`, do đội platform quản) và
**ứng dụng** (`.score-values/values.yaml` trong kho app, do đội ứng dụng quản). App khai ý
định; nền tảng suy ra toạ độ.

---

## 1. `platform.env.yaml` — cấu hình nền tảng

Nơi DUY NHẤT chứa giá trị phụ thuộc hạ tầng. Chuyển nền tảng sang môi trường khác = sửa
file này, không sửa code/provisioner/patch/workflow. **Không để bí mật ở đây** (file nằm
trong git); token và mật khẩu đi qua secret của CI. File **không** ghim theo `platform.lock`
— nó là toạ độ hạ tầng đang chạy, không phải dữ liệu catalog.

Các khối chính (đọc comment trong chính file để biết chi tiết và cạm bẫy):

| Khối | Chứa gì |
|---|---|
| `git` | org, mẫu tên kho cấu hình/app, nhánh mặc định, danh tính committer của bot, có được bypass branch protection không |
| `registry` | host + path của registry, tên imagePullSecret nền tảng tự tạo |
| `kubernetes` | `state_namespace`, `namespace_pattern` (`{app}-{env}`), `storage_class`, `fleet_namespace`, `fleet_git_secret`, `cluster_domain` |
| `ci` | nhãn runner cho workflow verify; phiên bản ghim của `score-k8s`/`score-compose` |
| `vault` | địa chỉ Vault (góc nhìn của cụm), KV mount/type, `path_template`, khuôn tên role/policy/ServiceAccount, auth mount/audience, timeout |
| `database` + `database_profiles` | backend (`cnpg`/`statefulset`), provider/operator, ảnh, credential secret, backup, và profile staging/prod (instance, storage, HA, retention) |
| `features` | cờ bật/tắt từng tính năng: `application_values`, `vault_secrets`, `postgres_application`, `stack_onboarding` |
| `audit` | kho lịch sử deploy tuỳ chọn: `enabled`, `required` (fail-open/closed), tên biến chứa connection string |
| `ingress` | `gateway_name`/`gateway_namespace` (phải khớp Gateway thật), `section_name`, `route_scheme`, `deploy_check_domain` (domain wildcard cho URL debug của `deploy-check --keep`; rỗng = giữ hostname gốc) |
| `images` | ảnh datastore (`postgres`) và ảnh nền golden path (`node`, `nginx`) |
| `environments` | khác biệt theo môi trường: `config_branch`, `replicas`, `domain`, cpu/memory — phơi ra khi render dưới tiền tố `env.` |

Đọc một khoá bất kỳ: `python3 idpctl --env-config platform.env.yaml config --get <đường.dẫn.khoá>`.

### Bốn cạm bẫy toạ độ hay gặp (từ comment trong file)

- `kubernetes.storage_class` sai tên → PVC treo `Pending` vĩnh viễn, không có lỗi rõ.
- `ingress.gateway_name`/`gateway_namespace` sai → HTTPRoute không bao giờ được attach.
- `kubernetes.fleet_namespace` sai → Fleet không nhận `GitRepo` mà cũng không báo lỗi.
- `git.committer_email` dùng đuôi `@users.noreply.github.com` map nhầm sang người có thật →
  mọi commit triển khai bị ghi công cho người lạ.

## 2. Placeholder — cách provisioner/patch đọc toạ độ

Trong `provisioners/` và `patches/`, toạ độ viết dạng `%%đường.dẫn.khoá%%`:

- `%%ingress.gateway_name%%` → giá trị dùng chung.
- `%%env.replicas%%` → giá trị của môi trường ĐANG render (staging/prod).

Render thay placeholder bằng giá trị từ `platform.env.yaml`. Giữ hình dạng ở catalog và
toạ độ ở config tách nhau là bất biến cốt lõi — trộn hai thứ là nguồn lỗi im lặng.

---

## 3. Cấu hình ứng dụng theo môi trường (`ApplicationValues`)

Nguồn: ADR [0001](adr/0001-application-values-v1.md). Một `score.yaml` phục vụ cả staging
lẫn prod; giá trị khác nhau giữa hai môi trường nằm ở một file duy nhất ở gốc kho app.

**Bước 1 — khai resource `environment` trong `score.yaml`:**

```yaml
resources:
  app-config:
    type: environment
containers:
  app:
    variables:
      LOG_LEVEL: "${resources.app-config.LOG_LEVEL}"
```

**Bước 2 — tạo `.score-values/values.yaml`:**

```yaml
apiVersion: idp.company/v1
kind: ApplicationValues
spec:
  application:            # dùng chung mọi môi trường
    LOG_LEVEL: info
  environments:
    staging:
      LOG_LEVEL: debug
    prod:
      PUBLIC_HOST: payment-api.internal
```

Quy tắc phải nhớ:

- Precedence chỉ hai tầng: `spec.application` < `spec.environments.<env>`.
- **Mọi giá trị literal PHẢI là chuỗi.** YAML tự ép `yes`/`no`/`on`/`off`/`8080` thành
  bool/int — phải quote.
- Đúng hai môi trường: `staging` và `prod` (ADR [0003](adr/0003-hai-moi-truong-staging-prod.md)).
  `production` là lỗi, không phải alias.
- Một khoá giữ nguyên loại (`literal` hay `secretRef`) ở mọi môi trường.
- Khoá được Score tham chiếu nhưng thiếu sau resolve là **lỗi lúc render**, không phải
  chuỗi rỗng.
- Mỗi workload có 0 hoặc 1 resource `type: environment`.

App không có `.score-values/values.yaml` giữ nguyên hành vi cũ.

## 4. Placeholder Score `${resources...}` chỉ hợp lệ ở 4 chỗ

Nguồn: ADR [0004](adr/0004-placeholder-matrix.md). `${resources.x.y}` (cú pháp của Score,
khác `%%...%%` của platform) chỉ được nội suy ở: `containers.*.variables`, nội dung
`containers.*.files.*`, `containers.*.volumes.*.source`, và `resources.*.params`. Ở chỗ
khác (vd `command`, `args`, `image`) nó là **lỗi lúc render** — vì `score-k8s` chuyển
những field đó sang manifest nguyên văn, và app sẽ đọc đúng chuỗi rác `${...}`.

## 5. Bí mật

App khai `secretRef`, không khai đường Vault:

```yaml
STRIPE_KEY:
  secretRef:
    name: stripe
    key: api_key
```

Nền tảng suy ra đường `apps/<app>/<env>/stripe` và render `VaultStaticSecret`. Ghi giá trị
vào Vault bằng lệnh dành cho NGƯỜI (không cho CI — giá trị vào qua nhập ẩn hoặc stdin,
không có cờ `--value`):

```bash
python3 idpctl --env-config platform.env.yaml \
  secret-set --app <app> --env <env> --name stripe --key api_key
```

VSO đồng bộ ra Kubernetes Secret; bí mật thiếu là trạng thái **tự hồi phục** (ghi vào Vault
→ VSO sync → pod chạy tiếp, không cần deploy lại). Bí mật trong nội dung file chỉ hợp lệ khi
TOÀN BỘ nội dung là đúng một secret reference (ADR 0004).

## 6. Database PostgreSQL

App khai đúng một thứ, giống nhau mọi môi trường:

```yaml
resources:
  db:
    type: postgres
    class: application
```

và nhận `host/port/database/username/password`. Kích thước, HA, backup, retention khác nhau
staging↔prod đều nằm ở `database_profiles` trong `platform.env.yaml`, app không thấy. Prod
bị **chặn render** nếu chưa cấu hình kho object cho backup (`database.backup.object_store_url`
rỗng). Đổi `class` của một postgres đã có dữ liệu là thao tác nguy hiểm — xem
[chuyển đổi postgres sang class application](chuyen-doi-postgres-sang-class-application.md).

## 7. Promotion và prod

`config_branch` của mỗi môi trường quyết định nhánh kho cấu hình nó ghi vào (mô hình hai
nhánh: `dev` = staging, `main` = prod). Cần duyệt hay không **không** khai trong file — 
orchestrator hỏi thẳng branch protection của nhánh đó, tránh hai nguồn sự thật lệch nhau.
Lên prod đi qua `promote` (xem [docs/deployment.md](deployment.md)).

---

## 8. Profile công ty (`platform.env.company.yaml`)

Cùng một source chạy được cả harness (github.com + GHCR + CNPG + Traefik HTTP) lẫn hình
dạng công ty (GHES + Harbor/HTTPS + PostgreSQL StatefulSet trên StorageClass mạng + Traefik
listener HTTPS). Khác biệt nằm HẾT ở `platform.env.company.yaml`, **không fork code**.

- **Chọn profile:** thêm `--env-config platform.env.company.yaml` vào mọi lệnh (thay cho
  `platform.env.yaml`). Mọi lệnh đọc đúng file đó làm nguồn toạ độ.
- **Kiểm capability cụm khớp config** (read-only, theo feature/backend đang bật):

  ```bash
  python3 idpctl --env-config platform.env.company.yaml doctor            # với cụm đang trỏ
  python3 idpctl --env-config platform.env.company.yaml doctor --no-cluster
  ```

- **Đưa source sang repo công ty:** dùng `tools/dong-bo-sang-cong-ty.sh` — nó chép code
  nguyên vẹn và **merge** (không đè) `platform.env.yaml`; toạ độ mới phải tự điền vào
  profile công ty. Sau khi chép, chạy `python3 -m pytest test_engine.py -q` trước khi
  commit.
- **Điền gì:** khai đủ toạ độ hạ tầng công ty (Git host, registry, storageclass, gateway,
  listener, Vault address, database backend/backup…) rồi chạy trọn một vòng bằng chính
  file đó để lỗi chỉ nổ ở công ty lộ ra sớm. Bí mật (mật khẩu Harbor, kubeconfig, private
  key App…) **không** vào file này — chúng vào secret của repo bằng `gh secret set`.
