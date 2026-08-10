# ADR 0001 — `ApplicationValues v1`

Trạng thái: Accepted — 2026-08-10

## Bối cảnh

Một `score.yaml` mô tả NHU CẦU của workload. Nó cố tình không mô tả môi trường: cùng một
file phải render được cho staging và prod. Nhưng ứng dụng thật luôn cần vài giá trị khác
nhau giữa hai môi trường — mức log, hostname công khai, cờ tính năng.

Trước quyết định này, đường duy nhất là viết thẳng giá trị vào `score.yaml`. Kết quả là
staging và prod dùng chung một mức log, hoặc app phải tách thành hai file Score gần giống
nhau và trôi dạt khỏi nhau trong vài tuần.

## Quyết định

Một file duy nhất ở gốc repo app:

```text
.score-values/values.yaml
```

```yaml
apiVersion: idp.company/v1
kind: ApplicationValues
spec:
  application:            # dùng chung mọi môi trường
    LOG_LEVEL: info
  environments:
    staging:
      LOG_LEVEL: debug
    prod:
      PUBLIC_HOST: payment-api.internal
```

Workload lấy giá trị qua đúng một resource:

```yaml
resources:
  app-config:
    type: environment
containers:
  app:
    variables:
      LOG_LEVEL: "${resources.app-config.LOG_LEVEL}"
```

Quy tắc đã chốt:

- Precedence: `spec.application` < `spec.environments.<env>`. Chỉ hai tầng.
- Mỗi workload có 0 hoặc 1 resource `type: environment`. Từ 2 trở lên là lỗi lúc render.
- Alias KHÔNG cố định là `env` — renderer tra từ resource map của workload.
- Literal phải là string. YAML tự ép `yes`/`no`/`on`/`off`/`8080` thành bool và int; những
  giá trị đó phải được quote.
- Một key giữ nguyên loại (`literal` hoặc `secretRef`) ở mọi environment.
- Key được Score tham chiếu nhưng thiếu sau resolve là lỗi, không phải chuỗi rỗng.
- App KHÔNG khai được Vault mount hay path — xem [ADR 0002](0002-vault-only-secret-store.md).

App không có `.score-values/values.yaml` giữ nguyên hành vi cũ, không cần biết tính năng
này tồn tại.

## Hệ quả

Tích cực:

- Một `score.yaml` phục vụ cả hai môi trường, và khác biệt nằm ở một chỗ đọc được.
- Giá trị literal hiện ra trong config repo dưới dạng `env.value`, review được bằng mắt.
- `secretRef` dùng chung cú pháp với literal, nên chuyển một biến từ literal sang bí mật
  không phải sửa `score.yaml`.

Cái giá:

- Thêm một file app phải biết. Giảm nhẹ bằng việc stack template sinh sẵn.
- Renderer phải resolve trước khi gọi `score-k8s`, tức thêm một tầng có thể sai. Bù lại
  bằng test precedence và bằng việc thiếu key thì fail chứ không im lặng.

## Đã cân nhắc và loại

**Nhiều file, `values.staging.yaml` + `values.prod.yaml`.** Hai file thì phần dùng chung
bị chép đôi, và chúng trôi dạt. Vấn đề y hệt việc tách `score.yaml` làm hai.

**Overlay kiểu Kustomize.** Mạnh hơn nhiều so với nhu cầu, và mở cửa cho việc patch bất kỳ
field nào của manifest — tức app lại giành được quyền sửa thứ mà platform phải sở hữu.

**Đọc thẳng từ ConfigMap có sẵn trong namespace.** Giá trị biến mất khỏi Git, nên không
review được, không re-render lịch sử được, và không ai biết prod đang chạy với cấu hình gì
nếu không vào cụm hỏi.
