# idp-platform — Internal Developer Platform

Nền tảng triển khai nội bộ: lập trình viên mô tả ứng dụng bằng một file
[Score](https://score.dev) (`score.yaml`), nền tảng lo phần còn lại — render manifest,
đồng bộ bí mật, cấp phát database, đẩy qua GitOps và xác minh trên cụm thật. Mục tiêu
xuyên suốt (**luật số 1**): *toạ độ hạ tầng nằm ở config, không ở code* — mang nền tảng
sang môi trường/công ty khác chỉ bằng cách sửa `platform.env.yaml` và secret của CI.

## Thành phần chính

| Thành phần | Ở đâu | Vai trò |
|---|---|---|
| **`idpctl`** + engine | `idpctl`, `engine/*.py` | CLI và toàn bộ product logic: render, commit, verify, promote, secret, database, audit. Workflow chỉ gọi lại nó. |
| **Cấu hình nền tảng** | `platform.env.yaml`, `platform.env.company.yaml` | Nơi DUY NHẤT chứa toạ độ hạ tầng theo môi trường/công ty. |
| **Catalog** | `provisioners/`, `patches/` | *Hình dạng* của tài nguyên (postgres → Cluster/StatefulSet, route → HTTPRoute…), với `%%placeholder%%` cho mọi toạ độ. Ghim theo `platform.lock`. |
| **Stack catalog** | `templates/stacks/` | Khuôn kho ứng dụng ghép từ component + capability (golden path). |
| **Workflow** | `.github/workflows/{deploy,promote,verify}.yaml` | Adapter GitHub Actions mỏng cho luồng deploy/promote/verify. |
| **Công cụ vận hành/harness** | `tools/*.sh` | Dựng và kiểm hạ tầng thật (Vault/VSO, database, gateway HTTPS, registry riêng…). |
| **Ví dụ** | `examples/` | App mẫu (`simple-nginx`, `app-with-postgres`, `microservices`). |

## Hạ tầng nền tảng dựa vào

Fleet (GitOps kéo manifest về cụm), HashiCorp Vault + Vault Secrets Operator (kho bí mật
duy nhất), CloudNativePG (database `class: application`), Gateway API + Traefik (ingress),
một registry OCI, và GitHub Actions (self-hosted runner cho orchestrator).

## Bắt đầu từ đâu

- **Muốn hiểu hệ thống:** [docs/architecture.md](docs/architecture.md).
- **Cài nền tảng / đưa một app lên cụm:** [docs/deployment.md](docs/deployment.md).
- **Cấu hình `platform.env.yaml`, Score, values, secret, database:**
  [docs/configuration.md](docs/configuration.md).
- **Chạy test và verify trên cụm thật:** [docs/testing.md](docs/testing.md).
- **Có sự cố:** [docs/troubleshooting.md](docs/troubleshooting.md) và
  [docs/runbook/](docs/runbook/).
- **Vì sao hệ thống thiết kế thế này:** [docs/adr/](docs/adr/).
- **Nếu bạn là AI agent:** đọc [AGENTS.md](AGENTS.md) trước.

## Chạy thử nhanh (logic, không cần cụm)

```bash
# từ gốc repo (nơi có idpctl)
python3 -m pytest test_engine.py -v
python3 idpctl --env-config platform.env.yaml preflight
python3 idpctl --env-config platform.env.yaml stack-list
```

`preflight` kiểm công cụ trên runner; `stack-list` in các stack mà catalog phát hành.
Toàn bộ lệnh `idpctl` được liệt kê trong [docs/deployment.md](docs/deployment.md) và định
nghĩa trong `engine/cli.py`.
