# Hướng dẫn: đưa một app mới lên nền tảng, từ đầu đến staging

Tài liệu này đi theo đúng một ví dụ đã chạy thật: **`sinhvien`** — app quản lý sinh viên có
thêm/sửa/xoá, có biến môi trường khác nhau giữa staging và prod, và có một bí mật lấy từ
Vault. Mọi lệnh dưới đây đã được chạy trên harness và cho ra kết quả ghi kèm.

> Nếu bạn chỉ cần tra cứu contract: `HUONG-DAN-CAU-HINH-UNG-DUNG.md` (values/secret) và
> `docs/deployment-contract.md` (portal ↔ deployment engine). Tài liệu này là đường đi.

---

## 0. Chuẩn bị một lần

```bash
# Công cụ và cụm phải sẵn sàng — bước này bắt lỗi TRƯỚC khi tạo bất cứ thứ gì.
python3 idpctl --env-config platform.env.yaml \
  preflight --require-cluster --require-vault
```

> **Cập nhật sau khi gỡ onboarding (`c5d28ac`):** không còn lệnh `idpctl onboard*` và file
> `OnboardingRequest`. Đưa app lên nay là **`stack-new` → push → CI build → `deploy.yaml`**.
> Guide này đã sửa theo luồng đó.

Bốn biến môi trường mà luồng đưa app lên cần. **Thiếu chúng thì hỏng ở nơi không nhắc tới
chúng**, nên đặt trước:

| Biến | Dùng để | Thiếu thì |
|---|---|---|
| `VAULT_ADDR`, `VAULT_TOKEN` | ghi bí mật và policy | `secret-set` không chạy |
| `APP_DISPATCH_TOKEN` | đặt secret `PLATFORM_DISPATCH_TOKEN` cho kho app | CI của app đỏ ở `actions/checkout`, thông báo không nhắc tới secret |
| `REGISTRY_USER`, `REGISTRY_PASS` | tạo `registry-pull` trong namespace | **ảnh không kéo được, lỗi là `403` từ registry** — nay platform từ chối tạo secret rỗng và nói thẳng biến nào thiếu |
| `GH_TOKEN` | tạo kho, đặt secret | không tạo được kho |

```bash
export VAULT_ADDR=http://127.0.0.1:8200      # cần port-forward: kubectl -n vault port-forward svc/vault 8200:8200
export VAULT_TOKEN=<token có policy ghi>
export GH_TOKEN=<PAT: repo + write:packages>
export APP_DISPATCH_TOKEN="$GH_TOKEN"
export REGISTRY_USER=<user registry>
export REGISTRY_PASS="$GH_TOKEN"
```

---

## 1. Chọn stack

Bạn khai **ý định** qua việc chọn stack, không khai toạ độ. Không có namespace, không có đường
dẫn Vault, không có StorageClass, không có tài nguyên database thô — nền tảng suy ra hết từ
`platform.env.yaml`. Xem stack có gì:

```bash
python3 idpctl --env-config platform.env.yaml stack-list
```

Cho `sinhvien` (CRUD + một secret + database), chọn **`node-fullstack`** — stack này gói sẵn
frontend + backend + **PostgreSQL** (capability database), tương đương `database.enabled: true`
của file request cũ. Tên app + owner truyền vào `stack-new` (mục 2). Prod **không** dựng gì tới
khi bạn deploy `env=prod` (mục 8) — đi qua pull request có người duyệt.

---

## 2. Dựng khung, tạo kho, rồi dừng lại để sửa code

```bash
python3 idpctl --env-config platform.env.yaml stack-new \
  --stack node-fullstack --app sinhvien --owner team-daotao --out ./sinhvien
cd ./sinhvien && git init -b main && git add -A && git commit -m "scaffold sinhvien"
gh repo create pr3s3nt/sinhvien --private --source=. --push
gh secret set PLATFORM_DISPATCH_TOKEN --repo pr3s3nt/sinhvien   # để CI app dispatch sang platform
```

Xong bước này bạn có: kho ứng dụng `pr3s3nt/sinhvien` (score.yaml FE+API+PostgreSQL,
`.idp/stack.yaml`, `.score-values/values.yaml`, `ci.yaml`) và secret `PLATFORM_DISPATCH_TOKEN`.

> Kho cấu hình `pr3s3nt/idp-sinhvien-config` **không cần tạo tay** — lần deploy đầu, `deploy.yaml`
> chạy `tools/tao-app-moi.sh` (idempotent) tự tạo nó. Bot thiếu quyền tạo repo thì chạy tay:
> `ORG=pr3s3nt APP=sinhvien PLATFORM_REPO=pr3s3nt/idp-platform ./tools/tao-app-moi.sh`.

---

## 3. Biến môi trường và bí mật — `.score-values/values.yaml`

Đây là chỗ **duy nhất** khai cấu hình theo môi trường. Hai tầng: `spec.application` áp cho
mọi môi trường, `spec.environments.<env>` ghi đè.

```yaml
apiVersion: idp.company/v1
kind: ApplicationValues
spec:
  application:
    LOG_LEVEL: "info"
    TEN_TRUONG: "Trường Đại học Bách Khoa"
  environments:
    staging:
      MOI_TRUONG: "staging"
      LOG_LEVEL: "debug"                 # ồn hơn ở staging
      MAX_SINH_VIEN: "20"                # trần thấp để thấy cấu hình có hiệu lực
      TEN_TRUONG: "Trường Đại học Bách Khoa (STAGING)"
      API_KEY:
        secretRef: { name: api-credentials, key: api_key }
    prod:
      MOI_TRUONG: "prod"
      LOG_LEVEL: "info"
      MAX_SINH_VIEN: "5000"
      API_KEY:
        secretRef: { name: api-credentials, key: api_key }
```

Ba luật phải nhớ:

1. **Mọi giá trị literal là CHUỖI.** YAML đọc `yes/no/on/off` thành boolean và `1.10` thành
   `1.1`. Renderer từ chối thay vì tự ép kiểu.
2. **Giá trị bí mật KHÔNG BAO GIỜ nằm ở đây** — file này trong git. Chỉ khai `secretRef`;
   đường dẫn Vault do platform suy ra từ app + môi trường + tên. App không khai được
   mount/path, vì nếu khai được thì app này đọc được secret của app khác.
3. **`prod` chứ không phải `production`.** Một khối đặt sai tên sẽ không áp cho gì cả.

### Nối vào workload

Trong `backend/score.yaml`, khai một resource `environment` rồi tham chiếu:

```yaml
containers:
  api:
    variables:
      LOG_LEVEL:     "${resources.config.LOG_LEVEL}"
      TEN_TRUONG:    "${resources.config.TEN_TRUONG}"
      MOI_TRUONG:    "${resources.config.MOI_TRUONG}"
      MAX_SINH_VIEN: "${resources.config.MAX_SINH_VIEN}"
      API_KEY:       "${resources.config.API_KEY}"    # thành secretKeyRef, không phải value
resources:
  config:
    type: environment
```

`API_KEY` đi vào Deployment dưới dạng `valueFrom.secretKeyRef` — **không có `value`**. Giá
trị thật không đi qua git, không đi qua manifest, không đi qua Score state.

---

## 4. Nạp bí mật vào Vault

```bash
python3 idpctl --env-config platform.env.yaml secret-set \
  --app sinhvien --env staging --name api-credentials --key api_key --stdin --replace
```

- `--replace` cho **lần đầu** (patch không tạo được bản đầu tiên).
- Không có cờ `--value`: tham số dòng lệnh nằm trong shell history và trong `ps` của mọi
  user khác trên máy. Giá trị chỉ vào qua stdin hoặc nhập ẩn.
- Mật khẩu do platform sở hữu thì dùng `--generate` — giá trị không bao giờ được in ra.
- **Bí mật không tự chảy từ staging sang prod.** Prod cần một lần `secret-set --env prod`
  riêng; thiếu thì `VaultStaticSecret` của prod ở `SecretSynced=False` và pod chờ Secret.

---

## 5. Đẩy code, để CI build ảnh

```bash
cd ./sinhvien
git add -A && git commit -m "feat: CRUD sinh viên"
git push origin HEAD:dev
gh run watch -R pr3s3nt/sinhvien
```

CI **hỏi** platform tên ảnh và cách build (`image-plan --with-build`) chứ không tự đoán —
nếu hai bên tính khác nhau thì Fleet apply một ảnh chưa ai đẩy lên.

> **Trước khi merge nhánh platform vào `main`:** mẫu CI checkout platform ở `ref: main`, và
> `repository_dispatch` cũng luôn chạy platform từ `main`. App dùng capability platform chưa có
> trên `main` sẽ nhận workflow gọi lệnh mà `main` chưa có → CI đỏ với `unrecognized arguments`.
> **Thứ tự đúng: merge platform trước, để CI app dispatch sau.** Muốn thử code platform nhánh
> chưa merge thì `gh workflow run deploy.yaml --ref <nhánh>` (xem `HUONG-DAN-KIEM-THU.md`).

---

## 6. Deploy staging

Ảnh đã có trên registry (mục 5) rồi thì deploy đi một trong hai đường, cả hai chạy `deploy.yaml`:

```bash
# A. Tự động: job dispatch của CI app đã bắn repository_dispatch (deploy-request) -> deploy chạy.
# B. Chủ động (AI tự lái / code platform chưa merge): trigger tay với SHA CI vừa build.
gh workflow run deploy.yaml --ref main \
  -f app=sinhvien -f repo=pr3s3nt/sinhvien -f sha=<SHA-CI-vừa-build> -f env=staging
```

Deploy là idempotent (`render` giữ state, `ensure-gitrepo` không ghi đè, commit coi
`AlreadyExists` là xong), nên chạy lại an toàn. Theo dõi bằng tài nguyên thật (không còn
`onboard-status`):

```bash
gh run watch "$(gh run list -R pr3s3nt/idp-platform --workflow deploy.yaml --limit 1 --json databaseId --jq '.[0].databaseId')" -R pr3s3nt/idp-platform
kubectl -n sinhvien-staging get pods,vaultstaticsecret,cluster.postgresql.cnpg.io
kubectl -n fleet-local get gitrepo | grep sinhvien      # phải 1/1
```

---

## 7. Kiểm chứng — đo, đừng tin

```bash
kubectl -n traefik port-forward svc/traefik 18080:80 &
H='Host: sinhvien.staging.internal.dev'; B=http://127.0.0.1:18080

curl -s -H "$H" $B/api/thong-tin
# {"tenTruong":"Trường Đại học Bách Khoa (STAGING)","moiTruong":"staging",
#  "logLevel":"debug","maxSinhVien":20,"apiKeyDaNap":true}

# Bí mật có tác dụng thật: ghi mà không có khoá -> 401
curl -s -X POST -H "$H" -H 'Content-Type: application/json' \
  -d '{"maSv":"SV001","hoTen":"Nguyễn Văn A"}' $B/api/sinh-vien
# {"error":"X-API-Key sai hoặc thiếu"}

curl -s -X POST -H "$H" -H 'X-API-Key: <khoá>' -H 'Content-Type: application/json' \
  -d '{"maSv":"SV001","hoTen":"Nguyễn Văn A","lop":"CNTT-K65"}' $B/api/sinh-vien   # 201
```

Ba thứ đáng kiểm mà người ta hay quên:

```bash
# 1. Bí mật KHÔNG nằm trong manifest đã commit (clone kho config rồi kiểm cả lịch sử)
gh repo clone pr3s3nt/idp-sinhvien-config /tmp/sinhvien-config-check -- --quiet
git -C /tmp/sinhvien-config-check log --all -p | grep -c "<giá trị bí mật>"    # phải 0

# 2. Biến bí mật là secretKeyRef, không phải value
kubectl -n sinhvien-staging get deploy backend -o json \
  | python3 -c "import json,sys; print([e for e in json.load(sys.stdin)['spec']['template']['spec']['containers'][0]['env'] if e['name']=='API_KEY'])"

# 3. Database có phục hồi được không (không phải chỉ 'Ready')
kubectl -n sinhvien-staging get cluster.postgresql.cnpg.io \
  -o jsonpath='{.items[*].status.firstRecoverabilityPoint}'
```

---

## 8. Kích hoạt prod

```bash
python3 idpctl --env-config platform.env.yaml secret-set \
  --app sinhvien --env prod --name api-credentials --key api_key --stdin --replace
gh workflow run deploy.yaml --ref main \
  -f app=sinhvien -f repo=pr3s3nt/sinhvien -f sha=<sha-đã-verify-ở-staging> -f env=prod
```

Deploy `env=prod` ghi vào nhánh prod của kho cấu hình và **mở pull request**; nền tảng không
tự merge (branch protection do GitHub thực thi). Sau khi người duyệt merge, Fleet áp lên prod.
Cách khác: workflow `promote` (`repository_dispatch` type `promote-request`) copy đúng bộ ảnh
staging. Dù đường nào, prod dùng **đúng ảnh đã verify ở staging** — không build lại, không tag khác.

---

## 9. Vận hành sau khi lên

| Việc | Lệnh |
|---|---|
| Xoay vòng bí mật ứng dụng | `secret-set …` rồi VSO tự restart workload (đúng một lần) |
| Xoay vòng mật khẩu database | `rotate-db-credential --app sinhvien --env staging` — **đừng** chỉ ghi vào Vault |
| Kiểm sức khoẻ | `./tools/kiem-suc-khoe.sh --namespace sinhvien-staging` |
| Sự cố | `docs/runbook/` |
| Xoá app | dọn tay theo `docs/runbook/xoa-app-va-giu-du-lieu.md` (không còn lệnh `offboard`) |

**Vì sao xoay vòng mật khẩu database phải dùng lệnh riêng:** ghi mật khẩu mới vào Vault chỉ
làm VSO cập nhật Secret. CNPG chỉ đọc lại `passwordSecret` khi đối tượng Cluster được
reconcile, và pod đang chạy vẫn giữ mật khẩu cũ trong biến môi trường. Làm sai thứ tự sẽ có
một Secret mà database từ chối, và triệu chứng chỉ xuất hiện ở lần restart pod kế tiếp —
nhiều ngày sau, vì một lý do không liên quan.

---

## 10. Những chỗ đã trả giá để biết

- **Đừng render thẳng vào `examples/`** — renderer ghi đè image tag vào `score.yaml`.
- **Mọi lệnh `idpctl` trong CI/script phải truyền `--env-config`**, kể cả lệnh trông
  như không cần toạ độ: thiếu nó thì feature flag vô hình và hai bên tính ra hai kết quả.
- **Quantity của Kubernetes là CHUỖI.** `cpu: 1` (số) làm Fleet báo `Modified` vĩnh viễn
  trong khi cụm chạy đúng — và một bundle luôn đỏ là một bundle không ai còn đọc.
- **`registry-pull` là create-if-missing.** Một cái sai chỉ sửa được bằng cách xoá đi rồi
  tạo lại.
- **Router mount tại `/api`, không phải `/`.** Route chuyển tiếp nguyên đường dẫn và không
  cắt tiền tố; mount sai thì mọi request thành 404 sau khi deploy mới thấy.
- **Không thêm CORS.** Frontend và API cùng origin. Cần CORS nghĩa là routing sai — sửa
  route, đừng sửa header.
