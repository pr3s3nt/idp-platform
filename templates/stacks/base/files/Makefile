# Vòng lặp phát triển local của __APP__.
#
# `score.yaml` LÀ NGUỒN TOPOLOGY DUY NHẤT. Không có compose.yaml viết tay trong kho này —
# nó được SINH RA từ chính các file Score sẽ render lên staging. Nhờ vậy "chạy được ở local"
# và "chạy được trên cụm" không thể trôi ra xa nhau: thêm một resource, đổi một cổng, đổi
# một route đều tự động có mặt ở cả hai nơi.
#
# CẦN GÌ TRÊN MÁY: docker (kèm compose) và score-compose. KHÔNG cần checkout kho platform,
# không cần kubectl, không cần Vault — provisioner local đã được vendor vào
# .idp/score-compose/ lúc tạo app, và được cập nhật bằng PR khi nâng phiên bản stack.
SHELL := /bin/bash

APP         := __APP__
SCORE_FILES := __SCORE_FILES__
ROUTER_PORT ?= 8080

# Mỗi file provisioner được vendor vào kho này. Truyền đường dẫn TUYỆT ĐỐI vì score-compose
# coi tham số là URI và đường dẫn tương đối không phải lúc nào cũng được hiểu như ý.
PROVISIONER_FLAGS := $(foreach f,$(wildcard .idp/score-compose/*.provisioners.yaml),--provisioners $(abspath $(f)))

.PHONY: dev up down logs ps generate check-env check clean

## dev: sinh compose từ Score rồi bật toàn bộ stack (lệnh hay dùng nhất)
dev: generate up

.env:
	@cp .env.example .env
	@echo "==> đã tạo .env từ .env.example."
	@echo "    Điền các giá trị còn TRỐNG (bí mật của bên thứ ba) rồi chạy lại 'make dev'."

## check-env: mọi khoá trong .env.example phải có giá trị trong .env
#
# Vì sao phải kiểm: provisioner `environment` của score-compose đọc biến từ môi trường và
# một biến THIẾU trở thành chuỗi RỖNG, không phải một lỗi. Container vẫn khởi động, chỉ là
# với cấu hình sai — đúng kiểu hỏng im lặng khó truy nhất. Thà dừng ở đây.
check-env: .env
	@set -a; . ./.env; set +a; \
	missing=""; \
	while IFS= read -r line; do \
	  case "$$line" in ''|\#*) continue;; esac; \
	  key="$${line%%=*}"; \
	  if [ -z "$${!key}" ]; then missing="$$missing $$key"; fi; \
	done < .env.example; \
	if [ -n "$$missing" ]; then \
	  echo "==> .env còn thiếu giá trị cho:$$missing"; \
	  echo "    Đây thường là bí mật của bên thứ ba (API key…). Trên staging/prod chúng"; \
	  echo "    tới từ Vault; ở local bạn tự điền, và .env đã nằm trong .gitignore."; \
	  exit 1; \
	fi

## generate: Score -> compose.yaml (không bật container nào)
#
# MỘT LỆNH `generate` CHO MỖI FILE SCORE, không phải một lệnh liệt kê tất cả: score-compose
# từ chối cờ `--build` khi nhận nhiều file cùng lúc ("--build cannot be used when multiple
# score files are provided"). Các lần gọi kế tiếp cộng dồn vào cùng một project state, nên
# resource dùng chung giữa các workload — nhất là nginx same-origin — vẫn thấy đủ mọi
# workload. Biến `.env` được nạp một lần cho cả chuỗi lệnh.
#
# Địa chỉ của `--build` là TÊN CONTAINER trong score.yaml, không phải tên workload. Nhầm
# hai cái này thì score-compose để nguyên `image: .` và lỗi chỉ nổ ra ở bước docker build.
generate: check-env
	score-compose init --no-sample --project $(APP) $(PROVISIONER_FLAGS)
	@set -a; . ./.env; set +a; \
__GENERATE_STEPS__

## check: chỉ kiểm Score còn hợp lệ — dùng cho CI của kho ứng dụng
check: generate
	@docker compose config --quiet && echo "==> compose.yaml hợp lệ"

up:
	docker compose up --build -d
	@set -a; . ./.env; set +a; \
	echo "==> mở http://$$PUBLIC_HOST:$(ROUTER_PORT)/"; \
	echo "    API:  http://$$PUBLIC_HOST:$(ROUTER_PORT)/api/health"; \
	echo "    (mọi tên *.localhost đã tự phân giải về 127.0.0.1, không phải sửa /etc/hosts)"

down:
	docker compose down --remove-orphans

logs:
	docker compose logs -f --tail=100

ps:
	docker compose ps

## clean: xoá cả state local (mật khẩu database local, tên database) và volume dữ liệu
clean:
	-docker compose down --remove-orphans --volumes
	rm -rf .score-compose compose.yaml
