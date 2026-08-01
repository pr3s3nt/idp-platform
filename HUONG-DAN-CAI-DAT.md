# Cài đặt IDP Orchestrator từ đầu

> Dựng toàn bộ platform trên một hạ tầng mới. Khoảng 45 phút.
>
> **Đã kiểm chứng**: tài liệu này được dùng để cài lại platform từ đầu trên một cụm mới
> (`idp-platform-v2`). Kết quả: ứng dụng thử chạy, bundle Ready, gọi qua Gateway trả `200`.
> Ba chỗ thiếu phát hiện trong quá trình đó đã được bổ sung vào phần G.
>
> Ví dụ dùng hậu tố `v2` và một cụm kind tên `v2`. Thay bằng tên của bạn.

---

## Cần có trước

| Thứ | Ghi chú |
|---|---|
| Máy Linux có Docker chạy không cần `sudo` | Runner và cụm đều ở đây |
| Tài khoản GitHub (hoặc org) | Chứa repo platform, app, cấu hình |
| Registry chứa ảnh | GHCR, Harbor, hay tương đương |
| Cụm Kubernetes | Có thể là `kind` cho môi trường thử |

Công cụ trên máy: `docker`, `kubectl`, `helm`, `git`, `gh`, `python3` + `pyyaml`, `score-k8s`,
và `kind` nếu dựng cụm thử.

```bash
for t in docker kubectl helm git gh python3 score-k8s kind; do
  printf "%-12s %s\n" "$t" "$(command -v $t || echo THIẾU)"
done
python3 -c "import yaml" && echo "pyyaml OK"
```

---

## Phần A — Hai chỉnh sửa mức hệ điều hành

Bỏ qua nếu cụm Kubernetes nằm ở nơi khác. **Chỉ cần khi chạy `kind` trên cùng máy.**

### A1. Giới hạn inotify

Mặc định 128 mỗi UID. Cụm `kind` thứ ba trở đi sẽ không khởi động được — thành phần quản lý
container không nạp nổi plugin và tiến trình node chết trong vòng lặp, **không có thông báo
rõ ràng**.

```bash
sudo tee /etc/sysctl.d/99-kind-inotify.conf >/dev/null <<'EOF'
fs.inotify.max_user_instances = 1024
fs.inotify.max_user_watches = 1048576
EOF
sudo sysctl -p /etc/sysctl.d/99-kind-inotify.conf
```

### A2. MTU của Docker

**Chỉ cần khi MTU card mạng nhỏ hơn 1500** (thường gặp trên WSL2 hoặc máy sau VPN).

```bash
ip link show | grep -E "^[0-9]+: (eth0|ens)" | grep -o "mtu [0-9]*"
```

Nếu nhỏ hơn 1500, đặt Docker cho khớp — nếu không, **mọi lần tải ảnh từ trong cụm đều treo
rồi hết giờ**. Triệu chứng đánh lừa: tra tên miền vẫn được (gói nhỏ), chỉ bắt tay mã hoá là
treo (gói lớn).

```bash
echo '{"mtu": 1280}' | sudo tee /etc/docker/daemon.json   # đổi 1280 cho khớp
sudo systemctl restart docker
```

---

## Phần B — GitHub App

Danh tính máy để platform ghi vào Git. **Dùng lại App có sẵn nếu đã có** — chỉ cần cài thêm
vào các repo mới.

Chưa có thì làm theo `HUONG-DAN-GITHUB-APP.md`. Tóm tắt: 3 quyền `Contents: rw`,
`Pull requests: rw`, `Metadata: ro`; bỏ tick Webhook.

Cần lấy ra:

```bash
APP_ID=<số>                       # trang cài đặt App
ls -l ~/.idp-app-key.pem          # private key, quyền phải là 600
chmod 600 ~/.idp-app-key.pem      # `mv` giữ nguyên quyền file tải về, umask không ăn
```

---

## Phần C — Chuẩn bị cụm

Làm cho **mỗi cụm**. Bốn thứ, và **ba trong số đó hỏng im lặng nếu sai**.

### C1. Tạo cụm (chỉ khi dùng kind)

```bash
cat > /tmp/kind-v2.yaml <<'EOF'
kind: Cluster
apiVersion: kind.x-k8s.io/v1alpha4
nodes:
- role: control-plane
  extraPortMappings:
  - { containerPort: 30080, hostPort: 17080, protocol: TCP }
  - { containerPort: 30443, hostPort: 17443, protocol: TCP }
EOF
kind create cluster --name v2 --config /tmp/kind-v2.yaml
mkdir -p ~/.kube                      # có thể chưa tồn tại trên máy mới
kind get kubeconfig --name v2 > ~/.kube/v2.conf
```

> Chọn `hostPort` chưa ai dùng. Trùng port là cụm không tạo được.

### C2. StorageClass ⚠️

Tên **phải khớp** `kubernetes.storage_class` trong `platform.env.yaml`. Sai tên thì ổ đĩa
xin mãi không được cấp, đứng ở `Pending` **vô thời hạn, không báo lỗi**.

```bash
kubectl --kubeconfig ~/.kube/v2.conf apply -f - <<'EOF'
apiVersion: storage.k8s.io/v1
kind: StorageClass
metadata:
  name: storage-class          # ĐỔI cho khớp platform.env.yaml
  annotations:
    storageclass.kubernetes.io/is-default-class: "true"
provisioner: rancher.io/local-path      # cụm thật: rook-ceph.rbd.csi.ceph.com,...
reclaimPolicy: Delete
volumeBindingMode: WaitForFirstConsumer
EOF
```

Cụm có sẵn StorageClass rồi thì **đừng tạo mới** — chỉ cần điền đúng tên vào
`platform.env.yaml`.

### C3. Gateway API ⚠️

**Bản `experimental`, phiên bản `v1.6.1` trở lên.** Không phải `standard`.

Traefik cần hai loại tài nguyên chỉ có ở bản experimental, và cần chúng ở phiên bản `v1` —
bản cũ chỉ có `v1alpha2`. Thiếu thì Traefik **đứng im ở trạng thái "chờ controller"**, không
báo lỗi ở đâu.

```bash
kubectl --kubeconfig ~/.kube/v2.conf apply --server-side --force-conflicts \
  -f https://github.com/kubernetes-sigs/gateway-api/releases/download/v1.6.1/experimental-install.yaml
```

### C4. Traefik ⚠️

```bash
helm repo add traefik https://traefik.github.io/charts && helm repo update traefik
helm --kubeconfig ~/.kube/v2.conf upgrade --install traefik traefik/traefik \
  -n traefik --create-namespace \
  --set providers.kubernetesGateway.enabled=true \
  --set gateway.enabled=false \
  --set service.spec.type=NodePort \
  --set ports.web.nodePort=30080 \
  --set ports.websecure.nodePort=30443
```

Ba cái bẫy trong đúng một lệnh này:

| Bẫy | Hậu quả nếu sai |
|---|---|
| `service.spec.type`, **không phải** `service.type` | Bản chart mới đổi tên; giá trị cũ **bị bỏ qua trong im lặng** |
| `gateway.enabled=false` | Chart tự tạo Gateway tên khác, không khớp thứ platform sinh ra |
| Đừng dùng `--wait` trong CI có giới hạn thời gian | Lệnh bị giết giữa chừng để lại release ở trạng thái `failed` dù ứng dụng chạy bình thường |

### C5. Gateway ⚠️

Tên và namespace **phải khớp** `ingress.gateway_name` / `ingress.gateway_namespace`. Sai thì
tuyến đường không bao giờ được gắn vào — **và không có lỗi ở đâu cả**.

```bash
kubectl --kubeconfig ~/.kube/v2.conf apply -f - <<'EOF'
apiVersion: gateway.networking.k8s.io/v1
kind: GatewayClass
metadata: { name: traefik }
spec: { controllerName: traefik.io/gateway-controller }
---
apiVersion: gateway.networking.k8s.io/v1
kind: Gateway
metadata: { name: ingress-gateway, namespace: traefik }   # ĐỔI cho khớp cấu hình
spec:
  gatewayClassName: traefik
  listeners:
  - name: web
    protocol: HTTP
    port: 8000          # cổng entryPoint BÊN TRONG container Traefik, KHÔNG phải cổng Service
    allowedRoutes: { namespaces: { from: All } }
EOF
```

> `port: 8000` là chỗ sai nhiều nhất. Để `80` sẽ báo `PortUnavailable` vì Traefik đối chiếu
> theo cổng entryPoint bên trong, không phải cổng Service.

Kiểm:

```bash
kubectl --kubeconfig ~/.kube/v2.conf get gateway ingress-gateway -n traefik \
  -o jsonpath='{range .status.conditions[*]}{.type}={.status} {end}'
# phải ra: Accepted=True Programmed=True
```

### C6. Fleet

```bash
helm repo add fleet https://rancher.github.io/fleet-helm-charts/ && helm repo update fleet
helm --kubeconfig ~/.kube/v2.conf upgrade --install fleet-crd fleet/fleet-crd \
  -n cattle-fleet-system --create-namespace
helm --kubeconfig ~/.kube/v2.conf upgrade --install fleet fleet/fleet \
  -n cattle-fleet-system --create-namespace
```

Kiểm cụm đã tự đăng ký:

```bash
kubectl --kubeconfig ~/.kube/v2.conf get clusters.fleet.cattle.io -A
# phải thấy fleet-local/local ở trạng thái 1/1
```

### C7. Thông tin đăng nhập Git cho Fleet

Fleet đọc repo cấu hình từ **bên trong** cụm nên cần credential riêng.

> **Không dùng token của GitHub App ở đây.** Token App hết hạn sau 1 giờ, mà Fleet kiểm tra
> 15 giây một lần và không tự làm mới được — sau một giờ nó ngừng đồng bộ, **âm thầm**.
> Dùng deploy key chỉ-đọc, hoặc token dài hạn cho môi trường thử.

```bash
kubectl --kubeconfig ~/.kube/v2.conf create secret generic git-creds -n fleet-local \
  --type=kubernetes.io/basic-auth \
  --from-literal=username=<tên> --from-literal=password=<token>
```

---

## Phần D — Repo platform

```bash
git clone <repo-platform-hiện-có> idp-platform-v2
cd idp-platform-v2 && rm -rf .git && git init -b main
```

Sửa **một file duy nhất**: `platform.env.yaml`.

```yaml
git:
  org: <tài-khoản-hoặc-org>
  config_repo_pattern: "<tiền-tố>{app}-config"
  committer_name: "<tên-app-github>[bot]"
  committer_email: "<id>+<tên-app-github>[bot]@users.noreply.github.com"
registry:
  host: <registry>
  path: <registry>/<project>
kubernetes:
  storage_class: <tên ở C2>
ingress:
  gateway_name: <tên ở C5>
  gateway_namespace: <namespace ở C5>
images:
  postgres: <đường-dẫn-ảnh-postgres>
environments:
  staging: { config_branch: dev,  domain: <tên-miền>, replicas: 1 }
  prod:    { config_branch: main, domain: <tên-miền>, replicas: 3 }
```

> Lấy `<id>` của bot bằng `gh api "users/<tên-app>[bot]" --jq .id`. Đuôi
> `@users.noreply.github.com` ở đây là **đúng** vì nó ánh xạ về chính con bot. Nhưng đừng
> bịa một tên khác — nếu tên đó trùng một người dùng có thật thì **mọi commit triển khai sẽ
> bị ghi công cho người lạ**.

Đẩy lên:

```bash
git add -A && git commit -m "cài đặt platform"
gh repo create <org>/idp-platform-v2 --private --source=. --push
```

---

## Phần E — Máy chủ chạy CI

Runner đăng ký theo từng repo. Mỗi bản cài platform cần một runner riêng.

```bash
mkdir -p ~/actions-runner-v2 && cd ~/actions-runner-v2
V=$(curl -s https://api.github.com/repos/actions/runner/releases/latest | grep -m1 '"tag_name"' | sed 's/.*"v\([^"]*\)".*/\1/')
curl -sL -o r.tar.gz "https://github.com/actions/runner/releases/download/v${V}/actions-runner-linux-x64-${V}.tar.gz"
tar xzf r.tar.gz && rm r.tar.gz
sudo ./bin/installdependencies.sh          # cần root, chỉ một lần cho mỗi máy

TOKEN=$(gh api -X POST /repos/<org>/idp-platform-v2/actions/runners/registration-token --jq .token)
./config.sh --unattended --replace --url https://github.com/<org>/idp-platform-v2 \
  --token "$TOKEN" --name <tên-runner> --labels platform-orchestrator-v2 --work _work

# PATH cho service: bắt buộc có score-k8s, kubectl, git, gh
printf '%s' "$HOME/.local/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin" > .path
sudo ./svc.sh install $(whoami) && sudo ./svc.sh start
```

Nhãn runner **không đọc được từ file cấu hình** — GitHub quyết định chạy trên máy nào trước
khi thực thi bước đầu tiên. Khai bằng biến của repo:

```bash
gh variable set RUNNER_LABEL -R <org>/idp-platform-v2 --body "platform-orchestrator-v2"
```

Kiểm runner đã online:

```bash
gh api repos/<org>/idp-platform-v2/actions/runners --jq '.runners[]|"\(.name) \(.status)"'
```

---

## Phần F — Bí mật

```bash
R=<org>/idp-platform-v2
gh secret set APP_ID          -R $R --body "<app-id>"
gh secret set APP_PRIVATE_KEY -R $R < ~/.idp-app-key.pem
gh secret set REGISTRY_HOST   -R $R --body "<registry>"
gh secret set REGISTRY_USER   -R $R --body "<user>"
gh secret set REGISTRY_PASS   -R $R < <(printf %s "<mật-khẩu>")
kind get kubeconfig --name v2 | base64 -w0 | gh secret set KUBECONFIG_STAGING -R $R
kind get kubeconfig --name v2 | base64 -w0 | gh secret set KUBECONFIG_PROD    -R $R
```

> Hai môi trường có thể dùng chung một cụm — chúng tách nhau bằng namespace. Môi trường thật
> thì nên tách cụm.

Cài GitHub App vào repo platform mới (bỏ qua nếu App đang cài chế độ "tất cả repo").

---

## Phần G — Kiểm chứng bằng một ứng dụng thử

Làm theo `HUONG-DAN-TAO-APP-MOI.md`. Ngắn gọn: repo app 4 file, repo cấu hình 2 nhánh,
2 `GitRepo` của Fleet, một secret cho CI.

Ba chi tiết dễ vấp, phát hiện khi tự cài lại theo chính tài liệu này:

**Đặt tên `GitRepo` bằng đúng tên app**, không thêm hậu tố môi trường. Fleet đặt tên bundle
là `<tên-GitRepo>-<thư-mục>`, nên đặt `smoke-staging` sẽ ra bundle `smoke-staging-staging`.
Không sai chức năng nhưng gây nhầm khi tra cứu.

**Nhánh mặc định của repo app phải là `dev`**, vì CI kích hoạt theo nhánh và `main` là
production:

```bash
gh api -X PATCH repos/<org>/<repo-app> -f default_branch=dev
```

**Sửa `PLATFORM_REPO` trong CI của app** trỏ đúng repo platform của bản cài này. Sao chép CI
từ app cũ mà quên đổi thì nó gọi sang platform cũ, và triển khai chạy trên hạ tầng khác.

Coi là **cài đặt thành công** khi:

| Kiểm | Mong đợi |
|---|---|
| CI của app | xanh |
| Orchestrator | xanh, gồm cả bước kiểm cụm cuối |
| Nhánh `dev` của repo cấu hình | có commit do bot tạo |
| `kubectl get bundle -n fleet-local` | `1/1` |
| `kubectl get pods -n <app>-staging` | `Running` |
| Gọi qua Gateway | `200` |

> Bundle của **production sẽ chưa xuất hiện** sau lần triển khai đầu. Đúng thiết kế: hai môi
> trường ở hai nhánh khác nhau, nên bước khởi tạo production tự bỏ qua. Production lên khi
> có người merge `dev` → `main` ở repo app.

---

## Bốn lỗi im lặng, tra nhanh

| Triệu chứng | Nguyên nhân | Sửa ở |
|---|---|---|
| Orchestrator xanh, cụm trống trơn | Chưa tạo `GitRepo` của Fleet | Phần G |
| Ổ đĩa kẹt `Pending` mãi | Sai tên StorageClass | C2 |
| Có tuyến đường nhưng gọi vào `404` | Sai tên/namespace Gateway | C5 |
| Gateway kẹt "chờ controller" | Sai bản hoặc phiên bản Gateway API | C3 |

Cả bốn **không báo lỗi ở bất kỳ đâu** trong pipeline. Bước kiểm cụm ở cuối mỗi lần triển
khai bắt được cái thứ nhất và thứ hai; hai cái còn lại phải tự kiểm theo bảng ở phần G.
