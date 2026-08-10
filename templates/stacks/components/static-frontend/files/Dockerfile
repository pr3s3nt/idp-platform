# Ảnh của __WORKLOAD__: build bundle bằng Node, phục vụ bằng nginx.
#
# BUILD CONTEXT LÀ GỐC KHO (xem backend/Dockerfile để biết vì sao): bundle này import gói
# workspace `shared/`, nằm ngoài thư mục frontend/.
#   docker build -f frontend/Dockerfile .
FROM __NODE_IMAGE__ AS build
WORKDIR /app
COPY package.json ./
__WORKSPACE_PKG_COPIES__
# devDependencies CẦN cho bước build (vite nằm trong đó), nên KHÔNG --omit=dev ở đây.
# Ảnh cuối là nginx nên không mang theo node_modules nào cả.
RUN npm install --workspaces --include-workspace-root
COPY shared/ ./shared/
COPY frontend/ ./frontend/
RUN npm run build --workspace @__APP__/frontend

FROM __NGINX_IMAGE__ AS runtime
COPY frontend/nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=build /app/frontend/dist /usr/share/nginx/html
EXPOSE 80
