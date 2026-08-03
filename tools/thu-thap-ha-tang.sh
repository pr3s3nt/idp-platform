#!/usr/bin/env bash
# Thu thập hiện trạng hạ tầng để viết lại kế hoạch triển khai theo THỰC TẾ.
#
# CHỈ ĐỌC. Không tạo, không sửa, không xoá bất cứ thứ gì. Có thể chạy an toàn trên
# cụm production.
#
# KHÔNG in ra bí mật nào: không token, không mật khẩu, không nội dung Secret — chỉ in
# TÊN của Secret. Vẫn nên liếc qua kết quả trước khi dán đi.
#
# Dùng:
#   ./thu-thap-ha-tang.sh                      # cụm đang trỏ tới trong kubeconfig
#   KUBECONFIG=~/.kube/staging.conf ./thu-thap-ha-tang.sh > staging.txt
#
# Chạy cho TỪNG cụm (staging và production), gửi cả hai file.

TEAM_NS="${TEAM_NS:-}"   # tuỳ chọn: tiền tố namespace của đội, để lọc bớt

h()  { printf '\n===== %s =====\n' "$*"; }
# try: chạy lệnh, cắt bớt output cho gọn. KHÔNG bao giờ nối `try ... | grep`:
# dấu | nằm ngoài lời gọi hàm nên head cắt output THÔ trước khi grep, và kết quả
# lọc bị thiếu một cách âm thầm. Đã dính thật khi thử: danh sách CRD bị cắt ở 40
# dòng đầu nên mất tlsroutes, suýt kết luận nhầm là cụm không có Gateway API
# bản experimental. Cần lọc thì dùng tryf.
try()  { "$@" 2>&1 | head -40 || true; }
tryf() { bash -c "$1" 2>&1 | head -40 || true; }

echo "############ BÁO CÁO HẠ TẦNG ############"
echo "thời điểm: $(date -Is)"

# ------------------------------------------------------------------ CỤM
h "1. Cụm"
try kubectl version -o yaml
try kubectl config current-context
echo "--- số node và phiên bản ---"
try kubectl get nodes -o custom-columns=NAME:.metadata.name,VERSION:.status.nodeInfo.kubeletVersion,OS:.status.nodeInfo.osImage

h "2. StorageClass  (nền tảng cần đúng MỘT tên, sai là ổ đĩa Pending vĩnh viễn)"
try kubectl get storageclass -o custom-columns='NAME:.metadata.name,PROVISIONER:.provisioner,DEFAULT:.metadata.annotations.storageclass\.kubernetes\.io/is-default-class,BINDING:.volumeBindingMode'

h "3. Gateway API  (BẢN experimental hay standard — quyết định Traefik có chạy được không)"
echo "--- các CRD gateway đang có ---"
tryf 'kubectl get crd -o name | grep gateway.networking.k8s.io'
echo "--- TLSRoute có version v1 không (v1 = experimental, chỉ v1alpha2 = standard) ---"
try kubectl get crd tlsroutes.gateway.networking.k8s.io -o jsonpath='{.spec.versions[*].name}'
echo
echo "--- GatewayClass ---"
try kubectl get gatewayclass -o custom-columns=NAME:.metadata.name,CONTROLLER:.spec.controllerName

h "4. Gateway và quyền gắn route  (allowedRoutes SAI = route không attach, KHÔNG báo lỗi)"
try kubectl get gateway -A -o custom-columns=NS:.metadata.namespace,NAME:.metadata.name,CLASS:.spec.gatewayClassName
echo "--- chi tiết listener của từng Gateway ---"
for g in $(kubectl get gateway -A -o jsonpath='{range .items[*]}{.metadata.namespace}/{.metadata.name} {end}' 2>/dev/null); do
  ns="${g%%/*}"; name="${g##*/}"
  echo "  [$g]"
  kubectl -n "$ns" get gateway "$name" -o jsonpath='{range .spec.listeners[*]}    listener={.name} port={.port} protocol={.protocol} allowedRoutes={.allowedRoutes.namespaces}{"\n"}{end}' 2>/dev/null
  kubectl -n "$ns" get gateway "$name" -o jsonpath='    trạng thái: {range .status.conditions[*]}{.type}={.status} {end}{"\n"}' 2>/dev/null
done

h "5. Ingress controller đang chạy"
try kubectl get pods -A -l app.kubernetes.io/name=traefik -o custom-columns=NS:.metadata.namespace,NAME:.metadata.name,STATUS:.status.phase
try kubectl get ingressclass

h "6. Fleet  (cụm KÉO cấu hình về — không có Fleet thì không có gì áp lên cụm)"
echo "--- CRD ---"
try kubectl get crd gitrepos.fleet.cattle.io -o jsonpath='{.metadata.name}{"\n"}'
echo "--- namespace nào chứa GitRepo ---"
try kubectl get gitrepo -A -o custom-columns=NS:.metadata.namespace,NAME:.metadata.name,REPO:.spec.repo,BRANCH:.spec.branch
echo "--- cụm đã đăng ký với Fleet ---"
try kubectl get clusters.fleet.cattle.io -A
echo "--- Rancher có không ---"
try kubectl get ns cattle-system -o name

h "7. QUYỀN CỦA BẠN  (quyết định nền tảng tự làm được tới đâu)"
for q in \
  "create namespace" \
  "create secret -n default" \
  "create persistentvolumeclaim -n default" \
  "create deployment -n default" \
  "create gitrepo.fleet.cattle.io -A" \
  "create httproute.gateway.networking.k8s.io -n default" \
  "patch gateway.gateway.networking.k8s.io -n traefik" \
  "get secret -A" \
  "create clusterrolebinding" ; do
  printf '  %-52s %s\n' "$q" "$(kubectl auth can-i $q 2>/dev/null || echo '?')"
done

h "8. Namespace hiện có"
if [ -n "$TEAM_NS" ]; then
  tryf "kubectl get ns -o name | grep $TEAM_NS"
else
  echo "(đặt TEAM_NS=<tiền-tố> để lọc; dưới đây là tổng số)"
  echo "  tổng số namespace: $(kubectl get ns --no-headers 2>/dev/null | wc -l)"
  try kubectl get ns --no-headers -o custom-columns=NAME:.metadata.name
fi

h "9. Cụm có ra được internet không  (quyết định có phải mirror ảnh không)"
echo "Chạy tay nếu muốn chắc — lệnh này TẠO một pod tạm, nên tôi không tự chạy:"
echo '  kubectl run net-test --rm -it --restart=Never --image=<harbor>/<project>/alpine -- wget -qO- -T5 https://registry-1.docker.io/v2/ ; echo $?'
echo "Hoặc trả lời thẳng: cụm có proxy ra internet không?"

# ------------------------------------------------------------------ GITHUB
h "10. GitHub Enterprise Server"
try gh api /meta --jq '{installed_version, packages, pages}'
echo "--- tài khoản đang dùng ---"
try gh api /user --jq '.login'
echo "--- các tổ chức ---"
try gh api /user/orgs --jq '.[].login'
echo "--- action nào có sẵn trên GHES (bộ đi kèm) ---"
for a in actions/checkout actions/create-github-app-token actions/setup-python actions/cache; do
  printf '  %-38s %s\n' "$a" "$(gh api "repos/$a" --jq '.full_name' 2>/dev/null || echo 'KHÔNG CÓ')"
done
echo "--- runner cấp tổ chức ---"
echo "  (chạy: gh api /orgs/<ORG>/actions/runners --jq '.runners[]|\"\\(.name) \\(.status) \\(.labels[].name)\"' )"

h "11. Registry"
echo "Trả lời thẳng giúp tôi, không cần chạy lệnh:"
echo "  - Harbor host:"
echo "  - Project dùng cho IDP:"
echo "  - Đã có robot account chưa (đẩy / kéo):"
echo "  - Ảnh postgres:17-alpine đã mirror chưa:"

echo
echo "############ HẾT ############"
