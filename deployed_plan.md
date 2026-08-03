# Triển khai IDP vào hạ tầng công ty

Tài liệu chia làm hai phần:

| Phần | Cho ai |
|---|---|
| **A. Việc của bạn** | Người — cấp quyền, tạo token, dựng runner, duyệt |
| **B. Chat với AI nội bộ** | Các khối copy-paste, dán thẳng vào chat |

Làm xong hết phần A rồi mới sang phần B. AI không tự làm được phần A, và nếu thiếu thì nó sẽ
hỏng ở giữa chừng theo kiểu khó truy.

Chi tiết kỹ thuật nằm ở **Phụ lục** cuối file — AI được chỉ tới đó khi cần.

---

# PHẦN A — VIỆC CỦA BẠN

## A0. Đã khảo sát rồi, không phải làm

| Hạng mục | Kết quả |
|---|---|
| Quyền Kubernetes | tất cả `yes`, kể cả tạo namespace và clusterrolebinding |
| Gateway API | có bản experimental |
| `traefik-gateway` | `Accepted=True Programmed=True`, `allowedRoutes: All` |
| Fleet | có, Rancher quản, `GitRepo` ở `fleet-local` |
| StorageClass | `rook-ceph-block` (mặc định) |
| GHES | 3.18.12. Fine-grained PAT cần admin duyệt mà không có người duyệt -> **dùng classic** |
| Harbor | `harbor.stg.exampledevops.com`, HTTPS |

⛔ **Cụm đã đủ mọi thứ. Không cài đặt gì thêm lên cụm.** Cài đè lên Traefik đang phục vụ
ứng dụng của đội khác là rủi ro không cần thiết.

## A1. Quyết bốn giá trị

| Cần quyết | Đề xuất | Ghi chú |
|---|---|---|
| Project Harbor cho ảnh ứng dụng | `idp` | |
| Project Harbor cho ảnh mirror | `base` | Tách để robot đẩy ảnh ứng dụng không ghi đè được ảnh nền dùng chung |
| Quy ước tên repo cấu hình | `{app}-config` | |
| Tên miền production | ❓ chưa có | Cụm production đang dựng |

## A2. Harbor

- [ ] Tạo project `idp` và `base`
- [ ] Tạo **2 robot account**: một quyền đẩy (cho CI), một chỉ kéo (cho cụm)
- [ ] Mirror ảnh Postgres — **cụm không ra internet nên bắt buộc**

```bash
docker pull postgres:17-alpine
docker tag  postgres:17-alpine harbor.stg.exampledevops.com/base/postgres:17-alpine
docker push harbor.stg.exampledevops.com/base/postgres:17-alpine
```

Thiếu bước này: database `ImagePullBackOff`, ổ đĩa treo `Pending` **không rõ lý do**.

## A3. Hai token từ tài khoản dùng chung

**Dùng PAT classic.** Fine-grained PAT trỏ vào repo của tổ chức phải được admin tổ chức phê
duyệt, mà ở đây không có người duyệt — token sẽ nằm mãi ở trạng thái `Pending` và bị GHES từ
chối với `remote: Write access to repository not granted` (403), dù đã chọn "All repositories".

Settings → Developer settings → Personal access tokens → **Tokens (classic)**, tick đúng
**một** scope: `repo`.

| Tên | Đặt ở đâu | Dùng để |
|---|---|---|
| `BOT_TOKEN` | repo `idp-platform` | ghi manifest vào repo cấu hình, mở pull request |
| `PLATFORM_DISPATCH_TOKEN` | mỗi repo app và repo cấu hình | gọi platform, đọc repo platform |

Tạo **hai cái riêng** dù cùng scope: cái thứ hai phải nằm ở mọi repo ứng dụng nên phơi ra
rộng hơn nhiều. Lộ một cái thì thu hồi được mà không chết cái kia.

> **Đánh đổi của classic:** scope `repo` là thô — ghi được vào **mọi repo mà tài khoản đó
> nhìn thấy**, không giới hạn theo từng repo được. Phạm vi thiệt hại bị chặn bởi **tài khoản
> đó là thành viên của những repo nào**, chứ không phải bởi quyền của token. Nên chỉ mời tài
> khoản dùng chung vào đúng các repo IDP.
>
> Xin được duyệt fine-grained sau này thì đổi lại chỉ là thay giá trị secret, **không sửa
> code**.

- [ ] Ghi lại **tên** và **email** của tài khoản đó — dùng làm danh tính trên commit triển khai
- [ ] **Đặt hạn cho token** (ví dụ 1 năm) và lịch nhắc trước một tuần. Classic PAT cho phép
      "không bao giờ hết hạn" — đừng chọn; token vĩnh viễn là loại rò rỉ rồi sống mãi
- [ ] Nếu tổ chức bật SAML SSO: bấm **Authorize** cho token với tổ chức, nếu không nó vẫn 403

⚠️ Tài khoản này **phải khác** tài khoản người sẽ duyệt pull request. GitHub chặn tự duyệt
PR của chính mình — trùng nhau là cổng duyệt production tự khoá.

## A4. Ba runner

Mạng bị chia đôi: máy có internet không vào được Harbor, máy vào được Harbor thì không có
internet. Nên cần ba vai trò (máy Push và Orchestrator **có thể là một**).

| Vai trò | Cần gì |
|---|---|
| **Build** | Docker, **internet**, tới GHES |
| **Push** | Docker, **tới Harbor**, tới GHES |
| **Orchestrator** | `kubectl`, `helm`, `score-k8s`, `python3`+`pyyaml`, `gh`, **tới API server của cụm** |

- [ ] Đăng ký runner, ghi lại nhãn từng cái
- [ ] Máy Orchestrator: PATH của service phải có đủ các công cụ trên

## A5. Ba câu phải kiểm

```bash
# 1. Hai action này có trên GHES không — cách chuyển ảnh giữa hai máy phụ thuộc chúng
gh api repos/actions/upload-artifact   --jq .full_name
gh api repos/actions/download-artifact --jq .full_name

# 2. DNS wildcard — không có thì mỗi app mới phải xin một bản ghi riêng
dig +short bat-ky-ten-nao.stg.exampledevops.com

# 3. Runner đã online chưa
gh api /orgs/<ORG>/actions/runners --jq '.runners[]|"\(.name) \(.status) \(.labels[].name)"'
```

Câu 1 mà **không có** thì báo lại — mẫu CI phải đổi sang `gh release upload/download`.

## A6. Đưa mã nguồn vào GHES

> Mọi lệnh dưới đây dùng biến. **Sửa các dòng gán ở đầu khối rồi mới chạy cả khối** — đừng
> chép lẻ từng dòng, vì các dòng sau phụ thuộc biến ở dòng trước.

```bash
# ===== SỬA 2 DÒNG NÀY =====
ORG="example-org"                       # tên tổ chức trên GHES
NGUON="https://github.com/.../idp-platform.git"   # nơi lấy mã nguồn về
# ==========================
[ "$ORG" = "example-org" ] && echo "!! chưa sửa ORG, dừng lại" && return 2>/dev/null

git clone "$NGUON" idp-platform && cd idp-platform
rm -rf .git && git init -b main
git add -A && git commit -m "cài đặt platform"
gh repo create "$ORG/idp-platform" --private --source=. --push
```

## A7. Biến và bí mật

> ⚠️ **Mọi thứ dưới đây đặt ở cấp REPO, không dùng `--org`.** Đặt biến hoặc secret cấp tổ
> chức cần quyền owner/admin của tổ chức GitHub — quyền member không làm được. Ngoài ra biến
> cấp tổ chức còn ảnh hưởng tới repo của đội khác.

```bash
# ===== SỬA CÁC DÒNG NÀY =====
ORG="example-org"
NHAN_NOI_BO="runner-noi-bo"             # nhãn của runner vào được cụm
ROBOT_KEO="robot\$idp-pull"              # tài khoản robot CHỈ KÉO của Harbor
KUBECONFIG_FILE="$HOME/.kube/config"    # file kubeconfig của cụm
# ============================
R="$ORG/idp-platform"
[ -f "$KUBECONFIG_FILE" ] || { echo "!! không thấy $KUBECONFIG_FILE"; }

# Nhãn runner của orchestrator. GitHub chọn máy TRƯỚC khi chạy bước đầu tiên nên
# giá trị này không đọc được từ file trong repo.
gh variable set RUNNER_LABEL -R "$R" --body "$NHAN_NOI_BO"

gh secret set REGISTRY_HOST -R "$R" --body 'harbor.stg.exampledevops.com'
gh secret set REGISTRY_USER -R "$R" --body "$ROBOT_KEO"

# Ba secret dưới đây gõ giá trị vào khi được hỏi, KHÔNG truyền qua tham số —
# tránh để mật khẩu nằm lại trong lịch sử lệnh của shell.
echo "dán BOT_TOKEN rồi Ctrl-D:";              gh secret set BOT_TOKEN     -R "$R"
echo "dán mật khẩu robot chỉ kéo rồi Ctrl-D:"; gh secret set REGISTRY_PASS -R "$R"

# Hiện chỉ có một cụm. Trỏ cả hai môi trường vào đó, tách nhau bằng namespace.
# Khi cụm production xong thì đổi đúng secret KUBECONFIG_PROD.
base64 -w0 < "$KUBECONFIG_FILE" | gh secret set KUBECONFIG_STAGING -R "$R"
base64 -w0 < "$KUBECONFIG_FILE" | gh secret set KUBECONFIG_PROD    -R "$R"

gh secret list -R "$R"      # phải thấy đủ 6
```

## A7b. Nhãn runner cho CI của ứng dụng — sửa MỘT lần trong mẫu

CI của ứng dụng cũng cần biết chạy trên máy nào. Nhưng đặt biến cấp tổ chức thì không có
quyền, mà đặt cho từng repo lại phải nhớ mỗi lần thêm app.

Cách gọn nhất: **sửa giá trị mặc định trong mẫu CI** của bạn. Mọi app chép từ mẫu đó nên tự
đúng, không phải đặt biến nào cả.

Trong `.github/workflows/ci.yaml` của app mẫu, đổi hai dòng:

```yaml
# job build — máy CÓ internet
runs-on: ${{ vars.CI_RUNNER_LABEL || 'runner-internet' }}

# job push — máy vào được Harbor
runs-on: ${{ vars.PUSH_RUNNER_LABEL || vars.CI_RUNNER_LABEL || 'runner-noi-bo' }}
```

Thay `runner-internet` và `runner-noi-bo` bằng nhãn thật của bạn. Phần `vars.` giữ nguyên —
nó cho phép ghi đè cho một repo riêng lẻ sau này nếu cần, mà không phải sửa lại mẫu.

Làm tương tự với tên đăng nhập registry:

```yaml
REG_USER: ${{ vars.REGISTRY_USERNAME || '<robot-đẩy-của-Harbor>' }}
```

## A7c. Mỗi app mới cần 2 secret

Đây là phần **không tránh được** phải lặp lại, vì secret không hardcode được:

```bash
ORG="example-org"; APP="smoke"

echo "dán PLATFORM_DISPATCH_TOKEN rồi Ctrl-D:"
gh secret set PLATFORM_DISPATCH_TOKEN -R "$ORG/$APP"
gh secret set PLATFORM_DISPATCH_TOKEN -R "$ORG/$APP-config"

echo "dán mật khẩu robot ĐẨY của Harbor rồi Ctrl-D:"
gh secret set REGISTRY_PASSWORD -R "$ORG/$APP"
```

> Nếu sau này xin được quyền đặt secret cấp tổ chức thì gộp lại còn một lần cho tất cả.

## A8. Bảo vệ nhánh

**Đây là nơi DUY NHẤT quyết định môi trường nào cần duyệt.** Nền tảng không khai điều đó ở
đâu cả — nó hỏi thẳng GitHub xem nhánh đích có được bảo vệ không rồi làm theo.

| Repo | Nhánh | Cấu hình |
|---|---|---|
| repo cấu hình | `dev` | bảo vệ, **thêm tài khoản bot vào danh sách bypass** |
| repo cấu hình | `main` | bảo vệ, **bắt buộc PR + người duyệt**, bot **không** bypass |
| `idp-platform` | `main` | bảo vệ — `PLATFORM_DISPATCH_TOKEN` ghi được vào đây |

Thiếu bypass ở `dev`: mỗi lần deploy staging đều mở một PR chờ người. Vẫn đúng, chỉ chậm.

## A9. Trong lúc chạy thử

- [ ] Duyệt và merge pull request lên production khi AI báo đã mở

---

# PHẦN B — CHAT VỚI AI NỘI BỘ

Dán từng khối, **theo thứ tự**. Đợi AI báo xong và **đọc kết quả kiểm chứng** rồi mới sang
khối tiếp theo.

## B1. Khảo sát và xác nhận

```text
Tôi cần bạn giúp triển khai một Internal Developer Platform. Mã nguồn ở <ORG>/idp-platform
trên GHES, đọc file deployed_plan.md trong đó để hiểu hệ thống.

Việc đầu tiên: CHỈ KHẢO SÁT, chưa thay đổi gì. Chạy tools/thu-thap-ha-tang.sh và đối chiếu
với những gì đã biết:
- Gateway traefik-gateway ở namespace traefik, allowedRoutes phải là All
- StorageClass rook-ceph-block
- Fleet có, GitRepo nằm ở namespace fleet-local
- Gateway API bản experimental (TLSRoute có version v1)

Báo lại từng mục khớp hay không khớp. In nguyên văn kết quả lệnh, đừng tóm tắt.

Nếu có mục nào KHÔNG khớp thì dừng lại và báo, đừng tự sửa cụm — cụm này đang phục vụ ứng
dụng của đội khác.
```

## B2. Cấu hình nền tảng

```text
Sửa file platform.env.yaml trong repo <ORG>/idp-platform theo các giá trị sau:

  git.org: <ORG>
  git.config_repo_pattern: "{app}-config"
  git.committer_name: <tên-tài-khoản-bot>
  git.committer_email: <email-tài-khoản-bot>
  registry.host: harbor.stg.exampledevops.com
  registry.path: harbor.stg.exampledevops.com/idp
  images.postgres: harbor.stg.exampledevops.com/base/postgres:17-alpine
  kubernetes.storage_class: rook-ceph-block
  ingress.gateway_name: traefik-gateway
  ingress.gateway_namespace: traefik
  environments.staging.domain: stg.exampledevops.com
  environments.staging.config_branch: dev
  environments.prod.config_branch: main

QUAN TRỌNG: file phải sửa là platform.env.yaml. Trong repo có platform.env.company.yaml
nhưng đó chỉ là tờ nháp, workflow KHÔNG đọc nó. Điền nhầm vào đó thì hệ thống chạy bằng giá
trị mặc định và deploy vào nhầm chỗ mà không báo gì.

Sau khi sửa, chạy ba lệnh kiểm này và báo kết quả:

  grep -nE 'todo-|\.invalid|TODO' platform.env.yaml
  kubectl -n traefik get gateway traefik-gateway
  kubectl get storageclass rook-ceph-block

Lệnh đầu phải không ra gì. Hai lệnh sau phải tìm thấy đối tượng — vì sai tên Gateway hoặc
StorageClass KHÔNG gây lỗi lúc render, nó chỉ lộ ra rất muộn dưới dạng tuyến đường không bao
giờ gắn được hoặc ổ đĩa Pending vĩnh viễn.

Commit và push.
```

## B3. Tạo ứng dụng thử

```text
Tạo một ứng dụng thử tên "smoke" để kiểm chứng nền tảng. Làm theo HUONG-DAN-TAO-APP-MOI.md
trong repo platform. Tóm tắt:

1. Repo <ORG>/smoke với 4 file: score.yaml, Dockerfile, platform.lock,
   .github/workflows/ci.yaml
   - Chép ci.yaml từ mẫu MỘT service (không phải mẫu nhiều service)
   - Sửa APP, IMAGE_NAME, PLATFORM_REPO cho đúng
2. Repo <ORG>/smoke-config với HAI nhánh: dev và main, mỗi nhánh ít nhất 1 commit
3. Chép templates/config-repo-verify.yaml vào .github/workflows/verify.yaml của smoke-config,
   sửa APP và PLATFORM_REPO
4. Đặt secret cho repo (tôi sẽ cấp giá trị):
   - PLATFORM_DISPATCH_TOKEN cho CẢ HAI repo
   - REGISTRY_PASSWORD cho repo ứng dụng

CHƯA tạo GitRepo của Fleet — việc đó ở bước sau, có điều kiện phải kiểm trước.

Báo lại tên hai repo đã tạo và nội dung score.yaml.
```

## B4. Đăng ký với Fleet

```text
Tạo hai GitRepo của Fleet cho ứng dụng smoke, ở namespace fleet-local.

TRƯỚC KHI TẠO, bắt buộc kiểm trùng tên:

  kubectl -n fleet-local get gitrepo

Cụm này đang có GitRepo của đội khác. Đặt trùng tên là ĐÈ LÊN cái đang có và ứng dụng của họ
ngừng đồng bộ trong im lặng. Nếu tên định đặt đã tồn tại thì DỪNG và báo.

Quy ước tên bắt buộc: <app>-<môi-trường>, tức smoke-staging và smoke-prod. Hai môi trường
dùng chung một cụm nên tên phải kèm môi trường, nếu không cái sau đè cái trước.

  apiVersion: fleet.cattle.io/v1alpha1
  kind: GitRepo
  metadata: { name: smoke-staging, namespace: fleet-local }
  spec:
    repo: https://<GHES>/<ORG>/smoke-config
    branch: dev
    paths: [staging]
    clientSecretName: <tên-secret-git-creds-đang-dùng>
    pollingInterval: 15s

  # prod: giống trên, đổi name thành smoke-prod, branch thành main, paths thành [prod]

Kiểm xem các GitRepo sẵn có đang dùng clientSecretName nào và dùng đúng cái đó.

Quên bước này là triệu chứng đánh lừa nhất của cả hệ thống: mọi bước báo xanh, manifest có
trong repo cấu hình, nhưng cụm KHÔNG có gì — vì không ai kéo về.

Báo lại: kubectl -n fleet-local get gitrepo
```

## B5. Chạy thử staging

```text
Đẩy một commit lên nhánh dev của repo <ORG>/smoke để kích hoạt triển khai.

Theo dõi theo đúng thứ tự này, MỖI bước phải xanh mới sang bước sau. Báo lại kết quả từng
bước, in nguyên văn:

1. CI của app:      gh run list -R <ORG>/smoke --branch dev --limit 1
   - CI có HAI job: build (máy có internet) và push (máy vào được Harbor)
   - Job push hỏng ở bước tải tệp nghĩa là GHES thiếu actions/download-artifact -> báo tôi
2. Orchestrator:    gh run list -R <ORG>/idp-platform --limit 1
3. Manifest đã vào repo cấu hình:
                    git -C smoke-config show origin/dev:staging/manifests.yaml | head -30
4. Ảnh có thật trên Harbor:
                    docker manifest inspect <ảnh-ghi-trong-manifest>
5. Fleet đã đồng bộ: kubectl -n fleet-local get bundle
6. Pod chạy:        kubectl -n smoke-staging get pods
7. Trang trả lời:   curl -I https://smoke.stg.exampledevops.com

Bước 4 quan trọng: "manifest ghi đúng" KHÔNG có nghĩa là ảnh tồn tại. Đã có sự cố thật khi
CI đẩy một nhãn còn manifest đòi một nhãn khác — mọi bước báo xanh, chỉ pod là chết.

Bước nào đỏ thì dừng, in log, đừng chạy lại mù.
```

## B6. Chạy thử production

```text
Đưa ứng dụng smoke lên production:

  git checkout main && git merge dev && git push origin main

Lần này khác staging: nhánh main của repo cấu hình được bảo vệ nên orchestrator sẽ MỞ PULL
REQUEST thay vì ghi thẳng. Bước kiểm cụm bị bỏ qua ở lần chạy đó — đúng, vì manifest chưa
merge nên chưa có gì để kiểm.

1. Báo tôi link pull request. TÔI sẽ duyệt và merge, bạn đừng tự merge.
2. Sau khi tôi merge, workflow verify trong repo cấu hình tự gọi ngược lại platform để kiểm
   cụm. Theo dõi:

     gh run list -R <ORG>/idp-platform --limit 1

   Phải thấy một lần chạy verify-request và nó phải success.

   Nếu đỏ: production KHÔNG chạy đúng thứ vừa render. Log in sẵn danh sách pod và sự kiện,
   gửi tôi nguyên văn.

3. Kiểm cuối:  kubectl -n smoke-prod get pods
```

## B7. Báo cáo

```text
Tổng kết lại toàn bộ quá trình:

1. Kết quả NGUYÊN VĂN của mọi lệnh kiểm chứng ở B1 tới B6 — đừng tóm tắt thành "đã xong"
2. Bước nào phải làm khác kế hoạch, và vì sao
3. Bước nào bị chặn, chặn ở đâu, cần ai xử lý
4. Giá trị đã điền vào platform.env.yaml (che phần bí mật)
5. Những gì đã tạo mới: repo, GitRepo, namespace, secret — để tôi dọn được nếu cần làm lại
```

---

# PHỤ LỤC

## P1. Hệ thống này hoạt động ra sao

Lập trình viên chỉ viết một file `score.yaml` mô tả *ứng dụng cần gì*; nền tảng sinh toàn bộ
manifest Kubernetes.

| Kho | Ai ghi | Nội dung |
|---|---|---|
| App | người | `score.yaml`, `Dockerfile`, `platform.lock`, `ci.yaml` |
| Platform | đội nền tảng | `orchestrate.py`, workflow, provisioners, patches |
| Config | **máy** | manifest đã sinh. `dev` = staging, `main` = production |

Đẩy code → CI build ảnh → gọi platform → platform render manifest → ghi vào repo cấu hình →
Fleet trên cụm kéo về và áp dụng.

**Cụm kéo, platform không đẩy.** Orchestrator không có quyền `kubectl apply` manifest ứng
dụng — nó chỉ ghi vào git.

## P2. CI của ứng dụng chạy hai job

```
build (máy có internet)              push (máy nội bộ)
  hỏi platform tên ảnh                 tải tệp về
  docker build          ──tệp──▶       docker load
  docker save | gzip                   docker login Harbor
  tải lên artifact                     docker push
                                       gọi platform
```

- Tên ảnh tính đúng **một lần** ở job build rồi chuyển sang job push. Tính hai lần là hai cơ
  hội ra hai kết quả khác nhau — lỗi này đã làm hỏng production một lần.
- Gọi platform nằm ở job push, **sau** khi đẩy ảnh. Ngược lại thì manifest trỏ tới ảnh chưa
  tồn tại.
- Không đặt `PUSH_RUNNER_LABEL` thì nó rơi về `CI_RUNNER_LABEL` — môi trường một máy chạy y
  nguyên.

## P3. Những lỗi im lặng — tra nhanh

| Triệu chứng | Nguyên nhân thường gặp | Kiểm bằng |
|---|---|---|
| Mọi bước xanh, cụm trống trơn | Chưa tạo `GitRepo` của Fleet | `kubectl get gitrepo -A` |
| Ứng dụng của đội khác ngừng đồng bộ | `GitRepo` mới trùng tên cái đang có | `kubectl -n fleet-local get gitrepo` |
| Tạo `GitRepo` xong Fleet vẫn không nhận | Sai namespace | so với `GitRepo` đang có |
| Pod `ImagePullBackOff` | Nhãn ảnh trong manifest chưa được đẩy lên Harbor | `docker manifest inspect <ảnh>` |
| Database `ImagePullBackOff`, PVC `Pending` | Chưa mirror ảnh Postgres | `docker manifest inspect <images.postgres>` |
| Ứng dụng chạy nhưng không vào được | Sai tên/namespace Gateway, hoặc `allowedRoutes` chặn | `kubectl get httproute -A -o wide` |
| CI nằm chờ mãi không chạy | Nhãn runner không khớp máy nào | `gh api /orgs/<ORG>/actions/runners` |
| Job `push` hỏng ở bước tải tệp | GHES thiếu `actions/download-artifact` | `gh api repos/actions/download-artifact` |
| CI đẩy được ảnh nhưng pod kéo không được | `registry.path` chỉ máy CI phân giải được | thử `crictl pull` trên node |
| Fleet ngừng đồng bộ sau 1 giờ | `git-creds` dùng token GitHub App (hết hạn 1 giờ) | `kubectl -n fleet-local get gitrepo -o wide` |
| Deploy đột nhiên 401 | PAT hết hạn | xem ngày hết hạn của token |
| `remote: Write access to repository not granted` (403) | Fine-grained PAT chưa được tổ chức duyệt, hoặc Resource owner để nhầm tài khoản cá nhân | dùng classic PAT scope `repo` |

## P4. Hai quyết định còn treo

**Cụm production dùng Harbor nào.** Tên `harbor.stg...` cho thấy sẽ có Harbor riêng cho
production. Nhưng khi thăng cấp, nền tảng **chép nguyên tham chiếu ảnh** staging đã chạy —
đó chính là thứ bảo đảm "production chạy đúng ảnh đã kiểm, không phải bản xây lại". Hai
Harbor khác tên miền thì tham chiếu trỏ sai. Khuyến nghị: **một tên miền Harbor cho cả hai**.

**Tên miền production.** Chưa có, chờ cụm production.

## P5. Sau khi chạy được

| Việc | Vì sao |
|---|---|
| Kiểm tra quyền sở hữu ứng dụng | Tên app và repo do bên gọi **tự khai**, không xác thực. Vô hại khi một đội dùng, nguy hiểm khi nhiều đội |
| Fleet dùng deploy key chỉ đọc | Fleet chỉ cần đọc, không bao giờ ghi |
| `kubeconfig` giới hạn quyền | Nên là ServiceAccount đủ quyền trên namespace của app + `cluster-state`, không phải admin cụm |
| Tự động hoá đăng ký ứng dụng | 4 bước ở B3–B4 hiện làm tay |

## P6. Tài liệu chi tiết

| File | Dùng khi |
|---|---|
| `HUONG-DAN-TAO-APP-MOI.md` | Đăng ký một ứng dụng vào nền tảng |
| `HUONG-DAN-CAI-DAT.md` | Dựng nền tảng trên hạ tầng TRỐNG (không dùng cho cụm này) |
| `TAI-LIEU-DU-AN.md` | Hiểu vì sao thiết kế như vậy; lịch sử các sự cố |
| `tools/thu-thap-ha-tang.sh` | Khảo sát lại hiện trạng, chỉ đọc |
| `tools/mint-app-token.sh` | Chỉ dùng nếu chuyển sang GitHub App thay vì `BOT_TOKEN` |
