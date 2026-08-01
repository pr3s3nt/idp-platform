# Nhật ký kiểm thử — dự án `shop` trên platform v2

> Dự án 3 service + 1 cơ sở dữ liệu dùng chung, đăng ký vào bản cài `idp-platform-v2`.
> Mỗi bước ghi lại lệnh, thời điểm, và bằng chứng thật để audit lại được.

| | |
|---|---|
| Platform | `pr3s3nt/idp-platform-v2` |
| Cụm | kind `v2`, Gateway ở cổng `17080` |
| Repo app | `pr3s3nt/idp-shop-v2` |
| Repo cấu hình | `pr3s3nt/idpv2-shop-config` |

---

## Bước 0 — Bối cảnh trước khi bắt đầu

```
thời điểm: 2026-08-01 09:35:40 +0700
cụm v2:    v2-control-plane Ready
bundle:    2 cái
```

## Bước 1 — Dựng dự án `shop`

Ba service trong **một** repo, dùng chung **một** cơ sở dữ liệu.

| Service | Cổng | Khai báo phụ thuộc |
|---|---|---|
| `api` | 80 | `postgres` (id `shop-db`) |
| `worker` | không có service | `postgres` (**cùng id** `shop-db`) |
| `web` | 80 | `service` → `api`, `route` ra ngoài |

Chia sẻ database bằng cách đặt **cùng `id`** cho resource ở hai service.

### Kiểm chứng bằng render tại máy, trước khi đẩy lên

```
==> discovered 3 service(s): api, web, worker
==> pinned api.api    -> ghcr.io/pr3s3nt/shop-api:v1
==> pinned web.web    -> ghcr.io/pr3s3nt/shop-web:v1
==> pinned worker.worker -> ghcr.io/pr3s3nt/shop-worker:v1
==> split: 1 secret -> cụm, 8 manifest -> repo cấu hình
```

Số tài nguyên sinh ra: **3 Deployment, 3 Service, 1 StatefulSet, 1 HTTPRoute**.
Một StatefulSet nghĩa là **một** database chứ không phải hai.

Biến môi trường thật sự được nối vào:

```
api:     PGHOST=pg-api-267be4a1  PGDATABASE=db-zsCidtLG  PGPASSWORD=secretKeyRef->pg-api-267be4a1
worker:  PGHOST=pg-api-267be4a1  PGDATABASE=db-zsCidtLG  PGPASSWORD=secretKeyRef->pg-api-267be4a1
web:     API_ADDR=api:80
```

Hai service trỏ **cùng một** máy chủ, **cùng một** database, **cùng một** secret — đúng ý đồ.
Mật khẩu là tham chiếu, không phải giá trị.

## Bước 2 — Đăng ký vào platform

| Việc | Kết quả |
|---|---|
| Repo app `idp-shop-v2` | tạo, nhánh mặc định `dev` |
| CI trỏ `PLATFORM_REPO` | `pr3s3nt/idp-platform-v2` |
| Repo cấu hình `idpv2-shop-config` | tạo, 2 nhánh `main` + `dev` |
| `GitRepo` của Fleet | 2 cái |
| Secret cho CI | đã cấp |

### ⚠️ Lỗi phát hiện trong chính hướng dẫn

Đặt **cùng tên** `shop` cho cả hai `GitRepo` thì cái thứ hai **ghi đè** cái thứ nhất —
Kubernetes coi đó là cùng một đối tượng. Kết quả: chỉ còn cấu hình trỏ `prod`, môi trường
staging **không có ai kéo về**, và không có lỗi ở đâu cả.

Bản cài trước không đụng lỗi này vì mỗi môi trường một cụm riêng. Bản v2 dùng **một cụm cho
cả hai môi trường** nên hai đối tượng nằm cùng chỗ và đè nhau.

Đã sửa thành `shop-staging` / `shop-prod`.

> **Quy tắc rút ra:** một cụm phục vụ nhiều môi trường thì tên `GitRepo` phải mang tên môi
> trường. Đánh đổi: tên bundle sẽ thành `shop-staging-staging` do Fleet ghép
> `<tên-GitRepo>-<thư-mục>`.

## Bước 3 — Triển khai lần đầu

### ⚠️ Lỗi thứ hai phát hiện trong hướng dẫn

Lần chạy đầu **thất bại**:

```
ERROR: failed to build: failed to read dockerfile:
       open Dockerfile: no such file or directory
```

Nguyên nhân: hướng dẫn tạo app bảo "sao chép CI từ một app có sẵn", nhưng mẫu CI của app
**một service** thì:

- build từ **gốc repo** — trong khi `shop` có 3 Dockerfile ở 3 thư mục con
- lấy **image đầu tiên** trong danh sách — bỏ qua hai service còn lại

Nói cách khác: **mẫu CI phụ thuộc vào việc repo có một hay nhiều service**, mà hướng dẫn
không hề nói. Sao chép nhầm mẫu thì hỏng ngay từ bước build.

Đã đổi sang mẫu nhiều service. Mẫu này lấy danh sách service **động** từ platform:

```yaml
matrix:
  include: ${{ fromJson(needs.plan.outputs.matrix) }}
```

nên tự khớp với 3 service mà không phải khai tay — thêm service thứ tư cũng không cần sửa CI.

### Kết quả triển khai lần đầu — 09:42:13

```
api-7597bd668f-2vqrk           1/1 Running
pg-api-0fde295b-0              1/1 Running
web-dbc96d574-9lzwb            1/1 Running
worker-5f5fbd6b74-jxng6        1/1 Running

pvc pv-data-pg-api-0fde295b-0          Bound

bundle: 1/1
gọi qua Gateway: HTTP 200
```

Bốn pod: ba service cộng một Postgres. Ổ đĩa ở trạng thái `Bound` — đúng StorageClass
khai trong cấu hình. Database **một cái duy nhất** dù hai service cùng khai báo cần nó.

## Kịch bản A — sửa **một** service trong repo ba service

Sửa nội dung của `web`, không đụng `api` và `worker`.

### Nhãn ảnh: chỉ service bị sửa mới đổi

```
api     21eff32b9119 -> 21eff32b9119   KHÔNG đổi
web     4c26c55af180 -> 8ee35228f83d   ĐỔI
worker  d887e2ca123f -> d887e2ca123f   KHÔNG đổi
```

### CI chỉ build đúng service đó

```
bỏ qua  api    — ảnh đã có trên registry
cần build web
bỏ qua  worker — ảnh đã có trên registry
-> 1/3 job build chạy
```

### Chỉ một pod bị khởi động lại

```
api-7597bd668f-2vqrk      02:41:26   giữ nguyên
pg-api-0fde295b-0         02:41:30   giữ nguyên  <- CƠ SỞ DỮ LIỆU KHÔNG BỊ ĐỘNG
web-66b85d77b8-vmxsh      02:44:14   MỚI
worker-5f5fbd6b74-jxng6   02:41:26   giữ nguyên
```

Trang trả về `v2 - chi sua web`.

**Ý nghĩa:** trong một repo nhiều service, sửa một service **không** làm gián đoạn các
service khác, và đặc biệt **không đụng tới cơ sở dữ liệu**. Nếu nhãn ảnh lấy theo mã
commit của cả repo thì cả bốn pod đều bị khởi động lại.
