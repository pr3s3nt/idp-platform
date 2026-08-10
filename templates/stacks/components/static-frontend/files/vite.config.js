import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// `base: "/"` là bắt buộc và khớp với `path: /` trong score.yaml. Đổi route của frontend
// sang một tiền tố khác mà quên đổi chỗ này thì bundle đi xin asset ở sai đường dẫn và
// trang trắng — không có lỗi nào trong log server.
export default defineConfig({
  base: "/",
  plugins: [react()],
  build: {
    outDir: "dist",
    emptyOutDir: true,
  },
  server: {
    // CHỈ dùng cho `npm run dev` chạy trần trên máy, KHÔNG dùng trong `make dev`.
    // `make dev` dựng nginx same-origin thật qua score-compose, nên nó kiểm chứng đúng
    // cách routing sẽ hoạt động trên cụm; proxy của Vite thì không.
    proxy: {
      "/api": "http://localhost:8080",
    },
  },
});
