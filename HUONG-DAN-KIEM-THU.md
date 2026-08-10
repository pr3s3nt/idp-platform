# Harness kiểm thử — trạng thái hiện có

Đọc file này TRƯỚC khi sửa `orchestrate.py`, catalog (`provisioners/`, `patches/`) hoặc
`orchestrator.yaml`. Nó mô tả bộ kiểm thử đang có, cách chạy, nó bảo vệ điều gì, và cách
dùng nó để tự verify khi code thêm. Mục tiêu: một phiên mới không phá vỡ bất biến của dự án
chỉ vì không biết có harness.

## Harness là gì (một câu)

Là **một bộ test pytest duy nhất** — `test_orchestrate.py` — import thẳng `orchestrate.py`,
render **catalog thật** của repo với `platform.env.yaml` thật, rồi khẳng định hành vi.
Không có server, không Makefile, không CI riêng để dựng. Chỉ pytest.

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
| `app-owned secrets` / `vault paths` / `secretRef shape` | **Tính năng secret đang làm** (nhánh `feature/secret-onboarding`): secret ref/Vault path render ra `secretRef`, giá trị **không** vào manifest. |
| `kiểm cụm sau khi triển khai` | Logic `verify`: chờ rollout thật, không nhìn `availableReplicas`. |

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

## Tài liệu gốc liên quan

- `TAI-LIEU-DU-AN.md` — thiết kế + lý do từng quyết định.
- `docs/adr/` — quyết định kiến trúc (vd `0002-vault-only-secret-store.md` cho tính năng secret).
- `docs/orchestrator-contract.md` — hợp đồng portal ↔ orchestrator, kèm cách verify trên cụm thật.
- Comment tại chỗ trong `orchestrate.py` — phần lớn giải thích "vì sao", đọc trước khi đổi hành vi.

> Gợi ý: thêm một dòng trỏ tới file này trong `CLAUDE.md` ở gốc repo, để phiên Claude mới
> **tự động** đọc thay vì phải nhớ mở ra.
