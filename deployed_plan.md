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

### 1.2 Quyền — ĐÃ KIỂM, không phải xin gì

Đã chạy `kubectl auth can-i` trên cụm thật. **Tất cả đều `yes`:**

| Quyền | Kết quả |
|---|---|
| `create namespace` | yes |
| `create secret` / `pvc` / `deployment` | yes |
| `create gitrepo.fleet.cattle.io` | yes |
| `create httproute` | yes |
| `patch gateway` | yes |
| `create clusterrolebinding` | yes |

Nghĩa là nền tảng tự tạo được namespace, tự tạo `GitRepo`, tự tạo secret trong cụm. **Bỏ
qua mọi bước "nhờ đội khác làm hộ".**

#### [CHẶN] Còn đúng MỘT thứ chưa biết

`allowedRoutes` của Gateway. Có quyền `patch gateway` nên sửa được nếu cần, nhưng phải biết
hiện trạng trước — **đây là thứ hỏng im lặng nhất trong cả hệ thống**: route không attach,
ứng dụng vẫn chạy, pod vẫn khoẻ, chỉ là không ai vào được và **không có lỗi ở đâu cả**.

```bash
kubectl -n traefik get gateway traefik-gateway \
  -o jsonpath='{range .spec.listeners[*]}{.name}: {.allowedRoutes}{"\n"}{end}'
```

| Kết quả | Nghĩa |
|---|---|
| `{"namespaces":{"from":"All"}}` | mở cho mọi namespace — không phải làm gì |
| `{"namespaces":{"from":"Same"}}` | **chỉ** namespace `traefik` gắn được — phải sửa |
| có `selector` | chỉ namespace khớp nhãn — phải gắn nhãn cho namespace của app |

### 1.3 Mạng và registry — đã khảo sát

| Hạng mục | Kết quả | Hệ quả |
|---|---|---|
| Cụm ra internet | **không** | ảnh nào **kubelet** kéo lúc chạy thì phải có trên Harbor |
| Máy chạy CI ra internet | **có** | `docker build` và `pip install` chạy bình thường |
| Harbor | **HTTPS** | không phải khai `insecure-registries` |

**Chỉ còn MỘT việc phải mirror:**

```bash
docker pull postgres:17-alpine
docker tag  postgres:17-alpine <HARBOR>/<PROJECT>/postgres:17-alpine
docker push <HARBOR>/<PROJECT>/postgres:17-alpine
```

Điền vào `images.postgres`. Thiếu → `StatefulSet` của database `ImagePullBackOff` và
`PersistentVolumeClaim` treo `Pending` không rõ lý do.

> **Ảnh nền trong `Dockerfile` KHÔNG cần mirror.** Máy chạy CI có internet nên kéo được
> `nginx:1.27-alpine` lúc build, và ảnh kết quả đẩy lên Harbor đã tự chứa mọi thứ. Kubelet
> chỉ kéo ảnh cuối cùng từ Harbor.

#### Harbor — đã có địa chỉ, còn hai quyết định

`harbor.stg.exampledevops.com`, HTTPS. Tên ảnh sẽ có dạng
`harbor.stg.exampledevops.com/<project>/<app>:<tag>`.

**Đề xuất hai project, không phải một:**

| Project | Chứa gì | Vì sao tách |
|---|---|---|
| `idp` | ảnh do CI của ứng dụng build | robot đẩy ảnh dùng ở đây |
| `base` | ảnh bên thứ ba đã mirror (Postgres…) | vòng đời khác hẳn, và robot đẩy ảnh ứng dụng **không nên** ghi đè được ảnh nền dùng chung |

Gộp một project cũng chạy, chỉ là một robot bị lộ thì kéo theo cả ảnh nền.

#### ⚠️ [CHẶN] Cụm production sẽ dùng Harbor nào

Tên `harbor.**stg**.exampledevops.com` cho thấy đây là Harbor của môi trường staging. Khi cụm
production xong, nhiều khả năng sẽ có một Harbor riêng.

**Đó là vấn đề với cách nền tảng này thăng cấp.** Khi đưa lên production, nó **chép nguyên
tham chiếu ảnh** mà staging đã chạy — đó chính là thứ bảo đảm "production chạy đúng ảnh đã
được kiểm, không phải bản xây lại". Nếu hai môi trường dùng hai Harbor khác tên miền thì
tham chiếu đó **trỏ sai** ở production.

Hiện `registry.path` là giá trị **dùng chung**, không tách theo môi trường được.

Ba đường, nên chọn **trước khi** dựng cụm production:

| Cách | Đánh đổi |
|---|---|
| **Một tên miền Harbor cho cả hai** (ví dụ `harbor.exampledevops.com`) | Đơn giản nhất, giữ nguyên bảo đảm. **Khuyến nghị** |
| Cụm production kéo thẳng từ Harbor staging | Chạy được ngay, nhưng production phụ thuộc hạ tầng staging |
| Hai Harbor + replication | Cần sửa nền tảng để `registry.path` tách theo môi trường |

#### ❓ DNS wildcard

Địa chỉ ứng dụng sinh ra theo mẫu `<tên-workload>.<tên-miền>`, ví dụ
`thanh-toan.stg.exampledevops.com`.

Cần `*.stg.exampledevops.com` trỏ về Gateway Traefik. Nếu **không** có wildcard thì mỗi ứng
dụng mới phải xin một bản ghi DNS riêng — thêm một bước chờ người trong quy trình đăng ký.

```bash
dig +short bat-ky-ten-nao.stg.exampledevops.com
```

Ra IP của Gateway là có wildcard. Không ra gì là chưa có.

> Lưu ý nhỏ: Harbor đang ở `harbor.stg.exampledevops.com`, **cùng tên miền** với ứng dụng.
> Một ứng dụng có workload tên `harbor` sẽ sinh ra đúng tên miền đó và tranh chấp. Ít khả
> năng xảy ra, nhưng nên cấm tên đó.

### 1.3b [NGƯỜI] Máy chạy CI — hai vai trò, một hoặc hai máy

Chưa dựng runner nào. Cần hai vai trò, có thể trên cùng một máy nhưng **nhãn phải khác nhau**:

| Vai trò | Chạy gì | Cần gì |
|---|---|---|
| CI của ứng dụng | build và đẩy ảnh | `docker`, internet, mạng tới Harbor |
| Orchestrator | render manifest, ghi git, kiểm cụm | `kubectl`, `helm`, `score-k8s`, `python3`+`pyyaml`, `gh`, **mạng tới API server của cụm** |

Khai nhãn bằng **biến**, không sửa workflow:

```bash
gh variable set RUNNER_LABEL    -R <ORG>/idp-platform --body "<nhãn-orchestrator>"
gh variable set CI_RUNNER_LABEL --org <ORG>           --body "<nhãn-ci>"
```

> `ubuntu-latest` là runner do GitHub.com cấp — **GHES không có**. CI của ứng dụng đã được
> sửa để đọc `CI_RUNNER_LABEL`; không đặt biến này thì workflow **nằm chờ mãi không ai
> nhận**, và không có lỗi nào rõ ràng.

Đặt `CI_RUNNER_LABEL` ở **cấp tổ chức** để mọi repo ứng dụng tự có, khỏi phải nhớ từng cái.

### 1.4 GHES — đã khảo sát

| Hạng mục | Kết quả | Ảnh hưởng |
|---|---|---|
| Phiên bản | **3.18.12** | Hỗ trợ PAT fine-grained ✅ |
| `actions/checkout` | có | dùng bình thường |
| `actions/create-github-app-token` | **KHÔNG có** | đã xử lý — workflow tự ký JWT, không gọi action |
| Tổ chức | `example-org` | điền vào `git.org` |
| Tài khoản | `testacc` | |

> Việc GHES thiếu `create-github-app-token` **đã được xử lý sẵn**: workflow không còn gọi
> action đó. Nếu dùng `BOT_TOKEN` thì thậm chí không đụng tới phần ký JWT.

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
| `git.org` | `example-org` ✅ | đã khảo sát |
| `git.config_repo_pattern` | ví dụ `{app}-config` | quy ước của đội |
| `git.committer_name` | | tên tài khoản bot |
| `git.committer_email` | | email tài khoản bot |
| `registry.host` | | quản trị Harbor |
| `registry.path` | `<host>/<project>` | quản trị Harbor |
| `images.postgres` | | đường dẫn ảnh đã mirror ở 1.3 |
| `kubernetes.storage_class` | `rook-ceph-block` ✅ | đã khảo sát — mặc định của cụm |
| `kubernetes.namespace_pattern` | mặc định `{app}-{env}` | quy ước công ty |
| `kubernetes.state_namespace` | mặc định `cluster-state` | phải được phép tạo |
| `ingress.gateway_name` | `traefik-gateway` ✅ | đã khảo sát |
| `ingress.gateway_namespace` | `traefik` ✅ | đã khảo sát |
| `environments.staging.domain` | | đội mạng |
| `environments.prod.domain` | | đội mạng |

> ⚠️ **`namespace_pattern`**: nếu đổi khác mặc định, hai ứng dụng **không được dùng chung
> một namespace** trừ khi tên workload trong `score.yaml` của chúng khác nhau — nền tảng đặt
> tên tài nguyên theo tên workload, trùng tên là đè lên nhau.

---

## 3. Các bước triển khai

### 3.1 [NGƯỜI] Máy chạy CI

Chỉ cần một máy Linux có `docker`, `kubectl`, `helm`, `git`, `gh`, `python3` + `pyyaml`,
`score-k8s`; vào được GHES, Harbor và API server của cả hai cụm.

> Hai chỉnh sửa `inotify` và MTU trong `HUONG-DAN-CAI-DAT.md` **chỉ áp dụng khi dựng cụm
> bằng `kind` trên máy cá nhân**. Cụm công ty là 3 node thật nên bỏ qua. Nếu máy chạy CI có
> MTU nhỏ hơn 1500 (VPN, overlay) thì mới cần xem lại MTU của Docker.

### 3.2 [AI] Cụm — CHỈ KIỂM CHỨNG, KHÔNG CÀI GÌ

Đã khảo sát cụm thật. **Mọi thành phần nền tảng cần đều đã có sẵn và đang chạy:**

| Thành phần | Hiện trạng | Việc phải làm |
|---|---|---|
| Kubernetes | v1.35.1, 3 node Ubuntu 26.04 | — |
| StorageClass | `rook-ceph-block` (mặc định, Ceph RBD) | — |
| Gateway API | có bản **experimental** (`TLSRoute` có `v1`) | — |
| GatewayClass | `traefik` | — |
| Gateway | `traefik-gateway` ở ns `traefik`, `Accepted=True Programmed=True` | — |
| Traefik | 3 pod Running | — |
| Fleet | có, **Rancher quản** | — |

> ⛔ **KHÔNG chạy các lệnh cài đặt trong `HUONG-DAN-CAI-DAT.md` phần C.** Hướng dẫn đó viết
> cho cụm trống. Cài đè lên hạ tầng đang phục vụ ứng dụng khác là rủi ro không cần thiết —
> đặc biệt lệnh `helm upgrade --install traefik` sẽ ghi đè cấu hình Traefik đang chạy.

**Ba điểm khác sandbox, ghi lại để không sửa nhầm:**

1. **Gateway nghe cổng 80 và 443**, không phải 8000. Sandbox phải dùng 8000 vì Traefik ở đó
   cấu hình entryPoint khác. Nền tảng **không quan tâm** — `parentRefs` trong provisioner chỉ
   khai tên và namespace của Gateway, không khai cổng. Không phải sửa gì.
2. **`rook-ceph-block` dùng `Immediate` binding**, sandbox dùng `WaitForFirstConsumer`. Với
   ổ đĩa mạng thì không sao. `local-blk` là `no-provisioner`, ổ dính chặt vào một node —
   **tuyệt đối không dùng cho database** trên cụm 3 node vì pod chuyển node là mất dữ liệu.
3. **Gateway có 2 listener** (`web` HTTP, `websecure` HTTPS). `HTTPRoute` không khai
   `sectionName` nên sẽ thử gắn vào cả hai; gắn được vào `web` là đủ để chạy HTTP.

#### [CHẶN] Fleet đã có người dùng khác — đã khảo sát

`GitRepo` nằm ở namespace **`fleet-local`**. Hiện có 5 cái, tất cả đều ở nhánh `main` và
**không khai `paths`**:

| Tên | Nhánh | paths |
|---|---|---|
| `fem` | main | — |
| `idp-sample-nginx` | main | — |
| `okr` | main | — |
| `otm` | main | — |
| `shift-handover` | main | — |

Ba điều rút ra:

1. **`idp-sample-nginx` trùng tên một ứng dụng mẫu của nền tảng này.** Nếu đó là dấu vết của
   một lần thử trước thì phải dọn hoặc đổi tên trước khi chạy lại — tạo trùng tên là **đè
   lên nó**. Nếu là của người khác thì càng phải tránh.
2. **Quy ước hiện tại không khai `paths`**, tức Fleet quét cả kho. Nền tảng này khai `paths`
   (`staging/` hoặc `prod/`) — chặt chẽ hơn, không xung đột, nhưng khác thói quen của cụm.
3. **Chưa ai dùng nhánh `dev`.** Mô hình hai nhánh (`dev` = staging, `main` = production) là
   mới với cụm này.

**Quy ước đặt tên bắt buộc: `<app>-<môi-trường>`.** Ví dụ `thanh-toan-staging`,
`thanh-toan-prod`. Đặt trùng tên một `GitRepo` đang có là làm ứng dụng của người khác
**ngừng đồng bộ trong im lặng**.

Kiểm trước khi tạo, mỗi lần:

```bash
kubectl -n fleet-local get gitrepo <tên-định-đặt> 2>/dev/null \
  && echo "ĐÃ TỒN TẠI — ĐỔI TÊN" || echo "an toàn"
```

#### Hiện chỉ có MỘT cụm — cụm production đang dựng

Cách làm khuyến nghị: **trỏ cả hai môi trường vào cụm staging hiện có**, tách nhau bằng
namespace. Khi cụm production xong thì đổi đúng một secret.

```bash
gh secret set KUBECONFIG_STAGING -R <ORG>/idp-platform   # cụm staging
gh secret set KUBECONFIG_PROD    -R <ORG>/idp-platform   # TẠM THỜI: cũng cụm staging
```

Vì sao nên làm vậy thay vì chờ:

- Diễn tập được **trọn vẹn** luồng lên production — mở pull request, người duyệt, merge, Fleet
  đồng bộ, kiểm cụm — trước khi có cụm thật
- Namespace tách bạch (`<app>-staging` và `<app>-prod`) nên hai môi trường không đụng nhau
- Khi cụm production sẵn sàng: đổi `KUBECONFIG_PROD`, tạo lại `GitRepo` prod trên cụm mới,
  xoá namespace `-prod` ở cụm staging. Không phải sửa code hay cấu hình nào khác

⚠️ **Vì hai môi trường dùng chung một cụm, tên `GitRepo` BẮT BUỘC kèm môi trường.** Đặt trùng
tên thì cái sau đè cái trước và một môi trường **lặng lẽ không được đồng bộ** — đã gặp thật
ở sandbox.

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
| CI của app nằm chờ mãi không chạy | Chưa đặt biến `CI_RUNNER_LABEL`, hoặc nhãn không khớp runner nào | `gh api /orgs/<ORG>/actions/runners` |
| Database `ImagePullBackOff`, PVC `Pending` | Chưa mirror ảnh Postgres vào Harbor — cụm không ra internet | `docker manifest inspect <images.postgres>` |
| CI đẩy được ảnh nhưng pod kéo không được | `registry.path` là địa chỉ chỉ máy chạy CI phân giải được, node thì không | thử `crictl pull` trên node |
| Một môi trường không được đồng bộ | Hai `GitRepo` trùng tên trên cùng cụm | `kubectl get gitrepo -A` |
| **Ứng dụng của đội KHÁC ngừng đồng bộ** | `GitRepo` mới trùng tên với `GitRepo` sẵn có (`app1`, `app2`…) — đè lên nhau | `kubectl get gitrepo -A` trước khi tạo |
| Tạo `GitRepo` xong Fleet vẫn không nhận | Sai namespace — Rancher dùng `fleet-default` hoặc `fleet-local` tuỳ cụm | so với namespace của `GitRepo` đang có |

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
