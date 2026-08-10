// Mã dùng chung giữa frontend và backend của __APP__.
//
// ĐỪNG XOÁ FILE NÀY chỉ vì thấy nó nhỏ. Nó là chỗ duy nhất chứng minh cho quyết định
// `tagStrategy: commit` ở .idp/stack.yaml: sửa một dòng ở đây phải làm CẢ HAI ảnh được
// build lại. Chiến lược `content` băm theo thư mục frontend/ và backend/ nên sẽ bỏ sót
// thay đổi này và deploy hai ảnh cũ — không có lỗi nào được báo, chỉ có hành vi cũ.

/** Nhãn phiên bản của contract dùng chung. Frontend hiển thị, backend trả về. */
export const SHARED_VERSION = "1.0.0";

/** Hình dạng một item mà API trả về và frontend hiển thị — một định nghĩa, hai nơi dùng. */
export function formatItem(item) {
  return {
    id: item.id,
    label: String(item.label ?? "").trim(),
    createdAt: item.created_at ?? item.createdAt ?? null,
  };
}

/** Dùng trong health check để hai bên chứng minh chúng build từ cùng một bản shared. */
export function banner(who) {
  return `${who} dùng @__APP__/shared v${SHARED_VERSION}`;
}
