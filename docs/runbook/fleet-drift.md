# 5. Fleet drift / không reconcile

## Triệu chứng

- `GitRepo` đứng ở `0/1`, bundle `Modified` hoặc `NotReady`.
- Manifest nằm đúng trong repo cấu hình nhưng cụm không đổi gì.

## Xác nhận

```bash
kubectl -n fleet-local get gitrepo
kubectl -n fleet-local get gitrepo <tên> -o jsonpath='{.status.conditions}' ; echo
kubectl get bundle -A | grep -v '1/1'
```

## 5A. Bundle `Modified` **vĩnh viễn** trong khi cụm hoàn toàn đúng

Đây là dạng drift giả nguy hiểm nhất: **một bundle luôn đỏ là một bundle không ai còn
đọc**, nên lần drift THẬT tiếp theo sẽ không ai thấy.

Nguyên nhân đã đo được: **quantity của Kubernetes là CHUỖI.** Manifest ghi

```yaml
resources: {requests: {cpu: 1}}      # SỐ
```

thì API server nhận và lưu lại thành `"1"`. Desired (số) và live (chuỗi) khác nhau mãi
mãi. Triệu chứng thật đã gặp:

```text
modified {"spec":{"resources":{"requests":{"cpu":1}}}}
```

trong khi 3 pod app, 3 instance Postgres và HTTP đều đúng. Staging không bao giờ lộ ra vì
`250m` không thể là số — **chỉ prod mới lộ**.

### Xử lý

Nháy kép mọi quantity trong catalog, render lại, merge. Kiểm nhanh cả cụm:

```bash
kubectl get bundle -A -o json | python3 -c "
import json,sys
for b in json.load(sys.stdin)['items']:
    for r in (b.get('status') or {}).get('resources') or []:
        if r.get('state') == 'Modified':
            print(b['metadata']['name'], r.get('kind'), r.get('name'), r.get('message'))"
```

## 5B. Fleet không kéo được repo

| Nguyên nhân | Nhận ra bằng |
|---|---|
| Sai `fleet_git_secret` | status của GitRepo có lỗi clone; **không ai nhìn**, và triệu chứng y hệt "quên tạo GitRepo" |
| Repo private, chưa có credential | `authentication required` |
| Sai `fleet_namespace` | GitRepo tạo ra ở namespace Fleet không quản → im lặng tuyệt đối |
| Sai nhánh | GitRepo `Ready` nhưng nội dung là của nhánh khác |

Mặc định nên dùng: để trống `kubernetes.fleet_git_secret` — platform học theo các GitRepo
đang chạy cùng namespace. Khai cứng một tên chỉ đúng khi bạn CHẮC secret đó tồn tại.

## 5C. Fleet dựng lại thứ bạn vừa xoá

Nếu bạn xoá tài nguyên bằng tay trong khi `GitRepo` còn trỏ vào nó, Fleet sẽ dựng lại —
hai bên đánh nhau và Fleet thắng. Xoá `GitRepo` **trước**, tài nguyên sau. Lệnh `offboard`
của platform đã làm đúng thứ tự này.

## Xác minh đã xong

```bash
kubectl -n fleet-local get gitrepo        # 1/1
kubectl get bundle -A | grep -v '1/1'     # không còn dòng nào
```

## Nếu vẫn hỏng

- Ép đọc lại: `kubectl -n fleet-local annotate gitrepo <tên> fleet.cattle.io/redeploy="$(date +%s)" --overwrite`.
- Bundle `NotReady` mà tài nguyên đều đúng: thường là CRD chưa có trên cụm lúc apply —
  apply lại sau khi operator đã cài xong.
