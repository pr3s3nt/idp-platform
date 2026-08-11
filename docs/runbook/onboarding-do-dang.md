# 6. Onboarding dở dang và retry

## Nguyên tắc

Onboarding là **máy trạng thái có bản ghi nằm ngoài tiến trình** (ADR `0010`). "Đang chờ
người" là một **trạng thái**, không phải lỗi. Mỗi bước kiểm-trước-khi-tạo, nên chạy lại
đúng lệnh cũ là an toàn và không tạo bản sao thứ hai.

**Không bao giờ** dọn dẹp bằng tay rồi chạy lại từ đầu. Đó chính là thứ máy trạng thái
sinh ra để khỏi phải làm.

## Xác nhận đang ở đâu

```bash
python3 orchestrate.py --env-config platform.env.yaml onboard-status --app <app>
kubectl -n cluster-state get configmap idp-onboarding-<app> -o jsonpath='{.data.record\.json}' \
  | python3 -m json.tool | head -40
```

## Trạng thái và việc phải làm

| Trạng thái | Nghĩa | Việc |
|---|---|---|
| `WAITING_FOR_USER_SECRETS` | Thiếu bí mật bên thứ ba. **Không phải lỗi** | Chạy đúng lệnh `secret-set` mà `onboard-status` in ra, rồi chạy lại `onboard` |
| `PENDING_PROD_ACTIVATION` | Staging xong, prod chưa được yêu cầu | `onboard-activate-prod` khi đội ứng dụng sẵn sàng |
| `PENDING_PROD_APPROVAL` | Pull request prod đang chờ duyệt | Người duyệt merge PR, rồi chạy lại `onboard-activate-prod` |
| `FAILED_RETRYABLE` | Hỏng ở một bước cụ thể | Sửa nguyên nhân, chạy lại cùng lệnh |
| `PARTIALLY_READY` | Một phần chạy được | Chạy lại; bước đã xong bị bỏ qua |

## Retry

```bash
python3 orchestrate.py --env-config platform.env.yaml onboard --request <file> ...   # đúng lệnh cũ
```

Bước đã `done` bị bỏ qua. Muốn chạy lại một bước cụ thể (mọi bước đều kiểm-trước-khi-tạo
nên an toàn):

```bash
... onboard --force-step verify-staging
... onboard-activate-prod --force-step <bước>     # cờ này có ở CẢ HAI lệnh
```

Muốn dừng sớm để xem xét: `--stop-after <bước>`.

## Bẫy đã gặp

- **Bản checkout của lần chạy trước không còn.** Retry trên máy khác vẫn chạy được: mỗi
  bước tự dựng lại từ remote. Bước build lấy **đỉnh nhánh**, bước deploy/verify lấy **đúng
  commit đã build** — render theo đỉnh nhánh sẽ trỏ tới một ảnh chưa ai đẩy lên.
- **Thiếu `PLATFORM_DISPATCH_TOKEN`.** Lần push đầu tiên của đội ứng dụng đỏ ở
  `actions/checkout` với thông báo không hề nhắc tới secret. Onboarding đặt token này khi
  người chạy cung cấp `APP_DISPATCH_TOKEN`, và **báo là còn thiếu** khi không.
- **CI sinh từ nhánh chưa merge thì đỏ.** Mẫu CI checkout platform ở `ref: main`. Onboard
  từ nhánh phát triển giao cho đội ứng dụng một workflow gọi lệnh mà `main` chưa có. Thứ
  tự bắt buộc: **merge trước, onboard sau**.
- **Bí mật không tự chảy từ staging sang prod.** Đó là chủ ý. Prod sẽ dừng ở
  `WAITING_FOR_USER_SECRETS` riêng của nó.

## Xác minh đã xong

```bash
python3 orchestrate.py --env-config platform.env.yaml onboard-status --app <app>   # READY
kubectl -n <app>-staging get pods,cluster.postgresql.cnpg.io
kubectl -n fleet-local get gitrepo | grep <app>
```

Đếm để chắc không có bản sao: đúng **1** kho app, **1** kho cấu hình, **1** namespace mỗi
môi trường, **1** GitRepo mỗi môi trường, **1** ConfigMap state.

## Nếu vẫn hỏng

Bản ghi state mất (ConfigMap bị xoá) nhưng tài nguyên còn: mọi tài nguyên onboarding tạo
ra đều mang nhãn, nên vẫn tìm lại được:

```bash
kubectl get all,gitrepo -A -l idp.platform/application=<app>
```

Dựng lại bản ghi bằng cách chạy lại `onboard` — các bước kiểm-trước-khi-tạo sẽ nhận ra
những gì đã tồn tại và chỉ ghi lại state.
