# Cảnh báo — đặc tả kiểm được

Cụm harness **không có** stack giám sát (không Prometheus, không Grafana, không CRD
`monitoring.coreos.com`). Dựng cả một Prometheus chỉ để chứng minh một harness là lãng
phí, và nó cũng không phải thứ công ty sẽ dùng — công ty đã có hệ giám sát riêng.

Nên tài liệu này không phát hành dashboard. Nó phát hành **điều kiện** — mỗi cảnh báo có
một biểu thức chạy được ngay bằng `kubectl` (để kiểm rằng nó đúng, hôm nay) và một dạng
metric để gắn vào hệ giám sát sẵn có.

`tools/kiem-suc-khoe.sh` chạy toàn bộ các kiểm phía cụm bên dưới và thoát khác 0 nếu có
cảnh báo nào đang kêu. Đó là bằng chứng những biểu thức này là thật, không phải văn vẻ.

---

## Ba bài học định hình mọi ngưỡng dưới đây

**1. Đọc `status`, đừng đọc `reason`.** Đo trên harness: một `VaultStaticSecret` đang hỏng
đồng bộ có `status: "False"` nhưng `reason` vẫn là chuỗi `"Synced"`. Cảnh báo nào so sánh
`reason == "Synced"` sẽ báo xanh trong lúc đồng bộ đã chết.

**2. Mọi cảnh báo VSO phải có ngưỡng THỜI GIAN.** VSO tự lành sau sự cố Vault, nhưng có
backoff: đo được **~2,5 phút** từ lúc Vault trở lại tới lúc `SecretSynced=True`. Cảnh báo
tức thời sẽ đánh thức người trực ở mọi lần Vault khởi động lại. Dùng `for: 10m`.

**3. `Ready` không có nghĩa là an toàn.** Một `Cluster` `Ready` với
`ContinuousArchiving=True` vẫn có thể **không phục hồi được gì**. Trường phân biệt là
`firstRecoverabilityPoint`. Cảnh báo phải nhắm vào thứ đo đúng điều ta lo, không phải thứ
dễ lấy nhất.

---

## A. Vault Secrets Operator

### A1. Bí mật không đồng bộ được — `nghiêm trọng`

| | |
|---|---|
| **Điều kiện** | `VaultStaticSecret` có condition `SecretSynced.status != "True"` |
| **Ngưỡng** | liên tục **10 phút** |
| **Vì sao ngưỡng đó** | tự lành sau sự cố Vault mất ~2,5 phút; 10 phút loại hết nhiễu mà vẫn bắt được hỏng thật |
| **Runbook** | [1](runbook/thieu-bi-mat-vault.md) nếu `empty response`; [2](runbook/vault-tu-choi-quyen.md) nếu `permission denied`; [3](runbook/vso-xac-thuc-hong.md) nếu `invalid role`/`connection refused` |

```bash
kubectl get vaultstaticsecret -A -o json | python3 -c "
import json,sys
for i in json.load(sys.stdin)['items']:
    c = next((c for c in (i.get('status') or {}).get('conditions') or []
              if c['type']=='SecretSynced'), None)
    if not c or c['status'] != 'True':
        print(i['metadata']['namespace'], i['metadata']['name'],
              (c or {}).get('message','(chưa có status)')[:90])"
```

Dạng metric (kube-state-metrics + customresource_state):

```promql
max_over_time(idp_vaultstaticsecret_synced{status!="True"}[10m]) == 1
```

### A2. Bí mật lâu không được đồng bộ lại — `cảnh báo`

Bắt trường hợp CR còn sống nhưng đã ngừng làm việc mà chưa kịp chuyển `False`.

| | |
|---|---|
| **Điều kiện** | `now - lastGeneration timestamp > 3 × refreshAfter` |
| **Ngưỡng** | với `refreshAfter: 5m` → **15 phút** |
| **Runbook** | [3](runbook/vso-xac-thuc-hong.md) |

### A3. Pod restart bất thường sau khi làm mới bí mật — `cảnh báo`

Xoay vòng đúng phải cho **đúng một** lần rollout cho mỗi workload. Nhiều hơn nghĩa là
restart loop.

| | |
|---|---|
| **Điều kiện** | số lần `.spec.template.metadata.annotations."vso.secrets.hashicorp.com/restartedAt"` đổi trong 1 giờ |
| **Ngưỡng** | **> 2** với một Deployment |
| **Đối chứng đã đo** | hai workload, mỗi cái đúng 1 lần restart, theo dõi trọn một chu kỳ `refreshAfter=5m` sau đó: không lần nào nữa, `restartCount=0` |
| **Runbook** | [3](runbook/vso-xac-thuc-hong.md) |

```promql
changes(kube_deployment_status_observed_generation{deployment=~".+"}[1h]) > 2
```

---

## B. Database

### B1. Database KHÔNG phục hồi được — `nghiêm trọng`, và là cảnh báo quan trọng nhất trong file này

| | |
|---|---|
| **Điều kiện** | `Cluster` có `spec.backup.barmanObjectStore` nhưng `status.firstRecoverabilityPoint` **rỗng** |
| **Ngưỡng** | liên tục **1 giờ** sau khi Cluster `Ready` |
| **Vì sao** | đây là trạng thái mà `Ready=True`, `ContinuousArchiving=True`, WAL vào kho thật — và `bootstrap.recovery` chết với `no target backup found` |
| **Runbook** | [4B](runbook/database-provisioning-backup-that-bai.md) |

```bash
kubectl get cluster.postgresql.cnpg.io -A -o json | python3 -c "
import json,sys
for c in json.load(sys.stdin)['items']:
    if not ((c['spec'].get('backup') or {}).get('barmanObjectStore')): continue
    if not (c.get('status') or {}).get('firstRecoverabilityPoint'):
        print('KHÔNG PHỤC HỒI ĐƯỢC:', c['metadata']['namespace'], c['metadata']['name'])"
```

### B2. Backup quá cũ — `nghiêm trọng`

| | |
|---|---|
| **Điều kiện** | `now - status.lastSuccessfulBackup` |
| **Ngưỡng** | **> 2 × chu kỳ lịch**; với lịch hằng ngày → **48 giờ** |
| **Runbook** | [4B](runbook/database-provisioning-backup-that-bai.md) |

### B3. Cluster không `Ready` — `nghiêm trọng`

| | |
|---|---|
| **Điều kiện** | condition `Ready.status != "True"` |
| **Ngưỡng** | **15 phút** (prod join replica lâu hơn staging nhiều) |
| **Runbook** | [4A](runbook/database-provisioning-backup-that-bai.md) |

### B4. Xoay vòng credential mới xong một nửa — `nghiêm trọng`

Trạng thái này **không có triệu chứng** cho tới lần restart pod kế tiếp: Secret chứa mật
khẩu mà database từ chối, còn pod cũ vẫn chạy bằng mật khẩu cũ.

| | |
|---|---|
| **Điều kiện** | `Cluster.status.managedRolesStatus.passwordStatus.<role>.resourceVersion` **khác** `resourceVersion` của Secret credential |
| **Ngưỡng** | **15 phút** |
| **Đã đo** | lệch kéo dài **> 8 phút** và không tự hết; chạm vào Cluster thì hết trong < 20 giây |
| **Xử lý** | chạy `idpctl rotate-db-credential`, lệnh này làm đúng thứ tự và chờ từng bước |

```bash
kubectl get cluster.postgresql.cnpg.io -A -o json | python3 -c "
import json,subprocess,sys
for c in json.load(sys.stdin)['items']:
    ns=c['metadata']['namespace']
    st=((c.get('status') or {}).get('managedRolesStatus') or {}).get('passwordStatus') or {}
    for role in (((c['spec'].get('managed') or {}).get('roles')) or []):
        sec=(role.get('passwordSecret') or {}).get('name')
        if not sec: continue
        rv=subprocess.run(['kubectl','get','secret',sec,'-n',ns,'-o',
                           'jsonpath={.metadata.resourceVersion}'],
                          capture_output=True,text=True).stdout.strip()
        seen=(st.get(role['name']) or {}).get('resourceVersion')
        if rv and seen and rv!=seen:
            print('XOAY VÒNG DỞ DANG:', ns, c['metadata']['name'], role['name'], seen, '!=', rv)"
```

### B5. Dung lượng lưu trữ — `cảnh báo`

| | |
|---|---|
| **Điều kiện** | `kubelet_volume_stats_used_bytes / kubelet_volume_stats_capacity_bytes` trên PVC của Cluster |
| **Ngưỡng** | **> 80%** trong 30 phút; `nghiêm trọng` ở **> 90%** |

---

## C. Fleet

### C1. Bundle không `Ready` — `cảnh báo`

| | |
|---|---|
| **Điều kiện** | `GitRepo` có `readyClusters < desiredReadyClusters`, hoặc Bundle ở `NotReady`/`Modified` |
| **Ngưỡng** | **20 phút** (một lần deploy bình thường xong nhanh hơn nhiều) |
| **Runbook** | [5](runbook/fleet-drift.md) |

```bash
kubectl get bundle -A -o json | python3 -c "
import json,sys
for b in json.load(sys.stdin)['items']:
    for r in (b.get('status') or {}).get('resources') or []:
        if r.get('state') in ('Modified','NotReady'):
            print(b['metadata']['namespace'], b['metadata']['name'],
                  r.get('kind'), r.get('name'), (r.get('message') or '')[:80])"
```

### C2. Bundle `Modified` kéo dài — `cảnh báo`, ngưỡng dài

Tách khỏi C1 vì nguyên nhân khác hẳn: đây là **drift giả**, thường do quantity ghi bằng
số. Nguy hiểm không phải vì cụm sai, mà vì **một bundle luôn đỏ là một bundle không ai còn
đọc** — lần drift thật tiếp theo sẽ chìm.

| **Ngưỡng** | `Modified` liên tục **24 giờ** |
|---|---|

---

## D. Onboarding — ĐÃ GỠ

> Máy trạng thái onboarding (và `record.state` mà các luật D1–D3 dựa vào) bị gỡ ở commit
> `c5d28ac`. **Không còn cảnh báo onboarding.** "Chờ người" giờ hiện diện dưới dạng tài
> nguyên thật, không phải enum trạng thái, nên nó thuộc các mục khác chứ không phải một
> cảnh báo riêng:
>
> - Thiếu bí mật bên thứ ba → `VaultStaticSecret` `SecretSynced=False` (mục A / runbook
>   `thieu-bi-mat-vault.md`), không phải `WAITING_FOR_USER_SECRETS`.
> - Chờ duyệt prod → pull request đang mở trên kho config prod (`gh pr list`), không phải
>   `PENDING_PROD_APPROVAL`.
> - Deploy hỏng → theo dõi qua audit store (`idpctl audit-report`) và trạng thái workflow,
>   không phải `FAILED_RETRYABLE`.

---

## E. Vault

### E1. Đăng nhập Vault thất bại — `cảnh báo`

| | |
|---|---|
| **Nguồn** | audit device của Vault (`vault audit enable file`) |
| **Điều kiện** | số lượt `auth/<mount>/login` trả lỗi |
| **Ngưỡng** | **> 10 lượt trong 5 phút** từ cùng một role |
| **Runbook** | [3](runbook/vso-xac-thuc-hong.md) |

### E2. Từ chối quyền lặp lại — `cảnh báo`

403 trên một tiền tố **không** thuộc app đang hỏi là dấu hiệu cấu hình sai — hoặc dấu hiệu
đáng xem xét về an ninh.

| **Ngưỡng** | **> 5 lượt trong 10 phút** | **Runbook** | [2](runbook/vault-tu-choi-quyen.md) |
|---|---|---|---|

---

## F. Ứng dụng

### F1. Rollout quá giờ — `cảnh báo`

| | |
|---|---|
| **Điều kiện** | `Deployment.status.updatedReplicas < spec.replicas` hoặc `observedGeneration < metadata.generation` |
| **Ngưỡng** | **15 phút** |
| **Ghi chú** | đo `updatedReplicas`/`observedGeneration`, **không** đo `availableReplicas` — pod cũ vẫn available trong lúc bản mới không lên nổi, nên `availableReplicas` báo xanh cho một lần deploy đã chết |

### F2. Pod restart bất thường — `cảnh báo`

| **Điều kiện** | `increase(kube_pod_container_status_restarts_total[1h])` | **Ngưỡng** | **> 5** | |
|---|---|---|---|---|

---

## Những gì cố ý KHÔNG cảnh báo

- **Vault không tới được, một mình nó.** App đang chạy không hỏng vì việc đó — Secret đã
  đồng bộ nằm lại trong cụm. Cảnh báo đúng là A1/A2 (đồng bộ hỏng kéo dài), vì đó mới là
  thứ dẫn tới hậu quả.
- **`ContinuousArchiving=True`.** Nó không chứng minh điều gì về khả năng phục hồi. Dùng
  B1.
- **Giá trị bí mật hay độ dài của nó.** Không metric nào của nền tảng này được chạm vào
  giá trị bí mật. Mọi kiểm ở trên chỉ đọc tên, điều kiện và `resourceVersion`.
