export const $ = (s, el = document) => el.querySelector(s);
export const $$ = (s, el = document) => [...el.querySelectorAll(s)];
export async function api(path, opts = {}) {
  const res = await fetch(path, { headers: { 'content-type': 'application/json' }, ...opts,
    body: opts.body ? JSON.stringify(opts.body) : undefined });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || res.status);
  return data;
}
export const esc = (s) => String(s ?? '').replace(/[&<>"]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
export const badge = (s) => `<span class="badge b-${esc(s)}">${esc({
  feasible: '可行', infeasible: '不可行', pending_adjustment: '待调整', locked: '已锁定',
  pass: '满足', fail: '不满足', warn: '注意', unknown: '未知'
}[s] ?? s)}</span>`;
export const NAV = `
  <header>
    <h1>🗼 海岛灯塔导览站</h1>
    <nav>
      <a href="/story.html">故事页</a>
      <a href="/itinerary.html">行程</a>
      <a href="/admin.html">后台</a>
      <a href="/offline.html">离线包</a>
      <a href="/docs/design.md">设计文档</a>
      <a href="/docs/acceptance.md">数据适配验收</a>
    </nav>
  </header>`;
export function toLocal(iso, off = 480) {
  if (!iso) return '—';
  const d = new Date(new Date(iso).getTime() + off * 60000);
  return d.toISOString().slice(0, 16).replace('T', ' ');
}
