# ADR 0009 — Stack là phép cộng component, và `make dev` sinh ra từ chính Score

Trạng thái: Accepted — 2026-08-10

## Bối cảnh

Golden path đầu tiên (`node-fullstack`) cần bốn thứ cùng lúc: một bộ file mẫu cho kho ứng
dụng, một cách chạy được ở local, routing cùng origin giữa frontend và API, và một quy tắc
đặt tag ảnh đúng cho monorepo.

Cách hiển nhiên là viết một template cho mỗi tổ hợp — `node-fullstack`, `node-api`,
`node-worker`, `static-frontend` — và một `compose.yaml` viết tay đi kèm để chạy local. Cả
hai đều hỏng theo cùng một kiểu: **hai bản sao của cùng một sự thật, và bản sao thứ hai
lặng lẽ lệch đi**. Sửa `node-api` ở một template không sửa nó ở ba template kia; thêm một
resource vào `score.yaml` không tự thêm nó vào `compose.yaml`, và local vẫn chạy được nên
không ai phát hiện cho tới lúc deploy.

## Quyết định

**1. Stack = Archetype × Runtime × Capability, ghép lúc sinh, không sao chép.**

```text
node-fullstack = static-frontend + node-api + shared-lib + capability database
```

`templates/stacks/<id>.stack.yaml` liệt kê component; `components/<id>/files/` giữ file;
`capabilities/<id>/capability.yaml` giữ đoạn YAML được chèn vào score của workload nào khai
là mình tiêu thụ nó. Sửa `node-api` một lần là sửa cho mọi stack chứa nó.

Capability được chèn dưới dạng **văn bản**, không phải cấu trúc YAML được dump lại. Kho ứng
dụng do platform sinh ra vẫn phải tự giải thích được cho người mở nó ra đọc, mà round-trip
qua `yaml.safe_dump` thì mất sạch comment.

**2. `score.yaml` là nguồn topology duy nhất, kể cả cho local.** `compose.yaml` được sinh
ra bằng `score-compose` và nằm trong `.gitignore`. Provisioner local
(`templates/score-compose/`) được **vendor** vào kho ứng dụng lúc tạo app, đã resolve sẵn
`%%placeholder%%`, nên `make dev` chỉ cần `docker` và `score-compose` — không cần checkout
kho platform, không cần `kubectl`, không cần Vault.

**3. Local là bản diễn tập của staging, không phải môi trường thứ ba.** Platform vẫn có
đúng hai môi trường (ADR 0003). `.env.example` được sinh từ tier staging của
`.score-values/values.yaml`, cộng thêm `localValues` của stack ghi đè đúng những khoá phải
khác (`PUBLIC_HOST` thành `*.localhost`). Phiên bản major của PostgreSQL local bị **chặn**
nếu lệch với `database_profiles.staging.application.engine_version`.

**4. `tagStrategy: commit` cho monorepo, khai trong `.idp/stack.yaml`.** Thứ tự quyết định:
cờ dòng lệnh → `.idp/stack.yaml` → `content`. App không có file đó render **y hệt như
trước**, và khai báo trong file chỉ có hiệu lực khi `features.stack_onboarding` bật.

**5. Nâng stack là pull request có diff, không phải ghi đè.** `stack-upgrade` mặc định chỉ
đề xuất diff cho các file platform sở hữu (`managedFiles`) và **không tự ghi**. Phiên bản
stack và phiên bản catalog (`platform.lock`) ghim độc lập.

## Hệ quả

- Thêm một stack = thêm một file manifest, không phải sao chép một cây thư mục.
- `make dev` không thể lệch khỏi staging về topology: cùng file Score sinh ra cả hai.
- Đổi lại, kho ứng dụng mang **giá trị đã resolve** (ảnh nền, tên host). Đó là đúng: một
  kho ứng dụng thuộc về một tổ chức. Cập nhật chúng là việc của `stack-upgrade`.
- `content` mất đi ưu điểm "chỉ build service đã đổi" cho monorepo. Chấp nhận ở quy mô hai,
  ba workload; đổi lại là không bao giờ deploy nhầm ảnh cũ.

## Đã cân nhắc và loại

**Một template cho mỗi tổ hợp.** Loại: bốn bản sao của `node-api`, và bản sao thứ tư sẽ là
bản không ai nhớ sửa.

**Commit `compose.yaml` viết tay.** Loại: đó chính là bản sao thứ hai của topology. Nó chạy
được ở local nên không ai phát hiện nó đã lệch.

**Dùng provisioner `route` mặc định của score-compose.** Loại sau khi **đo**: nó khoá map
shared bằng `.Uid`, nên nginx sinh `location` theo thứ tự **tên workload**, mà nginx lấy
regex location khớp ĐẦU TIÊN. Đặt tên frontend là `app-ui` và backend là `orders` thì
`^/` đứng trước và **mọi request `/api/...` rơi vào frontend**. Trên cụm, Gateway API xếp
hạng `PathPrefix` theo ĐỘ DÀI, nên bản mặc định làm local và staging cư xử khác nhau — đúng
thứ mà `make dev` sinh ra để loại bỏ. Bản của platform khoá theo `999 - len(path)`.

**Dùng ảnh operand của CloudNativePG cho Postgres local.** Loại sau khi đo:
`ghcr.io/cloudnative-pg/postgresql:17` có `CMD` là `bash` và chạy bằng uid 26 — nó để
operator điều khiển, không phải server chạy độc lập. `docker run` thoát ngay với mã 0 và
log rỗng. Local dùng `%%images.postgres%%`, và platform chặn nếu major version lệch.

**Bơm địa chỉ API vào frontend lúc chạy.** Loại: bundle đã build nằm trong trình duyệt,
biến môi trường của container nginx không với tới nó. Cách duy nhất đúng là cùng origin.

**Để `stack-upgrade` tự ghi đè kho ứng dụng.** Loại: chỉ con người biết một sửa đổi local
là cố ý hay không.
