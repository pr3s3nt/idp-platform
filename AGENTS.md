# AGENTS.md — quy tắc làm việc cho AI agent trên `idp-platform`

File này dành cho mọi AI agent (và người) sắp sửa `idpctl`, engine (`engine/`),
catalog (`provisioners/`, `patches/`), template (`templates/`) hoặc workflow
(`.github/workflows/`). Đọc trước khi đổi bất cứ hành vi nào.

## Nguồn sự thật — theo đúng thứ tự ưu tiên

1. **Code và test là nguồn sự thật cao nhất.** `engine/*.py`, `idpctl` và
   `test_engine.py`/`test_audit.py` quyết định hành vi thật. Test đỏ nghĩa là code sai
   (hoặc hợp đồng vừa đổi có chủ ý), không phải test sai — đừng nới lỏng test cho pass.
2. **Workflow mô tả quy trình triển khai thực tế.** `.github/workflows/deploy.yaml`,
   `promote.yaml`, `verify.yaml` là luồng deploy đang chạy. Chúng chỉ là adapter mỏng gọi
   `idpctl`; product logic replay được qua `idpctl`.
3. **`platform.env.yaml` là nguồn cấu hình nền tảng.** Mọi toạ độ hạ tầng (org, registry,
   gateway, storageclass, đường Vault, profile database…) đọc từ đây, không gán cứng vào
   code. `platform.env.company.yaml` là profile công ty tương đương.
4. **Script và template** (`tools/`, `templates/`) là hiện thực cụ thể của các thao tác.
5. **Tài liệu Markdown chỉ để GIẢI THÍCH.** Nó không bao giờ được ưu tiên hơn code.

## Khi tài liệu mâu thuẫn với code

- **Đọc code, và báo rõ khác biệt.** Nếu một tài liệu nói khác với `engine/`, `idpctl`
  hoặc workflow, thì code đúng và tài liệu sai — sửa tài liệu, đừng sửa code cho khớp
  tài liệu.
- **Không mô tả một tính năng là đang hoạt động nếu không tìm thấy hiện thực trong code
  và test.** Trước khi viết "platform làm X", tìm X trong `engine/`/`idpctl`/test. Không
  thấy thì không viết, hoặc ghi rõ đó là đề xuất chưa hiện thực.
- **Kiểm lệnh trước khi viết.** Danh sách lệnh `idpctl` thật nằm ở các `sub.add_parser(...)`
  trong `engine/cli.py`. Nếu một tài liệu nhắc một lệnh không có ở đó, lệnh đó không tồn tại.

## Không dùng tài liệu lịch sử để suy luận hành vi hiện tại

- **Không suy luận hành vi hiện tại từ kế hoạch, báo cáo hay nhật ký.** Các tài liệu dạng
  `KE-HOACH-*`, `PLAN-*`, `BAO-CAO-*`, `*NHAT-KY*`, `GAP-REGISTER` đã bị gỡ khỏi nhánh
  chính đúng vì lý do này; lịch sử nằm trong git nếu cần tra.
- **Không tự thêm một tính năng chỉ vì tài liệu cũ (hoặc một ADR ở trạng thái `Proposed`)
  từng mô tả nó.** ADR `Proposed`/`Superseded` là thiết kế, không phải thứ đã tồn tại.

## Hai lớp kiểm — đừng nhầm lớp này thành lớp kia

- **pytest chỉ chứng minh *logic* đúng.** `python3 -m pytest test_engine.py -v` (từ gốc
  repo) kiểm render/commit/verify sinh đúng thứ mong đợi — nhanh, local, không cần cụm.
  Xanh **không** nghĩa là app chạy được.
- **"App chạy được" chỉ đúng khi nó đi hết luồng thật:** đẩy code → GitHub Actions build
  ảnh → orchestrator render + commit → Fleet kéo về → cụm chạy → `kubectl` + `curl` qua
  gateway trả 200. Chi tiết ở [docs/testing.md](docs/testing.md).

## Bất biến dễ vô tình phá (đọc comment tại chỗ để biết vì sao)

- **Toạ độ ở config, không ở code.** `--registry`/`--image` không có default; catalog chỉ
  chứa `%%placeholder%%`. Cần giá trị hạ tầng mới ⇒ thêm key vào `platform.env.yaml`.
- **Secret không vào git/manifest công khai** — split-manifest + `encodeSecretRef`;
  manifest chỉ giữ tham chiếu, VSO mới đọc Vault.
- **Render idempotent** — giữ state, sort manifest, strip `managed-by`. Mất tính này là
  Fleet churn liên tục.
- **`guard_ordering`** — không để commit cũ đè commit mới hơn.
- **`verify` chờ rollout thật** (`updatedReplicas`/`observedGeneration`), không nhìn
  `availableReplicas`.
- **Catalog = hình dạng; `platform.env.yaml` = toạ độ theo env.** Trộn hai thứ là nguồn
  lỗi im lặng.
- Giữ tương thích ngược; comment "vì sao" là lịch sử lỗi đã trả giá — đọc trước khi đổi.

## Tài liệu gốc

- [README.md](README.md) — bản đồ dự án.
- [docs/architecture.md](docs/architecture.md) — kiến trúc thực tế theo code.
- [docs/deployment.md](docs/deployment.md) — luồng deploy và tạo app.
- [docs/configuration.md](docs/configuration.md) — `platform.env.yaml`, Score, values.
- [docs/testing.md](docs/testing.md) — harness và cách verify.
- [docs/troubleshooting.md](docs/troubleshooting.md) — khoanh vùng lỗi theo tầng.
- [docs/adr/](docs/adr/) — quyết định kiến trúc còn hiệu lực.
- [docs/runbook/](docs/runbook/) — quy trình xử lý sự cố.
