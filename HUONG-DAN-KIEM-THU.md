# Harness kiểm thử — trạng thái hiện có

Đọc file này TRƯỚC khi sửa `orchestrate.py`, catalog (`provisioners/`, `patches/`) hoặc
`orchestrator.yaml`. Nó mô tả bộ kiểm thử đang có, cách chạy, nó bảo vệ điều gì, và cách
dùng nó để tự verify khi code thêm. Mục tiêu: một phiên mới không phá vỡ bất biến của dự án
chỉ vì không biết có harness.

## Harness là gì (một câu)

Là **hai lớp dùng chung source/catalog thật**: `test_orchestrate.py` kiểm logic nhanh, còn
`tools/dung-*-harness.sh` + `tools/thu-nghiem-*.sh` dựng capability trên các cụm `kind-*` và
đo workload chạy thật. Pytest xanh không thay thế lớp runtime.

## Chạy thế nào

```bash
# từ gốc repo idp-platform (nơi có orchestrate.py)
python3 -m pytest test_orchestrate.py -v
```

- Test **import `orchestrate as orc`** ⇒ phải chạy ở thư mục có `orchestrate.py` (gốc repo).
- Test **nạp `platform.env.yaml` của chính repo** để resolve `%%placeholder%%` ⇒ nó kiểm cả
  catalog + config thật, không phải mô hình giả.
- Cần trên PATH: `score-k8s`, `kubectl`, `git`, `gh`, và `pyyaml` (import `yaml`).
  - **26 test có `@needs_score_k8s` sẽ TỰ SKIP nếu thiếu `score-k8s`** (biến `HAS_SCORE_K8S`).
    Thiếu score-k8s ⇒ vẫn chạy được phần còn lại, nhưng **các test idempotency/render quan
    trọng nhất bị bỏ qua** — muốn verify thật thì phải có score-k8s.
  - Các test khác chỉ cần `git` (chúng dựng repo git tạm trong `tmp_path`, không đụng repo thật).
- Tổng hiện tại: khoảng **180 case** (từ ~150 hàm test, parametrize nở thêm; ~26 hàm cần
  score-k8s). Trên máy có đủ công cụ, cả bộ **xanh sạch, 0 skip** (~73s). Muốn biết pass/skip
  lúc này thì chạy lệnh trên — đừng tin một con số chép cứng trong tài liệu.

## Nó bảo vệ bất biến nào (map test → luật)

Mỗi "mảng" test canh một bất biến. Test đỏ ở mảng nào = bạn vừa phá luật đó:

| Mảng test (comment trong file) | Bất biến nó canh |
|---|---|
| `state stability (the big one)` | Render **idempotent**: hai lần render chung state ra **y hệt** tên resource + mật khẩu. Đây là chống churn. |
| (đối chứng) `without state everything churns` | Chứng minh test trên không pass rỗng: tắt state là bug tái xuất. |
| `ancestry guard` | `guard_ordering`: **không deploy commit cũ đè commit mới hơn**. |
| `managed-by / Fleet drift` | Strip `managed-by` để Fleet/Helm sở hữu resource. |
| `state Secret optimistic lock` | Khoá ghi đồng thời state bằng resourceVersion. |
| `promote from-staging` / `promotion digest` | 3 chế độ promote; from-staging copy đúng bộ image. |
| `PR flow (branch protection)` | Env cần duyệt thì **mở PR**, không push thẳng. |
| `environment config` | Đọc đúng giá trị theo env từ `platform.env.yaml`. |
| `image naming` / `retag` / `per-service tagging` | `image-plan`, `tag_strategy` commit vs content. |
| `multi-workload` / `cross-repo service dependencies` | `${resources.x}` chéo workload/repo resolve đúng. |
| `app-owned secrets` / `vault paths` / `secretRef shape` | Secret ref/Vault path render ra `secretRef`, giá trị **không** vào manifest. |
| `kiểm cụm sau khi triển khai` | Logic `verify`: chờ rollout thật, không nhìn `availableReplicas`. |
| `PHASE 5 — stack catalog` | Catalog `templates/stacks/` tự nhất quán; sinh app không để sót `__TOKEN__`; `.env.example` không lệch khỏi values. |
| `PHASE 5 — tích hợp score-compose` | Chạy **chính `make generate`** của app sinh ra: routing `/api` phải đứng trước `/` bất kể tên workload, và nginx phải re-resolve DNS. |
| `PHASE 6 — onboarding` | Request bị kiểm chặt (khoá lạ, `"false"` dạng chuỗi, stack version không phát hành); máy trạng thái **bỏ qua bước đã xong** khi retry; chờ-người không bao giờ thành `READY`; prod luôn đi qua pull request và mang đúng bộ ảnh staging; CI sinh ra không còn chỗ phải sửa tay. |

## Render/verify cục bộ — KHÔNG cần cụm

`cmd_render` có state store dạng file, nên chạy được offline:

- `--state-file <path>`: giữ state trong file (`FileStateStore`) — dùng để test và replay tay
  trên runner. Đây là cách các test render mà không cần cụm.
- `--no-state`: tắt persistence — **tái hiện đúng bug churn**, chỉ dùng để đối chứng trong test.
- Kiểm nhanh runner đủ công cụ: `python3 orchestrate.py --env-config platform.env.yaml preflight`.

## Cách một phiên mới DÙNG harness để verify (quy trình bắt buộc)

1. **Trước khi coi là xong**, chạy full: `python3 -m pytest test_orchestrate.py -v`. Đỏ = hành vi sai.
2. **Không bao giờ sửa/nới lỏng test cho pass.** Test đỏ nghĩa là code sai, không phải test sai.
   Nếu thật sự đổi hợp đồng có chủ ý, đổi test kèm lý do rõ ràng (và cập nhật ADR/tài liệu).
3. **Thêm hành vi mới ⇒ thêm test** vào đúng mảng ở trên (đặt cạnh test cùng chủ đề). Ví dụ
   đang làm secret onboarding thì test nằm ở `app-owned secrets` / `vault paths` / `secretRef shape`.
4. Nếu thiếu `score-k8s` trên máy: cài nó rồi chạy lại — **đừng** coi "pass" khi 26 test render bị skip.

## Harness KHÔNG chạm tới đâu (đừng nhầm xanh)

- Đây là test **đơn vị/tích hợp cục bộ** (render + git fixture trong tmp). Nó **không** deploy
  lên cụm thật. `pytest xanh` chỉ nghĩa "logic render/commit/verify đúng", **không** đảm bảo
  cụm đã chạy. Đúng như triết lý "mỗi lớp xanh độc lập" của dự án.
- Lớp e2e thật nằm ngoài file này: các cụm `kind-staging`/`kind-prod` sống + các lần
  `repository_dispatch` chạy `orchestrator.yaml`. Muốn kiểm tới cụm thì đối chiếu trực tiếp
  (`kubectl`, `gh api`) — xem `docs/orchestrator-contract.md`.

## Test một FEATURE qua luồng thật (AI tự lái) — khi sửa orchestrate.py/catalog

`pytest` xanh chỉ nói **logic** đúng (mục trên). Để biết một feature **chạy được**, phải cho nó
đi hết luồng thật trên harness sống. Có một nút thắt và một cửa thoát:

- **Nút thắt:** `repository_dispatch` (app CI gọi platform) LUÔN chạy code platform từ nhánh mặc
  định `main` — nên code feature trên nhánh **chưa merge** không được chạy qua đường app-CI thường.
- **Cửa thoát:** `workflow_dispatch` cho chọn ref. `gh workflow run orchestrator.yaml --ref <nhánh>`
  chạy đúng `orchestrator.yaml` **và** checkout `orchestrate.py` **theo nhánh** (bước "Checkout
  platform" không ghim `ref`). Đây là cách chạy code chưa merge trên runner + cụm thật.

**Ranh giới cô lập = TÊN APP, không phải nhánh git.** Platform tách mọi tài nguyên theo tên app
(`<app>-staging`, `idp-<app>-config`, state Secret theo app, đường Vault theo app). Nên luôn test
bằng **một app tên-mới throwaway**; **không** trỏ run nhánh feature vào một app đang chạy — nó commit
vào config repo của app đó → Fleet áp lên cụm → đổi app thật, và state dùng chung có thể làm run
`main` sau đó đọc sai (vỡ bất biến tương thích ngược). Xong thì `offboard` app throwaway.

### Vòng lặp phát triển (rẻ → đắt, mỗi lớp bắt một loại lỗi)

| Lớp | AI làm gì | Bắt lỗi gì | Khi nào |
|---|---|---|---|
| 1. pytest | `pytest test_orchestrate.py` | logic render/commit/verify | mỗi lần sửa |
| 2. `--ref` + ảnh thật | vòng dưới | render/deploy/state + tên ảnh, trên cụm thật | vài lần/ngày |
| 3. CI dispatch → v2 | app test CI build+dispatch tới `idp-platform-v2` (main=feature) → `kind-v2` | khúc CI dựng payload `repository_dispatch` mà lớp 2 bỏ qua | trước khi merge |

**Lớp 2 — vòng AI tự lái (ảnh thật, không để người chen giữa build và trigger):**

```
1. Đẩy code app test  ->  CI build + push ẢNH THẬT lên registry
     (CI của app test nên là BUILD-ONLY: nếu nó tự dispatch, cú đó đi vào main = code cũ.
      Tách vai rõ — CI lo build ảnh, AI lo trigger + verify.)
2. Poll registry tới khi tag ảnh thật sự có mặt.                # chốt "đợi ảnh"
3. gh workflow run orchestrator.yaml --ref <nhánh> \
     -f app=<app-test> -f repo=<org/app-test> -f sha=<sha CI vừa build> -f env=staging
4. Verify (đo, đừng tin):
   - ảnh trong manifest render == ảnh thật trong registry       # chốt "khớp tên ảnh" (chống §6.14)
   - rollout thật: updatedReplicas/observedGeneration
   - curl qua gateway -> 200
5. Sửa code -> đẩy nhánh -> lặp lại từ 1 (hoặc từ 3 nếu ảnh không đổi).
```

Ba "chốt" (đợi ảnh · khớp tên ảnh · đúng ref) là thứ khiến việc tách build khỏi trigger **an toàn
khi một agent kỷ luật lái**: đọc giá trị thật thay vì tính lại, chờ điều kiện thay vì đoán. Đây
chính là chỗ §6.14 (`TAI-LIEU-DU-AN.md`) từng hỏng vì hai bên tự tính tên ảnh rồi lệch nhau.

**Lớp 3** phủ nốt khúc `repository_dispatch` thật: đưa code nhánh lên `main` của `idp-platform-v2`
(`git push -f v2 <nhánh>:main` — v2 là platform test dùng-một-lần nên force-push thoải mái), trỏ CI
app test dispatch tới v2, chạy trên `kind-v2` (tách hẳn kind-staging). Runner/cụm v2: xem memory dự
án + `HUONG-DAN-CAI-DAT.md`.

## Môi trường verify & hạ tầng (đọc trước khi làm phase cần cụm)

Một số phase (Vault/VSO, DB provider, score-compose…) cần hạ tầng thật, không chỉ pytest.
**Đừng tin bất kỳ ảnh chụp trạng thái nào chép trong tài liệu — nó lỗi thời ngay.** Luôn tự
lấy trạng thái SỐNG:

- **Cụm verify** trên máy này: `kind-staging`, `kind-prod` (và `kind-v2`) — context `kind-*`.
  Xem có gì: `kubectl config get-contexts` / `kind get clusters`.
- **Probe hạ tầng (nguồn sự thật LIVE, CHỈ ĐỌC, an toàn cả trên prod):**
  ```bash
  ./tools/thu-thap-ha-tang.sh                                   # cụm đang trỏ tới
  KUBECONFIG=... ./tools/thu-thap-ha-tang.sh                    # chạy cho TỪNG cụm
  ```
  Nó in node, storageClass, Gateway API/gateway, traefik, Fleet, CRD… **không in secret**.
  Chạy cái này TRƯỚC khi kết luận "phải dựng mới" hay "đã có sẵn".
- **Công cụ đã ghim** (ADR `0006`): kiểm bằng `preflight` + `--version`:
  `score-k8s`, `score-compose`, `helm`, `kubectl`, `vault`. Sai version = render/deploy lệch.

**Phase nào cần hạ tầng gì (để biết phải dựng hay đã có — tự kiểm bằng lệnh, đừng đoán):**

| Phase | Cần | Kiểm nhanh |
|---|---|---|
| 2 — Vault/VSO | VSO (CRD `secrets.hashicorp.com`) + một Vault + VaultConnection/Auth | `python3 orchestrate.py preflight --require-cluster --require-vault` (kiểm CRD + phiên bản VSO + hai object nền tảng, một lệnh) |
| 3 — App secret | Phase 2 chạy được (VSO sync ra Secret trong ns) | `kubectl get vaultstaticsecret,secretstore -A` |
| 4 — Postgres capability | DB provider/operator production-grade | probe: tìm operator postgres; `kubectl get crd \| grep -iE 'postgres\|cnpg\|zalando'` |
| 5 — Stack + score-compose | `score-compose` bản đã ghim + `docker` + `make` | `score-compose --version`; `docker info` |
| 6 — Onboarding | Phase 2-5 chạy được + `gh` đã đăng nhập + kho object cho backup (nếu bật prod) | `gh auth status`; `kubectl -n object-store get deploy minio` |
| 6 — CI của app trên runner tự dựng | `python3`, `jq`, `docker`, `git` trên máy chạy | `for t in python3 jq docker git; do command -v $t; done` |
| chung — deploy tới cụm | Fleet + gateway (traefik) + storageClass | có trong output `thu-thap-ha-tang.sh` |

**Dựng lại Vault/VSO trên harness (Phase 2) — một lệnh, chạy lại được nhiều lần:**

```bash
./tools/dung-vault-harness.sh --context kind-staging
```

Nó đọc toạ độ từ `platform.env.yaml` (`vault.address` quyết định Vault nằm ở namespace
nào), cài Vault **dev mode** + VSO đúng phiên bản đã ghim, bật KV/kubernetes-auth/audit,
apply `VaultConnection`+`VaultAuthGlobal` sinh từ config, rồi tự kiểm bằng `preflight`.
Vault dev mode **mất sạch dữ liệu khi pod restart** — chạy lại script để dựng lại mount,
nhưng secret đã ghi thì phải ghi lại. Công ty đã có Vault thật ⇒ **không** chạy script này,
chỉ điền `vault.*` rồi chạy `orchestrate.py vault-foundation --apply`.

**Vault/VSO E2E thật (có chủ ý làm Vault gián đoạn):**

```bash
./tools/thu-nghiem-vault-e2e.sh --context kind-staging
```

Nó dùng một app throwaway để đi hết `secret-set` → render `VaultStaticSecret` → VSO sync →
container nhận biến, kiểm không có `_raw`, chặn prefix app khác và kiểm RBAC không đọc được
Kubernetes Secret. Sau đó script scale Vault dev về 0, xác minh app đang chạy vẫn sống và
VSO thấy lỗi nguồn, dựng/bootstrap Vault lại, ghi lại secret rồi đòi VSO `Synced` và owner
cleanup. Vì restart xoá **toàn bộ** dữ liệu Vault dev, chỉ chạy bài này trên harness thử nghiệm;
những fixture khác phải tự seed lại policy/role/secret nếu còn cần.

Onboard một app vào Vault (hai nửa, hai chủ sở hữu — xem ADR `0007`):

```bash
python3 orchestrate.py vault-onboard --app <app> --env staging --apply  # SA + VaultAuth
python3 orchestrate.py vault-onboard --app <app> --env staging          # in policy/role Vault
python3 orchestrate.py verify-rbac  --app <app> --env staging --apply   # danh tính verify
```

**Golden path và phát triển local (Phase 5):**

```bash
python3 orchestrate.py --env-config platform.env.yaml stack-list
python3 orchestrate.py --env-config platform.env.yaml stack-new \
  --stack node-fullstack --app <app> --owner <đội> --out /duong/dan/kho-moi
cd /duong/dan/kho-moi && make dev          # cần docker + score-compose, KHÔNG cần cụm
```

`stack-new` **không ghi đè** file đã có (chạy lại được, dùng `--force` để ép).
`stack-validate --app-dir <kho>` kiểm kho ứng dụng còn khớp stack; `stack-upgrade` in diff
và chỉ ghi khi có `--write`.

> Ba test tích hợp Phase 5 chạy **chính `make generate` của app sinh ra**, nên chúng cần
> `make` và `score-compose` trên PATH. Gate `make dev` đầy đủ (dựng container thật) không
> nằm trong pytest — chạy tay theo lệnh trên rồi kiểm `/` và `/api/health`.

**Onboarding một app mới từ đầu (Phase 6):**

Onboarding hiện bị tắt bằng `onboarding.enabled: false`. Khi cần mở lại có chủ ý, đổi key
đó thành `true`; `features.stack_onboarding` là capability của stack/deploy và không phải
kill switch vận hành. Khi tắt, `onboard` (kể cả resume) và `onboard-activate-prod` dừng trước
mọi thao tác ghi, còn `onboard-status` và deploy app hiện hữu vẫn hoạt động.

```bash
# 1. Kho object cho backup — BẮT BUỘC nếu app xin database và có bật prod.
#    Render prod bị CHẶN khi database.backup.object_store_url rỗng (fail-closed có chủ ý).
./tools/dung-object-store-harness.sh --context kind-staging      # chỉ harness, không chạy ở công ty

# 2. Vault nhìn thấy được từ máy đang chạy lệnh (vault.address là địa chỉ CỤM nhìn thấy).
kubectl -n vault port-forward svc/vault 8200:8200 &
export VAULT_ADDR=http://127.0.0.1:8200 VAULT_TOKEN=<token có quyền quản trị>

# 3. Thông tin đăng nhập registry cho imagePullSecret, và (tuỳ chọn) token cho CI của app.
export REGISTRY_USER=... REGISTRY_PASS=...     # KHÔNG bao giờ là tham số dòng lệnh
export APP_DISPATCH_TOKEN=...                  # bỏ trống -> onboarding chỉ BÁO là còn thiếu

# 4. Chạy. Cùng một file request chạy lại bao nhiêu lần cũng được.
python3 orchestrate.py --env-config platform.env.yaml onboard \
  --request request-<app>.yaml --work /duong/dan/tam/onboard-<app>
python3 orchestrate.py --env-config platform.env.yaml onboard-status --app <app>
python3 orchestrate.py --env-config platform.env.yaml onboard-activate-prod \
  --app <app> --work /duong/dan/tam/onboard-<app>
```

Hình dạng file request nằm ở mục 13.1 của `KE-HOACH...`. Trạng thái sống trong ConfigMap
`idp-onboarding-<app>` ở `kubernetes.state_namespace` — đọc bằng `onboard-status --json`.

Ba trạng thái KHÔNG phải lỗi và cũng KHÔNG phải xong:

| Trạng thái | Nghĩa | Làm gì tiếp |
|---|---|---|
| `WAITING_FOR_USER_SECRETS` | app deploy rồi nhưng thiếu bí mật bên thứ ba | chạy đúng lệnh `secret-set` mà nó in ra, rồi chạy lại `onboard` |
| `PENDING_PROD_APPROVAL` | pull request prod đã mở, chờ người merge | merge rồi chạy lại `onboard-activate-prod` |
| `FAILED_RETRYABLE` | một bước hỏng, lý do nằm trong bản ghi | sửa nguyên nhân rồi chạy lại — nó tiếp tục từ đúng bước đó |

`--force-step <tên bước>` chạy lại một bước đã `done` (mọi bước đều kiểm-trước-khi-tạo).
`--stop-after <tên bước>` dừng sớm để xem kết quả từng phần.

> Thứ chỉ tồn tại ở công ty (Vault addr thật, policy…) thì theo `KE-HOACH...` mục 0.6: tạo
> config key + validation/preflight + checklist, **không** tự đánh dấu pass, và **không** để
> nó chặn phần verify được ở local.

## Company compatibility — một source, hai profile

Cùng một source chạy được cả harness (github.com + GHCR + CNPG + Traefik HTTP) lẫn hình dạng
công ty (GHES + Harbor/HTTPS + PostgreSQL StatefulSet trên StorageClass mạng + Traefik có
listener HTTPS). Khác biệt nằm HẾT ở `platform.env.company.yaml`, không fork code. Xem
`GAP-REGISTER.md` để biết từng khe hở đã phân loại và sửa ra sao.

**Chọn profile:** `--env-config platform.env.yaml` (harness) hay `--env-config
platform.env.company.yaml` (công ty). Mọi lệnh đọc đúng file đó làm nguồn toạ độ.

**doctor — kiểm capability cụm khớp config (read-only, theo feature/backend):**

```bash
python3 orchestrate.py --env-config platform.env.yaml doctor                 # với cụm đang trỏ
python3 orchestrate.py --env-config platform.env.company.yaml doctor --no-cluster   # chỉ config
```

Doctor chỉ kiểm thứ feature đang bật cần: `postgres_application` off → KHÔNG kiểm CNPG/
StorageClass/object-store; backend `statefulset` → KHÔNG kiểm CNPG; `vault_secrets` off →
KHÔNG kiểm Vault/VSO. Thiếu-quyền-kiểm là WARN (không bao giờ báo xanh giả). FAIL = blocker.

**database backend (`database.backend: cnpg | statefulset`):**
- `cnpg` (mặc định, harness): CloudNativePG Cluster — hành vi cũ, không đổi.
- `statefulset` (công ty): PostgreSQL StatefulSet do platform quản, một bản sao, không HA,
  không backup nội tại → render prod bị CHẶN. App nhận BỘ OUTPUT GIỐNG HỆT hai backend.

**Runtime StatefulSet backend trên kind (chứng minh chạy thật, không chỉ render):**

```bash
./tools/thu-nghiem-db-statefulset.sh --context kind-staging   # [--keep] để giữ ns soi
```

Nó render backend statefulset qua một overlay runtime (ánh xạ StorageClass/ảnh/credential công
ty sang tương đương local — profile công ty KHÔNG bị sửa), apply lên kind, chờ StatefulSet
Ready, kiểm PVC `Bound`, chạy SQL thật, xoá pod rồi kiểm dữ liệu CÒN LẠI. Teardown tự động
(xoá namespace) trừ khi `--keep`.

**Runtime Gateway HTTPS và registry private kiểu công ty:**

```bash
./tools/dung-harness-cong-ty.sh --up --context kind-staging
./tools/thu-nghiem-gateway-https.sh --context kind-staging
./tools/thu-nghiem-registry-private.sh --context kind-staging
./tools/dung-harness-cong-ty.sh --down --context kind-staging   # teardown listener test
```

Gateway test render `sectionName=websecure`, đòi `Accepted=True` + `ResolvedRefs=True` và
curl HTTPS bằng CA thật. Registry test tự dựng OCI registry HTTPS có custom CA và auth, cho
runner push image thật, chứng minh pod thiếu Secret bị từ chối, rồi dùng chính
`apply-secrets` + manifest có `imagePullSecrets` để kubelet pull và đưa workload lên Ready.
Registry fixture và credential ngẫu nhiên luôn tự teardown; không sửa profile công ty.

**Test hai profile (trong `test_orchestrate.py`, mảng "COMPANY-PROFILE COMPATIBILITY"):**

```bash
python3 -m pytest test_orchestrate.py -q -k "profile or backend or doctor or statefulset or sectionName or company_coordinates or github"
```

Phủ: cả hai profile load hợp lệ + default tương thích ngược; chọn backend qua config; render
matrix (statefulset vs cnpg khác nhau CHỈ ở toạ độ); prod statefulset bị chặn; sectionName
theo config; URL theo `GITHUB_SERVER_URL`/scheme; doctor ma trận feature/backend (FakeProbe);
hardcode-scan (không literal công ty trong source dùng chung + GAP-REGISTER).

**Đã chứng minh RUNTIME (cụm thật):** StatefulSet backend (Ready · PVC Bound · SQL · bền qua
restart); Gateway HTTPS (`sectionName`, route conditions, TLS 200); registry private
(TLS/custom CA/auth, push, imagePullSecret, pull); Vault/VSO (secret-only render, sync,
prefix isolation, no-secret RBAC, outage continuity, bootstrap + resync, owner cleanup);
doctor read-only trên `kind-staging`.
**Mới chứng minh RENDER/MOCK:** route scheme, GHES URL, backend selection, guard prod.
**Prerequisite production còn thiếu:** backup backend cho database prod, VSO + Vault role cho
`vault_secrets`, và mirror đủ ảnh nền về Harbor thật.

## Tài liệu gốc liên quan

- `KE-HOACH-TRIEN-KHAI-SECRET-VA-APP-ONBOARDING.md` — master plan; **bảng trạng thái phase ở mục 0.5** (đã làm tới đâu).
- `TAI-LIEU-DU-AN.md` — thiết kế + lý do từng quyết định.
- `docs/adr/` — quyết định kiến trúc (vd `0002-vault-only-secret-store.md` cho tính năng secret).
- `docs/orchestrator-contract.md` — hợp đồng portal ↔ orchestrator, kèm cách verify trên cụm thật.
- Comment tại chỗ trong `orchestrate.py` — phần lớn giải thích "vì sao", đọc trước khi đổi hành vi.

> Gợi ý: thêm một dòng trỏ tới file này trong `CLAUDE.md` ở gốc repo, để phiên Claude mới
> **tự động** đọc thay vì phải nhớ mở ra.
