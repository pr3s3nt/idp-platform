# Hướng dẫn: đưa một app mới lên nền tảng, từ đầu đến staging

Tài liệu này đi theo đúng một ví dụ đã chạy thật: **`sinhvien`** — app quản lý sinh viên có
thêm/sửa/xoá, có biến môi trường khác nhau giữa staging và prod, và có một bí mật lấy từ
Vault. Mọi lệnh dưới đây đã được chạy trên harness và cho ra kết quả ghi kèm.

> Nếu bạn chỉ cần tra cứu contract: `HUONG-DAN-CAU-HINH-UNG-DUNG.md` (values/secret) và
> `docs/orchestrator-contract.md` (portal ↔ orchestrator). Tài liệu này là đường đi.

---

## 0. Chuẩn bị một lần

```bash
# Công cụ và cụm phải sẵn sàng — bước này bắt lỗi TRƯỚC khi tạo bất cứ thứ gì.
python3 orchestrate.py --env-config platform.env.yaml \
  preflight --require-cluster --require-vault
```

Bốn biến môi trường mà onboarding cần. **Thiếu chúng thì hỏng ở nơi không nhắc tới chúng**,
nên đặt trước:

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

## 1. Viết file yêu cầu

Bạn khai **ý định**, không khai toạ độ. Không có namespace, không có đường dẫn Vault, không
có StorageClass, không có tài nguyên database thô — nền tảng suy ra hết từ
`platform.env.yaml`.

`sinhvien.request.yaml`:

```yaml
apiVersion: idp.company/v1
kind: OnboardingRequest
application:
  name: sinhvien                 # quyết định tên kho, namespace <app>-<env>, tiền tố Vault
  owner: team-daotao
  description: Quản lý sinh viên — thêm, sửa, xoá
stack:
  id: node-fullstack             # frontend + backend + thư viện dùng chung
  version: 1.0.0
database:
  enabled: true
  profile: application           # CloudNativePG, profile theo môi trường
routing:
  visibility: internal
environments:
  staging: true
  prod: true                     # CHUẨN BỊ contract cho prod, KHÔNG dựng gì ở prod ngay
```

`prod: true` **không** tạo tài nguyên production. Nó chỉ nói "app này sẽ có prod"; việc dựng
xảy ra khi bạn chạy `onboard-activate-prod`, và nó đi qua pull request có người duyệt.

---

## 2. Dựng khung và dừng lại để sửa code

```bash
python3 orchestrate.py --env-config platform.env.yaml onboard \
  --request sinhvien.request.yaml --work ./onboard-sinhvien \
  --images ci --stop-after bootstrap-platform
```

Xong bước này bạn có: kho ứng dụng `pr3s3nt/sinhvien`, kho cấu hình
`pr3s3nt/idp-sinhvien-config`, workflow CI, và secret `PLATFORM_DISPATCH_TOKEN` đã đặt.

`--images ci` nghĩa là **CI của kho ứng dụng build ảnh**, orchestrator chỉ chờ và dùng. Đây
là đường đúng cho app thật (`--images local` chỉ hợp khi thử nhanh trên máy).

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
python3 orchestrate.py --env-config platform.env.yaml secret-set \
  --app sinhvien --env staging --name api-credentials --key api_key --stdin --replace
```

- `--replace` cho **lần đầu** (patch không tạo được bản đầu tiên).
- Không có cờ `--value`: tham số dòng lệnh nằm trong shell history và trong `ps` của mọi
  user khác trên máy. Giá trị chỉ vào qua stdin hoặc nhập ẩn.
- Mật khẩu do platform sở hữu thì dùng `--generate` — giá trị không bao giờ được in ra.
- **Bí mật không tự chảy từ staging sang prod.** Prod cần một lần `secret-set --env prod`
  riêng, và nó sẽ dừng ở `WAITING_FOR_USER_SECRETS` cho tới khi có.

---

## 5. Đẩy code, để CI build ảnh

```bash
cd onboard-sinhvien/app
git add -A && git commit -m "feat: CRUD sinh viên"
git push origin HEAD:dev
gh run watch -R pr3s3nt/sinhvien
```

CI **hỏi** platform tên ảnh và cách build (`image-plan --with-build`) chứ không tự đoán —
nếu hai bên tính khác nhau thì Fleet apply một ảnh chưa ai đẩy lên.

> **Trước khi merge nhánh platform vào `main`:** mẫu CI checkout platform ở `ref: main`. Một
> app onboard từ nhánh phát triển sẽ nhận workflow gọi lệnh mà `main` chưa có → CI đỏ với
> `unrecognized arguments`. Bộ sinh cảnh báo cả hai trường hợp ngay lúc tạo file. **Thứ tự
> đúng: merge trước, onboard sau.**

---

## 6. Deploy staging

```bash
python3 orchestrate.py --env-config platform.env.yaml onboard \
  --request sinhvien.request.yaml --work ./onboard-sinhvien --images ci
```

Chạy lại đúng lệnh cũ là **an toàn**: mỗi bước kiểm-trước-khi-tạo, bước đã xong bị bỏ qua,
không tạo bản sao thứ hai. Nếu thiếu bí mật, nó dừng ở `WAITING_FOR_USER_SECRETS` kèm đúng
lệnh phải chạy — đó là **trạng thái**, không phải lỗi.

Theo dõi:

```bash
python3 orchestrate.py --env-config platform.env.yaml onboard-status --app sinhvien
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
# 1. Bí mật KHÔNG nằm trong manifest đã commit
grep -r "<giá trị bí mật>" onboard-sinhvien/config-staging/    # phải rỗng

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
python3 orchestrate.py --env-config platform.env.yaml secret-set \
  --app sinhvien --env prod --name api-credentials --key api_key --stdin --replace
python3 orchestrate.py --env-config platform.env.yaml \
  onboard-activate-prod --app sinhvien
```

Nó **mở pull request** vào nhánh prod của kho cấu hình và dừng ở `PENDING_PROD_APPROVAL`.
Nền tảng không tự merge. Sau khi người duyệt merge, chạy lại lệnh trên để deploy và verify.

Prod dùng **đúng ảnh đã verify ở staging** — không build lại, không tag khác.

---

## 9. Vận hành sau khi lên

| Việc | Lệnh |
|---|---|
| Xoay vòng bí mật ứng dụng | `secret-set …` rồi VSO tự restart workload (đúng một lần) |
| Xoay vòng mật khẩu database | `rotate-db-credential --app sinhvien --env staging` — **đừng** chỉ ghi vào Vault |
| Kiểm sức khoẻ | `./tools/kiem-suc-khoe.sh --namespace sinhvien-staging` |
| Sự cố | `docs/runbook/` |
| Xoá app | `offboard --app sinhvien --env staging` (mặc định chỉ xem trước) |

**Vì sao xoay vòng mật khẩu database phải dùng lệnh riêng:** ghi mật khẩu mới vào Vault chỉ
làm VSO cập nhật Secret. CNPG chỉ đọc lại `passwordSecret` khi đối tượng Cluster được
reconcile, và pod đang chạy vẫn giữ mật khẩu cũ trong biến môi trường. Làm sai thứ tự sẽ có
một Secret mà database từ chối, và triệu chứng chỉ xuất hiện ở lần restart pod kế tiếp —
nhiều ngày sau, vì một lý do không liên quan.

---

## 10. Những chỗ đã trả giá để biết

- **Đừng render thẳng vào `examples/`** — renderer ghi đè image tag vào `score.yaml`.
- **Mọi lệnh `orchestrate.py` trong CI/script phải truyền `--env-config`**, kể cả lệnh trông
  như không cần toạ độ: thiếu nó thì feature flag vô hình và hai bên tính ra hai kết quả.
- **Quantity của Kubernetes là CHUỖI.** `cpu: 1` (số) làm Fleet báo `Modified` vĩnh viễn
  trong khi cụm chạy đúng — và một bundle luôn đỏ là một bundle không ai còn đọc.
- **`registry-pull` là create-if-missing.** Một cái sai chỉ sửa được bằng cách xoá đi rồi
  tạo lại.
- **Router mount tại `/api`, không phải `/`.** Route chuyển tiếp nguyên đường dẫn và không
  cắt tiền tố; mount sai thì mọi request thành 404 sau khi deploy mới thấy.
- **Không thêm CORS.** Frontend và API cùng origin. Cần CORS nghĩa là routing sai — sửa
  route, đừng sửa header.
