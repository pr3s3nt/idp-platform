# Ảnh của __WORKLOAD__.
#
# BUILD CONTEXT LÀ GỐC KHO, KHÔNG PHẢI THƯ MỤC NÀY. Bắt buộc: workload này phụ thuộc gói
# workspace `shared/`, nằm ngoài thư mục backend/. Build với context là backend/ thì npm
# không tìm thấy gói đó và lỗi chỉ hiện ra lúc cài dependency.
#   docker build -f backend/Dockerfile .
# CI và `make dev` đều đã truyền đúng context — đừng "sửa cho gọn" thành `-f Dockerfile .`
# trong thư mục backend/.
FROM __NODE_IMAGE__ AS deps
WORKDIR /app
# Chỉ chép manifest trước rồi mới cài: lớp này chỉ mất hiệu lực khi dependency đổi, nên sửa
# mã nguồn không phải cài lại từ đầu.
COPY package.json ./
__WORKSPACE_PKG_COPIES__
RUN npm install --workspaces --include-workspace-root --omit=dev

FROM __NODE_IMAGE__ AS runtime
ENV NODE_ENV=production
WORKDIR /app
COPY --from=deps /app/node_modules ./node_modules
COPY package.json ./
# Chép cả shared/ — npm workspaces để lại một symlink trong node_modules trỏ về đây, nên
# thiếu thư mục này là container chết ngay lúc import với ERR_MODULE_NOT_FOUND.
COPY shared/ ./shared/
COPY backend/ ./backend/
USER node
EXPOSE __PORT__
CMD ["node", "backend/src/index.js"]
