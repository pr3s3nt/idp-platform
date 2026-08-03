# Kế hoạch triển khai IDP vào hạ tầng công ty

**Đọc kỹ phần 0 trước khi chạy bất kỳ lệnh nào.**

Tài liệu này viết cho một tác nhân AI nội bộ thực thi. Mỗi bước được đánh dấu:

| Nhãn | Nghĩa |
|---|---|
| **[AI]** | Tác nhân tự làm được |
| **[NGƯỜI]** | Phải do người thực hiện — cần quyền, cần bí mật, hoặc phải xin đội khác |
| **[CHẶN]** | Không qua được bước này thì **dừng lại**, đừng đi tiếp |

---

## 0. Bối cảnh — hệ thống này là gì

Một Internal Developer Platform. Lập trình viên chỉ viết một file `score.yaml` mô tả *ứng
dụng cần gì*; nền tảng sinh ra toàn bộ manifest Kubernetes.

**Ba kho mã, ba vai trò:**

| Kho | Ai ghi | Nội dung |
|---|---|---|
| App | người | `score.yaml`, `Dockerfile`, `platform.lock`, `ci.yaml` |
| Platform | đội nền tảng | `orchestrate.py`, workflow, provisioners, patches, cấu hình môi trường |
| Config | **máy** | manifest đã sinh. Hai nhánh: `dev` = staging, `main` = production |

**Luồng:** đẩy code → CI build ảnh → gọi platform → platform render manifest → ghi vào kho
config → Fleet trên cụm kéo về và áp dụng.

Cụm **kéo**, platform **không đẩy**. Orchestrator không có quyền `kubectl apply` manifest
ứng dụng — nó chỉ ghi vào git.

### Nguyên tắc quan trọng nhất khi triển khai

**Những lỗi nguy hiểm nhất của hệ thống này đều im lặng.** Sai tên Gateway → tuyến đường
không bao giờ được gắn, không có lỗi ở đâu. Sai tên StorageClass → ổ đĩa `Pending` vĩnh
viễn. Quên tạo `GitRepo` → mọi bước báo xanh, cụm trống trơn.

Vì vậy: **sau mỗi bước phải chạy lệnh kiểm chứng đi kèm.** Không được suy ra "lệnh không báo
lỗi nghĩa là đã đúng".

---

## 1. Điều kiện tiên quyết

### 1.1 [NGƯỜI][CHẶN] Đưa mã nguồn vào GHES công ty

Mã nguồn hiện nằm ngoài công ty. Phải mirror vào GHES nội bộ trước:

```bash
git clone <nguồn> idp-platform && cd idp-platform
rm -rf .git && git init -b main
git add -A && git commit -m "cài đặt platform"
gh repo create <ORG>/idp-platform --private --source=. --push
```

### 1.2 [NGƯỜI][CHẶN] Quyền phải xin trước

Ba việc dưới đây **không có đường vòng**. Xin xong mới bắt đầu.

| Xin ai | Xin gì | Vì sao |
|---|---|---|
| Đội vận hành K8s | Tạo namespace theo tiền tố `<đội>-*`, **hoặc** tạo sẵn `{app}-staging`, `{app}-prod`, `cluster-state` | Nền tảng tự tạo namespace; không có quyền thì phải có sẵn |
| Đội vận hành K8s | Gateway `traefik-gateway` cho namespace của đội gắn `HTTPRoute` | **Không mở thì route không attach và không báo lỗi gì** |
| Quản trị Harbor | 1 project + 2 robot account (một đẩy, một kéo) | CI đẩy ảnh, cụm kéo ảnh |

Kiểm Gateway có cho phép không:

```bash
kubectl -n traefik get gateway traefik-gateway \
  -o jsonpath='{range .spec.listeners[*]}{.name}{" -> "}{.allowedRoutes.namespaces}{"\n"}{end}'
```

Nếu ra `{"from":"All"}` là mở cho mọi namespace. Nếu là `Selector` hoặc `Same` thì **phải
xin bổ sung**.

### 1.3 [NGƯỜI][CHẶN] Ảnh nền phải có trên Harbor

Cụm nội bộ thường không ra được internet. Mirror sẵn:

```bash
# ví dụ
docker pull postgres:17-alpine
docker tag postgres:17-alpine <HARBOR>/<PROJECT>/postgres:17-alpine
docker push <HARBOR>/<PROJECT>/postgres:17-alpine
```

Bỏ qua bước này thì `StatefulSet` của database sẽ `ImagePullBackOff`, còn `PersistentVolumeClaim`
thì `Pending` mãi không rõ lý do.

### 1.4 [NGƯỜI] Máy chạy CI

Một máy Linux:

- Docker chạy được **không cần `sudo`**
- Mạng tới: GHES, Harbor, **API server của cả hai cụm**
- Cần **root một lần** cho hai chỉnh sửa ở bước 3.1

### 1.5 [NGƯỜI] Danh tính bot

Nền tảng ghi vào kho config bằng một danh tính máy. **Bắt buộc phải khác tài khoản người sẽ
duyệt pull request** — GitHub chặn tự duyệt PR của chính mình, nên nếu trùng thì cổng duyệt
production tự khoá.

Công ty đã có **tài khoản dùng chung** hỗ trợ PAT fine-grained → dùng nó.

Tạo **hai** PAT riêng, cùng tài khoản đó:

| Tên | Phạm vi | Quyền | Đặt ở đâu |
|---|---|---|---|
| `BOT_TOKEN` | các repo config | `Contents: write`, `Pull requests: write` | repo platform |
| `PLATFORM_DISPATCH_TOKEN` | repo platform | `Contents: write` | mỗi repo app và repo config |

Tách hai cái vì cái thứ hai phải nằm ở **mọi repo app**, phơi ra rộng hơn nhiều. Lộ một cái
thì thu hồi được mà không chết cái kia.

> ⚠️ PAT fine-grained **bắt buộc có hạn**. Đến ngày hết hạn deploy sẽ chết với lỗi 401.
> Đặt lịch nhắc trước một tuần.

---

## 2. [NGƯỜI] Thông tin phải có trước khi cấu hình

> ⚠️ **File phải sửa là `platform.env.yaml`** — đó là file workflow thật sự đọc
> (`ENV_CONFIG: platform/platform.env.yaml`).
>
> Trong kho có sẵn `platform.env.company.yaml`: đó chỉ là **tờ nháp** dùng để thu thập giá
> trị, workflow **không đọc nó**. Điền vào tờ nháp rồi tưởng xong là sai lầm nguy hiểm —
> hệ thống sẽ chạy bằng giá trị sandbox (sai tên Gateway, sai StorageClass, sai tổ chức) mà
> không báo gì rõ ràng.
>
> Cách làm đúng: chép giá trị đã chốt **đè lên `platform.env.yaml`**, hoặc đơn giản là
> `mv platform.env.company.yaml platform.env.yaml`.

Điền bảng này rồi mới sang phần 3. Giá trị chưa biết thì **để nguyên dạng `todo-*.invalid`**
— chúng cố ý không hợp lệ để nếu quên thì hỏng ngay, chứ không âm thầm deploy vào nhầm chỗ.

| Khoá | Giá trị | Lấy từ đâu |
|---|---|---|
| `git.org` | | tên tổ chức trên GHES |
| `git.config_repo_pattern` | ví dụ `{app}-config` | quy ước của đội |
| `git.committer_name` | | tên tài khoản bot |
| `git.committer_email` | | email tài khoản bot |
| `registry.host` | | quản trị Harbor |
| `registry.path` | `<host>/<project>` | quản trị Harbor |
| `images.postgres` | | đường dẫn ảnh đã mirror ở 1.3 |
| `kubernetes.storage_class` | `rook-ceph-block` | đã xác nhận |
| `kubernetes.namespace_pattern` | mặc định `{app}-{env}` | quy ước công ty |
| `kubernetes.state_namespace` | mặc định `cluster-state` | phải được phép tạo |
| `ingress.gateway_name` | `traefik-gateway` | đã xác nhận |
| `ingress.gateway_namespace` | `traefik` | đã xác nhận |
| `environments.staging.domain` | | đội mạng |
| `environments.prod.domain` | | đội mạng |

> ⚠️ **`namespace_pattern`**: nếu đổi khác mặc định, hai ứng dụng **không được dùng chung
> một namespace** trừ khi tên workload trong `score.yaml` của chúng khác nhau — nền tảng đặt
> tên tài nguyên theo tên workload, trùng tên là đè lên nhau.

---

## 3. Các bước triển khai

### 3.1 [NGƯỜI] Hai chỉnh sửa mức hệ điều hành trên máy chạy CI

Cần `sudo`. **Cả hai đều từng làm hỏng cụm theo kiểu rất khó đoán.**

```bash
# Giới hạn inotify — thiếu thì cụm thứ ba trở đi không khởi động được
echo 'fs.inotify.max_user_instances = 1024' | sudo tee /etc/sysctl.d/99-inotify.conf
sudo sysctl --system

# MTU của Docker phải khớp MTU của máy
ip route get 1.1.1.1 | grep -o 'dev [^ ]*'          # xem card mạng
ip link show <card> | grep -o 'mtu [0-9]*'          # xem MTU
# Nếu MTU máy < 1500, sửa /etc/docker/daemon.json: {"mtu": <giá-trị>} rồi khởi động lại Docker
```

**Vì sao quan trọng:** MTU lệch làm mọi lần kéo ảnh **treo vô hạn** trong khi DNS vẫn hoạt
động — triệu chứng trông y hệt lỗi registry, rất tốn thời gian truy.

**Kiểm:** `docker run --rm alpine ping -c1 -s 1400 <harbor-host>` phải thông.

### 3.2 [AI] Chuẩn bị cụm — làm cho **cả hai** cụm

Nếu cụm công ty đã có sẵn Traefik + Gateway API + Fleet thì **bỏ qua phần lớn**, chỉ kiểm
chứng. Chạy các lệnh kiểm dưới đây trước:

```bash
# Gateway API bản experimental v1.6.1+ (bản standard KHÔNG đủ)
kubectl get crd tlsroutes.gateway.networking.k8s.io -o jsonpath='{.spec.versions[*].name}'
# phải có v1, không chỉ v1alpha2

# Gateway đã sẵn sàng
kubectl -n traefik get gateway traefik-gateway \
  -o jsonpath='{range .status.conditions[*]}{.type}={.status} {end}'
# phải ra: Accepted=True Programmed=True

# Fleet đã cài
kubectl get clusters.fleet.cattle.io -A
# phải thấy một cluster ở trạng thái 1/1

# StorageClass đúng tên
kubectl get storageclass rook-ceph-block
```

**[CHẶN]** Bất kỳ lệnh nào ở trên không ra kết quả mong đợi → dừng, báo người, đừng tự cài
đè lên hạ tầng dùng chung.

Nếu phải cài mới, làm theo `HUONG-DAN-CAI-DAT.md` phần C. Ba bẫy hay gặp nhất:

| Bẫy | Hậu quả |
|---|---|
| Dùng Gateway API bản `standard` | Traefik đứng im ở "chờ controller", không báo lỗi |
| `service.type` thay vì `service.spec.type` | Giá trị bị bỏ qua **trong im lặng** |
| Listener để `port: 80` | Báo `PortUnavailable` — phải là `8000`, cổng bên trong container |

### 3.3 [NGƯỜI] Thông tin đăng nhập Git cho Fleet

Fleet đọc kho config từ **bên trong** cụm nên cần credential riêng.

```bash
kubectl create secret generic git-creds -n fleet-local \
  --type=kubernetes.io/basic-auth \
  --from-literal=username=<tài-khoản-bot> --from-literal=password=<PAT-chỉ-đọc>
```

> ⚠️ **Không dùng token của GitHub App ở đây.** Token App hết hạn sau 1 giờ; Fleet kiểm tra
> 15 giây một lần và không tự làm mới được — sau một giờ nó **ngừng đồng bộ trong im lặng**.
>
> Đúng nhất là **deploy key chỉ đọc** cho mỗi kho config. Fleet chỉ cần đọc, không bao giờ ghi.

### 3.4 [AI] Cấu hình nền tảng

Sửa **một file duy nhất**: `platform.env.yaml` — đúng file workflow đọc. Điền theo bảng ở
phần 2 (xem cảnh báo về tờ nháp `platform.env.company.yaml` ở đó).

Kiểm bằng **chính đường workflow đi**, không phải bằng đường khác:

```bash
python3 orchestrate.py --env-config platform.env.yaml config --export
```

**[CHẶN]** Ba điều kiện, thiếu một là dừng:

```bash
# 1. không còn giá trị chưa điền
grep -nE 'todo-|\.invalid|TODO' platform.env.yaml && echo "CHƯA XONG" || echo "ok"

# 2. tên Gateway trong cấu hình khớp Gateway thật trên cụm
kubectl -n "$(python3 orchestrate.py --env-config platform.env.yaml config --get ingress.gateway_namespace)" \
  get gateway "$(python3 orchestrate.py --env-config platform.env.yaml config --get ingress.gateway_name)"

# 3. StorageClass trong cấu hình có thật
kubectl get storageclass "$(python3 orchestrate.py --env-config platform.env.yaml config --get kubernetes.storage_class)"
```

Hai lệnh sau là bắt buộc vì **sai tên Gateway hoặc StorageClass không gây lỗi lúc render** —
chúng chỉ lộ ra rất muộn, dưới dạng tuyến đường không bao giờ gắn được hoặc ổ đĩa `Pending`
vĩnh viễn.

### 3.5 [NGƯỜI] Đăng ký máy chạy CI

Runner đăng ký theo từng repo. Xem `HUONG-DAN-CAI-DAT.md` phần E.

Nhãn runner **không đọc được từ file cấu hình** — GitHub chọn máy trước khi chạy bước đầu
tiên. Khai bằng biến của repo:

```bash
gh variable set RUNNER_LABEL -R <ORG>/idp-platform --body "<nhãn>"
gh api repos/<ORG>/idp-platform/actions/runners --jq '.runners[]|"\(.name) \(.status)"'
# phải thấy: <tên> online
```

PATH của service **bắt buộc** có `score-k8s`, `kubectl`, `git`, `gh`, `python3`, `helm`.

### 3.6 [NGƯỜI] Đặt bí mật

```bash
R=<ORG>/idp-platform
gh secret set BOT_TOKEN     -R $R < <(printf %s '<PAT-fine-grained>')
gh secret set REGISTRY_HOST -R $R --body '<harbor-host>'
gh secret set REGISTRY_USER -R $R --body '<robot-chỉ-kéo>'
gh secret set REGISTRY_PASS -R $R < <(printf %s '<mật-khẩu-robot>')
cat <kubeconfig-staging> | base64 -w0 | gh secret set KUBECONFIG_STAGING -R $R
cat <kubeconfig-prod>    | base64 -w0 | gh secret set KUBECONFIG_PROD    -R $R
```

Kiểm: `gh secret list -R $R` phải có đủ 6.

> Workflow chấp nhận **`BOT_TOKEN`** hoặc **`APP_ID` + `APP_PRIVATE_KEY`**, ưu tiên
> `BOT_TOKEN`. Không có cái nào thì hỏng ngay bước đầu kèm thông báo rõ.

### 3.7 [NGƯỜI] Bảo vệ nhánh trên kho config

**Đây là nơi duy nhất quyết định môi trường nào cần duyệt.** Nền tảng không khai điều đó ở
đâu cả — nó hỏi thẳng GitHub xem nhánh đích có được bảo vệ không rồi làm theo.

| Nhánh | Cấu hình |
|---|---|
| `dev` (staging) | Bảo vệ, nhưng **thêm tài khoản bot vào danh sách bypass** |
| `main` (production) | Bảo vệ, **bắt buộc pull request + người duyệt**, bot **không** bypass |

Thiếu bypass ở `dev` thì mỗi lần deploy staging đều mở một pull request chờ người — nền tảng
vẫn đúng, chỉ là chậm và phiền.

Cũng nên **bảo vệ nhánh `main` của repo platform**: `PLATFORM_DISPATCH_TOKEN` ghi được vào
repo đó, mà orchestrator chạy code từ `main`.

---

## 4. Kiểm chứng bằng một ứng dụng thử

**Không tuyên bố triển khai xong khi chưa qua phần này.**

### 4.1 [AI] Tạo ứng dụng thử

Theo `HUONG-DAN-TAO-APP-MOI.md`. Tóm tắt:

1. Repo app 4 file: `score.yaml`, `Dockerfile`, `platform.lock`, `.github/workflows/ci.yaml`
2. Repo config, **2 nhánh** `dev` và `main`, mỗi nhánh ít nhất 1 commit
3. Secret `PLATFORM_DISPATCH_TOKEN` cho repo app
4. Workflow `verify.yaml` vào repo config (mẫu ở `templates/config-repo-verify.yaml`)
5. **2 `GitRepo` của Fleet**, một cho mỗi cụm

```bash
# staging -> nhánh dev, thư mục staging/
kubectl --kubeconfig <staging> apply -f - <<EOF
apiVersion: fleet.cattle.io/v1alpha1
kind: GitRepo
metadata: { name: <app>-staging, namespace: fleet-local }
spec:
  repo: https://<GHES>/<ORG>/<app>-config
  branch: dev
  paths: [staging]
  clientSecretName: git-creds
  pollingInterval: 15s
EOF
# prod -> nhánh main, paths [prod], tên <app>-prod
```

> ⚠️ **Đặt tên `GitRepo` kèm môi trường.** Nếu một cụm phục vụ cả hai môi trường mà đặt trùng
> tên thì cái sau đè lên cái trước, và môi trường còn lại **lặng lẽ không được đồng bộ**.
>
> ⚠️ **Quên bước này là triệu chứng đánh lừa nhất**: orchestrator xanh, manifest có trong
> kho config, nhưng cụm **không có gì** — vì không ai kéo về.

### 4.2 [AI] Chạy thử staging

```bash
git push origin dev
```

Theo dõi theo thứ tự, **mỗi bước phải xanh mới sang bước sau**:

| Kiểm | Lệnh |
|---|---|
| CI của app | `gh run list -R <ORG>/<app> --branch dev --limit 1` |
| Orchestrator | `gh run list -R <ORG>/idp-platform --limit 1` |
| Manifest đã vào kho config | `git -C <config> show origin/dev:staging/manifests.yaml \| head` |
| Fleet đã đồng bộ | `kubectl get bundle -n fleet-local` → `1/1` |
| Pod chạy | `kubectl -n <app>-staging get pods` |
| **Ảnh có thật trên Harbor** | `docker manifest inspect <ảnh-trong-manifest>` |
| Trang trả lời | `curl -H "Host: <app>.<domain>" http://<gateway>/` |

### 4.3 [AI] Chạy thử production

```bash
git checkout main && git merge dev && git push origin main
```

Khác biệt: nhánh `main` của kho config được bảo vệ nên orchestrator **mở pull request** thay
vì ghi thẳng. Bước kiểm cụm bị bỏ qua ở lần chạy đó — đúng, vì manifest chưa merge.

**[NGƯỜI]** Duyệt và merge pull request đó.

Sau khi merge, workflow `verify.yaml` trong kho config tự gọi ngược lại platform để kiểm cụm.
Kiểm nó đã chạy và đạt:

```bash
gh run list -R <ORG>/idp-platform --limit 1     # phải thấy verify-request, success
```

**[CHẶN]** Nếu `verify-request` báo đỏ → production **không** chạy đúng thứ vừa render.
Đọc log, nó in sẵn danh sách pod và sự kiện.

---

## 5. Bảy lỗi im lặng — tra nhanh

| Triệu chứng | Nguyên nhân thường gặp | Kiểm bằng |
|---|---|---|
| Mọi bước xanh, cụm trống trơn | Chưa tạo `GitRepo` của Fleet | `kubectl get gitrepo -A` |
| Pod `ImagePullBackOff` | Nhãn ảnh trong manifest chưa được đẩy lên Harbor | `docker manifest inspect <ảnh>` |
| `PersistentVolumeClaim` `Pending` mãi | Sai tên StorageClass | `kubectl get sc` |
| Ứng dụng chạy nhưng không vào được | Sai tên/namespace Gateway, hoặc `allowedRoutes` không cho | `kubectl get httproute -A -o wide` |
| Traefik "chờ controller" | Gateway API bản `standard` thay vì `experimental` | `kubectl get crd tlsroutes... -o jsonpath='{.spec.versions[*].name}'` |
| Fleet ngừng đồng bộ sau 1 giờ | `git-creds` dùng token GitHub App (hết hạn 1 giờ) | `kubectl -n fleet-local get gitrepo -o wide` |
| Kéo ảnh treo vô hạn, DNS vẫn chạy | MTU của Docker lệch MTU máy | `docker run --rm alpine ping -c1 -s 1400 <harbor>` |
| Deploy đột nhiên 401 | PAT fine-grained hết hạn | xem ngày hết hạn của token |
| Một môi trường không được đồng bộ | Hai `GitRepo` trùng tên trên cùng cụm | `kubectl get gitrepo -A` |

---

## 6. Sau khi chạy được

Những việc **cố ý gác lại**, cần xử lý trước khi mở rộng cho nhiều đội:

| Việc | Vì sao |
|---|---|
| **Kiểm tra quyền sở hữu ứng dụng** | Tên app và repo do bên gọi **tự khai**, không xác thực. Ai gọi được vào platform đều triển khai thay ứng dụng của đội khác được. Vô hại khi một đội dùng, nguy hiểm khi nhiều đội |
| Fleet dùng deploy key chỉ đọc | Hiện dùng credential có quyền ghi |
| `kubeconfig` giới hạn quyền | Nên là ServiceAccount chỉ đủ quyền trên namespace của app + `cluster-state`, không phải admin cụm |
| Tự động hoá đăng ký ứng dụng | 5 bước ở 4.1 hiện làm tay |
| Nhiều máy chạy CI | Một runner là nút thắt — đo thực tế: sáu ứng dụng cùng lúc mất ~15 phút |

---

## 7. Báo cáo lại những gì

Sau khi chạy xong, gửi lại cho người phụ trách:

1. **Kết quả từng lệnh kiểm chứng** ở phần 3 và 4 — nguyên văn, không tóm tắt
2. **Bước nào phải làm khác kế hoạch** và vì sao
3. **Giá trị đã điền** vào `platform.env.company.yaml`, che phần bí mật
4. **Bước nào bị chặn**, chặn ở đâu, cần xin ai

Nếu bất kỳ bước **[CHẶN]** nào không qua: **dừng lại và báo**, đừng tìm cách đi vòng. Hệ
thống này hỏng im lặng — đi vòng một hàng rào an toàn thường tạo ra một lỗi chỉ lộ ra nhiều
ngày sau.

---

## Phụ lục — tài liệu chi tiết

| File | Dùng khi |
|---|---|
| `HUONG-DAN-CAI-DAT.md` | Cần lệnh cụ thể cho phần chuẩn bị cụm và runner |
| `HUONG-DAN-TAO-APP-MOI.md` | Đăng ký một ứng dụng vào nền tảng |
| `TAI-LIEU-DU-AN.md` | Hiểu vì sao hệ thống thiết kế như vậy; lịch sử các sự cố đã gặp |
| `CAN-GI-DE-CHAY-THU.md` | Danh sách thông tin cần thu thập |
| `platform.env.company.yaml` | **Tờ nháp** thu thập giá trị — workflow KHÔNG đọc file này |
| `tools/mint-app-token.sh` | Chỉ dùng nếu chuyển sang GitHub App thay vì `BOT_TOKEN` |
