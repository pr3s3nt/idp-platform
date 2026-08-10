# CLAUDE.md — luật cho mọi phiên làm việc trên idp-platform

Đọc trước khi sửa `orchestrate.py`, catalog (`provisioners/`, `patches/`) hoặc
`orchestrator.yaml`. Ngắn gọn có chủ ý — chi tiết nằm ở các tài liệu được trỏ dưới.

## Verify — bắt buộc trước khi coi là XONG
- Chạy harness: `python3 -m pytest test_orchestrate.py -v` (từ gốc repo). Xem `HUONG-DAN-KIEM-THU.md`.
- Test đỏ = **hành vi sai**. **KHÔNG sửa/nới lỏng test cho pass.** Thêm hành vi ⇒ thêm test.
- Thiếu `score-k8s` ⇒ ~26 test render **tự skip**; cài rồi chạy lại — đừng coi là "pass".

## Luật số 1: PORT BẰNG CONFIG, KHÔNG SỬA CODE
Nền tảng phải mang sang công ty khác **chỉ bằng sửa `platform.env.yaml` + secrets**, không đụng code.
- **Không hard-code** giá trị hạ tầng/công ty (org, registry host, domain, tên context cụm,
  storage class, gateway, namespace…) vào `.py`/`.yaml`/`.sh`.
- Mọi "tọa độ" đọc từ `platform.env.yaml` (`CONFIG.get(...)` / `--env-config`). Cần giá trị mới
  ⇒ **thêm key vào `platform.env.yaml`**, đừng gán cứng.
- Code vốn đã tự ép luật này: `--registry`/`--image` **không có default**; `orchestrator.yaml`
  ghi "NO INFRASTRUCTURE VALUES HERE". Giữ đúng tinh thần đó.

## Bất biến khác (đều có comment giải thích tại chỗ — đọc trước khi đổi hành vi)
- **Secret không bao giờ vào git/manifest công khai** — split-manifest + `encodeSecretRef`.
- **Render idempotent** — giữ state, sort manifest, strip `managed-by`. Phá là Fleet churn.
- `apply-secrets` là **create-if-missing**; `_tolerate_exists` chỉ nuốt `AlreadyExists`, đừng nới.
- `guard_ordering`: **không deploy commit cũ đè commit mới hơn**.
- `verify` **chờ rollout thật** (`updatedReplicas`/`observedGeneration`), không nhìn `availableReplicas`.
- **Catalog = hình dạng; `platform.env.yaml` = tọa độ theo env** (`%%placeholder%%`). Đừng trộn.
- GitRepo: **liệt-kê-rồi-khớp**, không giả định tên `{app}-{env}`, không ghi đè của team khác.
- Giữ **tương thích ngược**; **đừng xóa các comment "vì sao"**.

## Tài liệu gốc
- `HUONG-DAN-KIEM-THU.md` — harness kiểm thử (cách chạy, nó canh gì) **+ môi trường/cụm verify
  và cách probe hạ tầng sống** (`tools/thu-thap-ha-tang.sh`) trước khi làm phase cần cụm.
- `TAI-LIEU-DU-AN.md` — thiết kế + lý do từng quyết định.
- `docs/adr/` — quyết định kiến trúc (vd `0002-vault-only-secret-store.md`).
- `docs/orchestrator-contract.md` — hợp đồng portal ↔ orchestrator + cách verify trên cụm thật.
