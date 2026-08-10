// API của __APP__.
//
// BA ĐIỀU KHÔNG ĐƯỢC ĐỔI NẾU KHÔNG HIỂU HẬU QUẢ:
//
// 1. Router mount tại `/api`, không phải `/`. Provisioner route của platform chuyển tiếp
//    nguyên đường dẫn (HTTPRoute PathPrefix trên cụm, `proxy_pass` local) và KHÔNG cắt tiền
//    tố. Mount tại `/` thì `/api/items` tới đây thành 404 — sau khi deploy mới thấy.
//
// 2. Thông tin kết nối database đọc từ biến PG* do Score bơm vào, không đọc file cấu hình,
//    không có giá trị mặc định. `PGPASSWORD` trên cụm là một tham chiếu tới Kubernetes
//    Secret do Vault Secrets Operator đổ đầy; ở local là mật khẩu ngẫu nhiên của
//    score-compose. Cả hai đường đều tới đây dưới dạng biến môi trường, nên code không cần
//    biết mình đang chạy ở đâu.
//
// 3. KHÔNG có CORS header và KHÔNG cần. Frontend được phục vụ CÙNG ORIGIN với API
//    (`/` -> frontend, `/api` -> API), nên trình duyệt không coi đây là cross-origin.
//    Thêm CORS vào đây nghĩa là routing đã sai ở đâu đó — sửa route, đừng sửa header.
import express from "express";
import pg from "pg";
import { banner, formatItem, SHARED_VERSION } from "@__APP__/shared";

const PORT = Number(process.env.PORT || __PORT__);

// pg đọc PGHOST/PGPORT/PGDATABASE/PGUSER/PGPASSWORD từ môi trường, nên không phải chép
// từng biến ra đây — và không có cơ hội chép sót một cái.
const pool = new pg.Pool({
  max: Number(process.env.PGPOOL_MAX || 5),
  connectionTimeoutMillis: 5000,
});

// Schema tối thiểu, tạo idempotent lúc khởi động.
//
// ĐÂY LÀ GIẢI PHÁP TẠM CỦA STACK v1.0.0, CÓ CHỦ Ý VÀ CÓ GIỚI HẠN: chạy migration lúc khởi
// động không an toàn khi có nhiều bản sao (prod chạy 3). Advisory lock bên dưới làm cho hai
// bản sao cùng khởi động không giẫm lên nhau, nhưng nó KHÔNG thay thế được một migration
// Job chạy trước rollout. Hợp đồng Job đó thuộc phase sau của kế hoạch (mục 7.4).
async function ensureSchema() {
  const client = await pool.connect();
  try {
    // Khoá tư vấn cấp phiên: bản sao thứ hai chờ, không chạy song song cùng một DDL.
    await client.query("SELECT pg_advisory_lock(4242)");
    await client.query(`
      CREATE TABLE IF NOT EXISTS items (
        id         SERIAL PRIMARY KEY,
        label      TEXT NOT NULL,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now()
      )
    `);
    const { rows } = await client.query("SELECT count(*)::int AS n FROM items");
    if (rows[0].n === 0) {
      await client.query(
        "INSERT INTO items (label) VALUES ($1), ($2)",
        ["item đầu tiên", "item thứ hai"],
      );
    }
  } finally {
    await client.query("SELECT pg_advisory_unlock(4242)").catch(() => {});
    client.release();
  }
}

const app = express();
app.use(express.json());

const api = express.Router();

// Sống hay chưa — KHÔNG chạm database. Nếu health check phụ thuộc database thì một sự cố
// database ngắn sẽ làm Kubernetes giết sạch pod của app, biến một sự cố thành hai.
api.get("/health", (_req, res) => {
  res.json({
    status: "ok",
    workload: "__WORKLOAD__",
    shared: SHARED_VERSION,
    banner: banner("__WORKLOAD__"),
  });
});

// Sẵn sàng nhận traffic chưa — chỗ NÀY mới chạm database.
api.get("/ready", async (_req, res) => {
  try {
    await pool.query("SELECT 1");
    res.json({ status: "ready" });
  } catch (err) {
    res.status(503).json({ status: "not-ready", error: String(err.message || err) });
  }
});

api.get("/items", async (_req, res) => {
  try {
    const { rows } = await pool.query(
      "SELECT id, label, created_at FROM items ORDER BY id",
    );
    res.json({ items: rows.map(formatItem), shared: SHARED_VERSION });
  } catch (err) {
    res.status(500).json({ error: String(err.message || err) });
  }
});

api.post("/items", async (req, res) => {
  const label = String((req.body || {}).label || "").trim();
  if (!label) {
    return res.status(400).json({ error: "thiếu 'label'" });
  }
  try {
    const { rows } = await pool.query(
      "INSERT INTO items (label) VALUES ($1) RETURNING id, label, created_at",
      [label],
    );
    res.status(201).json(formatItem(rows[0]));
  } catch (err) {
    res.status(500).json({ error: String(err.message || err) });
  }
});

app.use("/api", api);

async function main() {
  // Thử lại: `docker compose up` bật API cùng lúc với Postgres, và trên cụm thì Cluster của
  // CloudNativePG mất vài chục giây mới nhận kết nối. Chết ngay lần đầu là biến một khoảng
  // chờ bình thường thành CrashLoopBackOff.
  for (let attempt = 1; ; attempt++) {
    try {
      await ensureSchema();
      break;
    } catch (err) {
      if (attempt >= 30) throw err;
      console.warn(`database chưa sẵn sàng (lần ${attempt}): ${err.message}`);
      await new Promise((r) => setTimeout(r, 2000));
    }
  }
  app.listen(PORT, "0.0.0.0", () => {
    console.log(`__WORKLOAD__ nghe cổng ${PORT}, ${banner("__WORKLOAD__")}`);
  });
}

main().catch((err) => {
  console.error(err);
  process.exit(1);
});
