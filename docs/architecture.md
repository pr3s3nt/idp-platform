# Kiến trúc

Mô tả kiến trúc **thực tế theo code** của `idp-platform`. Nguồn: `engine/*.py`, `idpctl`,
`.github/workflows/`, `platform.env.yaml`. Lý do đằng sau từng quyết định lớn nằm ở
[docs/adr/](adr/).

## 1. Ý tưởng trung tâm

Lập trình viên khai **ý định** trong `score.yaml` (workload cần gì: container, cổng,
resource `postgres`, `route`, `environment`, `secret`…). Nền tảng suy ra **toạ độ** (đường
Vault, tên namespace, StorageClass, Gateway…) từ `platform.env.yaml` và biến ý định thành
manifest Kubernetes. Lập trình viên không bao giờ viết namespace, đường Vault hay tên
Secret; nếu một trường toạ độ hạ tầng lọt vào đầu vào của app, đó là lỗi thiết kế.

Hệ quả kiến trúc: **catalog mang hình dạng, `platform.env.yaml` mang toạ độ.** Provisioner
và patch chỉ chứa `%%placeholder%%`; render mới thay bằng giá trị theo môi trường.

## 2. Ba loại repository

| Repo | Ai ghi | Chứa gì |
|---|---|---|
| **Kho ứng dụng** | đội ứng dụng | mã nguồn, `score.yaml`, `.score-values/values.yaml`, `Dockerfile`, `platform.lock`, CI của app |
| **Kho platform** (repo này) | đội platform | engine, catalog, template, workflow, `platform.env.yaml` |
| **Kho cấu hình** (`idp-{app}-config`) | CHỈ orchestrator | manifest đã render, tách theo môi trường (`staging/`, `prod/`), Fleet đọc từ đây |

Ba kho, ba vòng đời, ba bộ quyền. Đội ứng dụng không ghi được manifest lên cụm; chỉ
orchestrator ghi kho cấu hình, và chỉ Fleet áp kho cấu hình lên cụm.

## 3. `idpctl` và engine

`idpctl` là entrypoint mỏng gọi `engine.cli.main`. Business logic chia theo trách nhiệm:

| Module | Trách nhiệm |
|---|---|
| `engine/context.py` | Nạp `platform.env.yaml` (`EnvConfig`), suy ra toạ độ theo env. |
| `engine/resources.py` | Provisioner, database (`class: application`), Vault onboard, RBAC verify. |
| `engine/values.py` | `ApplicationValues` (values theo môi trường), guard đổi class postgres. |
| `engine/catalog.py` | Stack catalog: sinh/validate/upgrade kho ứng dụng, discover workload. |
| `engine/render.py` | Render Score → manifest; state store; idempotency; split secret. |
| `engine/delivery.py` | Commit kho cấu hình, `ensure-gitrepo` (Fleet), `verify`, promote. |
| `engine/audit.py` | Kho lịch sử triển khai + KPI (tuỳ chọn, PostgreSQL). |
| `engine/cli.py` | Định nghĩa mọi lệnh con và cờ. |

Danh sách lệnh `idpctl` đầy đủ nằm trong các `sub.add_parser(...)` của `engine/cli.py` và
được liệt kê ở [docs/deployment.md](deployment.md).

## 4. Luồng triển khai (deploy)

Nguồn: `.github/workflows/deploy.yaml`. Workflow là adapter; mỗi bước gọi `idpctl`.

1. **Kích hoạt** — `repository_dispatch` (`deploy-request`, đường sản xuất, CI app gọi)
   hoặc `workflow_dispatch` (chọn ref, đường kiểm chứng). Concurrency theo tên app: mọi
   run cùng app xếp hàng tuần tự (chống ghi state/kho cấu hình lệch thứ tự).
2. **Token bot** — mint token GitHub App (hoặc `BOT_TOKEN`), sống ngắn, phạm vi hẹp.
3. **`config --export`** — nạp toạ độ từ `platform.env.yaml` vào môi trường workflow.
4. **`audit-start`** — ghi bắt đầu deploy (no-op nếu audit tắt).
5. **`preflight --require-cluster`** — kiểm công cụ + phiên bản đã ghim khớp runner/cụm.
6. **Checkout** — app tại đúng SHA (full history cho ancestry guard), catalog tại ref ghim
   trong `platform.lock`, và kho cấu hình (tạo trước nếu chưa có bằng `tao-app-moi.sh`).
7. **`render`** — Score + values + catalog → `config/<env>/manifests.yaml`; giữ state
   trong Secret của cụm để render idempotent; tách secret ra khỏi manifest công khai.
8. **`vault-auto-setup`** — tạo Vault policy/role cho app/env nếu có `VAULT_TOKEN`.
9. **`apply-secrets`** — tạo (create-if-missing) imagePullSecret và các Secret nền tảng.
10. **`commit`** — ghi manifest vào kho cấu hình; push thẳng hoặc mở PR tuỳ branch
    protection; `guard_ordering` chặn commit cũ đè commit mới hơn.
11. **`ensure-gitrepo`** — tạo `GitRepo` của Fleet nếu chưa có (không bao giờ ghi đè của
    đội khác). Đây là thứ khiến Fleet KÉO manifest về cụm.
12. **`verify`** — chờ rollout thật trên cụm (`updatedReplicas`/`observedGeneration`,
    condition Ready), rồi `audit-snapshot` chụp read-only tài nguyên đã lên.
13. **Audit finalizers** — đóng sổ success/failure/cancelled; khi fail, `notify-failure`
    bình luận lên đúng commit app.

Promote lên prod là một đường riêng (`promote.yaml`): `from-staging` (chép đúng bộ ảnh
staging đã verify), `tag-only` (đổi tag một manifest có sẵn) hoặc `re-render`.

## 5. State và tính idempotent

`render` giữ state cho mỗi (app, env) — mặc định trong một Secret ở
`kubernetes.state_namespace`, hoặc file (`--state-file`) khi chạy offline/test. State giữ
tên resource và mật khẩu đã sinh để hai lần render liên tiếp ra **y hệt** (chống Fleet
churn). Ghi state đồng thời được bảo vệ bằng optimistic lock theo `resourceVersion`.
`--no-state` tái hiện đúng bug churn và chỉ dùng để đối chứng trong test.

## 6. Bí mật (Vault + VSO)

Vault là kho bí mật duy nhất (ADR [0002](adr/0002-vault-only-secret-store.md)). App chỉ
khai `secretRef: {name, key}`; nền tảng ráp đường dẫn theo `vault.path_template`
(`apps/<app>/<env>/<name>`). Render sinh `VaultStaticSecret → VaultAuth → VaultAuthGlobal`;
Vault Secrets Operator trong cụm mới là thứ đọc Vault và tạo Kubernetes Secret. Nhờ vậy CI
không cần Vault token và git chỉ chứa tham chiếu, không chứa giá trị. Mỗi app một danh
tính Vault theo namespace; verify dùng danh tính chỉ-đọc **không** đọc được Secret (ADR
[0007](adr/0007-topo-vso-va-danh-tinh-verify.md)).

## 7. Database (`class: application`)

App khai `type: postgres, class: application` và nhận cùng một bộ output
(`host/port/database/username/password`) ở mọi môi trường. Khác biệt staging↔prod nằm hết
trong `database_profiles` của `platform.env.yaml` (số instance, dung lượng, HA, backup).
Backend chọn qua `database.backend`: `cnpg` (CloudNativePG, mặc định, HA + backup) hoặc
`statefulset` (một bản sao, không HA/backup, **bị chặn ở prod**). Output giống hệt hai
backend. Mật khẩu do platform sinh và ghi thẳng Vault (không nằm trong state), CNPG dùng
chính Secret đó lúc bootstrap. Xem ADR [0005](adr/0005-database-profile.md),
[0008](adr/0008-provider-database-va-credential.md) và
[chuyển đổi postgres sang class application](chuyen-doi-postgres-sang-class-application.md).

## 8. GitOps (Fleet)

Fleet **kéo** manifest từ kho cấu hình về cụm; nền tảng không `kubectl apply` workload.
`ensure-gitrepo` liệt-kê-rồi-khớp `GitRepo` theo `spec.repo` (không giả định tên
`{app}-{env}`) để không ghi đè bundle của đội khác. Thiếu `GitRepo` là lỗi im lặng số một:
mọi bước xanh, manifest đúng trong git, mà cụm trống trơn — nên bước tạo nó nằm ngay trong
luồng deploy.

## 9. Ghim phiên bản

Hai thứ được ghim, độc lập nhau: **catalog** (hình dạng) ghim theo `platform.lock` trong
kho app; **công cụ render** (`score-k8s`, `score-compose`, VSO, CNPG operator) ghim trong
`platform.env.yaml` và `preflight`/`render` dừng nếu lệch (ADR
[0006](adr/0006-ghim-phien-ban-toolchain.md)). `platform.env.yaml` **không** ghim theo lock
vì nó là toạ độ của hạ tầng đang chạy, không phải dữ liệu catalog.

## 10. Audit (tuỳ chọn)

Khi `audit.enabled: true` và có `AUDIT_DATABASE_URL`, các lệnh `audit-*` ghi timeline
deploy + KPI vào một PostgreSQL. Mặc định fail-open: sự cố kho lịch sử chỉ cảnh báo, không
đánh sập deploy. Tắt (mặc định của bản gốc) thì luồng deploy chạy y như trước, không mở DB.
SQL nằm hết trong `engine/audit.py`; giá trị bí mật không bao giờ vào cột nào.

## Sơ đồ luồng

```text
 app repo (score.yaml, values)          platform repo (engine + catalog + platform.env.yaml)
        │  push                                   │
        ▼                                         ▼
   CI app: build+push ảnh  ──dispatch──►  deploy.yaml ──► idpctl render
        (image-plan)                                   │  (+ vault-auto-setup, apply-secrets)
                                                       ▼
                                             kho cấu hình (idp-<app>-config)
                                                       │  Fleet KÉO
                                                       ▼
                                                    cụm K8s  ──► idpctl verify (rollout thật)
                                             VSO ← Vault (bí mật)   CNPG (database)
```
