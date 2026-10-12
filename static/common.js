/* 共享样式与公共函数 */

/* ---------- 公共 API ---------- */
async function api(url, opts = {}) {
  const r = await fetch(url, opts);
  if (r.status === 401) { location.href = '/login.html'; throw new Error('未登录'); }
  const data = await r.json().catch(() => ({}));
  if (!r.ok) { alert(data.error || '操作失败'); throw new Error(data.error); }
  return data;
}
function esc(s) {
  return String(s ?? '').replace(/[<>&'"]/g,
    c => ({'<':'&lt;','>':'&gt;','&':'&amp;',"'":'&#39;','"':'&quot;'}[c]));
}
function closeModal(id) { document.getElementById(id).style.display = 'none'; }
function openModal(id) { document.getElementById(id).style.display = 'flex'; }

async function checkLogin() {
  const { user } = await api('/api/me');
  if (!user) { location.href = '/login.html'; return null; }
  const el = document.getElementById('nav-user');
  if (el) el.innerHTML = `${esc(user.username)}（${user.role === 'admin' ? '管理员' : '普通用户'}）
    <a href="#" onclick="logout();return false;" style="color:#ffd04b;margin-left:8px">退出</a>`;
  // 非管理员隐藏系统管理菜单
  if (user.role !== 'admin') {
    const sys = document.querySelector('nav a[href="/system.html"]');
    if (sys) sys.style.display = 'none';
  }
  return user;
}
async function logout() {
  await fetch('/api/logout', { method: 'POST' });
  location.href = '/login.html';
}
