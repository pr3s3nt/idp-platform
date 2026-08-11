# Hướng dẫn cho AI Agent: tạo một app mới và đưa nó lên staging

**Đối tượng đọc: một AI coding agent.** Làm theo đúng thứ tự. Mỗi bước có **CỔNG KIỂM** —
nếu cổng không đạt thì DỪNG và đọc mục "Bẫy đã biết" ở cuối, đừng đi tiếp và đừng nới cổng.

## 0. Mục tiêu và tiêu chí thành công

Tạo một app mới gồm **1 backend + 1 frontend + 1 database PostgreSQL**, có **biến môi
trường khác nhau giữa staging và prod** và có **một bí mật lấy từ Vault**, rồi đưa nó chạy
thật trên staging. **Ảnh container do GitHub Actions build**, không build tay.

Coi là THÀNH CÔNG khi tất cả các mệnh đề sau đúng:

| # | Tiêu chí | Cách kiểm |
|---|---|---|
| S1 | CI trên GitHub Actions xanh cả 4 job | `gh run view <id> --json jobs` |
| S2 | Orchestrator dùng ảnh của CI, **không** build lại | log có `mọi ảnh đã có trên registry -> không build lại` |
| S3 | Trạng thái onboarding đạt `STAGING_READY` hoặc `PENDING_PROD_ACTIVATION` | `onboard-status` |
| S4 | 3 pod Running: backend, frontend, database | `kubectl get pods` |
| S5 | Cả hai `VaultStaticSecret` đều `SecretSynced=True` | `kubectl get vaultstaticsecret` |
| S6 | Database phục hồi được (`firstRecoverabilityPoint` khác rỗng) | `kubectl get cluster.postgresql.cnpg.io` |
| S7 | GitRepo của Fleet `1/1` | `kubectl -n fleet-local get gitrepo` |
| S8 | HTTP: `/` → 200, `/api/health` → 200 | `curl` qua Gateway |
| S9 | Biến môi trường theo môi trường có hiệu lực | `/api/thong-tin` trả giá trị của staging |
| S10 | Bí mật có tác dụng: ghi **không** kèm khoá → 401, có khoá → 201 | `curl` |
| S11 | Giá trị bí mật **không** nằm trong manifest đã commit | `grep` trả rỗng |

---

## 0.5. NHẬT KÝ CÀI ĐẶT — ghi trong suốt quá trình, không phải viết lại lúc cuối

Mở một file nhật ký **ngay bây giờ**, trước lệnh đầu tiên, và ghi vào đó **liên tục**:

```bash
export LOG=/tmp/nhat-ky-cai-dat-$APP.md
{ echo "# Nhật ký cài đặt $APP"; echo; echo "- Bắt đầu: $(date -Is)"; echo "- Máy: $(hostname)";
  echo "- Nhánh platform: $(git branch --show-current) @ $(git rev-parse --short HEAD)"; echo; } > "$LOG"
```

Sau **mỗi mục** của hướng dẫn này, nối thêm một khối theo đúng khuôn:

```markdown
## Mục <số> — <tên mục>
- Thời điểm: 2026-08-11T12:34:56+07:00
- Lệnh đã chạy:
  ```
  <lệnh nguyên văn>
  ```
- Mã thoát: 0
- CỔNG KIỂM <n>: ĐẠT
- Bằng chứng:
  ```
  <output thật, cắt gọn nhưng KHÔNG sửa>
  ```
```

Ba luật của nhật ký:

1. **Ghi ngay sau khi chạy, không ghi lại từ trí nhớ lúc cuối.** Nếu tiến trình chết giữa
   chừng thì thứ còn lại phải đủ để người khác dựng lại được chuyện gì đã xảy ra.
2. **Không bao giờ ghi giá trị bí mật vào nhật ký.** Ghi tên khoá, đường dẫn Vault, độ dài
   — không ghi giá trị. Nhật ký này sẽ được đọc lại và có thể được chia sẻ.
3. **Output lỗi phải ghi NGUYÊN VĂN, đầy đủ**, không tóm tắt thành "lỗi gì đó". Dòng lỗi
   thật là toàn bộ giá trị của nhật ký.

### DỪNG NGAY khi có lỗi — không đi tiếp, không tự chữa cháy

Khi một lệnh thoát khác 0 hoặc một CỔNG KIỂM không đạt:

1. Ghi vào nhật ký một khối `## ❌ DỪNG TẠI MỤC <số>` gồm: lệnh, mã thoát, **toàn bộ**
   output lỗi, trạng thái hiện tại (`onboard-status`, `kubectl get pods`, `kubectl get
   vaultstaticsecret`), và mã bẫy tương ứng ở mục 10 nếu tra được.
2. **DỪNG.** Không chạy bước sau. Không thử một cách khác. Không xoá tài nguyên để "làm
   sạch rồi làm lại".
3. Chỉ được chạy lại **đúng lệnh cũ** sau khi đã sửa **nguyên nhân** đã ghi ở bước 1, và
   phải ghi rõ trong nhật ký: đã sửa gì, vì sao tin là nguyên nhân đó.

Vì sao nghiêm ngặt: mọi bước ở đây đều kiểm-trước-khi-tạo, nên **dừng lại là an toàn** —
chạy lại sẽ tiếp tục từ đúng chỗ. Cái không an toàn là đi tiếp khi một cổng đã đỏ: lỗi thật
bị chôn dưới ba lỗi kế tiếp, và tài nguyên nửa vời thì khó gỡ hơn nhiều so với một lần dừng
sạch sẽ.

### Khi kết thúc

Nối vào cuối nhật ký:

- Bảng **S1..S11**, mỗi dòng ĐẠT/KHÔNG kèm bằng chứng.
- **Đã tạo những gì** (kho GitHub, namespace, đường dẫn Vault, GitRepo, ảnh trên registry).
- **Đã dọn những gì**, hoặc ghi rõ cái gì được giữ lại và vì sao.
- Thời gian tổng, và bước nào tốn thời gian nhất.

Đường dẫn nhật ký phải được nêu trong báo cáo cuối.

---

## 1. Điều kiện tiên quyết

Chạy từ gốc kho `idp-platform`.

```bash
python3 orchestrate.py --env-config platform.env.yaml \
  preflight --require-cluster --require-vault
```

**CỔNG KIỂM 1:** in ra `preflight OK`. Nếu không: thiếu công cụ, cụm không tới được, hoặc
Vault/VSO chưa dựng. Dựng bằng `./tools/dung-vault-harness.sh`,
`./tools/dung-database-harness.sh`, `./tools/dung-object-store-harness.sh`.

### Biến môi trường — đặt HẾT trước khi bắt đầu

Thiếu bất kỳ biến nào ở đây đều hỏng ở **một nơi không nhắc tới nó**. Đây là nguồn lỗi số
một của quy trình này.

```bash
# Vault: cần port-forward vì vault.address trong config là địa chỉ CỤM nhìn thấy.
kubectl -n vault port-forward svc/vault 8200:8200 >/dev/null 2>&1 &
sleep 4
export VAULT_ADDR=http://127.0.0.1:8200
export VAULT_TOKEN=<token có policy ghi>          # harness dev mode: root

# GitHub: PAT cần scope repo + write:packages.
export GH_TOKEN=<PAT>
export APP_DISPATCH_TOKEN="$GH_TOKEN"             # để onboarding đặt secret cho kho app

# Registry: thiếu -> KHÔNG tạo registry-pull -> ảnh không kéo được, lỗi là 403, không
# nhắc gì tới biến này.
export REGISTRY_USER=<user registry>
export REGISTRY_PASS="$GH_TOKEN"

# Kho object: thiếu -> WAL archiving hỏng -> không có base backup -> S6 không bao giờ đạt.
export BACKUP_ACCESS_KEY_ID="$(kubectl -n object-store get secret minio-root \
  -o jsonpath='{.data.MINIO_ROOT_USER}' | base64 -d)"
export BACKUP_ACCESS_SECRET_KEY="$(kubectl -n object-store get secret minio-root \
  -o jsonpath='{.data.MINIO_ROOT_PASSWORD}' | base64 -d)"
```

**CỔNG KIỂM 2:**

```bash
for v in VAULT_ADDR VAULT_TOKEN GH_TOKEN APP_DISPATCH_TOKEN REGISTRY_USER REGISTRY_PASS \
         BACKUP_ACCESS_KEY_ID BACKUP_ACCESS_SECRET_KEY; do
  [ -n "${!v}" ] && echo "  $v ok" || echo "  $v THIẾU"
done
curl -s -o /dev/null -w "vault %{http_code}\n" -H "X-Vault-Token: $VAULT_TOKEN" \
  "$VAULT_ADDR/v1/sys/health"
```

Cả 8 biến phải `ok` và Vault phải trả `200`.

### Cấu hình dùng cho lần chạy này

`platform.env.yaml` đã commit có `features.* = false` và không có kho object. E2E cần một
**BẢN SAO** bật cờ, để ngoài kho, **KHÔNG commit**:

```bash
python3 - <<'EOF'
import yaml
d = yaml.safe_load(open('platform.env.yaml'))
d['features'] = {k: True for k in d['features']}
d['database']['backup'].update({
    'object_store_url': 's3://idp-backup/',
    'endpoint_url': 'http://minio.object-store.svc.cluster.local:9000',
    'credentials_secret': 'backup-object-store',
})
with open('/tmp/platform.env.e2e.yaml', 'w') as f:
    yaml.safe_dump(d, f, sort_keys=False, allow_unicode=True)
EOF
export CFG=/tmp/platform.env.e2e.yaml
```

> **Mọi lệnh `orchestrate.py` từ đây phải truyền `--env-config "$CFG"`**, kể cả lệnh trông
> như không cần toạ độ. Thiếu nó thì feature flag vô hình và hai bên tính ra hai kết quả.

---

## 2. TRƯỚC KHI MERGE — bước bắt buộc, bỏ qua là CI đỏ

Đọc kỹ mục này. Nó là khác biệt lớn nhất giữa "chạy thử hôm nay" và "chạy thật sau merge".

Mẫu CI mà platform sinh ra checkout code platform ở **`ref: main`** — cố ý, để CI và
orchestrator dùng cùng một bản renderer. Hệ quả: nếu tính năng onboarding **chưa được merge
vào `main`**, app mới sẽ nhận một workflow gọi lệnh mà `main` chưa có, và CI đỏ ngay với
`unrecognized arguments: --with-build`. Thêm nữa, `platform.env.yaml` trên `main` còn tắt
`features.stack_onboarding`, nên CI tính tag `content` trong khi orchestrator tính `commit`
— hai tag khác nhau cho cùng một commit.

**Kiểm xem bạn đang ở tình huống nào:**

```bash
git show main:orchestrate.py | grep -c "with-build" || echo 0
git show main:platform.env.yaml | grep -A5 "^features:" | grep stack_onboarding
```

- Nếu `main` **đã có** `with-build` **và** `stack_onboarding: true` → bạn ở **SAU MERGE**.
  **Bỏ qua toàn bộ mục 2** và đi tiếp mục 3.
- Ngược lại → bạn ở **TRƯỚC MERGE**. Làm hai việc dưới đây.

### 2.1. Tạo một nhánh platform mô phỏng trạng thái sau-merge

```bash
export PIN_BRANCH=test/agent-onboard-$(date +%s)
git checkout -b "$PIN_BRANCH"
python3 - <<'EOF'
p='platform.env.yaml'; s=open(p).read()
s=s.replace("  stack_onboarding: false","  stack_onboarding: true")
open(p,'w').write(s)
EOF
git add platform.env.yaml
git commit -q -m "test(tạm): bật stack_onboarding để mô phỏng trạng thái sau merge"
git push -q origin "$PIN_BRANCH"
git checkout -   # QUAY LẠI nhánh làm việc — đừng ở lại nhánh tạm
```

**CỔNG KIỂM 3:** `git branch --show-current` **không** phải `$PIN_BRANCH`, và
`git ls-remote --heads origin "$PIN_BRANCH" | wc -l` trả `1`.

### 2.2. Tắt tạm workflow orchestrator của kho platform

Job cuối của CI gửi `repository_dispatch` tới kho platform. GitHub chạy workflow đó **từ
nhánh mặc định**, tức bằng code của `main` — chưa có tính năng mới. Tắt nó trong lúc thử,
**và nhớ bật lại ở mục 8**:

```bash
gh workflow disable orchestrator -R <org>/<kho-platform>
gh workflow list -R <org>/<kho-platform>     # phải thấy: disabled_manually
```

> Bước triển khai lên cụm sẽ do bạn chạy `onboard` từ máy này (mục 6), không qua
> orchestrator. Sau khi merge, `repository_dispatch` là đường tự động đầy đủ.

---

## 3. File yêu cầu

Bạn khai **ý định**, không khai toạ độ: không namespace, không đường dẫn Vault, không
StorageClass. Chọn tên app **chưa tồn tại**.

```bash
export APP=thuvien          # đổi thành tên của bạn; chỉ chữ thường, số và '-'
cat > /tmp/$APP.request.yaml <<YAML
apiVersion: idp.company/v1
kind: OnboardingRequest
application:
  name: $APP
  owner: team-$APP
  description: App mẫu do agent tạo
stack:
  id: node-fullstack          # -> 1 backend + 1 frontend + thư viện dùng chung
  version: 1.0.0
database:
  enabled: true
  profile: application        # -> 1 database PostgreSQL do CloudNativePG quản
routing:
  visibility: internal
environments:
  staging: true
  prod: true                  # CHUẨN BỊ contract cho prod, KHÔNG dựng gì ở prod ngay
YAML
```

**CỔNG KIỂM 4:** tên app chưa bị dùng.

```bash
kubectl get ns | grep -w "$APP-staging" && echo "TRÙNG — đổi tên" || echo "tên dùng được"
gh repo view <org>/$APP >/dev/null 2>&1 && echo "TRÙNG kho — đổi tên" || echo "kho dùng được"
```

---

## 4. Sinh khung app rồi DỪNG lại để sửa code

```bash
python3 orchestrate.py --env-config "$CFG" onboard \
  --request /tmp/$APP.request.yaml --work /tmp/onboard-$APP \
  --images ci --stop-after bootstrap-platform
```

`--images ci` nghĩa là **CI của kho ứng dụng build ảnh**, orchestrator chỉ chờ rồi dùng.
Đây là điều kiện của tiêu chí S2.

**CỔNG KIỂM 5:**

```bash
python3 orchestrate.py --env-config "$CFG" onboard-status --app $APP
gh secret list -R <org>/$APP      # phải có PLATFORM_DISPATCH_TOKEN
ls -a /tmp/onboard-$APP/app       # backend/ frontend/ shared/ .score-values/ .idp/
```

Phải thấy `[OK ] validate`, `[OK ] scaffold-repository`, `[OK ] bootstrap-platform`, hai kho
GitHub đã tạo, và secret đã đặt. **Nếu `PLATFORM_DISPATCH_TOKEN` không có** thì
`APP_DISPATCH_TOKEN` chưa được đặt lúc chạy — đặt rồi chạy lại đúng lệnh trên (nó idempotent).

---

## 5. Thêm biến môi trường và bí mật

### 5.1. Khai trong `.score-values/values.yaml`

Đây là chỗ **duy nhất** khai cấu hình theo môi trường.

**KHÔNG ghi đè nguyên file.** Khung app sinh ra đã khai sẵn `PUBLIC_HOST` cho mỗi môi
trường, và `frontend/score.yaml` đang tham chiếu nó. Ghi đè mất khoá đó thì render fail với
`references ['PUBLIC_HOST'] ... but no such key resolves`. THÊM khoá vào file đang có:

```bash
cd /tmp/onboard-$APP/app
python3 - <<'EOF'
import yaml
p = '.score-values/values.yaml'
d = yaml.safe_load(open(p))
spec = d.setdefault('spec', {})
app = spec.setdefault('application', {})
app.update({"LOG_LEVEL": "info", "TEN_HIEN_THI": "App mẫu"})
envs = spec.setdefault('environments', {})
envs.setdefault('staging', {}).update({
    "MOI_TRUONG": "staging", "LOG_LEVEL": "debug", "GIOI_HAN": "20",
    "TEN_HIEN_THI": "App mẫu (STAGING)",
    "API_KEY": {"secretRef": {"name": "api-credentials", "key": "api_key"}},
})
envs.setdefault('prod', {}).update({
    "MOI_TRUONG": "prod", "LOG_LEVEL": "info", "GIOI_HAN": "5000",
    "TEN_HIEN_THI": "App mẫu",
    "API_KEY": {"secretRef": {"name": "api-credentials", "key": "api_key"}},
})
open(p, 'w').write(yaml.safe_dump(d, sort_keys=False, allow_unicode=True))
EOF
```

**CỔNG KIỂM 6a:** `PUBLIC_HOST` vẫn còn ở cả hai môi trường —

```bash
python3 -c "
import yaml; e=yaml.safe_load(open('.score-values/values.yaml'))['spec']['environments']
assert e['staging'].get('PUBLIC_HOST') and e['prod'].get('PUBLIC_HOST'), 'MẤT PUBLIC_HOST'
print('  PUBLIC_HOST còn nguyên')"
```

Ba luật, vi phạm là render fail:

1. **Mọi giá trị literal là CHUỖI.** YAML đọc `yes/no/on/off` thành boolean và `1.10` thành
   `1.1`. Renderer từ chối thay vì tự ép kiểu.
2. **Không đặt giá trị bí mật ở đây** — file này nằm trong git. Chỉ khai `secretRef`.
3. Tên môi trường là **`prod`**, không phải `production`.

### 5.2. Nối vào workload

`backend/score.yaml` đã có sẵn resource `config` kiểu `environment`. Thêm các biến mới vào
`containers.api.variables`:

```bash
python3 - <<'EOF'
import yaml
p = 'backend/score.yaml'
raw = open(p).read()
d = yaml.safe_load(raw)
v = d['containers']['api']['variables']
for k in ("TEN_HIEN_THI", "MOI_TRUONG", "GIOI_HAN", "API_KEY"):
    v[k] = "${resources.config.%s}" % k
head = raw.split('apiVersion:')[0]          # giữ lại khối comment đầu file
open(p, 'w').write(head + yaml.safe_dump(d, sort_keys=False, allow_unicode=True))
EOF
```

**CỔNG KIỂM 6:** `grep -c 'resources.config' backend/score.yaml` ≥ 5, và
`grep -n 'type: environment' backend/score.yaml` có kết quả.

### 5.3. Dùng bí mật cho một việc THẬT

Một bí mật chỉ để in ra là một bí mật không ai xoay vòng. Bắt mọi thao tác ghi phải kèm nó.
Sửa `backend/src/index.js`: thêm middleware và gắn vào các route ghi.

```js
const API_KEY = process.env.API_KEY || "";
function kiemTraApiKey(req, res, next) {
  if (!API_KEY) return res.status(503).json({ error: "API_KEY chưa được nạp" });
  const gui = String(req.get("X-API-Key") || "");
  if (gui.length !== API_KEY.length || gui !== API_KEY) {
    return res.status(401).json({ error: "X-API-Key sai hoặc thiếu" });
  }
  next();
}
// dùng: api.post("/items", kiemTraApiKey, async (req, res) => { ... });
```

Và một endpoint để kiểm S9 — **không bao giờ trả về giá trị bí mật**, chỉ trả về việc nó đã
được nạp hay chưa:

```js
api.get("/thong-tin", (_req, res) => res.json({
  tenHienThi: process.env.TEN_HIEN_THI,
  moiTruong: process.env.MOI_TRUONG,
  gioiHan: Number(process.env.GIOI_HAN || 0),
  apiKeyDaNap: Boolean(API_KEY),
}));
```

### 5.4. Nạp giá trị bí mật vào Vault

```bash
cd /home/<...>/idp-platform
python3 orchestrate.py --env-config "$CFG" secret-set \
  --app $APP --env staging --name api-credentials --key api_key --stdin --replace <<< "khoa-demo-cua-agent"
```

- `--replace` cho **lần đầu** (patch không tạo được bản đầu tiên).
- Không có cờ `--value`: tham số dòng lệnh nằm trong shell history và trong `ps`.
- Bí mật **không** tự chảy từ staging sang prod; prod cần một lần `--env prod` riêng.

**CỔNG KIỂM 7:**

```bash
curl -s -H "X-Vault-Token: $VAULT_TOKEN" \
  "$VAULT_ADDR/v1/kv/data/apps/$APP/staging/api-credentials" \
  | python3 -c "import json,sys; print(sorted(json.load(sys.stdin)['data']['data'].keys()))"
# -> ['api_key']
```

---

## 6. Đẩy code để GitHub Actions build ảnh

### 6.1. Nếu ở TRƯỚC MERGE: ghim ci.yaml của **kho fixture** vào nhánh tạm

Chỉ sửa bản sao trong kho ứng dụng. **Không** sửa `templates/` trong kho platform.

```bash
cd /tmp/onboard-$APP/app
sed -i "s|^\(\s*\)ref: main$|\1ref: $PIN_BRANCH|" .github/workflows/ci.yaml
grep -n "ref: $PIN_BRANCH" .github/workflows/ci.yaml    # phải có đúng 1 dòng
```

### 6.2. Đẩy

```bash
git add -A
git -c user.name=agent -c user.email=agent@local commit -q -m "feat: biến môi trường và bí mật"
git push -q origin HEAD:dev
```

### 6.3. Chờ CI

```bash
RUN=$(gh run list -R <org>/$APP --limit 1 --json databaseId --jq '.[0].databaseId')
gh run watch "$RUN" -R <org>/$APP
gh run view "$RUN" -R <org>/$APP --json jobs --jq '.jobs[]|"\(.name): \(.conclusion)"'
```

**CỔNG KIỂM 8 (= S1):** cả 4 job `success` — `plan`, `build (backend…)`, `build
(frontend…)`, `dispatch`. Tên job build có chứa tag ảnh; **ghi lại tag đó**, mục 7 phải
khớp.

Nếu đỏ: xem "Bẫy đã biết" B1–B3.

---

## 7. Deploy lên staging

```bash
cd /home/<...>/idp-platform
python3 orchestrate.py --env-config "$CFG" onboard \
  --request /tmp/$APP.request.yaml --work /tmp/onboard-$APP --images ci
```

Lệnh này chạy tiếp từ chỗ đang dở. Chạy lại đúng lệnh cũ **luôn an toàn**: mỗi bước
kiểm-trước-khi-tạo, bước đã xong bị bỏ qua, không tạo bản sao thứ hai.

> **Nếu bạn vừa sửa code app và đẩy lại sau khi `build-images` đã xong**, phải thêm
> `--force-step build-images`. Bước đó đã ghi commit cũ vào state, và `deploy-staging` cố ý
> render đúng commit ĐÃ BUILD — render theo đỉnh nhánh sẽ trỏ tới một ảnh chưa ai đẩy lên.
> Không có cờ này thì lỗi cũ quay lại y nguyên dù bạn đã sửa. Xem bẫy B13.

Bước database chờ base backup đầu tiên nên có thể mất vài phút. Nếu shell của bạn có giới
hạn thời gian, chạy nền và theo dõi bằng `onboard-status`.

**CỔNG KIỂM 9 (= S2):** trong log phải có

```text
tag_strategy=commit (from .idp/stack.yaml)
mọi ảnh đã có trên registry -> không build lại
```

Dòng thứ hai là bằng chứng orchestrator **dùng ảnh của CI**. Nếu thấy `docker build` thì
`--images ci` không có hiệu lực — kiểm lại lệnh.

---

## 8. Kiểm chứng — đo, đừng tin

```bash
export NS=$APP-staging
python3 orchestrate.py --env-config "$CFG" onboard-status --app $APP          # S3
kubectl -n $NS get pods                                                        # S4
kubectl -n $NS get vaultstaticsecret \
  -o custom-columns=NAME:.metadata.name,SYNCED:.status.conditions[0].status    # S5
kubectl -n $NS get cluster.postgresql.cnpg.io \
  -o jsonpath='{.items[*].status.firstRecoverabilityPoint}{"\n"}'              # S6
kubectl -n fleet-local get gitrepo | grep $APP                                 # S7
```

S6 **rỗng nghĩa là database KHÔNG phục hồi được**, kể cả khi Cluster báo `Ready` và
`ContinuousArchiving=True`. Xem bẫy B4.

HTTP qua Gateway. Trên harness kind, cổng `18080` của máy đã map sẵn vào Gateway:

```bash
H="Host: $APP.staging.internal.dev"; B=http://localhost:18080
curl -s -o /dev/null -w "/ -> %{http_code}\n"           -H "$H" $B/           # S8
curl -s -o /dev/null -w "/api/health -> %{http_code}\n" -H "$H" $B/api/health # S8
curl -s -H "$H" $B/api/thong-tin                                              # S9
# S10: ghi KHÔNG kèm khoá -> 401
curl -s -w " <- %{http_code}\n" -X POST -H "$H" -H 'Content-Type: application/json' \
  -d '{"label":"thu"}' $B/api/items
# S10: có khoá -> 201
curl -s -w " <- %{http_code}\n" -X POST -H "$H" -H 'X-API-Key: khoa-demo-cua-agent' \
  -H 'Content-Type: application/json' -d '{"label":"thu"}' $B/api/items
# S11: bí mật KHÔNG nằm trong manifest đã commit
grep -r "khoa-demo-cua-agent" /tmp/onboard-$APP/config-staging/ && echo "RÒ RỈ" || echo "sạch"
```

S9 phải trả `"moiTruong":"staging"`, `"gioiHan":20`, `"apiKeyDaNap":true` và tên có hậu tố
`(STAGING)` — đó là bằng chứng cấu hình theo môi trường có hiệu lực.

Cuối cùng:

```bash
./tools/kiem-suc-khoe.sh --namespace $NS      # phải: không có cảnh báo nào
```

### Bật lại workflow orchestrator — BẮT BUỘC nếu bạn đã tắt ở mục 2.2

```bash
gh workflow enable orchestrator -R <org>/<kho-platform>
gh workflow list -R <org>/<kho-platform>      # phải thấy: active
```

---

## 9. Dọn dẹp

Bỏ qua mục này nếu app được giữ lại. Nếu đây là một lần chạy thử thì dọn hết và **ghi lại
đã dọn những gì**.

```bash
python3 orchestrate.py --env-config "$CFG" offboard --app $APP --env staging   # xem trước
python3 orchestrate.py --env-config "$CFG" offboard --app $APP --env staging \
  --execute --confirm $APP --purge-secrets
gh repo delete <org>/$APP --yes
gh repo delete <org>/idp-$APP-config --yes
gh api -X DELETE /user/packages/container/$APP-backend
gh api -X DELETE /user/packages/container/$APP-frontend
git push origin --delete "$PIN_BRANCH"       # nếu đã tạo ở mục 2.1
```

`offboard` **cố ý không xoá** backup database và kho Git. Nếu muốn xoá thư mục backup của
app trong kho object thì làm tay.

---

## 10. Bẫy đã biết — tra ở đây trước khi tự chẩn đoán

| # | Triệu chứng | Nguyên nhân thật | Xử lý |
|---|---|---|---|
| B1 | CI đỏ: `unrecognized arguments: --with-build` | `ref: main` nhưng `main` chưa có tính năng | Làm mục 2.1 + 6.1 |
| B2 | CI xanh nhưng orchestrator vẫn báo thiếu ảnh | CI tính tag `content`, orchestrator tính `commit` (cờ `stack_onboarding` tắt ở nhánh CI đọc) | Ghim vào nhánh có cờ BẬT (mục 2.1) |
| B3 | CI đỏ ở `actions/checkout` | thiếu secret `PLATFORM_DISPATCH_TOKEN` | đặt `APP_DISPATCH_TOKEN` rồi chạy lại mục 4 |
| B4 | `firstRecoverabilityPoint` rỗng mãi | thiếu `BACKUP_ACCESS_*` → không có credential kho object → WAL archiving hỏng | đặt biến, chạy lại; **nếu Cluster đã tồn tại trước khi có Secret thì phải xoá pod instance** — instance manager cache lúc khởi động và báo `failed to get envs: cache miss` |
| B5 | Pod `ImagePullBackOff`, registry trả `403` | thiếu `REGISTRY_USER/PASS` | đặt biến; nếu `registry-pull` đã tồn tại và sai thì **xoá đi rồi tạo lại** — nó là create-if-missing |
| B6 | `CreateContainerConfigError`, VSS `SecretSynced=False` với `empty response from Vault` | chưa nạp bí mật | mục 5.4, nhớ `--replace` cho lần đầu |
| B7 | VSS `False` với `permission denied` | app/env sai, hoặc chưa onboard vào Vault | `vault-onboard --app $APP --env staging` |
| B8 | VSS `False` nhưng `reason` vẫn là `Synced` | `reason` là TÊN LOẠI điều kiện, không phải kết quả | đọc `.status.conditions[].status`, đừng đọc `reason` |
| B9 | VSS `False` kéo dài ~2 phút sau khi Vault vừa dựng lại | VSO backoff, thông điệp đứng yên | **chờ**; đừng xoá `VaultStaticSecret` — xoá nó thu hồi luôn Secret đích và pod đang chạy mất biến môi trường |
| B10 | Fleet báo `Modified` vĩnh viễn dù cụm đúng | quantity ghi bằng SỐ; Kubernetes lưu thành CHUỖI | nháy kép mọi quantity trong catalog |
| B11 | `render` dừng với "đổi class ... KHÔNG di chuyển dữ liệu" | app đang đổi `class` postgres trên database có dữ liệu | `docs/chuyen-doi-postgres-sang-class-application.md` |
| B12 | Mọi request `/api/...` trả về HTML của frontend | router mount tại `/` thay vì `/api` | route chuyển tiếp nguyên đường dẫn, không cắt tiền tố |
| B13 | Sửa code app rồi chạy lại `onboard`, nhưng render vẫn dùng commit CŨ và lỗi cũ quay lại | bước `build-images` đã `done` và đã GHI commit đó vào state; `deploy-staging` cố ý render đúng commit đã build (render theo đỉnh nhánh sẽ trỏ tới ảnh chưa ai đẩy) | chạy lại với `--force-step build-images` để state ghi commit mới |

**Không bao giờ làm:** sửa `templates/` trong kho platform rồi commit để CI chạy được; nới
một cổng kiểm cho qua; xoá tài nguyên để test pass; đưa giá trị bí mật vào git hoặc vào log.

---

## 11. Sau khi lên staging

| Việc | Lệnh |
|---|---|
| Kích hoạt prod | nạp secret `--env prod`, rồi `onboard-activate-prod --app $APP` → mở PR, chờ duyệt |
| Xoay vòng bí mật ứng dụng | `secret-set …`; VSO restart workload đúng một lần |
| Xoay vòng mật khẩu database | `rotate-db-credential --app $APP --env staging` — **đừng** chỉ ghi vào Vault |
| Sự cố | `docs/runbook/` |
| Ngưỡng cảnh báo | `docs/canh-bao.md` |
