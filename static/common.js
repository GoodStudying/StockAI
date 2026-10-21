/* 公共 API 与工具函数 */

/* ---------- 主题切换 ---------- */
const THEMES = [
  { id: 'classic', name: '经典', ico: '◐' },
  { id: 'light',   name: '洁净', ico: '○' },
  { id: 'dark',    name: '暗夜', ico: '●' },
];
/* 尽早应用主题，防止刷新闪白（各页面 head 内调用） */
function applyThemeEarly() {
  const t = localStorage.getItem('theme') || 'classic';
  if (t !== 'classic') document.documentElement.setAttribute('data-theme', t);
}
/* 切换主题并持久化 */
function switchTheme(id) {
  localStorage.setItem('theme', id);
  if (id === 'classic') document.documentElement.removeAttribute('data-theme');
  else document.documentElement.setAttribute('data-theme', id);
  document.querySelectorAll('.theme-dot').forEach(d =>
    d.classList.toggle('on', d.dataset.theme === id));
}

/* ---------- 首屏鉴权门（防未登录闪首页） ----------
   各页 head 的内联脚本会先于渲染给 <html> 加 auth-pending 类隐藏整页；
   鉴权通过 → revealPage() 显示；未登录 → api() 跳 login.html，页面从未可见 */
function revealPage() {
  document.documentElement.classList.remove('auth-pending');
}

/* ---------- 导航悬停预取（点击秒开） ---------- */
const _prefetched = new Set();   // 已预取过的地址，避免重复注入 link
function prefetchPage(href) {
  if (_prefetched.has(href)) return;
  _prefetched.add(href);
  const l = document.createElement('link');
  l.rel = 'prefetch'; l.href = href; l.as = 'document';
  document.head.appendChild(l);
}

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

/* 侧栏导航模板（各页共用） */
function renderNav(active) {
  const pages = [
    ['/', '物料管理', '◈'],
    ['/stock.html', '出入库', '⇅'],
    ['/bom.html', 'BOM 分析', '≣'],
    ['/chat.html', 'AI 助手', '✦'],
    ['/purchase.html', '采购清单', '⇫'],
    ['/warehouses.html', '仓库管理', '▤'],
    ['/system.html', '系统管理', '⚙'],
  ];
  const cur = localStorage.getItem('theme') || 'classic';
  return `
  <aside class="nav">
    <div class="brand"><img class="brand-logo" src="/logo-64.png" alt="logo"><span class="brand-txt">个人ERP<small>INVENTORY</small></span></div>
    ${pages.map(([href, label, ico]) =>
      `<a href="${href}" class="${href === active ? 'active' : ''}" onmouseenter="prefetchPage('${href}')"><span class="ico">${ico}</span>${label}</a>`).join('')}
    <span class="spacer"></span>
    <div class="theme-switch">
      ${THEMES.map(t => `<span class="theme-dot${t.id === cur ? ' on' : ''}" data-theme="${t.id}"
        title="${t.name}主题" onclick="switchTheme('${t.id}')">${t.ico}</span>`).join('')}
    </div>
    <div class="nav-user" id="nav-user"></div>
  </aside>`;
}

async function checkLogin() {
  const { user } = await api('/api/me');
  if (!user) { location.href = '/login.html'; return null; }
  const el = document.getElementById('nav-user');
  if (el) el.innerHTML = `${esc(user.username)}<br>
    <span style="opacity:.7">${user.role === 'admin' ? '管理员' : '普通用户'}</span>
    <a href="#" onclick="logout();return false;">退出</a>`;
  // 非管理员隐藏系统管理菜单
  if (user.role !== 'admin') {
    const sys = document.querySelector('aside.nav a[href="/system.html"]');
    if (sys) sys.style.display = 'none';
  }
  revealPage();   // 鉴权通过才显示页面（配合首屏鉴权门，未登录不闪首页）
  return user;
}
async function logout() {
  await fetch('/api/logout', { method: 'POST' });
  location.href = '/login.html';
}
