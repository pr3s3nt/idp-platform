# ADR 0010 — Onboarding là một máy trạng thái có bản ghi ngoài tiến trình

Trạng thái: **Superseded** — 2026-08-11 (Accepted) → gỡ ở commit `c5d28ac`.

> **Không còn hiệu lực.** Máy trạng thái onboarding (`engine/onboarding.py` + các lệnh
> `onboard`/`onboard-status`/`onboard-activate-prod`/`offboard`) đã bị gỡ. Đưa app lên nay là
> **luồng deploy chuẩn** (push → CI build → `deploy.yaml` render+commit → Fleet); Vault làm
> bằng `vault-onboard` + `vault-auto-setup`; prod qua deploy `env=prod`/`promote`; dọn app làm
> tay theo `runbook/xoa-app-va-giu-du-lieu.md`. Giữ ADR này làm lịch sử quyết định.
> Bối cảnh bên dưới mô tả trạng thái *khi được chấp nhận*, không phải code hiện tại.

## Bối cảnh

Tạo một app mới cần mười hai việc chạm vào bốn hệ thống khác nhau: GitHub (hai kho), Vault
(policy, role, credential), Kubernetes (namespace, ServiceAccount, VaultAuth, GitRepo của
Fleet) và registry (ảnh). Chúng phụ thuộc nhau theo thứ tự, và **bất kỳ việc nào cũng có
thể hỏng giữa chừng** — hết quyền, mạng chập, ai đó chưa nạp API key.

Cách hiển nhiên là một script chạy từ trên xuống. Nó hỏng theo ba kiểu, cả ba đều đã thấy
trong các phase trước:

1. **Chạy lại tạo bản sao.** Script không nhớ nó đã làm tới đâu, nên lần chạy thứ hai tạo
   thêm một kho, thêm một namespace, và — tệ nhất — ghi một mật khẩu database MỚI đè lên
   database đang có dữ liệu. Mọi bước đều báo thành công.
2. **Hỏng ở giữa để lại rác không ai biết.** Không có chỗ nào ghi "đã tạo kho, chưa tạo
   Vault role", nên người tiếp theo phải tự đi dò từng hệ thống.
3. **Báo xong khi chưa xong.** Một app thiếu bí mật của bên thứ ba vẫn deploy được: pod
   dừng ở `CreateContainerConfigError`, và `verify` chờ hết giờ rồi báo "0/1 replicas
   ready". Câu đó đúng, vô dụng, và gửi người trực đi soi image trong khi chẳng có gì
   hỏng — chỉ là chưa ai dán khoá vào.

## Quyết định

**1. Mười ba bước, mỗi bước kiểm-trước-khi-tạo, và bản ghi nằm NGOÀI tiến trình.**

Bản ghi là một ConfigMap `idp-onboarding-<app>` trong `kubernetes.state_namespace` (hoặc
một file khi chạy offline). Nó ghi: request đã chuẩn hoá, khoá idempotency, trạng thái,
trạng thái từng bước, và kết quả (URL kho, URL staging/prod, user database, bí mật còn
thiếu).

Cất ở đó vì hai lý do. Một lần onboarding hỏng thường hỏng **trước khi** kho cấu hình kịp
tồn tại, nên state không thể nằm trong kho cấu hình. Và người mở lại việc dở dang hiếm khi
là người bỏ dở nó — nên state phải đọc được từ một máy khác.

**ConfigMap chứ không phải Secret, có chủ ý**: bản ghi này không chứa giá trị bí mật nào,
chỉ đường dẫn Vault, tên kho và tên ảnh. Cất vào Secret là dạy người đọc rằng trong đó có
bí mật, và rồi sẽ có người viết bí mật thật vào.

**2. Khoá idempotency là băm của chính request.**

Cùng một file request = cùng khoá = tiếp tục bản ghi cũ. Sửa request rồi chạy lại = khoá
khác, và công cụ **DỪNG** thay vì âm thầm dựng lại một app đang chạy theo hình dạng mới.
Đổi stack version của một app đang sống là một cuộc nâng cấp có pull request
(`stack-upgrade`), không phải một lần onboarding thứ hai.

**3. "Đang chờ người" là một TRẠNG THÁI, không phải một lỗi.**

`WAITING_FOR_USER_SECRETS` và `PENDING_PROD_APPROVAL` dừng máy trạng thái đúng chỗ, ghi lý
do và **đúng lệnh phải chạy tiếp**, rồi thoát 0. Biến chúng thành lỗi sẽ dạy người vận
hành bỏ qua lỗi của công cụ này. Điều kiện bù lại: cả hai **không bao giờ** trở thành
`READY`.

Bí mật được kiểm **TRƯỚC** khi chờ rollout, không phải sau. Đó là toàn bộ điểm: kiểm sau
thì thông tin duy nhất người dùng nhận được là một lần verify hết giờ.

**4. Bí mật của platform và bí mật của người dùng là hai loại khác nhau.**

Credential database do platform tự sinh và ghi thẳng vào Vault (bước 6) — người dùng không
bao giờ thấy nó. Mọi `secretRef` khác trong values là của đội ứng dụng, và onboarding chỉ
**đếm khoá còn thiếu**, không bao giờ đọc giá trị. Phân biệt hai loại là thứ làm nên khác
nhau giữa "đang chờ bạn dán API key" và "platform hỏng".

**5. Prod luôn đi qua pull request, không điều kiện.**

Nhánh prod của kho cấu hình **có thể** chưa bật bảo vệ — trên một cụm thử thì gần như chắc
chắn là chưa. Nếu để logic đoán theo branch protection như đường deploy thường, prod sẽ
được push thẳng và mất luôn điểm kiểm soát duy nhất của con người. Nên `onboard-activate-prod`
truyền `via_pr=True` cứng.

Và prod chạy **đúng bộ ảnh staging đã verify**: render prod xong thì `copy_images` chép
tham chiếu ảnh từ manifest staging đè lên. Với `tagStrategy: content` mỗi workload mang
nhãn riêng, nên "ảnh đã verify" không phải MỘT giá trị mà là cả một bộ — chép từ manifest
là cách duy nhất đúng khi kho có nhiều service.

**6. Hai nửa quyền, hai chủ sở hữu.**

Thao tác GitHub chạy bằng `gh` (danh tính người dùng). Thao tác Vault cần `VAULT_TOKEN`
riêng, đọc từ môi trường và không bao giờ từ cấu hình. Quyền tạo repo **không** được suy
ra thành quyền viết policy Vault. Khi người chạy không có token Vault, công cụ dừng và in
đúng lệnh cho người quản trị Vault (`vault-onboard --print-policy`).

## Hệ quả

- Một lần retry là an toàn và rẻ: bước đã `done` thì bỏ qua. Đo được trên harness — lần
  chạy thứ hai không tạo kho, namespace, GitRepo hay credential thứ hai nào.
- `--force-step` cho phép chạy lại một bước đã xong khi cần sửa chữa, vì mọi bước đều
  kiểm-trước-khi-tạo.
- Bản checkout kho ứng dụng được dựng lại từ remote ở mỗi bước cần nó, nên retry chạy được
  trên một máy chưa từng thấy app. Bước build lấy **đỉnh nhánh** (đội ứng dụng thường đã
  đẩy code trong lúc onboarding bỏ dở); bước deploy/verify lấy **đúng commit đã build**
  (render theo đỉnh nhánh sẽ sinh tham chiếu tới một ảnh chưa ai đẩy lên).
- Cái giá: một app dở dang để lại tài nguyên thật ở nhiều hệ thống. Đó là lựa chọn có chủ
  ý của mục 13.4 — **không rollback bằng cách xoá sạch**. Xoá app là một workflow riêng có
  preview và duyệt.
- Bản ghi giữ tối đa 50 mục lịch sử để vài trăm lần retry không làm nó phình vô hạn.

## Đã cân nhắc và loại

**Một script bash chạy từ trên xuống.** Đã có (`tools/tao-app-moi.sh`) và vẫn được dùng —
nhưng chỉ cho phần GitHub, nơi nó đã idempotent. Nó không có chỗ ghi trạng thái, nên không
trả lời được "đã tới đâu" sau khi hỏng.

**State trong kho cấu hình.** Hỏng ngay ở tiền đề: bước tạo kho cấu hình là bước 4, còn
bước 3 đã có thể hỏng.

**Một service/worker có hàng đợi ngay từ đầu.** Mục 13.5 nói rõ MVP có thể dùng
`repository_dispatch` làm execution engine và chuyển sang service khi cần theo dõi tốt
hơn, contract không đổi. Máy trạng thái + bản ghi ngoài tiến trình chính là contract đó:
chuyển sang worker chỉ là đổi thứ gọi `run_onboarding`.

**Xoá sạch khi hỏng (rollback).** Bị mục 13.4 cấm, và có lý do: "hỏng" thường là "chưa
xong", và xoá namespace của một app đã có database đang chạy để dọn cho sạch là cách nhanh
nhất mất dữ liệu.
