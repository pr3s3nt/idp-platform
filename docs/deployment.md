# Triển khai

Tài liệu này mô tả **luồng deploy thực tế** và cách đưa một app mới từ đầu đến staging rồi
prod. Nguồn: `.github/workflows/{deploy,promote,verify}.yaml`, `engine/`, `idpctl`,
`tools/tao-app-moi.sh`, template CI trong `templates/app-ci-*.yaml`.

Nguyên tắc bao trùm: **portal/CI gửi Ý ĐỊNH, không gửi toạ độ.** Đầu vào chỉ có tên app,
commit, môi trường; nền tảng suy ra namespace, đường Vault, tên Secret, registry… từ
`platform.env.yaml`.

---

## 1. Hai đường vào

### 1.1. `repository_dispatch` — đường sản xuất

GitHub chỉ chạy workflow `repository_dispatch` **từ nhánh mặc định**, nên một lần dispatch
thành công không chứng minh code trên nhánh phát triển đã chạy.

```jsonc
// POST /repos/<org>/<platform-repo>/dispatches
{
  "event_type": "deploy-request",
  "client_payload": {
    "app":   "order-management",   // BẮT BUỘC — quyết định tên kho cấu hình và namespace
    "repo":  "org/order-management",
    "sha":   "<commit SHA đầy đủ>",
    "image": "orders",             // TUỲ CHỌN — mặc định = app
    "env":   "staging",            // TUỲ CHỌN — mặc định staging
    "tag_strategy": ""             // TUỲ CHỌN — BỎ TRỐNG là lựa chọn đúng
  }
}
```

Promote dùng `event_type: "promote-request"` với `{app, repo, tag, mode}` (mode:
`from-staging` | `tag-only` | `re-render`). `verify-request` nhận `{app, env}` và chỉ chạy
lại phần kiểm chứng.

### 1.2. `workflow_dispatch` — đường kiểm chứng

Dùng khi cần chạy bằng code của một nhánh cụ thể (`gh workflow run deploy.yaml --ref <nhánh>`).
Bước "Checkout platform" không ghim ref, nên đây là cách chạy code `idpctl` chưa merge trên
runner + cụm thật. Xem [docs/testing.md](testing.md).

## 2. Nền tảng đảm bảo gì

- **Idempotent** — gửi lại đúng một payload không tạo bản sao thứ hai; mọi bước
  kiểm-trước-khi-tạo.
- **Không bao giờ deploy lùi** — `guard_ordering` từ chối apply một commit cũ hơn commit
  đang chạy. Rollback là thao tác có chủ ý (`promote`), không phải hệ quả gửi nhầm thứ tự.
- **Tag ảnh do nền tảng quyết định** — CI của app không tự suy ra tên ảnh; nó HỎI:

  ```bash
  python3 idpctl --env-config platform.env.yaml image-plan \
    --app <app> --registry <registry> --tag <sha> --app-dir . --with-build
  ```

  rồi build đúng những gì lệnh đó trả về. Hai bên tính khác nhau nghĩa là Fleet apply một
  ảnh chưa ai đẩy lên.

## 3. Luồng deploy (từng bước, `deploy.yaml`)

Concurrency theo tên app (mọi run cùng app xếp hàng tuần tự). Mỗi bước gọi `idpctl`:

1. Mint token bot (GitHub App hoặc `BOT_TOKEN`).
2. `config --export` — nạp toạ độ từ `platform.env.yaml`.
3. `audit-migrate` + `audit-start` (no-op nếu audit tắt).
4. `preflight --require-cluster` — kiểm công cụ + phiên bản ghim.
5. Checkout app (đúng SHA, full history), catalog (ref trong `platform.lock`), kho cấu hình
   (tạo trước bằng `tao-app-moi.sh` nếu chưa có).
6. `render` → `config/<env>/manifests.yaml` (giữ state, split secret).
7. `vault-auto-setup --apply` — tạo Vault policy/role nếu có `VAULT_TOKEN`.
8. `apply-secrets` — imagePullSecret + Secret nền tảng (create-if-missing).
9. `commit` — ghi kho cấu hình (push thẳng hoặc PR tuỳ branch protection).
10. `ensure-gitrepo` — tạo `GitRepo` của Fleet nếu chưa có.
11. `verify` — chờ rollout thật; `audit-snapshot` chụp read-only.
12. Audit finalizers; khi fail, `notify-failure` bình luận lên commit app.

## 4. Đưa một app mới lên nền tảng

### 4.1. Sinh khung từ một stack (golden path)

```bash
python3 idpctl --env-config platform.env.yaml stack-list          # các stack có sẵn
python3 idpctl --env-config platform.env.yaml stack-new \
  --stack node-fullstack --app <app> --owner <đội> --out /đường/dẫn/kho-mới
```

`stack-new` **không ghi đè** file đã có (chạy lại được; `--force` để ép). Kho sinh ra có
`score.yaml`, `.score-values/values.yaml`, `Dockerfile`, `platform.lock`, `Makefile`,
`.env.example` và một `README.md`. Chạy thử local (cần `docker` + `score-compose`, không cần
cụm):

```bash
cd /đường/dẫn/kho-mới && make dev
```

Không dùng stack thì tự viết `score.yaml`, `Dockerfile`, `platform.lock` và CI theo mẫu
`templates/app-ci-{mot,nhieu}-service.yaml`.

### 4.2. Cấu hình và bí mật

Khai values theo môi trường trong `.score-values/values.yaml`, nạp bí mật vào Vault bằng
`secret-set`, khai database bằng `type: postgres, class: application` — tất cả ở
[docs/configuration.md](configuration.md).

### 4.3. Onboard app vào Vault (hai nửa, hai chủ sở hữu — ADR 0007)

```bash
python3 idpctl --env-config platform.env.yaml vault-onboard --app <app> --env staging --apply  # SA + VaultAuth
python3 idpctl --env-config platform.env.yaml vault-onboard --app <app> --env staging          # in policy/role Vault
python3 idpctl --env-config platform.env.yaml verify-rbac  --app <app> --env staging --apply   # danh tính verify
```

Phần Kubernetes (`--apply`) do platform chạy; phần Vault (policy + role) do **người quản trị
Vault** chạy bằng token của họ. Trong luồng deploy, `vault-auto-setup --apply` làm phần này
tự động khi có `VAULT_TOKEN`.

### 4.4. Chuẩn bị kho cấu hình + Fleet

`tools/tao-app-moi.sh` (idempotent) tạo kho cấu hình `idp-<app>-config` với hai nhánh và
`fleet.yaml`; deploy workflow tự gọi nó. `GitRepo` của Fleet được tạo bằng `ensure-gitrepo`
(không ghi đè của đội khác).

### 4.5. Deploy lên staging

Đẩy code → CI của app build+push ảnh (dùng `image-plan --with-build`) rồi dispatch
`deploy-request` (xem §1.1). Hoặc chạy tay để kiểm chứng:

```bash
gh workflow run deploy.yaml -f app=<app> -f repo=<org/app> -f sha=<sha> -f env=staging
```

### 4.6. Lên production

Prod luôn đi qua `promote` (đường `promote-request`, luôn nhắm prod):

```bash
# chép đúng bộ ảnh staging đã verify sang prod:
gh workflow run ...   # hoặc dispatch promote-request {app, repo, tag, mode:"from-staging"}
```

Nhánh prod của kho cấu hình thường bật branch protection → `commit --via-pr` mở PR, và bước
verify hoãn cho tới khi PR merge. Rollback một lần deploy dùng `promote --mode tag-only` (xem
[runbook 7](runbook/rollback-nang-cap-stack.md)).

## 5. Cài đặt nền tảng (một lần)

Trước khi app đầu tiên chạy, cần dựng:

- **Cụm Kubernetes** với: một `StorageClass` (khớp `kubernetes.storage_class`), **Gateway
  API** + **Traefik** với một `Gateway` (khớp `ingress.gateway_name`/`gateway_namespace`),
  và **Fleet** (khớp `kubernetes.fleet_namespace`).
- **Vault + Vault Secrets Operator** đúng phiên bản ghim; áp foundation bằng
  `idpctl vault-foundation --apply`. Trên harness dùng `tools/dung-vault-harness.sh`.
- **CloudNativePG** (nếu bật `postgres_application`, backend `cnpg`).
- **GitHub App** (hoặc `BOT_TOKEN` của tài khoản bot) cho danh tính ghi kho cấu hình và
  cổng duyệt prod — App token sống 1 giờ, phạm vi hẹp. Xem `tools/mint-app-token.sh`
  (tự ký JWT, chạy được cả github.com lẫn GHES). Cấp quyền: nội dung repo + tạo repo trong
  org + pull request.
- **Self-hosted runner** cho orchestrator (nhãn khớp `RUNNER_LABEL`, mặc định
  `platform-orchestrator`) với `score-k8s`, `kubectl`, `git`, `gh`, `python3` trên PATH.
- **Secret của repo platform:** `APP_ID`+`APP_PRIVATE_KEY` (hoặc `BOT_TOKEN`),
  `KUBECONFIG_STAGING`/`KUBECONFIG_PROD`, `REGISTRY_HOST`/`USER`/`PASS`, `VAULT_ADDR`/
  `VAULT_TOKEN`, và (nếu bật audit) `AUDIT_DATABASE_URL`.

Kiểm nhanh runner đủ công cụ: `python3 idpctl --env-config platform.env.yaml preflight`.
Kiểm capability cụm khớp config: `idpctl doctor`.

Đưa nền tảng sang hạ tầng công ty: xem mục "Profile công ty" trong
[docs/configuration.md](configuration.md).

---

## 6. Tham chiếu lệnh `idpctl`

Định nghĩa đầy đủ ở `engine/cli.py`. Đây là toàn bộ lệnh hiện có:

| Nhóm | Lệnh |
|---|---|
| Cấu hình/kiểm | `config`, `preflight`, `doctor` |
| Stack/app | `stack-list`, `stack-new`, `stack-validate`, `stack-upgrade`, `image-plan` |
| Render/deploy | `render`, `apply-secrets`, `commit`, `ensure-gitrepo`, `verify`, `promote` |
| Vault/secret | `vault-foundation`, `vault-onboard`, `vault-auto-setup`, `secret-set`, `rotate-db-credential`, `verify-rbac` |
| Audit/KPI | `audit-migrate`, `audit-start`, `audit-event`, `audit-finish`, `audit-snapshot`, `audit-report`, `notify-failure` |

> Không có lệnh `onboard`, `onboard-status`, `onboard-activate-prod` hay `offboard`. Việc tạo
> app dùng các lệnh ở trên (`stack-new`, `vault-onboard`, `secret-set`) cộng luồng deploy;
> việc gỡ app làm thủ công theo [runbook 8](runbook/xoa-app-va-giu-du-lieu.md).
