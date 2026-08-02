# Cần gì để chạy thử nền tảng bằng cấu hình công ty

Bản rút gọn của `CAU-HOI-NGU-CANH.md` — chỉ còn những gì **chưa có**. Những câu đã trả lời
(Gateway, StorageClass, bảo vệ nhánh, bot được bypass) không hỏi lại.

Mục tiêu: điền hết `platform.env.company.yaml` rồi chạy trọn một vòng bằng chính file đó,
để lỗi nào chỉ nổ ở môi trường công ty thì lộ ra **trước** khi mang đi thật.

---

## ⚠️ Đọc trước — đừng gửi những thứ này

**KHÔNG gửi cho tôi:** mật khẩu Harbor, kubeconfig, private key của GitHub App, token, chứng
chỉ. Không có thứ nào trong đó cần cho việc điền cấu hình.

Chúng đi thẳng vào secret của repo, do **bạn** đặt:

```bash
gh secret set REGISTRY_PASS   -R <org>/idp-platform < <(printf %s '<mật-khẩu-robot>')
gh secret set APP_PRIVATE_KEY -R <org>/idp-platform < ~/app-key.pem
cat ~/.kube/config | base64 -w0 | gh secret set KUBECONFIG_PROD -R <org>/idp-platform
```

Cái tôi cần chỉ là **tên và đường dẫn**, không phải thứ mở được cửa.

---

## Phần 0 — Một câu hỏi phải trả lời TRƯỚC mọi thứ khác

**Công ty dùng github.com (Enterprise Cloud) hay GitHub Enterprise Server tự dựng?**

Nhìn địa chỉ là biết: `github.com/<tổ-chức>` là Cloud; `github.<tên-công-ty>.vn` hoặc bất kỳ
tên miền nội bộ nào là Enterprise Server.

Vì sao hỏi trước: **GHES chỉ có sẵn một bộ action giới hạn**. Nền tảng này dùng đúng hai
action — `actions/checkout` có trong bộ đi kèm, còn `actions/create-github-app-token` thì
không. Trên GHES không bật GitHub Connect, workflow sẽ hỏng ngay ở bước lấy token.

Nếu là GHES thì hỏi thêm hai câu:

| Câu hỏi | Nếu có | Nếu không |
|---|---|---|
| Đã bật **GitHub Connect** chưa? | dùng action bình thường, không phải làm gì | xem dòng dưới |
| Đã có quy trình **đồng bộ action vào tổ chức nội bộ** (`actions-sync`) chưa? | nhờ thêm một action, workflow giữ nguyên | xem dòng dưới |

Cả hai đều không thì vẫn còn hai đường, không bí:

- **Dùng tài khoản máy** thay GitHub App → không cần action đó nữa, vấn đề biến mất
- **`tools/mint-app-token.sh`** — đã viết sẵn và kiểm chạy thật, 47 dòng dùng `openssl` +
  `curl`, làm đúng việc của action kia mà không phụ thuộc gì bên ngoài

> Nếu là github.com thì bỏ qua toàn bộ phần này.

---

## Phần 1 — Bảy giá trị phải điền

Đây là những ô đang để `todo-*.invalid`. Chúng cố ý đặt dạng không hợp lệ để nếu quên điền
thì hỏng ngay chứ không âm thầm deploy vào nhầm chỗ.

| # | Cần | Ví dụ | Hỏi ai |
|---|---|---|---|
| 1 | **Tên tổ chức trên git** | `cong-ty-abc` | bạn tự biết |
| 2 | **Quy ước đặt tên repo cấu hình** | `{app}-config` hay `platform-{app}-config` | đội bạn thống nhất |
| 3 | **Host của Harbor** | `harbor.noi-bo.vn` | quản trị Harbor |
| 4 | **Project trên Harbor** | `harbor.noi-bo.vn/nen-tang` | quản trị Harbor |
| 5 | **Đường dẫn ảnh Postgres đã mirror** | `harbor.noi-bo.vn/base/postgres:17-alpine` | quản trị Harbor |
| 6 | **Tên miền cấp cho app ở staging** | `staging.noi-bo.vn` | đội mạng / hạ tầng |
| 7 | **Tên miền cấp cho app ở production** | `apps.noi-bo.vn` | đội mạng / hạ tầng |

**Về mục 5:** cụm nội bộ thường không ra được internet. Nếu Postgres chưa có trên Harbor thì
phải mirror trước — đây là loại lỗi im lặng khó chịu, PVC treo `Pending` mãi mà không có
thông báo nào nói vì sao.

**Về mục 6–7:** hỏi thêm hai ý — có **DNS wildcard** trỏ `*.<tên-miền>` về Gateway chưa, và
ai tạo bản ghi DNS khi có app mới. Nếu mỗi app phải xin một bản ghi riêng thì việc đăng ký
app sẽ có thêm một bước chờ người khác.

---

## Phần 2 — Bốn câu xác nhận (chỉ cần trả lời có/không)

| # | Câu hỏi | Vì sao quan trọng |
|---|---|---|
| 8 | Đội **được tự tạo namespace** không? Nếu có thì giới hạn theo tiền tố nào? | Quyết định phải xin quyền hay nhờ tạo sẵn |
| 9 | Quy ước đặt tên namespace của công ty là gì? | Mặc định `{app}-{env}`. Nếu công ty dùng kiểu khác thì đổi cấu hình — nhưng **hai app không được chung namespace** trừ khi tên workload khác nhau, vì chúng sẽ đè lên nhau |
| 10 | Được tạo namespace **`cluster-state`** không? Nếu không thì dùng namespace nào? | Nơi giữ GUID tài nguyên và **mật khẩu database đã sinh**. Mất chỗ này là mất mật khẩu |
| 11 | Số bản sao tối thiểu cho production là bao nhiêu? | Đang để 3 |

---

## Phần 3 — Ba việc phải xác nhận với đội vận hành Kubernetes

Đây **không phải giá trị cấu hình**. Nếu câu trả lời là không thì phải đổi cách làm, nên cần
biết sớm.

### 12. Gateway có cho namespace của đội gắn route không

`traefik-gateway` có `allowedRoutes` giới hạn namespace nào được gắn `HTTPRoute`. Không mở
thì **route không bao giờ attach — và không có lỗi ở bất kỳ đâu**. Ứng dụng chạy, pod khoẻ,
chỉ là không ai vào được.

Tự kiểm được:

```bash
kubectl -n traefik get gateway traefik-gateway \
  -o jsonpath='{range .spec.listeners[*]}{.name}{" -> "}{.allowedRoutes.namespaces}{"\n"}{end}'
```

### 13. Cụm có ra được internet không, và có Fleet chưa

```bash
# Fleet đã cài chưa
kubectl get crd gitrepos.fleet.cattle.io >/dev/null 2>&1 && echo "có Fleet" || echo "chưa có Fleet"

# GitRepo đặt ở namespace nào
kubectl get gitrepo -A
```

Nếu công ty dùng Rancher thì nhiều khả năng đã có sẵn. Nếu đội **không tự tạo được
`GitRepo`**, nhờ đội vận hành tạo — mỗi app 2 cái, nội dung tĩnh, không bao giờ đổi.

### 14. Máy chạy CI

- Có sẵn self-hosted runner nào dùng được không, nhãn là gì?
- Máy đó có vào được **API server của cả hai cụm** không?
- Có Docker chạy không cần `sudo` không?
- Xin được **root một lần** để chỉnh `inotify` và MTU không?

Hai chỉnh sửa mức hệ điều hành đó không phải chuyện nhỏ — cả hai từng làm hỏng cụm ở sandbox
theo kiểu rất khó đoán. MTU sai làm mọi lần kéo ảnh treo, mà DNS vẫn chạy nên trông y hệt
lỗi registry.

---

## Phần 4 — Danh tính của bot

Cần **một danh tính khác bạn**. GitHub chặn tự duyệt pull request của chính mình, nên nếu
orchestrator dùng tài khoản của bạn thì PR lên production sẽ do bạn tạo và bạn không duyệt
được — cổng duyệt tự khoá chính nó.

Chọn một trong hai, rồi cho tôi biết chọn cái nào:

| | GitHub App | Tài khoản máy |
|---|---|---|
| Cần | chủ tổ chức duyệt cài | một tài khoản GitHub riêng, mời vào đội |
| Token | sống 1 giờ, tự chết | PAT, đặt hạn được |
| Sửa code | không | 7 dòng trong workflow |

Cần cho tôi: **tên** và **email** của danh tính đó, để ghi lên commit triển khai.

> ⚠️ Nếu là GitHub App, email **phải** là địa chỉ chính thức của App (dạng
> `<id>+<tên>[bot]@users.noreply.github.com`). Ở sandbox từng dùng đại một địa chỉ
> `@users.noreply.github.com` và GitHub ánh xạ nó về **một người dùng có thật không liên
> quan** — mọi commit triển khai bị ghi công cho người lạ.

---

## Tóm lại: gửi cho tôi một khối như thế này

```yaml
to_chuc: cong-ty-abc
mau_ten_repo_cau_hinh: "{app}-config"

harbor_host: harbor.noi-bo.vn
harbor_project: harbor.noi-bo.vn/nen-tang
anh_postgres: harbor.noi-bo.vn/base/postgres:17-alpine

ten_mien_staging: staging.noi-bo.vn
ten_mien_production: apps.noi-bo.vn
co_dns_wildcard: có / không

duoc_tu_tao_namespace: có / không — tiền tố: ...
quy_uoc_ten_namespace: "{app}-{env}"
namespace_giu_trang_thai: cluster-state
so_ban_sao_production: 3

gateway_cho_namespace_nao: (kết quả lệnh ở mục 12)
cum_ra_duoc_internet: có / không
da_co_fleet: có / không — GitRepo ở namespace: ...
runner: có sẵn nhãn ... / phải dựng mới

danh_tinh_bot: GitHub App / tài khoản máy
  ten: idp-orchestrator
  email: ...

loai_github: github.com / Enterprise Server
  # chỉ khai 2 dòng dưới nếu là Enterprise Server
  co_github_connect: có / không
  co_actions_sync: có / không
```

Thiếu vài mục cũng được — cứ gửi phần đã có, tôi điền dần và chỉ rõ cái nào còn chặn.
