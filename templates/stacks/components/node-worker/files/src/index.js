// Worker nền của __APP__.
//
// Không mở cổng, không nhận HTTP. Nó đọc cùng database với API bằng cùng contract biến PG*,
// nên credential đi đúng một đường: Vault -> VSO -> Secret -> biến môi trường.
import pg from "pg";
import { banner } from "@__APP__/shared";

const INTERVAL_MS = Number(process.env.WORKER_INTERVAL_MS || 30000);

const pool = new pg.Pool({
  max: Number(process.env.PGPOOL_MAX || 2),
  connectionTimeoutMillis: 5000,
});

let stopping = false;

// Dừng êm: Kubernetes gửi SIGTERM rồi mới SIGKILL. Bỏ qua SIGTERM nghĩa là mỗi lần rollout
// là một lần cắt ngang công việc đang chạy dở.
for (const signal of ["SIGTERM", "SIGINT"]) {
  process.on(signal, () => {
    console.log(`nhận ${signal}, dừng sau vòng lặp hiện tại`);
    stopping = true;
  });
}

async function tick() {
  const { rows } = await pool.query("SELECT count(*)::int AS n FROM items");
  console.log(`[worker] ${rows[0].n} item trong database`);
}

async function main() {
  console.log(banner("worker"));
  while (!stopping) {
    try {
      await tick();
    } catch (err) {
      // Không thoát: database chưa lên hoặc mất kết nối tạm thời là chuyện bình thường,
      // và một worker chết vì việc đó sẽ vào CrashLoopBackOff thay vì tự hội tụ.
      console.warn(`[worker] bỏ qua một vòng: ${err.message}`);
    }
    await new Promise((r) => setTimeout(r, INTERVAL_MS));
  }
  await pool.end();
}

main().catch((err) => {
  console.error(err);
  process.exit(1);
});
