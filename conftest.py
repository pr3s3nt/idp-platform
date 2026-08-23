"""Cấu hình dùng chung cho pytest (áp cho test_engine.py và test_audit.py).

Vì sao file này tồn tại: các vòng chờ verify (rollout, VSO sync, base backup…) gọi
`time.sleep` thật giữa hai lần poll. Ở cụm thật đó là điều đúng; trong bộ test "logic
thuần" nó khiến ~7 test verify mỗi cái ngồi chờ 10s — tổng ~70s không làm gì.

`engine.context.poll_interval` đọc biến môi trường IDP_POLL_INTERVAL_SECONDS trước mọi
nguồn khác, nên đặt nó về 0 ở đây làm mọi vòng poll trong test không còn ngủ, mà không
một test riêng lẻ nào phải biết mình chạm vòng lặp nào. Hành vi ở cụm thật KHÔNG đổi:
biến này chỉ được đặt trong tiến trình test.
"""
import os

os.environ.setdefault("IDP_POLL_INTERVAL_SECONDS", "0")
