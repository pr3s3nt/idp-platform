# CLAUDE.md — bối cảnh cho mọi phiên trên idp-platform

Đọc khi sắp sửa `orchestrate.py`, catalog (`provisioners/`, `patches/`) hoặc `orchestrator.yaml`.
Ngắn gọn có chủ ý — chi tiết ở các tài liệu trỏ bên dưới. Đây là *vì sao* của những chỗ dễ vô
tình phá, không phải thủ tục phải diễn.

## Hai lớp kiểm — đừng nhầm lớp này thành lớp kia
- **pytest chỉ chứng minh *logic* đúng.** `python3 -m pytest test_orchestrate.py -v` (từ gốc
  repo) kiểm render/commit/verify sinh ra đúng thứ mong đợi — nhanh, chạy local, không cần cụm.
  Test đỏ = vừa đổi một hành vi thật, đọc nó trước khi nới. Nhưng **xanh KHÔNG nghĩa app chạy
  được**: harness không đẩy gì lên cụm (xem `HUONG-DAN-KIEM-THU.md`).
- **"App chạy được" chỉ đúng khi nó đi hết luồng thật.** Đẩy code → GitHub Actions thật build
  ảnh → orchestrator render + commit → Fleet kéo về → cụm chạy → xác nhận bằng `kubectl get pods`
  / rollout thật và `curl` qua gateway trả 200. Một lần chạy file python không thay được bước này.

## Toạ độ ở config, không ở code (luật số 1)
Mục tiêu: mang nền tảng sang công ty khác chỉ bằng `platform.env.yaml` + secrets, không đụng
code. Nên giá trị hạ tầng/công ty (org, registry host, domain, context cụm, storage class,
gateway, namespace…) đọc từ `platform.env.yaml` (`CONFIG.get(...)` / `--env-config`), không gán
cứng vào `.py`/`.yaml`/`.sh`. Cần giá trị mới ⇒ thêm key vào `platform.env.yaml`. Code vốn đã
giúp giữ điều này: `--registry`/`--image` không có default; `orchestrator.yaml` ghi "NO
INFRASTRUCTURE VALUES HERE".

## Các bất biến khác — và vì sao (comment tại chỗ giải thích kỹ hơn)
- **Secret không vào git/manifest công khai** — split-manifest + `encodeSecretRef`; manifest chỉ giữ tham chiếu.
- **Render idempotent** — giữ state, sort manifest, strip `managed-by`. Mất tính này là Fleet churn liên tục.
- `apply-secrets` **create-if-missing**; `_tolerate_exists` chỉ nuốt `AlreadyExists` — nới rộng hơn là nuốt cả lỗi thật.
- `guard_ordering` — không để commit cũ đè commit mới hơn.
- `verify` chờ rollout thật (`updatedReplicas`/`observedGeneration`), không nhìn `availableReplicas` (nhìn nhầm sẽ báo xanh khi cụm chưa lên).
- **Catalog = hình dạng; `platform.env.yaml` = toạ độ theo env** (`%%placeholder%%`). Trộn hai thứ là nguồn lỗi im lặng.
- GitRepo: liệt-kê-rồi-khớp, không giả định tên `{app}-{env}`, không ghi đè của team khác.
- Giữ tương thích ngược; các comment "vì sao" là lịch sử lỗi đã trả giá — đọc trước khi đổi, đừng xoá.

## Tài liệu gốc
- `HUONG-DAN-KIEM-THU.md` — harness (cách chạy, canh gì) + **quy trình test một feature qua luồng thật (AI tự lái)** + môi trường/cụm verify + probe hạ tầng sống (`tools/thu-thap-ha-tang.sh`).
- `HUONG-DAN-TRIEN-KHAI-APP-CHUAN.md` — đường chuẩn onboard một app tới staging (kèm bảng "Bẫy đã biết").
- `TAI-LIEU-DU-AN.md` — thiết kế + lý do từng quyết định.
- `docs/adr/` — quyết định kiến trúc (vd `0002-vault-only-secret-store.md`).
- `docs/orchestrator-contract.md` — hợp đồng portal ↔ orchestrator + verify trên cụm thật.
