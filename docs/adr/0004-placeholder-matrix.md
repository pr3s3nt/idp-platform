# ADR 0004 — Placeholder chỉ hợp lệ ở 4 vị trí, theo allowlist

Trạng thái: Accepted — 2026-08-10

## Bối cảnh

`${resources.x.y}` là cú pháp tham chiếu của Score. `score-k8s` KHÔNG thay thế nó ở mọi
nơi — `command`, `args`, `image` và probe được chuyển thẳng sang manifest nguyên văn.

Hậu quả khi viết nhầm chỗ:

```yaml
command: ["/app", "--log=${resources.config.LOG_LEVEL}"]
```

Manifest nhận đúng chuỗi `${resources.config.LOG_LEVEL}` làm tham số dòng lệnh. Deployment
apply thành công, pod chạy, và app đọc mức log là chuỗi rác đó. Không có lỗi ở đâu cả.

Vấn đề thứ hai, ngược hướng: bí mật bị trộn với literal trong nội dung file. Nếu
`${resources.config.PASSWORD}` nằm giữa một file cấu hình, renderer chỉ có hai lựa chọn —
nội suy giá trị vào (tức ghi bí mật vào ConfigMap trong Git) hoặc bỏ qua.

## Quyết định

**Allowlist, không phải blacklist.** Bốn vị trí có nội suy:

1. `containers.*.variables`
2. Nội dung hiệu lực của `containers.*.files.*`
3. `containers.*.volumes.*.source`
4. `resources.*.params`

`${resources.` ở BẤT KỲ vị trí nào khác là lỗi lúc render.

Ma trận theo loại giá trị:

| Vị trí | Literal | SecretRef |
|---|---:|---:|
| `variables` | Cho phép | Cho phép |
| Nội dung file | Cho phép | Chỉ khi TOÀN BỘ nội dung là đúng một secret reference |
| `volumes.source` | Chỉ resource UID hợp lệ | Cấm |
| `resources.*.params` | Cho phép | Cấm trong v1 |
| Vị trí khác | Cấm | Cấm |

Quy tắc bí mật trong file:

```yaml
content: "${resources.config.PRIVATE_KEY}"      # hợp lệ
content: |-
  ${resources.config.PRIVATE_KEY}               # hợp lệ
content: |
  ${resources.config.PRIVATE_KEY}               # LỖI — `|` thêm newline cuối
content: |-
  username=admin
  password=${resources.config.PASSWORD}         # LỖI — trộn secret với literal
```

`binaryContent` và file có `noExpand: true` không nội suy. `source` đọc file từ repo app;
renderer phải quét nội dung SAU KHI đọc.

**Defense in depth.** Ngoài kiểm tra theo kiểu, quét lần cuối trước khi ghi manifest công
khai:

- Hard-fail ở mọi môi trường với private key header và token prefix đã biết.
- Heuristic (tên biến nhạy cảm + entropy cao): ban đầu cảnh báo ở staging, fail ở prod;
  sau khi dọn baseline thì fail cả hai.
- Escape hatch nằm trong allowlist ở platform config, app không tự bypass được.
- Scanner KHÔNG BAO GIỜ in toàn bộ giá trị nghi là bí mật.

## Hệ quả

Tích cực:

- Placeholder đặt sai chỗ trở thành lỗi lúc render thay vì hành vi sai lúc chạy.
- Allowlist chịu được nâng cấp `score-k8s`: phiên bản mới thêm field thì field đó mặc định
  bị CẤM cho tới khi có người xem xét, thay vì im lặng lọt qua.

Cái giá:

- Vài cách viết hợp lý bị chặn: bí mật trong `resources.*.params`, secret nối chuỗi trong
  file. Ngăn cấm được ưu tiên vì cái giá của việc cho phép là bí mật lọt vào Git.
- Sự khác nhau giữa `|` và `|-` sẽ làm người ta vấp. Thông báo lỗi phải chỉ đích danh cách
  sửa, không chỉ nói "không hợp lệ".

## Đã cân nhắc và loại

**Blacklist vài field đã biết là không hỗ trợ.** Đúng cho đến lần nâng cấp `score-k8s` kế
tiếp thêm field mới. Blacklist không thể đúng với thứ chưa tồn tại.

**Strip newline cuối để `|` chạy được.** Ngầm sửa dữ liệu của người dùng, và trong đúng
loại file mà một byte thừa cũng làm hỏng (khoá riêng tư, chữ ký).

**Cho phép trộn bí mật với literal bằng cách sinh Secret cho cả file.** Chạy được, nhưng
phần literal biến mất khỏi review — người đọc config repo không còn thấy file cấu hình
chứa gì. Có thể mở lại ở v2 nếu có nhu cầu thật.
