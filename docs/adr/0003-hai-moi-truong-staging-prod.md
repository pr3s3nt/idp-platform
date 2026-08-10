# ADR 0003 — Đúng hai tên môi trường: `staging` và `prod`

Trạng thái: Accepted — 2026-08-10

## Bối cảnh

Cùng một môi trường bị gọi bằng nhiều tên là lỗi kinh điển của platform: `prod`,
`production`, `prd`. Mỗi tên xuất hiện ở một tầng khác nhau — thư mục config repo, khối
`environments` trong `platform.env.yaml`, `.score-values/values.yaml`, tên namespace, tên
nhánh Fleet.

Hỏng ra sao: developer viết `production:` trong values file, renderer tra `prod`, không
thấy, và dùng giá trị `application` dùng chung. Render XANH. Prod chạy với cấu hình
staging. Không có cảnh báo ở bất cứ đâu, vì "không có override cho môi trường này" là một
trạng thái hoàn toàn hợp lệ.

## Quyết định

Đúng hai chuỗi, ở mọi tầng: `staging` và `prod`.

`production` KHÔNG phải alias. Nó là lỗi.

Thực thi trong code:

```python
ENVIRONMENTS = ("staging", "prod")

def validate_environment(env: str) -> str: ...
```

Mọi đường vào đều đi qua đây: CLI `--env` (`choices=("staging","prod")`), khối
`environments` của values file, và `vault_path()`.

## Hệ quả

Tích cực:

- Gõ sai môi trường là lỗi ồn ào tại điểm gần nhất, không phải cấu hình sai âm thầm ở prod.
- Một chuỗi duy nhất ghép được vào namespace, nhánh, đường dẫn Vault và thư mục Fleet mà
  không cần bảng ánh xạ.

Cái giá:

- Công ty gọi môi trường bằng tên khác thì phải ánh xạ ở BIÊN (payload dispatch, form
  onboarding), không phải bằng cách thêm alias vào lõi. Thêm alias là quay lại đúng lỗi cũ.
- Thêm môi trường thứ ba (`qa`, `uat`) là một thay đổi có chủ ý, không phải việc gõ thêm
  một khối YAML. Đây là chủ ý: môi trường thứ ba kéo theo profile database, policy Vault
  và quy tắc promotion riêng.

## Đã cân nhắc và loại

**Chấp nhận alias và chuẩn hoá về `prod`.** Dễ chịu lúc gõ, nhưng khi đó `production:` và
`prod:` cùng tồn tại trong một file là hợp lệ, và không có câu trả lời đúng cho việc cái
nào thắng.

**Danh sách môi trường tự do trong `platform.env.yaml`.** Linh hoạt, nhưng lỗi chính tả
trở lại thành "một môi trường mới hoàn toàn hợp lệ mà chưa ai deploy vào" — không phân biệt
được với lỗi gõ.
