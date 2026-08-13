# GAP-REGISTER — tương thích một-source cho harness và hạ tầng công ty

> Đối chiếu Discovery (`kubernetes/fleet/gateway/vault/database/ghes/runner/registry`) với
> source thật + hai profile (`platform.env.yaml`, `platform.env.company.yaml`) + harness.
> Placeholder cho mọi toạ độ công ty — KHÔNG có endpoint/định danh thật ở đây:
> `<GIT_HOST> <REGISTRY_HOST> <STORAGE_CLASS> <GATEWAY_NAME> <LISTENER_NAME> <SECRET_NAME> <VAULT_ADDR>`.

## 0. Tóm tắt phân loại

| Loại | Số | Có sửa code |
|---|---|---|
| `PORTABILITY_CODE_GAP` | 3 | ✅ DATABASE-02 (backend statefulset), GATEWAY-06 (sectionName), REGISTRY-05 (log credential) |
| `CONFIG_ONLY` | 9 | không (source đã hỗ trợ, chỉ điền config) |
| `SECRET_OR_VARIABLE` | 3 | không (source đã biểu diễn reference) |
| `INFRA_PREREQUISITE` | 6 | không (đội hạ tầng cấp) |
| `UNKNOWN` | 3 | không (chờ bằng chứng) |

Hai khe hở code phụ nhỏ được xử lý kèm (URL scheme/host user-facing) — xem PORT-URL.

---

## 1. PORTABILITY_CODE_GAP (đã sửa source)

### DATABASE-02 — backend `statefulset` cho `class: application`
- **Domain:** database
- **Mô tả:** `postgres class: application` trước đây CHỈ render CNPG `Cluster`. Cụm công ty
  không có CNPG operator và không yêu cầu CNPG; database là PostgreSQL `StatefulSet` trên
  `<STORAGE_CLASS>`. Source không có cách biểu diễn backend thứ hai.
- **Bằng chứng Discovery:** `database.yaml` DATABASE-02 (`PORTABILITY_CODE_GAP`, blocker),
  `required_database_backend: statefulset`; `kubernetes.yaml` K8S-01 (không có CNPG CRD).
- **Bằng chứng source:** `provisioners/postgres-application.provisioners.yaml` chỉ có Cluster;
  `orchestrate.py` glob mọi provisioner, không chọn backend.
- **Bằng chứng harness:** render + `tools/thu-nghiem-db-statefulset.sh` (runtime kind).
- **Phân loại:** ban đầu `PORTABILITY_CODE_GAP` → cuối `PORTABILITY_CODE_GAP`.
- **Feature:** `postgres_application`.
- **Sửa code:** CÓ.
  - `orchestrate.py`: `database.backend` (DEFAULTS, mặc định `cnpg`), `database_backend()` +
    enum validate, `select_provisioner_files()` (chọn đúng MỘT file `class: application`),
    guard prod chặn statefulset (không HA/backup).
  - `provisioners/postgres-application-statefulset.provisioners.yaml` (mới): StatefulSet +
    Service `<cluster>-rw`/`-hl` + VaultStaticSecret, output GIỐNG HỆT backend CNPG.
- **Config còn lại:** `database.backend: statefulset`, `database.image_repository` (ảnh postgres
  thường đã mirror về `<REGISTRY_HOST>`), `database.storage_class`/`kubernetes.storage_class`.
- **Test bảo vệ:** `test_statefulset_backend_renders_a_real_statefulset_from_config`,
  `test_cnpg_backend_is_unchanged_by_the_new_key`, `test_statefulset_prod_is_refused_no_backup_no_ha`,
  `test_backend_selects_exactly_one_postgres_application_file`, `test_database_backend_enum`,
  `test_every_postgres_provisioner_declares_its_class` (đã cập nhật theo tập ĐÃ CHỌN).
- **Trạng thái:** ĐÃ SỬA — runtime PASS (Ready · PVC Bound · SQL · bền qua restart).

### GATEWAY-06 — `parentRefs.sectionName` chọn listener
- **Domain:** gateway
- **Mô tả:** Gateway công ty có 2 listener (`web`/HTTP, `<LISTENER_NAME>`/HTTPS Terminate).
  Route provisioner chỉ khai `name`/`namespace`, KHÔNG có `sectionName` → HTTPRoute attach
  nhầm listener HTTP, không đâu báo lỗi. Source không có cách khai listener.
- **Bằng chứng Discovery:** `gateway.yaml` GATEWAY-06 (medium), GATEWAY-07 (route hiện có dùng
  `sectionName: <LISTENER_NAME>`).
- **Bằng chứng source:** `provisioners/local.provisioners.yaml` route-traefik chỉ có name/namespace.
- **Bằng chứng harness:** `test_route_sectionName_is_config_driven`.
- **Phân loại:** ban đầu (contract) `CONFIG_ONLY` → cuối `PORTABILITY_CODE_GAP` (source chưa
  biểu diễn được → đúng luật §5: đổi sang code gap, KHÔNG hard-code).
- **Feature:** `application_values` (route).
- **Sửa code:** CÓ. `ingress.section_name` (DEFAULTS rỗng); route provisioner render
  `sectionName` CÓ ĐIỀU KIỆN — RỖNG = manifest cũ y hệt (một-listener).
- **Config còn lại:** `ingress.section_name: <LISTENER_NAME>` (công ty).
- **Test bảo vệ:** `test_route_sectionName_is_config_driven`, `test_route_scheme_and_section_per_profile`.
- **Trạng thái:** ĐÃ SỬA — render PASS (harness: không sectionName; công ty: `<LISTENER_NAME>`).

### REGISTRY-05 — credential không được xuất hiện trong transcript
- **Domain:** registry + secret hygiene.
- **Mô tả:** đường `apply-secrets` phải truyền password cho `kubectl create`, nhưng plumbing
  từng log nguyên argv nên một lần chạy tay có thể in credential registry ra transcript.
- **Bằng chứng harness:** bài registry private phát hiện trực tiếp trong lần runtime đầu;
  credential fixture là ngẫu nhiên, registry/credential đã bị teardown ngay sau lần chạy.
- **Phân loại:** phát hiện bổ sung → `PORTABILITY_CODE_GAP` vì cần sửa product code.
- **Sửa code:** CÓ. `run(..., sensitive=...)` chỉ che phần hiển thị, không đổi argv thực thi;
  áp dụng cho registry password và credential object-store.
- **Test bảo vệ:** `tools/thu-nghiem-registry-private.sh` fail nếu log chứa password thật
  hoặc không có marker `<redacted>`.
- **Trạng thái:** ĐÃ SỬA — runtime registry PASS, transcript chỉ còn `<redacted>`.

### PORT-URL — scheme/host của URL người-dùng-thấy (khe hở phụ)
- **Domain:** gateway + ghes
- **Mô tả:** `stagingUrls/prodUrls` in cứng `http://`; `onboarding_config_repo_url` in cứng
  `https://github.com/...` (còn dùng để KHỚP GitRepo Fleet → sai host trên GHES là khớp trượt).
- **Bằng chứng Discovery:** `gateway.yaml` GATEWAY-09 (TLS Terminate → https); `ghes.yaml` (GHES host).
- **Sửa code:** CÓ. `ingress.route_scheme` (mặc định `http`); `git_server_url()` đọc
  `GITHUB_SERVER_URL` (github.com hoặc GHES), fallback github.com.
- **Test bảo vệ:** `test_config_repo_url_follows_github_server_url`, `test_route_scheme_and_section_per_profile`,
  `test_user_facing_github_host_is_not_hardcoded`.
- **Trạng thái:** ĐÃ SỬA — unit PASS.

---

## 2. CONFIG_ONLY (source đã hỗ trợ, chỉ điền config)

| ID | Domain | Mô tả | Config key | Test/bằng chứng |
|---|---|---|---|---|
| DATABASE-01 | database | StorageClass StatefulSet lấy từ config | `database.storage_class`/`kubernetes.storage_class` | render statefulset: `storageClassName == config` |
| GATEWAY-01/03 | gateway | Gateway `<GATEWAY_NAME>`/`<namespace>` khai qua config | `ingress.gateway_name/namespace` | `test_both_profiles_load...` + doctor gateway |
| GATEWAY-02 | gateway | `platform.env.yaml` (harness) dùng `ingress-gateway` — ĐÚNG với cụm kind harness (không phải bug); công ty dùng `<GATEWAY_NAME>` | `ingress.gateway_name` | doctor kind-staging: Gateway có mặt |
| GATEWAY-08 | gateway | Wildcard DNS staging resolve — hạ tầng, khai domain qua config | `environments.staging.domain` | — |
| FLEET-01/02/04 | fleet | Fleet `fleet-local` + học credential từ GitRepo cùng namespace (fleet_git_secret rỗng) | `kubernetes.fleet_namespace`, `kubernetes.fleet_git_secret` | ensure-gitrepo liệt-kê-rồi-khớp |
| FLEET-03 | fleet | Không caBundle (git dùng cert công khai) | — | — |
| VAULT-05/06/07 | vault | kv_mount/kv_type/TLS khai qua config | `vault.kv_mount/kv_type/skip_tls_verify/ca_cert_secret` | `test_both_profiles_load...`; doctor vault.tls |
| GHES-01 | ghes | require-PR: source HỎI THẲNG branch protection thật rồi mở PR (không dựa cờ tĩnh) | `environments.*.config_branch` | `check_branch_protected` + PR-flow test |
| RUNNER-01 | runner | Nhãn runner qua Actions var, KHÔNG hard-code | `vars.RUNNER_LABEL` (workflow), `ci.verify_runner_label` | `runs-on: [...RUNNER_LABEL || 'platform-orchestrator']` |
| REGISTRY-01 | registry | Registry host/path qua config | `registry.host/path`, `images.*` | runtime HTTPS/auth/custom-CA: runner push + workload pull |

> Ghi chú GATEWAY-02: contract gắn nhãn "sai gateway name" cho `platform.env.yaml`, nhưng đó là
> tên Gateway THẬT của cụm kind harness (doctor xác nhận `ingress-gateway` tồn tại). Không phải
> lỗi — chỉ là toạ độ khác của một môi trường khác. Không đổi.

---

## 3. SECRET_OR_VARIABLE (cần variable/credential, source đã biểu diễn reference)

| ID | Domain | Mô tả | Reference | Test/bằng chứng |
|---|---|---|---|---|
| REGISTRY-02 | registry | Workload kéo ảnh private qua `imagePullSecret` — patch tiêm cho MỌI Deployment/StatefulSet/CronJob | `registry.pull_secret` | runtime: thiếu Secret bị auth từ chối; có Secret thì pull + Ready |
| GHES-02 | ghes | Bot auth: `BOT_TOKEN` hoặc `APP_ID`+`APP_PRIVATE_KEY` | `secrets.*` (GHES) | workflow GHES-aware (`GH_ENTERPRISE_TOKEN`, `mint-app-token.sh`) |
| RUNNER-01b | runner | Nhãn runner khai qua Actions variable | `vars.RUNNER_LABEL` | như trên |

---

## 4. INFRA_PREREQUISITE (đội hạ tầng cấp — KHÔNG sửa code)

| ID | Domain | Mô tả | Chặn feature | Ghi chú |
|---|---|---|---|---|
| DATABASE-03 | database | Chưa có backup backend cho production | `production_database_backup` | render prod backend cnpg CHẶN khi `object_store_url` rỗng; backend statefulset CHẶN prod hẳn |
| DATABASE-04 | database | Ảnh postgres phải mirror về `<REGISTRY_HOST>` trước deploy | none | supply chain runner→registry |
| K8S-01 | kubernetes | Không có CNPG operator | `postgres_application` (backend cnpg) | GIẢI QUYẾT bằng DATABASE-02: công ty dùng backend `statefulset` |
| K8S-02 / VAULT-01 | vault | VSO chưa cài | `vault_secrets` | feature OFF ở công ty; doctor chặn khi bật mà thiếu VSO |
| VAULT-02 | vault | Chưa có Kubernetes auth role cho idp-platform | `vault_secrets` | Vault Ops cấp; `vault.auth_role_template` |
| GATEWAY-10 / REGISTRY-03 | gateway/registry | LoadBalancer pending; custom CA phải tin cậy trên runner+runtime | none | không chặn; CA đã cài trên runner (runner.yaml RUNNER-02) |

---

## 5. UNKNOWN (chưa đủ bằng chứng)

| ID | Domain | Mô tả | Cần |
|---|---|---|---|
| FLEET-05 | fleet | Phiên bản Fleet controller (CRD v1alpha1) | soi pod controller |
| VAULT-03 | vault | `auth_audience` chưa xác định | Vault Ops cung cấp |
| VAULT-04 | vault | Sở hữu policy (tự tạo hay Vault Ops giữ) | quy trình Vault Ops |

---

## 6. Bất biến tương thích ngược đã giữ

- 4 feature flag mặc định OFF ở `platform.env.company.yaml` (test `test_company_config_also_ships_with_every_feature_off`).
- Key mới (`database.backend`, `ingress.section_name/route_scheme`) để trống = hành vi cũ
  (`test_new_keys_have_backward_compatible_defaults`).
- Backend mặc định `cnpg` render `Cluster` y như trước (`test_cnpg_backend_is_unchanged_by_the_new_key`).
- Không literal công ty trong source dùng chung (`test_no_company_coordinates_leak_into_shared_source`).
