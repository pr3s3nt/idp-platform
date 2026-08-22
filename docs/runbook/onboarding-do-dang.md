# 6. Deploy dở dang và retry

> **Máy trạng thái onboarding đã bị GỠ** (commit `c5d28ac`: xoá `engine/onboarding.py` +
> các lệnh `onboard`/`onboard-status`/`onboard-activate-prod`, và ConfigMap state
> `idp-onboarding-<app>`). Không còn "trạng thái onboarding" để tra, không còn
> `--force-step`/`--stop-after`. Runbook này thay phần đó bằng cách retry luồng deploy thật.

## Nguyên tắc

Đưa app lên là **luồng deploy chuẩn**, và mọi bước của nó **idempotent** nên retry an toàn:

- `render` giữ state, sort manifest → render lại ra **y hệt** (chống Fleet churn).
- `ensure-gitrepo` **không bao giờ ghi đè** GitRepo đã có (`engine/cli.py`).
- `apply-secrets`/commit coi `AlreadyExists` là thành công (create-if-missing).
- `vault-onboard` + `vault-auto-setup` kiểm-trước-khi-tạo policy/role/SA.

Nên "retry" = **chạy lại đúng luồng** (đẩy lại commit, hoặc `gh workflow run deploy.yaml`),
không phải dọn tay rồi làm lại từ đầu.

## Xác nhận đang ở đâu (không còn onboard-status — đọc tài nguyên thật)

```bash
APP=<app>; ENV=staging
kubectl -n fleet-local get gitrepo | grep "$APP"                     # config repo đã vào Fleet chưa
kubectl -n "$APP-$ENV" get pods,cluster.postgresql.cnpg.io,vaultstaticsecret
kubectl get all,gitrepo -A -l idp.platform/application="$APP"        # mọi thứ platform tạo cho app
```

Kiểm rollout thật (không nhìn `availableReplicas` — dễ xanh giả):

```bash
kubectl -n "$APP-$ENV" rollout status deploy --timeout=120s
```

> `idpctl verify` là bước trong workflow (cần `--manifests` là thư mục vừa render), không
> phải lệnh soi tay tiện dụng — vận hành cứ dùng `kubectl rollout status` ở trên.

## Retry theo triệu chứng

| Triệu chứng | Nguyên nhân thường gặp | Việc |
|---|---|---|
| GitRepo có, pod chưa lên | ảnh chưa có trong registry, hoặc render trỏ tag sai | poll registry tới khi tag thật xuất hiện, rồi deploy lại đúng SHA đó |
| `VaultStaticSecret` `SecretSynced=False` | Vault chưa có policy/role/secret cho app | chạy lại `vault-onboard` + `vault-auto-setup`, rồi `secret-set` các khoá còn thiếu |
| pod `CreateContainerConfigError` chờ Secret | secret bên thứ ba chưa nạp | `secret-set` đúng khoá vào `apps/<app>/<env>/<name>`, VSO tự sync lại |
| prod chưa có gì | prod KHÔNG tự chảy từ staging | kích hoạt prod bằng luồng riêng (xem dưới) |

## Kích hoạt prod (thay onboard-activate-prod)

Prod đi qua một lần deploy **env=prod** vào nhánh config prod, qua pull request có người
duyệt (branch protection). Không có lệnh `onboard-activate-prod`.

```bash
gh workflow run deploy.yaml --ref main -f app=$APP -f repo=<org/app> -f sha=<sha> -f env=prod
```

Cách khác: workflow `promote` (copy đúng bộ ảnh staging sang prod, `idpctl promote --mode
from-staging`). Nó **chỉ nhận `repository_dispatch` type `promote-request`** (không có
`workflow_dispatch`) — app CI phát khi merge sang nhánh production, hoặc phát tay bằng
`gh api .../dispatches -f event_type=promote-request`.

## Bẫy đã gặp (vẫn đúng)

- **Bí mật KHÔNG tự chảy từ staging sang prod** — có chủ ý. Prod phải `secret-set` riêng.
- **CI sinh từ nhánh chưa merge thì đỏ.** Mẫu CI checkout platform ở `ref: main`; app CI gọi
  `deploy.yaml` qua `repository_dispatch` cũng LUÔN chạy platform từ `main`. Thứ tự bắt buộc:
  **merge trước, để app CI dispatch sau**. Muốn thử code platform nhánh chưa merge thì dùng
  `gh workflow run deploy.yaml --ref <nhánh>` (xem `HUONG-DAN-KIEM-THU.md`).
- **Render theo đỉnh nhánh trỏ tới ảnh chưa ai đẩy.** Deploy lấy **đúng commit đã build**;
  đợi tag ảnh thật xuất hiện trong registry trước khi deploy.

## Nếu tài nguyên còn nhưng "không rõ trạng thái"

Không còn bản ghi state ngoài tiến trình để mất. Mọi tài nguyên platform tạo đều mang nhãn,
nên luôn tìm lại được bằng nhãn rồi chạy lại luồng deploy — idempotent sẽ nhận ra cái đã có:

```bash
kubectl get all,gitrepo -A -l idp.platform/application=<app>
```
