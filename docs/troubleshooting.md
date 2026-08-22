# Khoanh vùng lỗi

Một lần deploy đi qua nhiều tầng, và **lỗi im lặng là kiểu hỏng đặc trưng của nền tảng
này**: mọi bước báo xanh trong khi cụm không chạy đúng. Tài liệu này giúp trả lời câu hỏi
đầu tiên — **lỗi nằm ở tầng nào** — rồi trỏ tới runbook xử lý. Ngưỡng cảnh báo gắn vào hệ
giám sát nằm ở [docs/canh-bao.md](canh-bao.md); `tools/kiem-suc-khoe.sh` chạy các kiểm phía
cụm và thoát khác 0 nếu có cảnh báo đang kêu.

## Ba nguyên tắc trước khi chẩn đoán bất cứ gì

1. **Đọc `status`, đừng đọc `reason`.** Đo trên harness: một `VaultStaticSecret` đang hỏng
   đồng bộ vẫn có `reason: "Synced"` trong khi `status: "False"`. `reason` là tên loại điều
   kiện, không phải kết quả.
2. **`Ready`/`Available` không có nghĩa là đúng.** `verify` đo `updatedReplicas`/
   `observedGeneration` chứ không `availableReplicas`; một `Cluster` `Ready` vẫn có thể
   không phục hồi được (`firstRecoverabilityPoint` rỗng).
3. **App đang chạy hiếm khi hỏng cùng lúc với nền tảng.** Secret/biến đã nạp nằm lại trong
   pod, nên "Vault sập" thường không có triệu chứng phía người dùng — nó nổ ra ở lần restart
   pod kế tiếp. Đừng chờ app đỏ mới coi là sự cố.

## Bảng khoanh vùng theo tầng

Đi theo thứ tự luồng deploy. Ở mỗi tầng: triệu chứng → lệnh xác nhận → tầng/runbook.

| Tầng | Hỏi | Xác nhận nhanh |
|---|---|---|
| **1. Source / app repo** | `score.yaml`/`values` có hợp lệ? placeholder đặt đúng chỗ? | `render` báo lỗi ngay với message chỉ đích danh (vd `${resources}` sai vị trí, values thiếu key, literal chưa quote). Sửa trong kho app. |
| **2. GitHub / dispatch** | deploy có thực sự chạy đúng commit? | Xem Actions run; nhớ `repository_dispatch` luôn chạy code platform từ nhánh mặc định. Token bot hỏng → bước "Lấy token" đỏ. |
| **3. Render** | manifest sinh ra có đúng không? | Chạy lại `idpctl render --state-file …` local; đối chiếu ảnh trong manifest với ảnh thật trong registry (lệch tên ảnh = §6.14 kinh điển). |
| **4. Vault / VSO** | bí mật có tới cụm không? | `kubectl get vaultstaticsecret -A` — condition `SecretSynced != True`? → [runbook 1](runbook/thieu-bi-mat-vault.md) (thiếu bí mật), [2](runbook/vault-tu-choi-quyen.md) (403), [3](runbook/vso-xac-thuc-hong.md) (auth/role/Vault restart). |
| **5. Database** | Cluster Ready **và** phục hồi được? | `kubectl get cluster.postgresql.cnpg.io -A -o wide`; kiểm `firstRecoverabilityPoint` (rỗng = KHÔNG phục hồi được, dù `Ready`). → [runbook 4](runbook/database-provisioning-backup-that-bai.md). |
| **6. Fleet** | manifest trong git đã lên cụm chưa? | `kubectl -n <fleet_namespace> get gitrepo`; `kubectl get bundle -A \| grep -v 1/1`. Thiếu `GitRepo` = lỗi im lặng số một. → [runbook 5](runbook/fleet-drift.md). |
| **7. Kubernetes** | pod của bản mới có lên không? | `kubectl -n <app>-<env> get pods`; `kubectl rollout status`. `ImagePullBackOff` → tên ảnh/registry secret; `CreateContainerConfigError` → thường là bí mật (tầng 4). |

## Nhận diện nhanh vài lỗi im lặng đặc trưng

- **Mọi bước xanh, cụm trống trơn** → thiếu `GitRepo` của Fleet (tầng 6), hoặc `fleet_namespace`
  sai trong `platform.env.yaml`.
- **HTTPRoute không bao giờ attach** → `ingress.gateway_name`/`gateway_namespace` sai.
- **PVC treo `Pending` mãi** → `kubernetes.storage_class` sai tên.
- **Bundle `Modified` vĩnh viễn dù cụm đúng** → quantity ghi bằng số thay vì chuỗi (chỉ prod
  lộ). → [runbook 5](runbook/fleet-drift.md) mục 5A.
- **Backup báo `ContinuousArchiving=True` nhưng không phục hồi được** → không có base backup;
  chỉ `firstRecoverabilityPoint` mới đáng tin. → [runbook 4](runbook/database-provisioning-backup-that-bai.md) mục 4B.
- **Xoay vòng credential dở dang** → không triệu chứng tới lần restart pod kế tiếp;
  `idpctl rotate-db-credential` làm đúng thứ tự.

## Công cụ chẩn đoán

```bash
python3 idpctl --env-config platform.env.yaml preflight --require-cluster   # công cụ + cụm
python3 idpctl --env-config platform.env.yaml doctor                        # capability khớp config
./tools/kiem-suc-khoe.sh                                                     # chạy các cảnh báo phía cụm
./tools/thu-thap-ha-tang.sh                                                  # probe hạ tầng read-only
```

`preflight --require-vault` thêm kiểm VSO + Vault foundation. Xem thêm
[docs/runbook/](runbook/) cho từng tình huống và [docs/canh-bao.md](canh-bao.md) cho
ngưỡng giám sát.
