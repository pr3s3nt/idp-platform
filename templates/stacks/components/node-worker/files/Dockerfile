# Ảnh của __WORKLOAD__. BUILD CONTEXT LÀ GỐC KHO — xem backend/Dockerfile để biết vì sao.
#   docker build -f worker/Dockerfile .
FROM __NODE_IMAGE__ AS deps
WORKDIR /app
COPY package.json ./
__WORKSPACE_PKG_COPIES__
RUN npm install --workspaces --include-workspace-root --omit=dev

FROM __NODE_IMAGE__ AS runtime
ENV NODE_ENV=production
WORKDIR /app
COPY --from=deps /app/node_modules ./node_modules
COPY package.json ./
COPY shared/ ./shared/
COPY worker/ ./worker/
USER node
CMD ["node", "worker/src/index.js"]
