# ADR 0006 — Ghim phiên bản `score-k8s`, `score-compose` và VSO

Trạng thái: Accepted — 2026-08-10

## Bối cảnh

`score-k8s` quyết định HÌNH DẠNG mọi manifest platform sinh ra. Hai runner cài hai phiên
bản khác nhau thì cùng một commit app cho ra hai manifest khác nhau.

Điều đó biểu hiện thành: một lần deploy tự nhiên thay đổi thứ không ai sửa. Diff trong
config repo có thật, review được, nhưng không ai giải thích được nó từ đâu ra — vì nguyên
nhân không nằm trong bất kỳ commit nào.

`platform.lock` không giải quyết được: nó ghim CATALOG (provisioner, patch), tức DỮ LIỆU.
Phiên bản binary là CÔNG CỤ, và nó nằm trên runner.

Với VSO còn thêm một kiểu hỏng riêng: CRD và controller là hai thứ cài tách nhau. Lệch
phiên bản thì CR mới bị controller cũ bỏ qua trong im lặng — `VaultStaticSecret` tồn tại,
không có event lỗi, và Secret đích không bao giờ xuất hiện.

## Quyết định

Ghim trong `platform.env.yaml`:

```yaml
ci:
  score_k8s_version: "0.15.0"
  score_compose_version: "0.43.0"
vault:
  operator_version: "1.5.0"
```

Thực thi:

- `preflight` gọi `--version` và **dừng** nếu lệch.
- `render` kiểm tra lại `score-k8s`. Không phải thừa: `preflight` là một step riêng, nên nó
  chứng minh runner ổn ở ĐẦU job, không chứng minh lần render NÀY dùng đúng binary — và
  người replay bằng tay bỏ qua `preflight` hoàn toàn. Kết quả kiểm tra được memoise nên
  tốn một subprocess mỗi tiến trình, không phải mỗi workload.
- Chuỗi rỗng = TẮT kiểm tra.
- CRD và controller của VSO nâng cấp cùng nhau, cùng một phiên bản.

Về chuỗi rỗng: đây là on-ramp cho brownfield, có chủ ý. Platform này đang deploy app thật
từ những runner chưa ai cài lại. Một kiểm tra fail-closed ngay lần nâng cấp đầu sẽ làm
chết các app đó để thực thi một chính sách chúng có trước. `preflight` in ra rõ ràng khi
nó đang bỏ qua kiểm tra, nên "tắt" không im lặng. Môi trường thật thì phải ghim.

## Hệ quả

Tích cực:

- Runner lệch phiên bản trở thành lỗi trước khi render, thay vì một diff bí ẩn sau khi
  Fleet đã apply.
- Nâng cấp `score-k8s` trở thành một thay đổi có chủ ý, đi kèm chạy lại full test suite.
- Kiểm chứng được: gate "hai lần render cùng input cho ra output giống nhau từng byte"
  chỉ có nghĩa khi biết đó là binary nào.

Cái giá:

- Nâng cấp runner phải kèm sửa config. Đó chính là mục đích.
- Test suite phụ thuộc vào phiên bản `score-k8s` cài trên máy. Cũng là mục đích: nó đang
  kiểm tra hành vi thật của binary đã ghim, không phải một binary bất kỳ.
- Lệch phiên bản VSO phát hiện bằng `preflight` chỉ khi ai đó chạy nó với kubeconfig.
  Kiểm tra CRD/controller thuộc Phase 2.

## Đã cân nhắc và loại

**Ghim theo `platform.lock`.** `platform.lock` ghim catalog theo từng app, và mỗi app có
thể pin ref khác nhau. Nhưng chỉ có MỘT binary trên runner. Không thể vừa render app A với
`score-k8s` 0.14 vừa render app B với 0.15 trong cùng một job.

**Chỉ kiểm tra major.minor.** Phiên bản patch của `score-k8s` đã từng đổi hình dạng
manifest. Nếu "hầu như luôn tương thích" là đủ thì đã không cần ghim.

**Cài binary trong workflow thay vì kiểm tra.** Ràng mỗi lần deploy vào việc tải được từ
mạng, và cụm on-prem của công ty thường không ra được internet.

**Ghim bằng container image cho job.** Tốt hơn về nguyên tắc và có thể là bước sau. Hiện
tại runner của công ty là máy có sẵn công cụ, không phải môi trường container — thay đổi
đó vượt quá phạm vi chương trình này.
