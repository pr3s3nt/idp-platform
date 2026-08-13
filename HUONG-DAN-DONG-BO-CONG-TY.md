# Đồng bộ nền tảng này sang repo công ty (Git self-hosted / GHES)

Repo công ty là một **repo độc lập**, lịch sử không liên thông với repo sandbox này, nên không
`git merge`/`git push` được. Đồng bộ = **chép bộ file git theo dõi**. Tài liệu này nói chép cái
gì, chừa cái gì, và vì sao.

## Nguyên tắc: code chép nguyên, TOẠ ĐỘ mới phải sửa

Nền tảng được thiết kế để "toạ độ ở config, không ở code" (xem `CLAUDE.md`, luật số 1). Hệ quả
khi port sang công ty:

- **Code chép nguyên xi** — `idpctl`, `.github/workflows/deploy.yaml`, `tools/`,
  `templates/`, `provisioners/`, `patches/`, `test_engine.py`. Code đã viết để chạy **cả
  github.com lẫn GHES**: workflow dùng `GITHUB_SERVER_URL`/`GH_ENTERPRISE_TOKEN` và né
  `actions/create-github-app-token` (GHES không có); `tools/mint-app-token.sh` đọc `GITHUB_API_URL`;
  CI template đọc `vars.CI_RUNNER_LABEL || 'ubuntu-latest'`. **Không có "file github.com" nào phải
  xoá** — kể cả `.github/`, vì GHES cũng chạy Actions ở đúng thư mục đó.
- **`platform.env.yaml` = nơi DUY NHẤT mang chất công ty.** Đây là file phải sửa. Trên GHES,
  runner đọc đúng file này (`ENV_CONFIG: platform/platform.env.yaml`).
- Phần công ty còn lại **không phải file** mà là **biến + secret khai trên GHES** (xem cuối).

> Ngoại lệ nhỏ, không chặn: `idpctl` có vài chuỗi `https://github.com/...` gắn cứng
> (vd `onboarding_config_repo_url`) chỉ dùng để **in cho người đọc**, không dùng cho thao tác
> git thật. Trên GHES nó in nhầm hostname trong thông báo — cosmetic, sửa sau cũng được.

## Chép: dùng script

```bash
# ở repo SANDBOX này. TARGET là cây làm việc repo công ty, đã checkout NHÁNH MỚI (off main).
tools/dong-bo-sang-cong-ty.sh /duong/dan/repo-cong-ty [ref]     # ref mặc định HEAD
tools/dong-bo-sang-cong-ty.sh /duong/dan/repo-cong-ty --dry-run # xem trước, không ghi
```

Script dùng `git archive` (KHÔNG `cp -r`) nên:
- Chỉ lấy **file git theo dõi**, kèm dotfile (`.github/`, `.gitignore`).
- **Tự loại** `.git/`, `__pycache__`, `.claude/`, thư mục scratch `onboard-*`/`work-*`.
- **Chừa `platform.env.yaml`** mặc định, và in **diff** để bạn merge tay.

Nó chặn nếu repo đích đang ở `main`/`master` (bắt buộc một nhánh mới), và cảnh báo file
chỉ-có-ở-đích (ứng viên đã bị nguồn xoá — `git archive` không tự xoá).

## Merge `platform.env.yaml` (KHÔNG chép đè)

Chép đè file này = mang giá trị sandbox (`pr3s3nt`, `ghcr.io`, vault dev...) đè lên giá trị công
ty → deploy nhầm hạ tầng. Cách đúng: **thêm KEY mới, giữ VALUE công ty**. Các nhóm key thường
phải để mắt khi port tính năng secret/onboarding:

| Nhóm | Key | Giá trị công ty là gì |
|---|---|---|
| git | `org`, `platform_repo`, `committer_name/email` | tổ chức GHES, repo platform, danh tính bot |
| registry | `host`, `path` | Harbor/registry nội bộ |
| images | `postgres`, `node`, `nginx`, `database.image_repository` | mirror nội bộ (cụm thường không ra internet) |
| vault | `address`, `kv_mount`, `kv_type`, `path_template`, TLS (`skip_tls_verify=false`+`ca_cert_secret`) | Vault thật của công ty |
| ingress | `gateway_name`, `gateway_namespace` | Gateway đang chạy |
| kubernetes | `storage_class`, `fleet_namespace`, `state_namespace` | tên thật trên cụm |
| ci | `verify_runner_label`, `score_k8s_version`, `score_compose_version` | khớp runner công ty |
| features | `application_values`, `vault_secrets`, `postgres_application`, `stack_onboarding` | **mặc định OFF**; bật khi sẵn sàng |
| database.backup | `object_store_url`, `endpoint_url`, `credentials_secret`... | **BẮT BUỘC nếu bật prod** (render prod bị chặn khi rỗng) |

Có sẵn `platform.env.company.yaml` làm bản mẫu tham chiếu các key.

## Khai trên GHES (không phải file)

- **Biến (Settings > Actions > Variables):** `RUNNER_LABEL` (nhãn runner của orchestrator),
  `CI_RUNNER_LABEL` (nhãn runner CI của app — GHES không có `ubuntu-latest`).
- **Secret:** `APP_ID`+`APP_PRIVATE_KEY` (GitHub App) hoặc `BOT_TOKEN`; `KUBECONFIG_STAGING`,
  `KUBECONFIG_PROD` (base64); `REGISTRY_HOST`, `REGISTRY_USER`, `REGISTRY_PASS`.
- Cài GitHub App (hoặc cấp token) quyền Contents/Pull requests/Metadata trên repo platform +
  các repo cấu hình. Tạo repo trong tổ chức thì App **không** làm được (cố ý) — người chạy
  `tools/tao-app-moi.sh` bằng tài khoản của mình một lần cho mỗi app.

## Sau khi chép

1. `git status` / `git diff` ở repo công ty — soi kỹ.
2. Merge tay `platform.env.yaml` theo bảng trên.
3. `python3 -m pytest test_engine.py -q` — lớp 1 phải xanh (0 skip nếu đủ tool).
4. Commit + mở pull request. Verify luồng thật theo `HUONG-DAN-KIEM-THU.md`
   ("Test một FEATURE qua luồng thật").
