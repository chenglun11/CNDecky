'use strict';
const $ = (selector) => document.querySelector(selector);
const state = { plugins: [], tag: '', mode: 'install', origin: '', loading: false, proxy: false };
const escapeHTML = (value) => String(value ?? '').replace(/[&<>"']/g, (c) => ({'&':'&amp;', '<':'&lt;', '>':'&gt;', '"':'&quot;', "'":'&#39;'}[c]));
const bytes = (n) => n < 1024 ? `${n} B` : n < 1024 ** 2 ? `${(n / 1024).toFixed(1)} KB` : n < 1024 ** 3 ? `${(n / 1024 ** 2).toFixed(1)} MB` : `${(n / 1024 ** 3).toFixed(2)} GB`;
const count = (n) => new Intl.NumberFormat('zh-CN', { notation: n >= 10000 ? 'compact' : 'standard', maximumFractionDigits: 1 }).format(n || 0);
let toastTimer;
function toast(message) { $('#toast').textContent = message; $('#toast').classList.add('visible'); clearTimeout(toastTimer); toastTimer = setTimeout(() => $('#toast').classList.remove('visible'), 3000); }
async function request(path) {
  const response = await fetch(path, { signal: AbortSignal.timeout(90000) });
  const text = await response.text();
  let body;
  try { body = JSON.parse(text); }
  catch {
    if (!response.ok) throw new Error(`接口 ${path} 返回 HTTP ${response.status}，请检查 Worker 部署和路由`);
    if (!text.trim()) throw new Error(`接口 ${path} 返回空内容（HTTP ${response.status}），请确认 Worker 后端已部署`);
    throw new Error(`接口 ${path} 未返回有效 JSON（HTTP ${response.status}），请检查部署入口是否只包含静态网页`);
  }
  if (!response.ok) throw new Error(typeof body?.error === 'string' ? body.error : `接口 ${path} 返回 HTTP ${response.status}`);
  return { body, stale: response.headers.get('X-CNDecky-Cache') === 'STALE' };
}
function updateCommand() {
  if (!state.origin) return;
  const flag = { install: '', configure: ' --configure-only', restore: ' --restore-official' }[state.mode];
  // Save before running: curl failures cannot accidentally execute a partial pipe.
  const command = `curl -fSL --proto '=https' --proto-redir '=https' '${state.origin}/install.sh' -o ~/cndecky-install.sh && sh ~/cndecky-install.sh${flag}`;
  $('#command').textContent = command;
  const ready = state.origin.startsWith('https:');
  $('#copy-command').disabled = !ready;
  const launcher = $('#download-launcher');
  launcher.setAttribute('aria-disabled', String(!ready));
  const label = { install: '下载安装器', configure: '下载换源工具', restore: '下载恢复工具' }[state.mode];
  launcher.innerHTML = `${label} <span aria-hidden="true">↓</span>`;
  if (ready) {
    launcher.href = `/launchers/${state.mode}.desktop`;
    launcher.download = `CNDecky-${state.mode}.desktop`;
    launcher.tabIndex = 0;
  } else {
    launcher.removeAttribute('href');
    launcher.removeAttribute('download');
    launcher.tabIndex = -1;
  }
  $('#mode-note').textContent = {
    install: '下载稳定版并切换插件商店。需要 sudo 密码。',
    configure: '保留 Decky 版本，仅切换插件商店。',
    restore: '恢复官方商店，保留已安装的插件和设置。'
  }[state.mode];
  if (!state.origin.startsWith('https:')) $('#mode-note').textContent = '当前为本机预览，部署 HTTPS 域名后即可下载安装器。';
}
async function copy(text) {
  try {
    if (navigator.clipboard && window.isSecureContext) await navigator.clipboard.writeText(text);
    else { const input = document.createElement('textarea'); input.value = text; document.body.append(input); input.select(); if (!document.execCommand('copy')) throw new Error(); input.remove(); }
    toast('已复制，到 Steam Deck 的 Konsole 中粘贴即可');
  } catch { toast('浏览器未允许复制，请手动选中命令复制'); }
}
async function status() {
  try {
    const { body } = await request('/api/status');
    state.origin = body.origin;
    $('#connection').className = 'connection online'; $('#connection').innerHTML = '<i></i>服务在线';
    state.proxy = body.mode === 'proxy';
    $('#delivery-mode').textContent = state.proxy ? '按需转发 / 无需直连官方源' : '按需缓存 / 本地存储';
    $('#storage-label').textContent = state.proxy ? '下载方式' : '本地缓存';
    $('#delivery-label').textContent = state.proxy ? '文件保存' : '缓存命中';
    $('#limit-label').textContent = state.proxy ? '安装校验' : '存储额度';
    $('#limit-note').textContent = state.proxy ? '安装器与 Decky 校验下载内容' : '空间不足时自动回收旧文件';
    $('#recent-eyebrow').textContent = state.proxy ? '03 / ON DEMAND' : '03 / LOCAL STORAGE';
    $('#recent-title').textContent = state.proxy ? '下载即用' : '最近缓存';
    $('#storage-badge').textContent = state.proxy ? '转发' : 'SQLite';
    $('#coverage-note').textContent = 'Decky 稳定版安装包、官方插件商店列表、插件 ZIP 和封面图。' + (state.proxy ? '由本站连接官方源，文件直接转发给掌机，不保存下载记录。' : '首次请求由服务器下载，之后复用本地缓存。封面首次加载可能稍慢。');
    if (state.proxy) {
      $('#cache-bytes').textContent = '流式转发'; $('#cache-files').textContent = '本站代连官方源';
      $('#cache-ratio').textContent = '不落盘'; $('#cache-hits').textContent = '每次下载实时获取';
      $('#cache-limit').textContent = 'SHA-256';
      $('#recent').innerHTML = '<p class="muted">打开掌机上的 Decky 商店，照常安装和更新插件。安装包经本站直接传回，不需要等待同步。</p><p class="muted">此部署不保存文件或下载历史。更新 Decky 本体时，重新运行上方安装器。</p>';
    } else {
      $('#cache-bytes').textContent = bytes(body.cache.bytes); $('#cache-limit').textContent = bytes(body.cache.limit_bytes);
      $('#cache-files').textContent = `${body.cache.files} 个文件 · 本地持久化`;
      const total = body.cache.hits + body.cache.misses;
      $('#cache-ratio').textContent = total ? `${Math.round(body.cache.hits / total * 100)}%` : '—';
      $('#cache-hits').textContent = `${count(body.cache.hits)} 次命中 / ${count(total)} 次缓存查询`;
      $('#recent').innerHTML = body.recent.length ? body.recent.slice(0, 5).map(item => {
        const name = decodeURIComponent(new URL(item.url).pathname.split('/').pop());
        const type = name.endsWith('.zip') ? 'ZIP' : name.startsWith('PluginLoader') ? 'BIN' : /\.(png|jpg|webp|jpeg)$/i.test(name) ? 'IMG' : 'API';
        return `<div class="recent-row"><span class="file-icon">${type}</span><div class="recent-file"><strong title="${escapeHTML(name)}">${escapeHTML(name)}</strong><small>${new Date(item.accessed_at * 1000).toLocaleString('zh-CN', { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' })}</small></div><span>${bytes(item.bytes)}</span></div>`;
      }).join('') : '<p class="muted">还没有缓存记录。浏览图片、下载插件时会自动建立缓存。</p>';
    }
    $('#store-link').href = body.store_url;
    if (state.plugins.length) render();
    updateCommand();
  } catch (error) { $('#connection').className = 'connection offline'; $('#connection').innerHTML = '<i></i>连接失败'; toast(error.message); }
}
function artwork(plugin) {
  const initial = escapeHTML(plugin.name.slice(0, 1));
  return `<span class="artwork">${plugin.image_url ? `<img src="${escapeHTML(plugin.image_url)}" loading="lazy" alt="">` : initial}</span>`;
}
function imageFallbacks(root) {
  root.querySelectorAll('img').forEach(img => img.addEventListener('error', () => { img.parentNode.textContent = 'D'; }, { once: true }));
}
function render() {
  const search = $('#search').value.trim().toLocaleLowerCase();
  const filtered = state.plugins.filter(p => (!state.tag || p.tags.includes(state.tag)) && `${p.name} ${p.author} ${p.description} ${p.tags.join(' ')}`.toLocaleLowerCase().includes(search));
  const sort = $('#sort').value;
  filtered.sort(sort === 'name' ? (a, b) => a.name.localeCompare(b.name) : sort === 'date' ? (a, b) => (b.updated || '').localeCompare(a.updated || '') : (a, b) => (b.downloads || 0) - (a.downloads || 0));
  $('#results-count').textContent = `${filtered.length}`;
  $('#empty').hidden = filtered.length > 0 || state.loading;
  $('#plugins').innerHTML = filtered.map(p => {
    const version = p.versions[0];
    return `<button class="plugin-card" data-id="${p.id}" aria-label="查看 ${escapeHTML(p.name)} 的版本和下载"><div class="card-top">${artwork(p)}<div class="card-name"><h3>${escapeHTML(p.name)}</h3><p>by ${escapeHTML(p.author)}</p></div><span class="card-arrow">↗</span></div><p class="description">${escapeHTML(p.description)}</p><div class="card-tags">${p.tags.slice(0, 3).map(t => `<span>${escapeHTML(t)}</span>`).join('')}</div><div class="card-bottom"><span>↓ ${count(p.downloads)}</span><span>·</span><span>${escapeHTML(version?.name || '—')}</span><span class="cache-state ${version?.cached ? 'cached' : ''}"><i></i>${state.proxy ? '直接转发' : version?.cached ? '已缓存' : '按需下载'}</span></div></button>`;
  }).join('');
  imageFallbacks($('#plugins'));
  $('#plugins').querySelectorAll('button').forEach(button => button.addEventListener('click', () => detail(state.plugins.find(p => p.id === Number(button.dataset.id)))));
}
function detail(plugin) {
  $('#dialog-content').innerHTML = `<div class="dialog-title">${artwork(plugin)}<div><h2>${escapeHTML(plugin.name)}</h2><p>by ${escapeHTML(plugin.author)}</p></div></div><p class="dialog-description">${escapeHTML(plugin.description)}</p><p class="dialog-note">日常使用请直接在掌机的 Decky 商店安装。${state.proxy ? '下载 ZIP 将通过本站直接转发官方原始安装包' : '下载 ZIP 会将官方原始安装包缓存到服务器'}；部分插件的额外依赖仍需要直连。</p><div>${plugin.versions.map(v => `<div class="version-row"><div><strong>${escapeHTML(v.name)}</strong><small>${escapeHTML((v.created || '').slice(0, 10))} · ${state.proxy ? '经本站转发' : v.cached ? '已缓存' : '首次下载时缓存'}</small><small title="${escapeHTML(v.hash)}">SHA256 ${escapeHTML(v.hash.slice(0, 16))}…</small></div><a href="${escapeHTML(v.artifact)}" download="${escapeHTML(plugin.name)}-${escapeHTML(v.name)}.zip">下载 ZIP ↓</a></div>`).join('')}</div>`;
  imageFallbacks($('#dialog-content')); $('#plugin-dialog').showModal();
}
async function loadPlugins() {
  state.loading = true; $('#empty').hidden = true; $('#store-message').hidden = false; $('#store-message').className = 'message'; $('#store-message').textContent = '正在连接官方商店，加载插件…';
  try {
    const { body, stale } = await request('/plugins');
    state.plugins = body; $('#plugin-count').textContent = body.length;
    const tags = new Map(); body.forEach(p => p.tags.forEach(t => tags.set(t, (tags.get(t) || 0) + 1)));
    const top = [...tags].sort((a, b) => b[1] - a[1]).slice(0, 8);
    $('#filters').innerHTML = '<button class="filter" data-tag="">全部插件</button>' + top.map(([tag]) => `<button class="filter" data-tag="${escapeHTML(tag)}">${escapeHTML(tag)}</button>`).join('');
    $('#filters').querySelectorAll('button').forEach(button => { button.classList.toggle('active', button.dataset.tag === state.tag); button.addEventListener('click', () => { state.tag = button.dataset.tag; $('#filters').querySelectorAll('button').forEach(b => b.classList.toggle('active', b === button)); render(); }); });
    $('#store-message').hidden = !stale;
    if (stale) $('#store-message').textContent = '上游暂时不可用，当前展示最近 24 小时内的缓存列表。';
    state.loading = false; render();
  } catch (error) {
    state.loading = false; $('#store-message').className = 'message error'; $('#store-message').textContent = `${error.message}。`;
    const retry = document.createElement('button'); retry.textContent = '重新加载'; retry.addEventListener('click', loadPlugins); $('#store-message').append(retry);
  }
}
async function loadRelease() { try { const { body, stale } = await request('/api/release'); $('#release').textContent = `${body.version} STABLE${stale ? ' · 缓存' : ''}`; } catch { $('#release').textContent = '版本暂不可用'; } }
$('.tabs').querySelectorAll('button').forEach(button => button.addEventListener('click', () => { state.mode = button.dataset.mode; $('.tabs').querySelectorAll('button').forEach(b => b.setAttribute('aria-selected', String(b === button))); updateCommand(); }));
$('#download-launcher').addEventListener('click', event => {
  if (event.currentTarget.getAttribute('aria-disabled') === 'true') { event.preventDefault(); return; }
  toast('下载后双击 .desktop 文件，按系统提示允许执行');
});
$('#copy-command').addEventListener('click', () => copy($('#command').textContent));
$('#search').addEventListener('input', render); $('#sort').addEventListener('change', render);
$('#reset-filters').addEventListener('click', () => { state.tag = ''; $('#search').value = ''; $('#filters').querySelectorAll('button').forEach(b => b.classList.toggle('active', b.dataset.tag === '')); render(); });
$('.dialog-close').addEventListener('click', () => $('#plugin-dialog').close());
$('#plugin-dialog').addEventListener('click', e => { if (e.target === $('#plugin-dialog')) { const box = e.target.getBoundingClientRect(); if (e.clientX < box.left || e.clientX > box.right || e.clientY < box.top || e.clientY > box.bottom) e.target.close(); } });
document.addEventListener('keydown', event => { if (event.key === '/' && !['INPUT', 'TEXTAREA'].includes(document.activeElement.tagName) && !$('#plugin-dialog').open) { event.preventDefault(); $('#search').focus(); } });
$('#refresh').addEventListener('click', async () => { $('#refresh').disabled = true; await Promise.allSettled([status(), loadPlugins(), loadRelease()]); $('#refresh').disabled = false; });
Promise.allSettled([status(), loadPlugins(), loadRelease()]);
setInterval(() => { if (!document.hidden) status(); }, 30000);
