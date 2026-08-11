# Hợp đồng portal ↔ orchestrator

Tài liệu này là **hợp đồng**: những gì một portal (hoặc bất kỳ thứ gì tự động hoá) được
phép gửi tới nền tảng, và những gì nó được đảm bảo nhận lại. CLAUDE.md và
HUONG-DAN-KIEM-THU.md đều trỏ tới đây.

Nguyên tắc bao trùm: **portal gửi Ý ĐỊNH, không gửi toạ độ.** Portal không bao giờ gửi
namespace, đường dẫn Vault, tên Secret, StorageClass, URL registry hay tài nguyên database
thô. Nó gửi tên app, commit, môi trường — nền tảng suy ra phần còn lại từ
`platform.env.yaml`. Nếu một trường toạ độ hạ tầng xuất hiện trong payload, đó là lỗi
thiết kế, không phải một tính năng.

---

## 1. Hai đường vào

### 1.1. `repository_dispatch` — đường sản xuất

GitHub chỉ chạy workflow `repository_dispatch` **từ nhánh mặc định**. Đó là tính chất của
GitHub, không phải lựa chọn của nền tảng, và nó có một hệ quả phải nhớ: **một lần dispatch
thành công không chứng minh code trên nhánh phát triển đã chạy.**

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
    "tag_strategy": ""             // TUỲ CHỌN — xem 2.3; BỎ TRỐNG là lựa chọn đúng
  }
}
```

```jsonc
{
  "event_type": "promote-request",
  "client_payload": {
    "app": "order-management",
    "repo": "org/order-management",
    "tag": "v1.4.2",
    "mode": "from-staging"         // "from-staging" | "tag-only" | "re-render"
  }
}
```

`verify-request` nhận `{ app, env }` và chỉ chạy lại phần kiểm chứng.

### 1.2. `workflow_dispatch` — đường kiểm chứng

Dùng khi cần chạy bằng code của một nhánh cụ thể. Job đầu tiên **in ra `github.ref` và
commit SHA**; evidence phải ghi lại hai giá trị đó. Không được dùng kết quả của một lần
chạy `main` làm bằng chứng cho nhánh phát triển.

---

## 2. Những gì nền tảng đảm bảo

### 2.1. Idempotent

Gửi lại đúng một payload không tạo bản sao thứ hai. Mọi bước kiểm-trước-khi-tạo. Với
onboarding, các bước đã `done` bị bỏ qua và chỉ bước lỗi chạy lại.

### 2.2. Không bao giờ deploy lùi

`guard_ordering` từ chối apply một commit **cũ hơn** commit đang chạy. Rollback là một thao
tác có chủ ý (`promote`), không phải hệ quả của việc gửi nhầm thứ tự hai dispatch.

### 2.3. Tag ảnh do nền tảng quyết định, và CI phải HỎI

CI của app không được tự suy ra tên ảnh. Nó gọi:

```bash
python3 orchestrate.py --env-config platform.env.yaml image-plan \
  --app <app> --registry <registry> --tag <sha> --app-dir . --with-build
```

và build đúng những gì lệnh đó trả về. Hai bên tính khác nhau nghĩa là Fleet apply một ảnh
chưa ai đẩy lên — đã xảy ra hai lần trong chương trình này.

**Mọi lệnh `orchestrate.py` trong CI/script phải truyền `--env-config`**, kể cả lệnh trông
như không cần toạ độ: thiếu nó thì feature flag vô hình và hai bên tính ra hai kết quả.

### 2.4. Bí mật không bao giờ đi qua hợp đồng này

Payload không có trường nào chứa giá trị bí mật, và sẽ không bao giờ có. Portal muốn nạp
một bí mật thì gọi `secret-set` bằng danh tính của **con người** yêu cầu, với token có
policy ghi — không phải bằng token của CI. CI không cầm Vault token.

### 2.5. Prod đi qua người duyệt

`onboard-activate-prod` mở pull request và dừng ở `PENDING_PROD_APPROVAL`. Nền tảng không
tự merge. Branch protection trên nhánh prod là thứ **GitHub** thực thi — một cờ trong file
cấu hình không thay thế được nó.

### 2.6. Trạng thái đọc được từ ngoài

```bash
python3 orchestrate.py --env-config platform.env.yaml onboard-status --app <app>
```

Bản ghi nằm trong ConfigMap `idp-onboarding-<app>` ở namespace `kubernetes.state_namespace`
— cố ý **không** phải Secret: nó không chứa giá trị bí mật, chỉ tên đường dẫn, tên kho,
tên ảnh và trạng thái.

Máy trạng thái: mục 13.2 của kế hoạch. Nhánh `WAITING_FOR_USER_SECRETS`,
`PENDING_PROD_APPROVAL` là **trạng thái chờ người**, không phải lỗi — portal phải hiển thị
chúng như việc-cần-làm, không như sự cố.

---

## 3. Những gì nền tảng KHÔNG hứa

- **Không hứa rollback dữ liệu.** Rollback ảnh không rollback schema.
- **Không hứa đổi `class` database là an toàn.** Xem
  [`chuyen-doi-postgres-sang-class-application.md`](chuyen-doi-postgres-sang-class-application.md).
- **Không hứa xoá app là hoàn tác được.** `offboard` giữ backup, kho Git và (mặc định) bí
  mật ở dạng xoá mềm — nhưng namespace thì đi thật.
- **Không hứa app tự hồi phục sau khi xoay vòng credential database.** Dùng
  `rotate-db-credential`; nó làm đúng thứ tự và chờ từng bước.

---

## 4. Cách verify trên cụm thật

Thứ tự này là thứ tự các phase đã dùng, và nó phát hiện lỗi theo đúng thứ tự rẻ-trước.

### 4.1. Trước khi chạm cụm

```bash
git branch --show-current && git rev-parse HEAD     # ghi vào evidence
python3 -m pytest test_orchestrate.py -q            # thiếu score-k8s ⇒ ~26 test tự skip
python3 orchestrate.py --env-config platform.env.yaml \
  preflight --require-cluster --require-vault --require-score-compose
```

Nếu vừa đụng provisioner có `class`: chạy full suite **2–3 lần**. score-k8s chọn
provisioner nạp sau cùng, và lỗi loại này biểu hiện là test fail **ngẫu nhiên chỗ khác**.

### 4.2. Render từ working tree, không qua workflow

```bash
# KHÔNG render thẳng vào examples/ — renderer ghi đè image tag vào score.yaml được track.
cp -r examples/<app> /tmp/app-copy
python3 orchestrate.py --env-config platform.env.yaml render \
  --app <app> --env staging --registry <registry> --tag <sha> \
  --catalog . --app-dir /tmp/app-copy --work /tmp/work --out /tmp/manifests.yaml \
  --state-file /tmp/state.yaml
```

### 4.3. Regression app legacy — bằng chứng mạnh nhất của lời hứa brownfield

Render cùng một app bằng **worktree tại baseline** và bằng **HEAD**, dùng chung state file,
render từ **bản sao** thư mục app, với `features.*` tắt. Bốn cặp
(`simple-nginx`/`app-with-postgres` × `staging`/`prod`) phải **giống nhau từng byte**.

### 4.4. Apply và verify thật

```bash
kubectl -n <app>-<env> apply -f /tmp/manifests.yaml
python3 orchestrate.py --env-config platform.env.yaml verify \
  --app <app> --env <env> --manifests /tmp/manifests.yaml
```

`verify` chờ **rollout thật** (`updatedReplicas`/`observedGeneration`), không nhìn
`availableReplicas` — pod cũ vẫn available trong lúc bản mới không lên nổi. Nó cũng chờ
`VaultStaticSecret` đồng bộ, `Cluster` `Ready`, và **`firstRecoverabilityPoint`** khi có
kho object.

### 4.5. Sức khoẻ sau khi deploy

```bash
./tools/kiem-suc-khoe.sh --namespace <app>-<env>
kubectl -n fleet-local get gitrepo          # phải 1/1, không Modified vĩnh viễn
```

Ngưỡng và ý nghĩa từng kiểm: [`canh-bao.md`](canh-bao.md). Xử lý khi kêu:
[`runbook/`](runbook/).

### 4.6. Dọn fixture

Mọi thứ dựng lên để đo phải được dọn và **ghi lại đã dọn những gì**: namespace, kho GitHub,
đường dẫn Vault, package trên registry, GitRepo của Fleet.

---

## 5. Liên quan

- `HUONG-DAN-KIEM-THU.md` — harness test và môi trường verify.
- `docs/adr/0010-may-trang-thai-onboarding.md` — vì sao onboarding là máy trạng thái.
- `docs/canh-bao.md`, `docs/runbook/` — vận hành sau khi đã chạy.
