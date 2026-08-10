# __APP__

Sinh từ stack `__STACK_ID__` phiên bản `__STACK_VERSION__`.

## Chạy ở local

```bash
make dev
```

Cần đúng hai thứ trên máy: `docker` (kèm compose) và `score-compose`. Không cần kho
platform, không cần `kubectl`, không cần Vault.

Lần đầu chạy sẽ tạo `.env` từ `.env.example`. Điền các giá trị còn trống (bí mật của bên
thứ ba) rồi chạy lại. Trên staging/prod những giá trị đó tới từ Vault; ở local là của bạn,
và `.env` đã nằm trong `.gitignore`.

| Lệnh | Việc |
|---|---|
| `make dev` | sinh `compose.yaml` từ Score rồi bật cả stack |
| `make down` | tắt |
| `make logs` | xem log |
| `make check` | chỉ kiểm Score còn hợp lệ (CI dùng cái này) |
| `make clean` | xoá cả state local và dữ liệu database local |

## Vì sao không có `compose.yaml` trong kho

`score.yaml` của mỗi thư mục là **nguồn topology duy nhất**. `compose.yaml` được sinh ra từ
chính các file đó bằng `score-compose`, dùng provisioner local đã vendor trong
`.idp/score-compose/`. Thêm một resource hay đổi một cổng là cả local lẫn staging cùng thay
đổi — không có bản chép tay nào để quên.

## Frontend gọi API thế nào

Đường dẫn **tương đối**:

```javascript
fetch("/api/items")
```

`/` và `/api` nằm **cùng một origin** (nginx dùng chung ở local, HTTPRoute của Gateway trên
cụm), nên trình duyệt không coi đây là cross-origin: **không có CORS và không cần có**.

Cũng vì vậy frontend **không** nhận địa chỉ API qua biến môi trường. Bundle JavaScript đã
build chạy trong trình duyệt, biến của container nginx không với tới nó được. Nếu bạn thấy
mình đang tìm cách "bơm URL API vào lúc chạy" thì routing đã sai ở đâu đó — sửa route.

Backend mount router tại `/api`, không phải `/`: provisioner route chuyển tiếp **nguyên**
đường dẫn, không cắt tiền tố.

## Vì sao `tagStrategy: commit`

`.idp/stack.yaml` khai `tagStrategy: commit`. Kho này là monorepo có gói dùng chung
`shared/`, mà chiến lược `content` băm theo **thư mục của từng workload** — nên sửa
`shared/` không làm đổi tag của `frontend/` hay `backend/`, và lần deploy sau dùng lại hai
ảnh cũ **mà không báo lỗi gì**. `commit` gắn cùng một SHA cho mọi workload nên không bỏ sót.

Đánh đổi: một commit chỉ sửa `frontend/` vẫn build lại `backend/`. Chấp nhận được ở quy mô
hai, ba workload; đổi lại là không bao giờ deploy nhầm ảnh cũ.

## Cấu hình và bí mật

| Ở đâu | Chứa gì |
|---|---|
| `.score-values/values.yaml` | giá trị theo môi trường (literal) và tham chiếu bí mật (`secretRef`) |
| `.env` (local, không commit) | cùng bộ khoá đó, giá trị dành cho máy bạn |
| Vault | giá trị bí mật thật của staging/prod — platform không bao giờ đọc chúng |

Thêm một bí mật: khai `secretRef` trong `.score-values/values.yaml`, rồi nhờ Platform ghi
giá trị vào Vault. Đừng đặt giá trị thật vào bất kỳ file nào trong kho này.

## Nâng phiên bản stack

Stack được nâng bằng **pull request có diff**, không phải bằng cách ghi đè kho. Phiên bản
stack (`.idp/stack.yaml`) và phiên bản catalog (`platform.lock`) được ghim **độc lập**.
