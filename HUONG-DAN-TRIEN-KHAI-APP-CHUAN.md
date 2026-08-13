# Triển khai một app từ onboarding tới staging — đường chuẩn

Viết lại sau một lần chạy thật (app `dangky`, 2026-08-11) trong đó người chạy đã đi vòng ở
bước cuối và phải làm lại. Tài liệu này là đường thẳng, cộng với những chỗ dễ đi vòng và lý
do không được đi.

Tài liệu này nói **hình dạng đúng của quy trình** — cái gì là toạ độ, đặt ở đâu, và vì sao
không có bước nào được dùng `kubectl apply` tay.

---

## Nguyên tắc duy nhất cần nhớ

> **Mọi thứ chạy trên cụm phải tới từ git, qua Fleet. Không có ngoại lệ "chỉ để test".**

Từ đó suy ra ba hệ quả, và gần như mọi lỗi trong quy trình này là vi phạm một trong ba:

1. **Toạ độ ở config, không ở lệnh.** Tên miền, replicas, storage class, giới hạn tài
   nguyên — khai trong `platform.env.yaml` (theo môi trường) hoặc `.score-values/values.yaml`
   (theo app). Không bao giờ `kubectl` một tài nguyên vào cụm để "cho nhanh".
2. **Bí mật ở Vault, tham chiếu ở git.** Manifest chỉ chứa `secretRef`/`secretKeyRef`.
3. **Ảnh do CI build, orchestrator chỉ chọn.** Không `docker build` trên máy.

Kiểm nguyên tắc 1 bất cứ lúc nào — liệt kê tài nguyên **không** do Fleet quản và **không** do
controller nào sinh ra:

```bash
kubectl -n <app>-staging get deploy,svc,httproute,cm,secret,cluster.postgresql.cnpg.io,vaultstaticsecret -o json \
  | python3 -c "
import json,sys
for i in json.load(sys.stdin)['items']:
    m = i['metadata']
    if m.get('ownerReferences'): continue                              # controller sinh
    if 'objectset.rio.cattle.io/hash' in (m.get('labels') or {}): continue   # Fleet quản
    print(f\"  {i['kind']}/{m['name']}\")"
```

Bỏ `ownerReferences` là bắt buộc — không lọc thì Pod, ReplicaSet và các Service của CNPG lấp
đầy output và cái check thành vô dụng.

Namespace sạch trả về **đúng sáu dòng này, không hơn**:

```text
ConfigMap/cnpg-default-monitoring                      # CNPG operator
ConfigMap/kube-root-ca.crt                             # Kubernetes
Secret/backup-object-store                             # orchestrator, cố ý (xem dưới)
Secret/registry-pull                                   # orchestrator, cố ý
Secret/sh.helm.release.v1.<app>-staging-staging.v1     # bản ghi Helm của Fleet
Secret/sh.helm.release.v1.<app>-staging-staging.v2
```

Hai Secret của orchestrator **là ngoại lệ có chủ ý**, không phải vi phạm: split-manifest đẩy
bí mật thẳng lên cụm chứ không qua git, đúng bất biến "secret không bao giờ vào manifest công
khai". Số bản ghi Helm tăng theo mỗi lần deploy.

Bất kỳ dòng nào **ngoài** danh sách trên = ai đó đã `kubectl apply` tay. Thứ đó không có
trong git, sẽ biến mất trong lần dựng lại cụm tiếp theo, và không ai hiểu vì sao app hỏng.

---

## 0. Quyết định TRƯỚC khi gõ lệnh đầu tiên

Ba câu hỏi này quyết định file yêu cầu, và **đổi sau khi đã deploy thì tốn một vòng CI +
deploy**. Trả lời cho xong ở đây.

| Câu hỏi | Ảnh hưởng | Đổi sau có đắt không |
|---|---|---|
| Tên app | namespace, tên kho, hostname, đường dẫn Vault | **Rất đắt** — thực tế là làm lại từ đầu |
| Có database không | CNPG Cluster, backup, credential | Đắt — thêm được, bỏ thì mất dữ liệu |
| **Staging phải mở được từ trình duyệt nào** | `PUBLIC_HOST` | Rẻ, nhưng tốn 1 vòng CI + deploy |

Câu thứ ba là chỗ lần chạy `dangky` đi vòng. Đọc kỹ mục 3.

---

## 1. Điều kiện tiên quyết

```bash
python3 idpctl --env-config platform.env.yaml preflight --require-cluster --require-vault
```

Phải in `preflight OK`.

Tám biến môi trường phải có **trước** lệnh đầu tiên. Thiếu bất kỳ cái nào cũng hỏng ở một
nơi không nhắc tới nó:

| Biến | Thiếu thì hỏng ở đâu | Triệu chứng lừa người |
|---|---|---|
| `VAULT_ADDR`, `VAULT_TOKEN` | nạp bí mật | — |
| `GH_TOKEN` | tạo kho | — |
| `APP_DISPATCH_TOKEN` | secret của kho app | CI đỏ ở `actions/checkout`, không nhắc token |
| `REGISTRY_USER`, `REGISTRY_PASS` | `registry-pull` | pod `ImagePullBackOff` + registry `403` |
| `BACKUP_ACCESS_KEY_ID`, `BACKUP_ACCESS_SECRET_KEY` | WAL archiving | Cluster báo `Ready` **và vẫn không phục hồi được** |

Hai dòng cuối là loại tệ nhất: hệ thống báo xanh trong khi đã hỏng. Luôn kiểm bằng
`firstRecoverabilityPoint`, đừng tin `Ready`.

Cấu hình dùng cho lần chạy: nếu `platform.env.yaml` đã commit đang tắt feature flag, tạo một
**bản sao ngoài kho** bật cờ và trỏ kho object, rồi truyền `--env-config` **cho mọi lệnh**,
kể cả lệnh trông như không cần. Thiếu nó thì hai bên tính ra hai kết quả cho cùng một commit.

---

## 2. Nếu tính năng chưa merge vào `main`

CI mà platform sinh ra checkout code platform ở `ref: main` — **cố ý**, để CI và orchestrator
dùng chung một bản renderer. Hệ quả: tính năng chưa ở `main` thì app mới nhận một workflow
gọi lệnh `main` chưa có.

```bash
git show main:idpctl | grep -c "with-build"
git show main:platform.env.yaml | grep stack_onboarding
```

Có cả hai → bỏ qua mục này. Không → làm hai việc:

**2.1.** Đẩy một nhánh platform mô phỏng sau-merge (bật cờ trong `platform.env.yaml`), rồi
**quay lại nhánh làm việc**. Mục 4 sẽ ghim `ci.yaml` của kho app vào nhánh này.

**2.2.** Tắt workflow `deploy` của kho platform:

```bash
gh workflow disable deploy.yaml -R <org>/<kho-platform>
```

Job cuối của CI bắn `repository_dispatch`; GitHub chạy workflow đó **từ nhánh mặc định**, tức
code của `main`. Không tắt thì **mỗi lần push đều để lại một run đỏ** trên kho platform.

> **Bật lại lúc nào**: sau khi đã đẩy code app xong hẳn (hết mục 5), **không phải** ngay sau
> khi verify xong. Trong lần chạy `dangky`, orchestrator được bật lại rồi mới push thêm một
> commit → sinh đúng một run đỏ:
> `resource 'environment.default#backend.config' is not supported by any provisioner`.
> Nó chết ở bước render, trước khi chạm kho cấu hình, nên vô hại — nhưng vẫn là rác.

---

## 3. File yêu cầu — khai ý định, không khai toạ độ

```yaml
apiVersion: idp.company/v1
kind: OnboardingRequest
application:
  name: dangky
  owner: team-dangky
  description: ...
stack:
  id: node-fullstack          # 1 backend + 1 frontend + thư viện dùng chung
  version: 1.0.0
database:
  enabled: true
  profile: application        # CloudNativePG quản
routing:
  visibility: internal
environments:
  staging: true
  prod: true                  # CHUẨN BỊ contract, KHÔNG dựng gì ở prod
```

Không namespace, không đường dẫn Vault, không StorageClass — platform suy ra hết.

Kiểm-trước-khi-tạo, cả ba chỗ:

```bash
kubectl get ns | grep -w "$APP-staging"
gh repo view <org>/$APP
gh repo view <org>/idp-$APP-config
```

### 3.1. `PUBLIC_HOST` — chỗ dễ đi vòng nhất

Platform sinh hostname từ `templates/stacks/<stack>.stack.yaml`:

```yaml
values:
  environments:
    staging: {PUBLIC_HOST: "__APP__.__DOMAIN_STAGING__"}   # <- platform.env.yaml
    prod:    {PUBLIC_HOST: "__APP__.__DOMAIN_PROD__"}
localValues:
  PUBLIC_HOST: "__APP__.localhost"                          # <- .env.example, `make dev`
```

Ra `dangky.staging.internal.dev`. Ở công ty thật, một bản ghi DNS wildcard
`*.staging.internal.dev` trỏ về ingress lo phần phân giải, và lập trình viên mở URL là xong.

**Trên harness kind thì không có DNS đó.** Ba đường, và chỉ một đường là chuẩn:

| Cách | Chuẩn? | Vì sao |
|---|---|---|
| Sửa `PUBLIC_HOST` staging thành `<app>.127.0.0.1.nip.io` | ✅ | nip.io phân giải công khai về 127.0.0.1. Đi qua git, Fleet quản, mọi máy vào được, không cần cấu hình client. Đây là cách `demo-staging` và `helloworld-staging` đang dùng |
| hosts file của từng máy | ⚠️ | Chạy được, nhưng là cấu hình thủ công trên từng máy, không có trong git, người mới join không biết |
| `kubectl apply` một HTTPRoute `<app>.localhost` | ❌ | Trạng thái ngoài git, ngoài Fleet. `*.localhost` là `localValues` — dành cho `make dev`, không dành cho cụm. Mất khi dựng lại cụm |

Cách thứ ba là cái đã bị làm sai trong lần chạy `dangky`. Fleet **không** báo drift vì tài
nguyên đó không nằm trong bundle nên không có gì để so sánh. *"Không ai phàn nàn"* không phải
*"sạch"*.

Quyết ngay ở đây, vì đổi sau khi deploy tốn một vòng CI + deploy (mục 6):

```yaml
environments:
  staging:
    PUBLIC_HOST: dangky.127.0.0.1.nip.io    # harness: mở được từ mọi trình duyệt
  prod:
    PUBLIC_HOST: dangky.prod.internal.dev   # hình dạng công ty, giữ nguyên
```

Prod giữ nguyên hình dạng công ty là **cố ý**: prod không cần mở từ máy ai, và nó phải trông
đúng như thật.

---

## 4. Sinh khung, rồi DỪNG để viết code

```bash
python3 idpctl --env-config "$CFG" onboard \
  --request /tmp/$APP.request.yaml --work /tmp/onboard-$APP \
  --images ci --stop-after bootstrap-platform
```

`--images ci` = CI của kho app build ảnh, orchestrator chỉ chờ rồi dùng.

Xong phải thấy `[OK] validate`, `[OK] scaffold-repository`, `[OK] bootstrap-platform`, hai
kho GitHub, và secret `PLATFORM_DISPATCH_TOKEN` trên kho app.

Nếu ở TRƯỚC MERGE, ghim `ci.yaml` **của kho app** vào nhánh tạm — **không sửa `templates/`
của kho platform**:

```bash
sed -i "s|^\(\s*\)ref: main$|\1ref: $PIN_BRANCH|" .github/workflows/ci.yaml
```

> Đây là **nợ kỹ thuật có hạn trả**: sau khi merge phải trả về `ref: main`, nếu không CI của
> app ghim vĩnh viễn vào một nhánh tạm và gãy ngay khi ai đó xoá nhánh đó.

---

## 5. Cấu hình theo môi trường và bí mật

### 5.1. `.score-values/values.yaml` — chỗ duy nhất

**THÊM khoá vào file đang có, không ghi đè.** Khung sinh sẵn `PUBLIC_HOST` và
`frontend/score.yaml` đang tham chiếu nó; ghi đè mất khoá đó thì render fail.

```yaml
spec:
  application:            # áp cho mọi môi trường
    LOG_LEVEL: "info"
  environments:
    staging:
      PUBLIC_HOST: "dangky.127.0.0.1.nip.io"
      MOI_TRUONG: "staging"
      GIOI_HAN: "20"
      API_KEY: {secretRef: {name: api-credentials, key: api_key}}
    prod:
      PUBLIC_HOST: "dangky.prod.internal.dev"
      MOI_TRUONG: "prod"
      GIOI_HAN: "5000"
      API_KEY: {secretRef: {name: api-credentials, key: api_key}}
```

Ba luật, vi phạm là render fail:

1. **Mọi literal là CHUỖI.** YAML đọc `yes/no/on/off` thành boolean, `1.10` thành `1.1`.
   Renderer từ chối thay vì tự ép kiểu.
2. **Không đặt giá trị bí mật ở đây** — file này trong git. Chỉ `secretRef`.
3. Tên môi trường là **`prod`**, không phải `production`.

### 5.2. Nối vào workload

`backend/score.yaml` đã có resource `config` kiểu `environment`. Thêm biến vào
`containers.api.variables` dưới dạng `${resources.config.<TÊN>}`.

### 5.3. Cho bí mật một việc THẬT

Một bí mật chỉ để in ra là một bí mật không ai xoay vòng. Bắt mọi thao tác **ghi** phải kèm nó:

```js
const API_KEY = process.env.API_KEY || "";     // không có giá trị mặc định
function kiemTraApiKey(req, res, next) {
  // 503 chứ không 401: khoá chưa nạp là lỗi NỀN TẢNG, không phải người gọi sai.
  if (!API_KEY) return res.status(503).json({ error: "API_KEY chưa được nạp" });
  const gui = String(req.get("X-API-Key") || "");
  if (gui.length !== API_KEY.length || gui !== API_KEY) {
    return res.status(401).json({ error: "X-API-Key sai hoặc thiếu" });
  }
  next();
}
api.post("/items", kiemTraApiKey, async (req, res) => { /* ... */ });
```

Endpoint chẩn đoán **không bao giờ trả giá trị bí mật**, chỉ trả việc nó đã nạp hay chưa:

```js
api.get("/thong-tin", (_req, res) => res.json({
  moiTruong: process.env.MOI_TRUONG,
  gioiHan: Number(process.env.GIOI_HAN || 0),
  apiKeyDaNap: Boolean(API_KEY),      // BOOLEAN, không phải giá trị
}));
```

Biến theo môi trường cũng nên **được dùng thật**, không chỉ báo cáo lại — `GIOI_HAN` làm
`LIMIT` của truy vấn chẳng hạn. Biến không ai đọc thì sai lệch giữa hai môi trường không lộ
ra cho tới lúc muộn.

### 5.4. Nạp bí mật vào Vault

```bash
python3 idpctl --env-config "$CFG" secret-set \
  --app $APP --env staging --name api-credentials --key api_key --stdin --replace <<< "..."
```

- `--replace` cho **lần đầu** (patch không tạo được bản đầu tiên).
- **Không có cờ `--value`, và đó là cố ý**: tham số dòng lệnh nằm trong shell history và
  trong `ps`.
- Bí mật **không** tự chảy sang prod; prod cần một lần `--env prod` riêng.

---

## 6. Đẩy code, chờ CI build ảnh

```bash
git add -A && git commit -m "..." && git push origin HEAD:dev
RUN=$(gh run list -R <org>/$APP --limit 1 --json databaseId --jq '.[0].databaseId')
gh run watch "$RUN" -R <org>/$APP
```

Cả 4 job phải `success`: `plan`, `build (backend…)`, `build (frontend…)`, `dispatch`. **Ghi
lại tag ảnh** trong tên job build — mục 7 phải khớp.

Trước khi commit, kiểm không rò rỉ:

```bash
git diff --cached | grep -c "<giá-trị-bí-mật>"     # phải là 0
```

---

## 7. Deploy

```bash
python3 idpctl --env-config "$CFG" onboard \
  --request /tmp/$APP.request.yaml --work /tmp/onboard-$APP --images ci
```

Chạy lại đúng lệnh cũ **luôn an toàn**: mỗi bước kiểm-trước-khi-tạo, bước xong bị bỏ qua.

Trong log phải có, và đây là bằng chứng ảnh tới từ CI:

```text
tag_strategy=commit (from .idp/stack.yaml)
mọi ảnh đã có trên registry -> không build lại
```

Thấy `docker build` = `--images ci` không có hiệu lực.

### 7.1. Sửa code sau khi đã deploy một lần

`build-images` đã ghi commit cũ vào state, và `deploy-staging` **cố ý** render đúng commit đã
build (render theo đỉnh nhánh sẽ trỏ tới ảnh chưa ai đẩy). Phải ép lại:

```bash
--force-step build-images --force-step deploy-staging --force-step verify-staging
```

> **Ép một bước là không đủ.** Ép mỗi `build-images` thì orchestrator chạy xong bước đó rồi
> dừng, vì các bước sau vẫn `done`. Triệu chứng: lệnh `EXIT=0`, log trông sạch, mà cụm không
> đổi gì. Liệt kê hết các bước cần chạy lại.

---

## 8. Kiểm chứng — đo, đừng tin

```bash
export NS=$APP-staging
python3 idpctl --env-config "$CFG" onboard-status --app $APP
kubectl -n $NS get pods
kubectl -n $NS get vaultstaticsecret -o custom-columns=NAME:.metadata.name,SYNCED:.status.conditions[0].status
kubectl -n $NS get cluster.postgresql.cnpg.io -o jsonpath='{.items[*].status.firstRecoverabilityPoint}{"\n"}'
kubectl -n fleet-local get gitrepo | grep $APP
./tools/kiem-suc-khoe.sh --namespace $NS
```

- `firstRecoverabilityPoint` **rỗng = database KHÔNG phục hồi được**, kể cả khi Cluster
  `Ready` và `ContinuousArchiving=True`.
- Đọc `.status.conditions[].status`, **đừng đọc `reason`** — `reason` là tên loại điều kiện,
  không phải kết quả.

HTTP (với `PUBLIC_HOST` nip.io thì mở thẳng, không header, không hosts file):

```bash
B=http://$APP.127.0.0.1.nip.io:18080
curl -s -o /dev/null -w "%{http_code}\n" $B/                 # 200
curl -s $B/api/thong-tin                                      # đúng giá trị của staging
curl -s -w " %{http_code}\n" -X POST -H 'Content-Type: application/json' \
  -d '{"label":"x"}' $B/api/items                             # 401 — không khoá
curl -s -w " %{http_code}\n" -X POST -H 'X-API-Key: ...' -H 'Content-Type: application/json' \
  -d '{"label":"x"}' $B/api/items                             # 201 — có khoá
```

Và bí mật không được nằm trong git — kiểm cả **lịch sử**, không chỉ cây làm việc:

```bash
git -C /tmp/onboard-$APP/config-staging log --all -p | grep -c "<giá-trị>"   # 0
```

Cuối cùng, kiểm không có tài nguyên thủ công (script ở đầu tài liệu này), và **bật lại
workflow deploy** nếu đã tắt ở mục 2.2.

---

## 9. Bẫy đã biết

| # | Triệu chứng | Nguyên nhân thật | Xử lý |
|---|---|---|---|
| B1 | CI đỏ `unrecognized arguments: --with-build` | `ref: main` mà `main` chưa có tính năng | mục 2.1 + 4 |
| B2 | CI xanh, orchestrator vẫn báo thiếu ảnh | CI tính tag `content`, orchestrator tính `commit` | ghim vào nhánh có cờ BẬT |
| B3 | CI đỏ ở `actions/checkout` | thiếu `PLATFORM_DISPATCH_TOKEN` | đặt `APP_DISPATCH_TOKEN`, chạy lại mục 4 |
| B4 | `firstRecoverabilityPoint` rỗng mãi | thiếu `BACKUP_ACCESS_*` | đặt biến; **Cluster tạo trước khi có Secret thì phải xoá pod instance** (instance manager cache lúc khởi động) |
| B5 | `ImagePullBackOff`, registry `403` | thiếu `REGISTRY_USER/PASS` | đặt biến; `registry-pull` đã tồn tại và sai thì **xoá rồi tạo lại** — nó là create-if-missing |
| B6 | `CreateContainerConfigError`, VSS `empty response from Vault` | chưa nạp bí mật | mục 5.4, nhớ `--replace` |
| B7 | VSS `permission denied` | chưa onboard vào Vault | `vault-onboard --app $APP --env staging` |
| B8 | VSS `False` nhưng `reason` là `Synced` | `reason` là TÊN LOẠI điều kiện | đọc `.status.conditions[].status` |
| B9 | VSS `False` ~2 phút sau khi Vault dựng lại | VSO backoff | **chờ**; xoá `VaultStaticSecret` sẽ thu hồi luôn Secret đích |
| B10 | Fleet `Modified` vĩnh viễn | quantity ghi bằng SỐ, Kubernetes lưu CHUỖI | nháy kép mọi quantity |
| B11 | `render` dừng: "đổi class … KHÔNG di chuyển dữ liệu" | đang đổi `class` postgres trên database đã có dữ liệu | `docs/chuyen-doi-postgres-sang-class-application.md` |
| B12 | Mọi `/api/...` trả HTML của frontend | router mount tại `/` thay vì `/api` | route chuyển tiếp nguyên đường dẫn |
| B13 | Sửa code, chạy lại `onboard`, lỗi cũ quay lại | `build-images` đã ghi commit cũ vào state | `--force-step` cho **cả chuỗi** (mục 7.1) |
| **B14** | Không mở được app bằng trình duyệt | không có DNS cho `*.staging.internal.dev` trên harness | **sửa `PUBLIC_HOST` (mục 3.1)** — đừng `kubectl apply` HTTPRoute tay |
| **B15** | `--force-step X`, `EXIT=0`, cụm không đổi | các bước sau X vẫn `done` nên bị bỏ qua | liệt kê hết các bước cần chạy lại |
| **B16** | Run `repository_dispatch` đỏ trên kho platform | orchestrator bật lại quá sớm, chạy bằng code `main` | bật lại **sau** khi push code app xong hẳn |

**Không bao giờ làm:** sửa `templates/` của kho platform để CI chạy được; `kubectl apply` một
tài nguyên vào namespace app; nới một cổng kiểm; xoá tài nguyên để test pass; đưa giá trị bí
mật vào git hoặc vào log.

---

## 10. Sau khi lên staging

| Việc | Lệnh |
|---|---|
| Kích hoạt prod | nạp secret `--env prod`, rồi `onboard-activate-prod --app $APP` → PR, chờ duyệt |
| Xoay vòng bí mật app | `secret-set …` — VSO restart workload đúng một lần |
| Xoay vòng mật khẩu DB | `rotate-db-credential --app $APP --env staging` — **đừng** chỉ ghi vào Vault |
| Trả nợ trước-merge | `ci.yaml` về `ref: main`; xoá nhánh tạm |
| Sự cố | `docs/runbook/` |
