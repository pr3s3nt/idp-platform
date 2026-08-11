# Tạo một app mới trên IDP

> Công thức đã chạy thật, không phải lý thuyết. Khoảng 10 phút.
>
> **Đã kiểm chứng** bằng cách tạo app `demo` từ số 0 theo đúng các bước dưới đây:
> staging lên 1 pod, production lên 3 pod, cả hai trả HTTP 200, 7/7 bundle Ready.
>
> Ví dụ dùng tên app `demo`. Thay `demo` bằng tên app của bạn ở mọi chỗ.

---

## Quy ước tên — suy ra hết từ tên app

| Thứ | Tên | Ở đâu quyết định |
|---|---|---|
| Repo app | `idp-demo` | người tạo đặt |
| Repo cấu hình | `idp-demo-config` | `git.config_repo_pattern` trong `platform.env.yaml` |
| Namespace staging | `demo-staging` | `kubernetes.namespace_pattern` |
| Namespace prod | `demo-prod` | như trên |
| Ảnh | `<registry>/demo:<tag>` | `registry.path` |

**Tên app không được trùng** với app đang có: namespace và repo cấu hình đều suy ra từ nó.

---

## Đường ngắn nhất: một request, một lệnh (Phase 6)

Mọi thứ dưới đây — Phần A, Phần B, Phần C — gộp thành **một máy trạng thái chạy lại được**.
Viết một file request (mục 13.1 của `KE-HOACH-TRIEN-KHAI-SECRET-VA-APP-ONBOARDING.md`):

```yaml
apiVersion: idp.company/v1
kind: OnboardingRequest

application:
  name: don-hang
  owner: team-order
  description: Quản lý đơn hàng

stack:
  id: node-fullstack
  version: 1.0.0

database:
  enabled: true
  profile: application

routing:
  visibility: internal

environments:
  staging: true
  prod: true
```

Không hỏi namespace, đường dẫn Vault, tên Secret, StorageClass, địa chỉ registry hay
resource database thô — **platform suy ra hết**. Rồi:

```bash
export VAULT_ADDR=... VAULT_TOKEN=...          # nửa Vault: token RIÊNG, không suy ra từ gh
export REGISTRY_USER=... REGISTRY_PASS=...     # để tạo imagePullSecret
python3 orchestrate.py --env-config platform.env.yaml onboard \
  --request request-don-hang.yaml --work /tmp/onboard-don-hang
```

Nó chạy tuần tự: kiểm request → tạo kho ứng dụng từ stack (kèm `.github/workflows/ci.yaml`
đã điền sẵn) → tạo kho cấu hình + hai nhánh + Fleet skeleton → namespace, ServiceAccount,
VaultAuth, policy/role Vault → sinh credential database ghi thẳng vào Vault → build và đẩy
ảnh → render staging, ghi vào kho cấu hình, đăng ký GitRepo → chờ cụm chạy thật.

**Chạy lại bao nhiêu lần cũng được.** Bước đã xong thì bỏ qua; không có kho, namespace hay
mật khẩu database thứ hai nào được tạo ra.

Hai lần dừng là bình thường, không phải lỗi:

| Trạng thái | Nghĩa | Làm gì |
|---|---|---|
| `WAITING_FOR_USER_SECRETS` | app đã deploy, còn thiếu bí mật của bên thứ ba | chạy đúng lệnh `secret-set` nó in ra, rồi chạy lại `onboard` |
| `PENDING_PROD_APPROVAL` | pull request prod đã mở | người duyệt merge, rồi chạy lại `onboard-activate-prod` |

Production là một **lệnh riêng**, không phải bước tiếp theo:

```bash
python3 orchestrate.py --env-config platform.env.yaml onboard-activate-prod \
  --app don-hang --work /tmp/onboard-don-hang
```

Nó dựng tài nguyên prod, render prod bằng **đúng bộ ảnh staging đã được verify**, và luôn
mở pull request — kể cả khi nhánh prod chưa bật bảo vệ. Bí mật **không** được sao chép từ
staging sang prod: prod sẽ dừng ở `WAITING_FOR_USER_SECRETS` cho tới khi có người nạp.

Xem đang ở đâu: `orchestrate.py onboard-status --app don-hang` (thêm `--json` để xem bản
ghi đầy đủ). Trạng thái sống trong ConfigMap `idp-onboarding-<app>`.

Phần còn lại của tài liệu này mô tả **từng việc onboarding làm thay bạn** — đọc khi cần
hiểu, khi phải làm tay, hoặc khi một bước hỏng.

---

## Đường tắt: chạy script thay vì làm tay Phần B

Toàn bộ **Phần B** (dựng kho cấu hình, gieo hai nhánh, `fleet.yaml`, workflow verify, mời
bot) đã có script làm sẵn:

```bash
ORG=<tổ-chức> APP=<tên-app> BOT=<tài-khoản-bot> \
  PLATFORM_REPO=<tổ-chức>/idp-platform \
  ./tools/tao-app-moi.sh
```

Chạy bằng **tài khoản của bạn**, không phải tài khoản bot — việc tạo repo và mời cộng tác
viên cần quyền cao, cấp cho bot nghĩa là bot tạo được repo ở bất kỳ đâu trong tổ chức.

Chạy lại nhiều lần không sao, cái gì có rồi thì bỏ qua.

Script **không** làm 3 việc, và nó in ra nhắc ở cuối:

1. Đặt secret — không tự động hoá an toàn được
2. **Bật bảo vệ nhánh `main`** — cố ý để người làm, đây là điểm kiểm soát duy nhất của
   con người trong cả luồng
3. Đăng ký runner cho repo app

`GitRepo` của Fleet thì **platform tự tạo** ở lần deploy đầu tiên, không cần đụng tới.

Phần dưới đây mô tả từng bước script làm gì — đọc khi cần hiểu hoặc khi phải làm tay.

---

## Đường tắt cho Phần A: sinh repo app từ một stack

Thay vì viết tay `score.yaml`, `Dockerfile`, `platform.lock` (mục A1–A3), có thể sinh cả bộ
từ một **stack** đã phát hành:

```bash
python3 orchestrate.py --env-config platform.env.yaml stack-list

python3 orchestrate.py --env-config platform.env.yaml stack-new \
  --stack node-fullstack --app demo --owner đội-cua-ban --out ../idp-demo
```

| Stack | Gồm gì |
|---|---|
| `node-fullstack` | React/Vite + API Express + PostgreSQL, cùng origin |
| `node-api` | API Express + PostgreSQL |
| `node-worker` | tiến trình nền + PostgreSQL, không có route công khai |
| `static-frontend` | chỉ frontend React/Vite |

`stack-new` **không ghi đè** file đã có, nên chạy lại an toàn (`--force` để ép ghi đè).

### Chạy thử ngay trên máy — không cần cụm

```bash
cd ../idp-demo
make dev            # rồi mở http://demo.localhost:8080/
```

Chỉ cần `docker` và `score-compose`. **Không** cần kho platform, `kubectl` hay Vault:
provisioner local đã được vendor sẵn vào `.idp/score-compose/`.

`compose.yaml` được **sinh ra từ chính `score.yaml`** và nằm trong `.gitignore` — đừng
commit nó, và đừng viết tay một bản song song. Thêm một resource vào Score là local và
staging cùng có nó; một `compose.yaml` chép tay sẽ lệch mà không báo gì.

### Ba điều của golden path, đừng "sửa cho gọn"

1. **Backend mount router tại `/api`, không phải `/`.** Provisioner route chuyển tiếp
   nguyên đường dẫn, **không cắt tiền tố**. Mount tại `/` thì `/api/...` thành 404 và chỉ
   phát hiện được sau khi deploy.
2. **Frontend gọi đường dẫn tương đối `fetch("/api/...")`.** `/` và `/api` cùng một origin
   nên **không có CORS và không cần có**. Nếu bạn thấy mình đang tìm cách bơm địa chỉ API
   vào lúc chạy thì routing đã sai — bundle đã build nằm trong trình duyệt, biến môi trường
   của container không với tới nó.
3. **`tagStrategy: commit`** trong `.idp/stack.yaml`. Kho này có gói dùng chung `shared/`,
   mà `content` băm theo **thư mục của từng workload** nên không thấy thay đổi ở `shared/`
   và sẽ deploy lại ảnh cũ, không báo lỗi gì.

### Nâng phiên bản stack

```bash
python3 orchestrate.py --env-config platform.env.yaml stack-validate --app-dir ../idp-demo
python3 orchestrate.py --env-config platform.env.yaml stack-upgrade  --app-dir ../idp-demo
```

`stack-upgrade` **in diff** và không ghi gì; thêm `--write` để ghi vào working tree rồi tự
mở pull request. Mặc định nó chỉ đụng file do platform sở hữu (`Makefile`, `.gitignore`,
`.idp/`) — mã nguồn là của bạn. Phiên bản stack và `platform.lock` ghim **độc lập**.

Phần A dưới đây vẫn là mô tả đầy đủ từng file, dùng khi bạn tự dựng repo hoặc muốn hiểu
những gì `stack-new` vừa sinh ra.

---

## Phần A — Đội sản phẩm: dựng repo app

Repo app cần đúng **4 file** (cộng nội dung ứng dụng).

### A1. `score.yaml`

```yaml
apiVersion: score.dev/v1b1
metadata:
  name: demo                    # PHẢI trùng tên app
containers:
  web:
    image: .                    # placeholder, orchestrator ghi đè
    variables:
      LOG_LEVEL: "info"
service:
  ports:
    http: { port: 80, targetPort: 80 }
resources:
  route:
    type: route
    params:
      host: demo.127.0.0.1.nip.io   # sandbox; thật thì dùng ${resources.dns.host}
      port: 80
      path: /
```

Cần thêm cơ sở dữ liệu thì thêm:

```yaml
resources:
  db:
    type: postgres
containers:
  web:
    variables:
      PGHOST: "${resources.db.host}"
      PGPASSWORD: "${resources.db.password}"   # thành secretKeyRef, không vào git
```

Cần khoá API riêng của app:

```yaml
resources:
  apikey:
    type: secret
    params: { name: demo-credentials, key: token }
```

*(Secret đó do bạn tạo trong namespace trước khi deploy — platform không tạo hộ.)*

### A2. `Dockerfile`

Bất kỳ, miễn build được.

### A3. `platform.lock`

```
main
```

Một dòng, là ref của repo platform mà app render theo.

### A4. `.github/workflows/ci.yaml`

> `onboard` **sinh sẵn file này** (chọn đúng mẫu theo số workload, điền cả 4 dòng `env`).
> Phần dưới chỉ cần đọc khi bạn tự dựng repo bằng tay.

Mẫu nằm ngay trong repo platform, thư mục `templates/`. **Chọn đúng mẫu theo số service
trong repo** — đây là chỗ sai dễ mắc, và nó hỏng ngay ở bước build:

| Repo có | Chép file | Vì sao |
|---|---|---|
| **một** `score.yaml` | `templates/app-ci-mot-service.yaml` | Build từ gốc repo, một ảnh |
| **nhiều** `score.yaml` | `templates/app-ci-nhieu-service.yaml` | Build từng thư mục service, lấy danh sách động từ platform |

```bash
cp <repo-platform>/templates/app-ci-mot-service.yaml .github/workflows/ci.yaml
```

Chép nhầm mẫu một-service cho repo nhiều service sẽ ra:
`failed to read dockerfile: open Dockerfile: no such file or directory`.

> ⚠️ **MÁY CHẠY PHẢI CÓ SẴN: `python3`, `jq`, `docker`, `git`.** Runner do GitHub cấp
> luôn có đủ; runner tự dựng — tức **mọi** cài đặt GitHub Enterprise Server — thì không.
> Đo trên một runner WSL sạch: `pip install pyyaml` chết vì PEP 668
> (`error: externally-managed-environment`, chỉ có ở Linux hiện đại, không có trên runner
> của GitHub), rồi `jq: command not found` rơi ra giữa một khối bash dài. Mẫu CI nay kiểm
> bốn công cụ đó ở bước ĐẦU TIÊN và dừng sau vài giây với đúng tên thứ còn thiếu, thay vì
> hỏng ở dòng thứ 40 với một thông báo nói về argparse. `pyyaml` thì nó tự cài, thử lần
> lượt `pip`, `pip --user`, `pip --break-system-packages` — nhưng cài sẵn lên ảnh runner
> vẫn tốt hơn.

> ⚠️ **Đừng gắn cứng `runs-on`.** Cả hai mẫu đều đọc biến:
> `runs-on: ${{ vars.CI_RUNNER_LABEL || 'ubuntu-latest' }}`. `ubuntu-latest` là runner do
> GitHub.com cấp — **GitHub Enterprise Server không có nó**, workflow sẽ nằm chờ mãi không
> ai nhận mà cũng không báo lỗi gì rõ ràng. Trên GHES, đặt `CI_RUNNER_LABEL` ở **cấp tổ
> chức** là mọi repo ứng dụng tự có.

Sau khi chép, đổi 4 dòng đánh dấu `<-- SỬA`:

```yaml
env:
  APP: demo                              # tên app
  IMAGE_NAME: demo                       # tên ảnh, có thể khác tên app
  REGISTRY: <registry.path>              # khớp registry.path của platform.env.yaml
  PLATFORM_REPO: <org>/idp-platform      # ĐÚNG repo platform của bản cài này
```

Quên dòng cuối thì CI gọi sang platform khác — **vẫn chạy thành công**, chỉ là triển khai
lên nhầm hạ tầng.

> **CI hỏi platform CÁCH build, không tự đoán.** Bước `plan` gọi
> `image-plan --with-build`, và platform trả về cả context lẫn đường dẫn Dockerfile cho
> từng workload. Cần thế vì app sinh từ stack là monorepo: `backend/Dockerfile` có
> `COPY shared/`, nên context phải là **gốc kho**. Bản mẫu trước đây gắn cứng
> `docker build <workload>/` và mọi app golden path đều hỏng ở lần CI đầu tiên với
> `shared: not found` — sau khi kho đã được tạo, tức ở chỗ tốn nhất.

---

## Phần B — Đội platform: chuẩn bị hạ tầng

### B1. Tạo repo cấu hình, 2 nhánh

> ⚠️ **`fleet.yaml` là bắt buộc.** Fleet coi mỗi thư mục có `fleet.yaml` là một Bundle
> riêng, và lấy `defaultNamespace` trong đó làm nơi đặt tài nguyên. THIẾU FILE NÀY thì
> namespace của app trống trơn trong khi manifest vẫn nằm đúng trong git — bước kiểm cụm
> báo "chưa tồn tại trên cụm" và rất khó đoán ra nguyên nhân.

```bash
mkdir -p /tmp/demo-config/{staging,prod}
cd /tmp/demo-config
for env in staging prod; do
  sed "s/<app>/demo/g" <catalog>/templates/config-repo-template/$env/fleet.yaml > $env/fleet.yaml
done
git init -b main && git add -A && git commit -m "khung Fleet bundle"
gh repo create <org>/idp-demo-config --private --source=. --push
git push origin main:dev        # nhánh dev cho staging
```

### B2. Cấp secret cho repo app

```bash
gh secret set PLATFORM_DISPATCH_TOKEN -R <org>/idp-demo < ~/.idp-sandbox-pat
```

> Dùng để CI gọi sang platform và đăng nhập registry. Trên môi trường thật nên tách:
> GitHub App cho phần gọi platform, robot account của Harbor cho registry.

### B3. Tạo `GitRepo` của Fleet trên **cả hai** cụm

```bash
# staging -> nhánh dev
kubectl --kubeconfig <staging> apply -f - <<EOF
apiVersion: fleet.cattle.io/v1alpha1
kind: GitRepo
metadata: { name: demo, namespace: fleet-local }
spec:
  repo: https://github.com/<org>/idp-demo-config
  branch: dev
  paths: [staging]
  clientSecretName: git-creds
  pollingInterval: 15s
EOF

# prod -> nhánh main, paths [prod]
```

**Quên bước này là triệu chứng đánh lừa nhất**: orchestrator chạy xanh, manifest có trong
repo cấu hình, nhưng **cụm không có gì** — vì không ai kéo về.

### B4. Cài GitHub App vào 2 repo mới

Nếu App cài ở chế độ *all repositories* thì tự động có. Nếu chọn từng repo thì phải thêm
tay ở https://github.com/settings/installations

---

## Phần C — Chạy

### C1. Lên staging

```bash
git push origin dev
```

Xong. CI build ảnh → gọi platform → orchestrator render → ghi nhánh `dev` → Fleet apply.

Theo dõi:

```bash
gh run list -R <org>/idp-demo --limit 1          # CI app
gh run list -R <org>/idp-platform --limit 1      # orchestrator
kubectl --kubeconfig <staging> get pods -n demo-staging
```

### C2. Lên production

Merge `dev` → `main` ở **repo app**. Nhánh quyết định môi trường:

```
push dev   → staging
push main  → production
```

Sau đó platform tự chọn cách ghi, **dựa trên branch protection thật của repo cấu hình**:

| Repo cấu hình | Hành vi |
|---|---|
| `main` **không** bảo vệ | ghi thẳng — dự án demo tự phục vụ hoàn toàn |
| `main` **có** bảo vệ | mở pull request, người đọc diff rồi merge |

Không phải khai gì trong file cấu hình. Muốn siết thì bật branch protection, platform
tự chuyển sang chế độ pull request ngay lần deploy sau.

Vẫn chạy tay được từ giao diện GitHub: *Actions → orchestrator → Run workflow*, chọn
`env: prod`.

---

## Danh sách kiểm trước khi báo "xong"

| Kiểm | Lệnh |
|---|---|
| CI app xanh | `gh run list -R <org>/idp-demo --limit 1` |
| Orchestrator xanh | `gh run list -R <org>/idp-platform --limit 1` |
| Manifest đã vào nhánh `dev` | `gh api repos/<org>/idp-demo-config/commits/dev` |
| Fleet nhặt đúng commit | `kubectl get gitrepo demo -n fleet-local` |
| Bundle Ready | `kubectl get bundle -n fleet-local` |
| Pod chạy | `kubectl get pods -n demo-staging` |

---

## Bốn lỗi hay gặp

| Triệu chứng | Nguyên nhân |
|---|---|
| Orchestrator xanh nhưng cụm trống | Quên tạo `GitRepo` (B3) |
| Bundle `NotReady`, pod `ImagePullBackOff` | Ảnh chưa đẩy lên, hoặc tên ảnh trong CI khác `IMAGE_NAME` orchestrator dùng |
| PVC kẹt `Pending` | `kubernetes.storage_class` trong `platform.env.yaml` sai tên |
| HTTPRoute có nhưng gọi vào 404 | `ingress.gateway_name` / `gateway_namespace` không khớp Gateway thật |

Điểm chung: **ba lỗi cuối đều không báo lỗi ở đâu cả** — phải tự đi kiểm.
