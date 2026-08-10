# Cấu hình ứng dụng theo môi trường

Dành cho người phát triển ứng dụng. Trả lời đúng một câu hỏi: **làm sao để app chạy với
cấu hình khác nhau ở `staging` và `prod` mà chỉ có một `score.yaml`.**

> Tính năng này nằm sau cờ `features.application_values` trong `platform.env.yaml`. App
> không có `.score-values/values.yaml` giữ nguyên hành vi cũ, không cần đọc tài liệu này.

---

## 1. Ba bước

### Bước 1 — khai một resource `environment` trong `score.yaml`

```yaml
resources:
  app-config:            # tên tuỳ bạn đặt: cfg, config, env… đều được
    type: environment
```

Mỗi workload được có **0 hoặc 1** resource loại này. Hai cái trở lên là lỗi lúc render —
vì khi đó không có câu trả lời đúng cho việc cái nào cấp một khoá nào.

### Bước 2 — tham chiếu giá trị trong container

```yaml
containers:
  app:
    image: .
    variables:
      LOG_LEVEL: "${resources.app-config.LOG_LEVEL}"
      PUBLIC_HOST: "${resources.app-config.PUBLIC_HOST}"
```

### Bước 3 — tạo `.score-values/values.yaml` ở gốc repo

```yaml
apiVersion: idp.company/v1
kind: ApplicationValues

spec:
  application:                 # dùng chung mọi môi trường
    LOG_LEVEL: info
    FEATURE_X: "false"

  environments:
    staging:                   # đè lên application
      LOG_LEVEL: debug
      FEATURE_X: "true"
      PUBLIC_HOST: payment-api.staging.internal
    prod:
      PUBLIC_HOST: payment-api.internal
```

Thứ tự ưu tiên, chỉ hai tầng:

```text
spec.application  <  spec.environments.<môi-trường>
```

Kết quả: cùng một `score.yaml` cho `LOG_LEVEL=debug` ở staging và `info` ở prod.

---

## 2. Quy tắc phải nhớ

### Mọi giá trị literal PHẢI là chuỗi

Biến môi trường trong Kubernetes là chuỗi, không có kiểu nào khác. Renderer từ chối
mọi thứ không phải string thay vì tự ép kiểu.

```yaml
FEATURE_X: false      # LỖI — YAML đọc thành boolean
PORT: 8080            # LỖI — YAML đọc thành số
VERSION: 1.10         # LỖI — và ép kiểu sẽ cho ra "1.1", không phải "1.10"

FEATURE_X: "false"    # đúng
PORT: "8080"          # đúng
VERSION: "1.10"       # đúng
```

Cái bẫy ít ai ngờ: YAML đọc `yes`, `no`, `on`, `off` **thành boolean**.

```yaml
ENABLED: no           # LỖI — đây là false, không phải chuỗi "no"
ENABLED: "no"         # đúng
```

### Chỉ có `staging` và `prod`

`production` không phải bí danh của `prod`. Viết `production:` là lỗi — và đó là chủ ý:
nếu chấp nhận, một khối viết sai tên sẽ không áp dụng cho môi trường nào và cũng không
báo gì.

### Một khoá giữ nguyên loại ở mọi môi trường

Literal ở staging và `secretRef` ở prod là lỗi. Nếu cho phép, hai môi trường render ra hai
hình dạng manifest khác nhau từ một file Score — và thứ staging kiểm chứng không còn là
thứ prod chạy.

### Khoá được tham chiếu mà không resolve được là lỗi

Không phải chuỗi rỗng. Một biến rỗng trông giống lỗi trong code app, và người ta sẽ đi tìm
ở nhầm chỗ.

---

## 3. Cấu hình dạng file

Không cần tự viết ConfigMap. Khai thẳng trong Score:

```yaml
containers:
  app:
    files:
      /etc/app/application.yaml:
        content: |-
          logLevel: ${resources.app-config.LOG_LEVEL}
          featureX: ${resources.app-config.FEATURE_X}
```

Platform sinh ConfigMap và mount vào đúng đường dẫn. Nội dung nằm trong repo cấu hình nên
review được bằng mắt.

Muốn đọc từ file có sẵn trong repo thì dùng `source` thay cho `content`. `binaryContent`
và `noExpand: true` được giữ nguyên văn, không thay thế placeholder.

---

## 4. Bí mật

Khai `secretRef` thay cho giá trị:

```yaml
spec:
  environments:
    staging:
      STRIPE_KEY:
        secretRef:
          name: stripe
          key: api_key
```

Chỉ có `name` và `key`. **Không có mount, không có path** — platform tự suy ra đường dẫn
Vault. Đó là điều làm cho phân quyền theo app có hiệu lực: nếu app tự khai được path thì
app A đọc được bí mật của app B chỉ bằng cách gõ đúng chuỗi.

Ghi giá trị vào Vault là việc riêng, không đi qua Git và không đi qua CI. Xem
`docs/adr/0002-vault-only-secret-store.md`.

### Ghi giá trị vào Vault

Bạn cần một Vault token có policy ghi của app (Platform/Vault Ops cấp khi onboard). Giá
trị **chỉ vào qua nhập ẩn hoặc stdin** — cố tình không có cờ `--value`, vì tham số dòng
lệnh nằm trong history của shell và trong `ps` của mọi user khác trên cùng máy:

```bash
export VAULT_ADDR=https://vault.<công-ty>   # địa chỉ BẠN gọi được, không phải của cụm
export VAULT_TOKEN=...                      # token của BẠN, không bao giờ của CI

python3 orchestrate.py secret-set --app payment-api --env staging \
  --name stripe --key api_key            # nhập ẩn, gõ hai lần

printf '%s' "$KEY" | python3 orchestrate.py secret-set --app payment-api --env staging \
  --name stripe --key api_key --stdin    # hoặc qua stdin
```

Mặc định là **vá đúng khoá đó**, không đụng các khoá khác trong cùng secret. Lần đầu tạo
secret thì thêm `--replace` (vá không tạo được thứ chưa tồn tại — lệnh sẽ nói đúng câu đó).

Đường dẫn do platform suy ra, giống hệt đường app đọc: đó chính là lý do dùng lệnh này
thay vì `vault kv put` bằng tay — gõ nhầm một đoạn path là "permission denied" trên một
đường dẫn trông rất đúng.

### Sau khi ghi: chuyện gì xảy ra

Platform sinh cho **mỗi (workload, secret)** một `VaultStaticSecret`. VSO đọc Vault rồi
tạo Kubernetes Secret; container nhận biến qua `secretKeyRef`. Trong Git chỉ có tham
chiếu, không bao giờ có giá trị.

- Secret đích **chỉ chứa những khoá workload đó khai**. Thêm khoá mới vào Vault không tự
  chảy sang app.
- **Xoay vòng**: ghi giá trị mới, trong vòng `refreshAfter` (mặc định 5 phút) VSO cập nhật
  Secret và restart **đúng một lần** Deployment tương ứng. Không đổi gì thì không restart.
- Pod có thể **thoáng qua** `CreateContainerConfigError` ngay sau khi deploy — Fleet apply
  Deployment và VaultStaticSecret cùng lúc. Nó phải tự hết trong vài chục giây; `verify`
  chờ đúng việc đó.
- Vault sập **không** làm chết app đang chạy: Secret đã đồng bộ vẫn còn, chỉ là không cập
  nhật được cho tới khi Vault trở lại.

### Bí mật trong file

File lấy từ bí mật được mount thẳng từ Kubernetes Secret, nên nội dung phải là **đúng một
tham chiếu và không có gì khác**:

```yaml
content: "${resources.app-config.PRIVATE_KEY}"     # đúng

content: |-                                         # đúng
  ${resources.app-config.PRIVATE_KEY}

content: |                                          # LỖI
  ${resources.app-config.PRIVATE_KEY}
```

Khác biệt giữa `|` và `|-` là một ký tự xuống dòng ở cuối. `|` giữ nó lại, nên file thành
"bí mật + newline" — tức là trộn. Dùng `|-`.

Trộn bí mật với chữ thường cũng là lỗi:

```yaml
content: |-
  username=admin
  password=${resources.app-config.PASSWORD}         # LỖI
```

Nếu cho phép, phần `username=admin` phải được ghi vào manifest trong Git bên cạnh tham
chiếu bí mật. Tách thành hai file, hoặc để app tự ghép từ hai biến môi trường.

---

## 4b. Cơ sở dữ liệu PostgreSQL

Khai đúng ba dòng, giống hệt nhau ở staging và prod:

```yaml
resources:
  db:
    type: postgres
    class: application
```

và dùng đúng bộ output cũ:

```yaml
containers:
  backend:
    variables:
      PGHOST: "${resources.db.host}"
      PGPORT: "${resources.db.port}"
      PGDATABASE: "${resources.db.database}"
      PGUSER: "${resources.db.username}"
      PGPASSWORD: "${resources.db.password}"
```

`class: application` = database do operator production-grade quản lý. Số bản sao, CPU/RAM,
dung lượng, HA và retention **do platform quyết theo môi trường** (`database_profiles`), app
không thấy và không sửa được. Cùng major version ở cả hai môi trường — đó là lý do staging
còn nói được điều gì về prod.

**Không có `class`** (hoặc `class: development`) = database demo cũ: một bản sao, 1Gi,
không HA, không backup, mật khẩu nằm trong state. Dùng để chạy thử thì được; **render
`prod` với nó sẽ bị chặn** khi platform đã bật `features.postgres_application`.

Mật khẩu: bạn **không** đặt và **không** cần biết. Platform sinh ngẫu nhiên khi onboard,
ghi thẳng vào Vault; database tạo user từ chính secret đó và app đọc cũng từ đó. Đổi mật
khẩu = ghi lại vào Vault, không sửa gì trong repo.

Chẩn đoán:

| Thông báo | Nguyên nhân |
|---|---|
| `is \`type: postgres\` with class … refused in prod` | Dùng database demo ở prod. Đổi sang `class: application`. |
| `database.backup.object_store_url is empty` | Platform chưa cấu hình kho backup cho prod. Việc của Platform team, không phải của app. |
| `cơ sở dữ liệu chưa Ready sau …s` | Thường là credential chưa được ghi vào Vault trước khi render — cluster không bootstrap được. |

---

## 5. Placeholder chỉ hoạt động ở 4 chỗ

| Vị trí | Có thay thế |
|---|---|
| `containers.*.variables` | có |
| Nội dung `containers.*.files.*` | có |
| `containers.*.volumes.*.source` | có |
| `resources.*.params` | có |
| `command`, `args`, `image`, probe, annotation… | **không** |

Viết `${resources.…}` ở `command` hoặc `args` là lỗi lúc render. Lý do phải chặn: nếu
không chặn, chuỗi đó được chép nguyên văn vào manifest, pod khởi động bình thường, và app
đọc được đúng chuỗi `${resources.config.LOG_LEVEL}` làm mức log. Không có lỗi ở bất cứ đâu.

---

## 6. Promotion và prod

Sau mỗi lần render `prod`, platform ghi vào repo cấu hình:

```text
.platform/prod.values.sha256
```

Đây là dấu vân tay của cấu hình prod tại lần render đó. Khi promote bằng `--mode tag-only`
hoặc `--mode from-staging` — hai chế độ chỉ đổi tag ảnh mà **không chạy lại renderer** —
platform so lại dấu vân tay này.

Nếu bạn đã sửa khối `prod` trong values file thì promotion bị **chặn**, kèm hướng dẫn dùng
`--mode re-render`. Nếu không chặn, promotion sẽ báo thành công trong khi cấu hình mới
không hề tới production.

Sửa riêng khối `staging` không ảnh hưởng dấu vân tay prod.

---

## 7. Chẩn đoán nhanh

| Thông báo | Nguyên nhân |
|---|---|
| `value of 'X' is bool, not a string` | Thiếu dấu nháy. Chú ý `yes`/`no`/`on`/`off`. |
| `'X' is a literal in … but a secret in …` | Một khoá đổi loại giữa các môi trường. |
| `references ['X'], but no such key resolves` | Thiếu khoá trong values file cho môi trường đang render. |
| `does not substitute there` | Placeholder nằm ngoài 4 vị trí ở mục 5. |
| `mixes a secret reference with other content` | Xem mục 4 — thường là `\|` thay vì `\|-`. |
| `declare a 'type: environment' resource, but features.application_values is off` | Platform chưa bật cờ. Liên hệ Platform team. |
| `prod values have changed since the last prod render` | Dùng `--mode re-render`. |
| `is a secretRef, but features.vault_secrets is off` | Platform chưa bật secret. Liên hệ Platform team. |
| `bí mật chưa được VSO đồng bộ sau …s` + `empty response from Vault` | Chưa ai ghi giá trị vào đường dẫn Vault in kèm. Chạy `secret-set --replace`. |
| `… + permission denied` | Đã ghi nhưng role của app không đọc được tiền tố đó — sai app/env, hoặc chưa onboard. Gửi Platform team đúng dòng lỗi (nó có sẵn đường dẫn). |
| `VaultStaticSecret … chưa có trên cụm` | Fleet chưa apply bản render mới. Đợi/kiểm GitRepo, chưa cần động tới Vault. |
| Biến vẫn là giá trị CŨ sau khi xoay vòng | Biến môi trường chỉ đọc lúc container khởi động. Chờ VSO restart (trong `refreshAfter`), đừng sửa tay. |

---

Chi tiết quyết định thiết kế: [`docs/adr/0001`](docs/adr/0001-application-values-v1.md),
[`0002`](docs/adr/0002-vault-only-secret-store.md),
[`0004`](docs/adr/0004-placeholder-matrix.md).
