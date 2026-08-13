# 7. Rollback nâng cấp stack

## Ba thứ có phiên bản, và chúng độc lập

| Thứ | Ghim ở đâu | Rollback bằng |
|---|---|---|
| **Catalog** (hình dạng manifest) | `platform.lock` | trỏ lại commit catalog cũ |
| **Stack** (khuôn kho ứng dụng) | `.idp/stack.yaml` trong kho app | `stack-upgrade` ngược lại / revert commit |
| **Ảnh ứng dụng** | tag trong manifest | `promote --mode tag-only` |

Nhầm ba thứ này với nhau là nguồn gốc của phần lớn rollback sai chỗ. Hỏi trước: **cái gì
đổi?** Nếu manifest đổi mà kho app không đổi → catalog. Nếu chỉ ảnh đổi → tag.

## 7A. Rollback một lần deploy (thường gặp nhất)

```bash
python3 idpctl --env-config platform.env.yaml promote \
  --app <app> --image <image> --tag <tag-cũ-đã-chạy-tốt> --mode tag-only \
  --config-dir <checkout kho cấu hình>
```

`tag-only` **bị chặn** nếu khối values của môi trường đó đã đổi kể từ lần render trước —
đó là guard, không phải lỗi. Khi đó dùng `--mode re-render` để cấu hình và ảnh đi cùng
nhau.

Guard thứ hai: `guard_ordering` không cho deploy một commit **cũ hơn** commit đang chạy
đè lên nó. Rollback có chủ ý thì đó chính là điều bạn muốn làm — dùng đúng cờ mà thông
điệp lỗi chỉ ra, đừng vô hiệu hoá guard.

## 7B. Rollback một lần nâng cấp stack

`stack-upgrade` **không tự ghi**; nó in diff cho `managedFiles`. Lý do: nó so kho ứng dụng
với phiên bản stack **hiện tại**, không so hai phiên bản stack với nhau — nên nó không
phân biệt được "stack đổi" và "đội ứng dụng sửa tay".

Rollback vì thế là việc của Git trong **kho ứng dụng**:

```bash
git -C <kho-app> revert <commit nâng cấp stack>
git -C <kho-app> push
```

rồi để CI build lại và orchestrator render lại. Đừng sửa manifest trong kho cấu hình bằng
tay — lần render sau sẽ ghi đè.

## 7C. Rollback catalog

```bash
# trong kho platform
git checkout <commit catalog cũ> -- provisioners/ patches/ templates/
python3 -m pytest test_engine.py -q          # BẮT BUỘC
```

Sau đó render lại từng app. Kiểm bằng đối chứng byte, đúng cách các phase trước làm:

```bash
# render cùng một app bằng catalog cũ và catalog mới, dùng CHUNG state file, từ BẢN SAO
# thư mục app (đừng render thẳng vào examples/ — renderer ghi đè tag vào score.yaml)
cmp <(...) <(...)
```

**Cảnh báo riêng cho provisioner:** nếu rollback chạm tới provisioner có `class`, chạy full
suite **ít nhất 2–3 lần**. score-k8s chọn provisioner nạp sau cùng, và lỗi loại này biểu
hiện là test fail **ngẫu nhiên ở chỗ khác**.

## 7D. Rollback một tính năng của platform

Mọi tính năng mới đều sau cờ. Rollback rẻ nhất là tắt cờ:

```yaml
features:
  application_values: false
  vault_secrets: false
  postgres_application: false
  stack_onboarding: false
```

App đã opt-in sẽ **fail rõ ràng** ("features.X is off") chứ không deploy thiếu biến — có
chủ ý. App chưa opt-in không thấy gì thay đổi.

## Thứ KHÔNG rollback được bằng cờ

- **Database đã đổi từ class cũ sang `class: application`.** Xem
  [`../chuyen-doi-postgres-sang-class-application.md`](../chuyen-doi-postgres-sang-class-application.md).
  Quay lại đòi một lần migrate ngược, không phải một cờ.
- **Mật khẩu đã xoay vòng.** Giá trị cũ không lấy lại được từ Vault kv-v2 nếu đã destroy;
  nếu mới chỉ soft-delete thì `vault kv undelete` được.
- **Dữ liệu đã ghi.** Rollback ảnh không rollback schema. Migration phải tương thích ngược
  một phiên bản — đó là yêu cầu với đội ứng dụng, không phải thứ platform ép được.

## Xác minh đã xong

```bash
python3 idpctl --env-config platform.env.yaml verify --app <app> --env <env> \
  --manifests <đường dẫn manifests.yaml>
kubectl -n fleet-local get gitrepo      # 1/1
```
