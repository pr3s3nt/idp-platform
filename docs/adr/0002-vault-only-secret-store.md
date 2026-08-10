# ADR 0002 — Vault là kho bí mật duy nhất, đồng bộ bằng VSO

Trạng thái: Accepted — 2026-08-10

## Bối cảnh

Triệu chứng khởi phát: pod kẹt ở `CreateContainerConfigError` vì `score.yaml` trỏ tới một
Kubernetes Secret chưa ai tạo. Resource `type: secret` hiện có cố tình không kiểm tra sự
tồn tại lúc render — nó chỉ sinh `secretKeyRef` và tin rằng ai đó đã đặt Secret vào
namespace. "Ai đó" là thao tác tay, nên staging luôn thiếu.

Đồng thời platform phải bảo đảm bí mật KHÔNG nằm trong Git, không đi qua CI, và không nằm
trong Score state.

## Quyết định

HashiCorp Vault là kho bí mật duy nhất. Đồng bộ vào Kubernetes bằng Vault Secrets Operator.

Cú pháp app khai:

```yaml
STRIPE_KEY:
  secretRef:
    name: stripe
    key: api_key
```

Không có trường `store: vault`. Không có mount, không có path.

Đường dẫn do platform suy ra:

```text
<kv-mount>/apps/<application>/<environment>/<name>
```

Chuỗi object:

```text
VaultStaticSecret → VaultAuth (theo namespace) → VaultAuthGlobal (dùng chung)
```

`VaultStaticSecret` trỏ `VaultAuth`, KHÔNG trỏ thẳng `VaultAuthGlobal`.

Ranh giới tin cậy:

- Git chứa literal và metadata `secretRef`. Không chứa giá trị.
- CI/orchestrator không có Vault token đọc được bí mật.
- Kubernetes Secret chỉ là bản sao runtime do VSO quản; nó không đi qua
  `split_manifests`/`apply-secrets`.
- Policy Vault giới hạn theo đúng tiền tố `apps/<app>/<env>/`.

## Hệ quả

Tích cực:

- Bí mật thiếu là trạng thái TỰ HỒI PHỤC: ghi vào Vault, VSO đồng bộ, pod chạy tiếp. Không
  cần deploy lại, không cần Ops tạo Secret tay.
- Xoay vòng bí mật không cần build lại image hay render lại manifest.
- Ranh giới phân quyền là đường dẫn Vault, mà đường dẫn thì platform derive — app không tự
  mở rộng phạm vi của mình được.

Cái giá:

- Thêm một thành phần hạ tầng phải vận hành: VSO, và Vault phải có TLS/HA/backup/unseal.
  Đây là prerequisite, không phải thứ platform tự dựng.
- `CreateContainerConfigError` VẪN xuất hiện thoáng qua khi Fleet apply Deployment và VSO
  CR cùng lúc. Definition of Done là workload tự hội tụ trong SLO, KHÔNG phải là trạng thái
  đó không bao giờ xuất hiện. Đặt DoD kiểu tuyệt đối sẽ dẫn tới việc thêm sleep vào verify.
- Chẩn đoán khó hơn: lỗi có thể nằm ở Vault policy, ở VaultAuth, ở VSO, hoặc ở đường dẫn.
  Bù lại bằng thông báo lỗi bắt buộc chứa app/env/workload/logical secret/derived path/
  VSO condition — nhưng không bao giờ chứa giá trị.

## Đã cân nhắc và loại

**External Secrets Operator.** Chạy được, nhưng VSO do chính HashiCorp phát hành và bám
sát vòng đời auth của Vault (`VaultAuthGlobal`, token lifecycle). Với môi trường chỉ dùng
Vault, ESO là một tầng trừu tượng cho tính đa backend mà chúng ta không dùng.

**Sealed Secrets / SOPS trong Git.** Giá trị đã mã hoá vẫn nằm trong Git vĩnh viễn. Xoay
vòng khoá nghĩa là mã hoá lại toàn bộ lịch sử, và lộ khoá nghĩa là lộ mọi bí mật từng
commit. Không đạt yêu cầu "bí mật không nằm trong Git".

**Giữ `type: secret` và bảo Ops tạo Secret tay.** Đây chính là trạng thái đang hỏng.

**Cho app tự khai Vault path.** Gọn hơn cho app, nhưng app A đọc được bí mật của app B chỉ
bằng cách gõ đúng chuỗi. Phân quyền phải là thứ app không tác động được.

**Đổi luôn semantics của `type: secret` sang Vault.** Sẽ làm app đang chạy đổi hành vi
trong im lặng. `type: secret` giữ nguyên; app cũ opt-in bằng cách thêm
`.score-values/values.yaml`.
