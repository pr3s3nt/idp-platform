# Prompt giao cho AI Agent

Copy nguyên khối dưới đây làm prompt. Sửa ba chỗ trong ngoặc nhọn trước khi gửi.

---

```text
Bạn đang ở gốc kho `idp-platform` trên máy này. Nhiệm vụ: tạo một ứng dụng mới và đưa nó
CHẠY THẬT trên môi trường staging của cụm Kubernetes local.

App phải gồm: 1 backend, 1 frontend, 1 database PostgreSQL. Nó phải có biến môi trường
khác nhau giữa staging và prod, và một bí mật lấy từ Vault mà bí mật đó có tác dụng thật
(mọi thao tác ghi phải kèm khoá, thiếu khoá thì bị từ chối). Ảnh container phải do GitHub
Actions build — không được build tay bằng docker trên máy.

TÀI LIỆU BẮT BUỘC ĐỌC TRƯỚC KHI GÕ LỆNH ĐẦU TIÊN:

  HUONG-DAN-AGENT-DUA-APP-LEN-STAGING.md

Đọc HẾT file đó rồi mới bắt đầu. Nó có 11 mục, mỗi mục có CỔNG KIỂM. Làm đúng thứ tự.

QUY TẮC KHÔNG ĐƯỢC VI PHẠM:

1. Cổng kiểm nào không đạt thì DỪNG, tra mục 10 "Bẫy đã biết" của file đó, sửa nguyên
   nhân rồi chạy lại. TUYỆT ĐỐI không nới lỏng cổng kiểm, không bỏ qua bước, không sửa
   tiêu chí cho vừa kết quả.
2. Mọi lệnh `orchestrate.py` phải truyền `--env-config`, kể cả lệnh trông như không cần.
3. Không commit giá trị bí mật vào git. Không in giá trị bí mật ra log.
4. Không sửa `templates/` trong kho platform. Không commit lên nhánh `main`.
5. Không xoá tài nguyên của app khác để làm sạch chỗ. Tên app phải là tên chưa tồn tại.
6. Nếu một bước tạo tài nguyên bên ngoài (kho GitHub, namespace, đường dẫn Vault) thì
   kiểm-trước-khi-tạo và ghi lại; retry phải tiếp tục từ bước lỗi, không tạo bản sao.

THÔNG SỐ CỦA LẦN CHẠY NÀY:

  Tên app          : <đặt-tên-app>
  Tổ chức GitHub   : <org>
  Kho platform     : <org>/idp-platform

BÍ MẬT VÀ CREDENTIAL: file hướng dẫn liệt kê 8 biến môi trường phải đặt trước ở mục 1.
Lấy giá trị từ nơi người dùng đã cấu hình sẵn trên máy; không tự đoán, không tự sinh token
mới. Nếu thiếu bất kỳ biến nào và bạn không tìm được giá trị hợp lệ, DỪNG và hỏi — đừng
chạy tiếp với biến rỗng, vì nó sẽ hỏng ở một nơi không nhắc gì tới biến đó.

KẾT QUẢ PHẢI BÁO CÁO khi xong — bảng 11 tiêu chí S1..S11 ở mục 0 của file hướng dẫn, mỗi
dòng ghi ĐẠT/KHÔNG kèm bằng chứng thật (output lệnh, mã HTTP, tên pod). Không được ghi
"đạt" cho tiêu chí chưa thực sự chạy lệnh kiểm.

Kèm theo báo cáo:
  - URL kho ứng dụng và kho cấu hình đã tạo
  - link/cách truy cập app trên staging
  - những gì đã tạo ra và những gì đã dọn
  - nếu có tiêu chí KHÔNG đạt: nói thẳng cái nào và vì sao, đừng che

CUỐI CÙNG, nếu mục 2.2 của hướng dẫn yêu cầu tắt workflow orchestrator thì BẮT BUỘC bật
lại ở mục 8 và xác nhận nó `active`. Bỏ quên bước này là để lại cụm ở trạng thái hỏng.
```

---

## Ghi chú cho người giao việc

- **Ba chỗ phải sửa**: `<đặt-tên-app>`, `<org>` (hai lần).
- Tên app phải **chưa tồn tại** — agent có cổng kiểm cho việc này, nhưng chọn sẵn một cái
  chưa dùng thì đỡ một vòng.
- **Trước hay sau merge**: nếu nhánh tính năng đã merge vào `main`, agent sẽ tự phát hiện ở
  mục 2 và bỏ qua toàn bộ phần ghim nhánh tạm. Không cần nói gì thêm.
- **Thời gian**: khoảng 20–35 phút, phần lớn là chờ CI (~2 phút) và chờ base backup đầu
  tiên của database (vài phút). Nếu shell của agent có giới hạn thời gian mỗi lệnh, mục 7
  đã dặn chạy nền và theo dõi bằng `onboard-status`.
- **Muốn agent dọn sạch sau khi đo**: thêm một dòng vào cuối prompt —
  *"Sau khi báo cáo xong, làm mục 9 (Dọn dẹp) và liệt kê đã xoá những gì."*
  Bỏ dòng đó nếu bạn muốn giữ app lại để xem.
- **Đánh giá agent**: bài này bẫy ở bốn chỗ, và một agent cẩu thả sẽ trượt ít nhất một:
  1. Bỏ qua mục 2 (trước-merge) → CI đỏ ngay với `unrecognized arguments`.
  2. Quên `BACKUP_ACCESS_*` → S6 không bao giờ đạt, mà app vẫn chạy và trông như thành công.
  3. Tự `docker build` cho nhanh khi CI đỏ → S2 trượt, và nó thường không tự nhận ra.
  4. Quên bật lại workflow orchestrator.
