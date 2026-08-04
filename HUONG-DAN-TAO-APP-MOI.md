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

> ⚠️ **Đừng gắn cứng `runs-on`.** Cả hai mẫu đều đọc biến:
> `runs-on: ${{ vars.CI_RUNNER_LABEL || 'ubuntu-latest' }}`. `ubuntu-latest` là runner do
> GitHub.com cấp — **GitHub Enterprise Server không có nó**, workflow sẽ nằm chờ mãi không
> ai nhận mà cũng không báo lỗi gì rõ ràng. Trên GHES, đặt `CI_RUNNER_LABEL` ở **cấp tổ
> chức** là mọi repo ứng dụng tự có.

Sau khi chép, đổi 3 dòng:

```yaml
env:
  APP: demo                              # tên app
  IMAGE_NAME: demo                       # tên ảnh, có thể khác tên app
  PLATFORM_REPO: <org>/idp-platform      # ĐÚNG repo platform của bản cài này
```

Quên dòng thứ ba thì CI gọi sang platform khác — **vẫn chạy thành công**, chỉ là triển khai
lên nhầm hạ tầng.

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
