# Merge-readiness report — `feature/secret-onboarding`

**Trạng thái: `Ready for review`. Chưa merge, và sẽ không tự merge.**

Ngày: 2026-08-11 · Baseline: `36372b9` · Đã push lên `origin`.

---

## 1. Điều kiện ở mục 0.3.3

| Điều kiện | Trạng thái | Bằng chứng |
|---|---|---|
| Tất cả phase local ở mục 0.5 là `Done` | ✅ | Phase 0–7 đều `Done`, mỗi phase có nhật ký riêng |
| Full unit/integration/smoke suite pass | ✅ | **414 passed, 0 skipped**; chạy 3 lần liên tiếp ở mỗi lần chạm provisioner |
| Legacy regression pass khi feature flags tắt | ✅ | 4 cặp render baseline `36372b9` vs HEAD **giống nhau từng byte** |
| Không có secret/credential trong diff hoặc history | ✅ | quét toàn diff nhánh: không có key, token, mật khẩu thật |
| Có migration, configuration và rollback checklist | ✅ | mục 3–5 dưới đây + "Migration/Rollback" trong từng nhật ký phase |
| Có checklist bàn giao theo mục 0.7 | ✅ | mục 6 dưới đây |
| Nhánh không chứa file audit/thay đổi ngoài chương trình | ✅ | `git status --short` rỗng; không file nào dưới `audit/` bị chạm |

## 2. Phạm vi thay đổi

```text
git log --oneline 36372b9..feature/secret-onboarding   ->  18 commit
git diff --stat 36372b9...feature/secret-onboarding    ->  73 file, +15875 / -955
git status --short                                     ->  (rỗng)
```

Riêng Phase 7 (`b608a96..715fd9d`, 6 commit): `orchestrate.py`, `test_orchestrate.py`,
`provisioners/postgres-application.provisioners.yaml`, `platform.env*.yaml`, và 13 file tài
liệu/công cụ mới.

## 3. Cấu hình mới phải điền trước khi chạy trong công ty

| Khoá | Bắt buộc? | Ghi chú |
|---|---|---|
| `database.backup.object_store_url` | **Bắt buộc cho prod** | rỗng ⇒ render prod bị chặn |
| `database.backup.endpoint_url` | nếu kho object không phải AWS S3 | thiếu ⇒ WAL archiving hỏng trong im lặng |
| `database.backup.credentials_secret` | có | Secret phải nằm **cùng namespace với Cluster** |
| `database.backup.schedule` | có mặc định `"0 0 2 * * *"` | **cron CNPG có SÁU trường** (giây đứng đầu). `"0 2 * * *"` kiểu Unix ⇒ chụp **mỗi giờ** |
| `database.backup.first_backup_timeout_seconds` | mặc định 600 | base backup prod thật lâu hơn harness nhiều |
| `database_profiles.<env>.application.backup.schedule` | tuỳ chọn | ghi đè lịch theo môi trường |

Và bốn biến MÔI TRƯỜNG mà onboarding đọc (không phải khoá config — chúng là credential):

| Biến | Thiếu thì |
|---|---|
| `REGISTRY_USER` / `REGISTRY_PASS` | không tạo `registry-pull`; ảnh không kéo được nếu registry private |
| `BACKUP_ACCESS_KEY_ID` / `BACKUP_ACCESS_SECRET_KEY` | không tạo credential kho object; WAL archiving hỏng, không có base backup, và `verify` chặn |

Cả hai nay **từ chối tạo Secret rỗng** và nói thẳng biến nào thiếu — trước đây chúng tạo ra
một credential chứa chuỗi `"None"`, trông như đã cấu hình đầy đủ.

Toàn bộ khoá của các phase trước vẫn giữ nguyên ý nghĩa.

## 4. Migration

**App đang chạy không cần làm gì.** Mọi tính năng vẫn sau `features.*`, mặc định tắt.

**Ngoại lệ phải làm, và nó quan trọng:** app đã dùng `class: application` **phải render lại
và apply** để nhận `ScheduledBackup` và `managed.roles`. Không có hai thứ đó thì:

- backup **không phục hồi được** (chỉ có WAL, không có base) — dù cụm báo Ready; và
- xoay vòng credential **không có hiệu lực** — Secret đổi, database không đổi.

`rotate-db-credential` tự từ chối chạy trên Cluster render bằng catalog cũ, **trước khi**
ghi bất cứ thứ gì vào Vault.

**Không có migration tự động cho `type: postgres` → `class: application`.** Đổi class không
di chuyển dữ liệu; render nay **dừng lại** và bắt chọn tường minh. Quy trình đầy đủ ở
`docs/chuyen-doi-postgres-sang-class-application.md`.

## 5. Rollback

| Muốn quay lại | Cách |
|---|---|
| Toàn bộ chương trình | đặt cả 4 `features.*` = `false`. App chưa opt-in không thấy gì đổi |
| Chỉ phần backup của Phase 7 | `database.backup.object_store_url` rỗng ⇒ manifest y như trước |
| Một lần deploy | `promote --mode tag-only` (guard chặn nếu values đã đổi — dùng `re-render`) |
| Catalog | checkout `provisioners/`+`patches/` ở commit cũ, **chạy full suite 2–3 lần** |

**Không rollback được bằng cờ:** database đã đổi class, mật khẩu đã xoay vòng (kv-v2 đã
destroy), và dữ liệu đã ghi. Chi tiết: `docs/runbook/rollback-nang-cap-stack.md`.

## 6. Checklist bàn giao — người dùng tự chạy trong công ty (mục 0.7)

AI **không** chạy các bước này.

1. Checkout commit đầu nhánh đã pass harness (xem `git log -1 feature/secret-onboarding`).
2. Điền `platform.env.company.yaml` (mục 3 ở trên + các khoá của phase trước). **Không sửa
   source.**
3. `preflight --require-cluster --require-vault` — và `verify-rbac` cho một app mẫu.
4. Render dry-run một app legacy, so byte với bản render bằng commit đang chạy.
5. Deploy platform với **mọi feature flag vẫn tắt**. Xác minh app legacy trên staging.
6. Bật từng cờ cho **một** app pilot, theo thứ tự: `application_values` → `vault_secrets` →
   `postgres_application` → `stack_onboarding`.
7. **Chạy một lần diễn tập phục hồi database thật** theo `docs/runbook/…backup-that-bai.md`
   mục 4C, **trước khi** cho app đầu tiên lên prod. Quy trình nay đã cụ thể, không còn là
   lời khuyên.
8. Rollback sẵn sàng: tắt cờ, hoặc platform tag cũ.

**Thứ tự bắt buộc:** merge nhánh này vào nhánh mặc định **trước** khi onboard app golden
path đầu tiên, và bật `features.stack_onboarding` *trên chính nhánh đó*. CI của app đọc cả
code lẫn cấu hình từ nhánh mặc định — làm ngược thứ tự cho ra một app có CI đỏ, hoặc tệ hơn,
CI xanh nhưng tính tag khác orchestrator.

## 7. Còn nợ, nói thẳng

- **`offboard` chưa tự archive kho GitHub và chưa xoá package trên registry** — nó in ra
  lệnh để người chạy tự làm. Cố ý: cả hai đều cần scope token rộng hơn và đều không hoàn
  tác được.
- **Cảnh báo D3 (thời lượng onboarding theo bước) chưa có ngưỡng.** Nguồn dữ liệu đã có
  (`history[]`); ngưỡng đoán mò sẽ bị tắt sau tuần đầu.
- **CNPG sẽ bỏ `barmanObjectStore` gốc ở 1.31** (cụm hiện tại đã cảnh báo deprecated).
  Chuyển sang Barman Cloud Plugin là một thay đổi catalog đã biết trước, chưa làm.
- **Kho object của harness là một MinIO đơn lẻ** — đủ để chứng minh phục hồi chạy, không
  phải một kho backup thật.
- **Scanner theo entropy (mục 6) chưa làm** — hiện có allowlist theo vị trí + kiểm typed.
- **Vault harness là dev mode**: mất sạch khi pod restart. TLS/HA/unseal là prerequisite hạ
  tầng theo mục 7.5, không phải việc của platform.

## 8. Đề xuất

Nhánh đã có trên `origin` tại `715fd9d`. Khi người dùng muốn:

```bash
gh pr create --base main --head feature/secret-onboarding \
  --title "Environment Values, Vault Secret và App Onboarding (Phase 0-7)" \
  --body-file BAO-CAO-SAN-SANG-MERGE.md
```

Nếu `main` đã đổi trong thời gian phát triển, fetch/merge/rebase và xử lý conflict là **một
task riêng** — cố ý không làm trong bước hoàn tất này.
