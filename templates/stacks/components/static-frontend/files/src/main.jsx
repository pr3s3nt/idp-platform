import React, { useEffect, useState } from "react";
import { createRoot } from "react-dom/client";
import { banner, SHARED_VERSION } from "@__APP__/shared";

// ĐƯỜNG DẪN TƯƠNG ĐỐI, KHÔNG PHẢI URL TUYỆT ĐỐI. Đây là điểm mấu chốt của golden path:
//
//   fetch("/api/items")            <- đúng
//   fetch("https://api.abc/items") <- sai
//
// Frontend và API dùng CHUNG một origin (`/` -> frontend, `/api` -> API), nên trình duyệt
// không coi đây là cross-origin và không có preflight nào cần xử lý. Đổi sang URL tuyệt đối
// là tự tạo ra bài toán CORS, rồi phải bơm địa chỉ API vào bundle lúc chạy — thứ không làm
// được với file JavaScript đã build. Xem mục 10.2 của kế hoạch.
const API = "/api";

function App() {
  const [items, setItems] = useState(null);
  const [error, setError] = useState(null);
  const [label, setLabel] = useState("");

  async function load() {
    try {
      const res = await fetch(`${API}/items`);
      if (!res.ok) throw new Error(`API trả về ${res.status}`);
      const body = await res.json();
      setItems(body.items);
      setError(null);
    } catch (err) {
      setError(String(err.message || err));
    }
  }

  useEffect(() => {
    load();
  }, []);

  async function add(event) {
    event.preventDefault();
    if (!label.trim()) return;
    await fetch(`${API}/items`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ label }),
    });
    setLabel("");
    load();
  }

  return (
    <main style={{ fontFamily: "system-ui, sans-serif", maxWidth: 640, margin: "3rem auto" }}>
      <h1>__APP__</h1>
      <p style={{ color: "#666" }}>{banner("frontend")}</p>

      <form onSubmit={add} style={{ display: "flex", gap: 8, margin: "1.5rem 0" }}>
        <input
          value={label}
          onChange={(e) => setLabel(e.target.value)}
          placeholder="Thêm một item"
          style={{ flex: 1, padding: 8 }}
        />
        <button type="submit" style={{ padding: "8px 16px" }}>Thêm</button>
      </form>

      {error && <p style={{ color: "#b00" }}>Lỗi: {error}</p>}
      {items === null && !error && <p>Đang tải…</p>}
      {items && (
        <ul>
          {items.map((item) => (
            <li key={item.id}>
              {item.label} <small style={{ color: "#999" }}>#{item.id}</small>
            </li>
          ))}
        </ul>
      )}

      <hr style={{ margin: "2rem 0" }} />
      <small style={{ color: "#999" }}>
        shared v{SHARED_VERSION} — frontend và API build từ cùng một bản gói dùng chung.
      </small>
    </main>
  );
}

createRoot(document.getElementById("root")).render(<App />);
