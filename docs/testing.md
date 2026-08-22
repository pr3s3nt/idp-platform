# Kiểm thử

Đọc trước khi sửa `idpctl`, catalog (`provisioners/`, `patches/`) hoặc `deploy.yaml`.
Mô tả bộ kiểm thử đang có, cách chạy, nó bảo vệ điều gì, và cách dùng nó để tự verify.

## Hai lớp — đừng nhầm lớp này thành lớp kia

- **`test_engine.py` / `test_audit.py` (pytest)** kiểm *logic* nhanh, local, không cần cụm.
- **`tools/dung-*-harness.sh` + `tools/thu-nghiem-*.sh`** dựng capability trên cụm `kind-*`
  và đo workload chạy thật.

Pytest xanh **không** thay thế lớp runtime: nó chỉ chứng minh render/commit/verify sinh
đúng thứ mong đợi, không chứng minh app đã lên cụm.

## Chạy pytest

```bash
# từ gốc repo idp-platform (nơi có idpctl và engine/)
python3 -m pytest test_engine.py -v
python3 -m pytest test_audit.py -v
```

- Test **import `engine as orc`** ⇒ phải chạy ở thư mục có `engine/`.
- Test **nạp `platform.env.yaml` của chính repo** để resolve `%%placeholder%%` ⇒ nó kiểm cả
  catalog + config thật, không phải mô hình giả.
- Cần trên PATH: `score-k8s`, `kubectl`, `git`, `gh`, và `pyyaml`.
  - Các test có `@needs_score_k8s` **tự SKIP nếu thiếu `score-k8s`** — nhưng đó là các test
    idempotency/render quan trọng nhất, nên muốn verify thật thì phải có `score-k8s`.
  - `test_audit.py` có lớp DB-backed chỉ chạy khi đặt `AUDIT_TEST_DATABASE_URL`, nếu không
    thì skip.
- **Đừng tin một con số case chép cứng trong tài liệu** — chạy lệnh trên để biết pass/skip
  hiện tại.

## Bộ test bảo vệ bất biến nào (map mảng test → luật)

Mỗi "mảng" (comment `# PHASE …` trong `test_engine.py`) canh một nhóm bất biến. Test đỏ ở
mảng nào = vừa phá luật đó:

| Mảng | Bất biến |
|---|---|
| PHASE 0 — naming/digest, toolchain pinning, feature flags | quy tắc tên/đường/digest; ghim phiên bản; cờ mặc định off |
| PHASE 1 — ApplicationValues, placeholder allowlist, secret-in-file | values theo env; `${resources}` chỉ ở 4 chỗ; secret không trộn literal |
| PHASE 2 — Vault foundation, verify identity, onboarding tool | policy theo tiền tố; verify không đọc Secret; `vault-onboard` |
| PHASE 3 — app secrets, verify chờ secret | `secretRef` → `secretKeyRef`, giá trị không vào manifest |
| PHASE 4 — postgres class `application` | profile theo env; credential không nằm trong state |
| PHASE 5 — stack catalog + score-compose | catalog tự nhất quán; app sinh ra không sót `__TOKEN__`; routing `/api` đúng thứ tự |
| state stability / ancestry guard / managed-by / promote | render idempotent; `guard_ordering`; strip `managed-by`; 3 chế độ promote |

## Render/verify cục bộ — không cần cụm

`render` có state store dạng file nên chạy offline:

- `--state-file <path>`: giữ state trong file (để test/replay tay).
- `--no-state`: tắt persistence — **tái hiện đúng bug churn**, chỉ dùng đối chứng.
- Kiểm nhanh runner: `python3 idpctl --env-config platform.env.yaml preflight`.

## Quy trình bắt buộc khi thêm/đổi hành vi

1. **Trước khi coi là xong**, chạy full `pytest`. Đỏ = hành vi sai.
2. **Không sửa/nới lỏng test cho pass.** Test đỏ nghĩa là code sai. Nếu đổi hợp đồng có chủ
   ý, đổi test kèm lý do và cập nhật ADR/tài liệu.
3. **Thêm hành vi mới ⇒ thêm test** vào đúng mảng cùng chủ đề.
4. Thiếu `score-k8s` thì cài rồi chạy lại — đừng coi "pass" khi các test render bị skip.

## Test một feature qua luồng thật (khi sửa idpctl/catalog)

`pytest` xanh chỉ nói **logic** đúng. Để biết một feature **chạy được**, cho nó đi hết luồng
thật trên harness sống:

- **Nút thắt:** `repository_dispatch` LUÔN chạy code platform từ nhánh mặc định, nên code
  chưa merge không đi qua đường app-CI thường được.
- **Cửa thoát:** `workflow_dispatch` cho chọn ref —
  `gh workflow run deploy.yaml --ref <nhánh>` chạy đúng `deploy.yaml` và checkout `idpctl`
  theo nhánh đó.

**Ranh giới cô lập = TÊN APP, không phải nhánh git.** Luôn test bằng một app tên-mới
throwaway; đừng trỏ run nhánh feature vào một app đang chạy (nó ghi vào config repo của app
đó → Fleet áp lên cụm). Xong thì gỡ app throwaway thủ công
([runbook 8](runbook/xoa-app-va-giu-du-lieu.md)).

Vòng AI tự lái (ảnh thật): đẩy code app test → CI **build-only** push ảnh → poll registry
tới khi tag có mặt → `gh workflow run deploy.yaml --ref <nhánh> -f app=… -f sha=<sha CI vừa
build>` → verify bằng phép đo (ảnh trong manifest == ảnh trong registry; rollout thật;
`curl` qua gateway → 200). Ba "chốt" — đợi ảnh, khớp tên ảnh, đúng ref — là thứ khiến việc
tách build khỏi trigger an toàn.

## Môi trường verify & hạ tầng

**Đừng tin ảnh chụp trạng thái chép trong tài liệu — tự lấy trạng thái sống:**

- **Cụm verify:** `kind-staging`, `kind-prod` (context `kind-*`). Xem:
  `kubectl config get-contexts` / `kind get clusters`.
- **Probe hạ tầng (read-only, an toàn cả prod):** `./tools/thu-thap-ha-tang.sh` — in node,
  storageClass, Gateway API, traefik, Fleet, CRD… không in secret. Chạy trước khi kết luận
  "phải dựng mới" hay "đã có".
- **Công cụ đã ghim (ADR 0006):** kiểm bằng `preflight` + `--version`.

Kiểm capability theo feature/backend đang bật (read-only):

```bash
python3 idpctl --env-config platform.env.yaml doctor            # với cụm đang trỏ
python3 idpctl --env-config platform.env.company.yaml doctor --no-cluster
```

Doctor chỉ kiểm thứ feature đang bật cần; thiếu-quyền-kiểm là WARN (không báo xanh giả),
FAIL = blocker.

## Harness capability (dựng lại được nhiều lần)

```bash
./tools/dung-vault-harness.sh --context kind-staging       # Vault dev + VSO đúng phiên bản
./tools/thu-nghiem-vault-e2e.sh --context kind-staging     # E2E: secret-set → sync → outage → resync
./tools/thu-nghiem-db-statefulset.sh --context kind-staging  # backend statefulset chạy thật
./tools/dung-harness-cong-ty.sh --up --context kind-staging  # + gateway HTTPS / registry riêng
```

Vault dev mode **mất sạch dữ liệu khi pod restart** — chạy lại script để dựng lại mount,
secret đã ghi thì phải ghi lại. Công ty đã có Vault thật ⇒ không chạy script này, chỉ điền
`vault.*` rồi `idpctl vault-foundation --apply`.

## Company compatibility — một source, hai profile

Cùng source chạy cả harness (github.com + GHCR + CNPG + Traefik HTTP) lẫn hình dạng công ty
(GHES + Harbor/HTTPS + StatefulSet + Traefik HTTPS). Khác biệt nằm hết ở
`platform.env.company.yaml`. Chọn profile bằng `--env-config`; test hai profile:

```bash
python3 -m pytest test_engine.py -q \
  -k "profile or backend or doctor or statefulset or sectionName or company_coordinates or github"
```

## Tài liệu liên quan

- [docs/deployment.md](deployment.md) — hợp đồng portal ↔ engine và cách verify trên cụm.
- [docs/architecture.md](architecture.md) — kiến trúc theo code.
- [docs/adr/](adr/) — quyết định kiến trúc.
- Comment tại chỗ trong `idpctl`/`engine/` — phần lớn giải thích "vì sao".
