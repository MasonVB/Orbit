import { Viewer } from '@photo-sphere-viewer/core';
import { EquirectangularVideoAdapter } from '@photo-sphere-viewer/equirectangular-video-adapter';
import { VideoPlugin } from '@photo-sphere-viewer/video-plugin';
import { GyroscopePlugin } from '@photo-sphere-viewer/gyroscope-plugin';

const $ = (id) => document.getElementById(id);
const shareToken = location.pathname.startsWith('/s/') ? location.pathname.slice(3) : null;

const state = { folder: null, items: [], sharePw: '', allowDownload: true, viewer: null, poll: null };

// --------------------------------------------------------------------------
// transport
// --------------------------------------------------------------------------
async function api(path, opts = {}) {
  const headers = opts.headers || {};
  if (shareToken && state.sharePw) headers['X-Share-Password'] = state.sharePw;
  const res = await fetch(path, { ...opts, headers });
  if (!res.ok) {
    let detail = res.statusText;
    try { detail = (await res.json()).detail || detail; } catch {}
    throw Object.assign(new Error(detail), { status: res.status });
  }
  return res.json();
}

const mediaUrl = (id, kind) => shareToken
  ? `/api/s/${shareToken}/items/${id}/media/${kind}${state.sharePw ? `?pw=${encodeURIComponent(state.sharePw)}` : ''}`
  : `/api/items/${id}/media/${kind}`;

function toast(msg, ms = 2600) {
  const t = $('toast'); t.textContent = msg; t.classList.add('on');
  clearTimeout(t._t); t._t = setTimeout(() => t.classList.remove('on'), ms);
}

// --------------------------------------------------------------------------
// formatting
// --------------------------------------------------------------------------
const clock = (s) => {
  if (!s) return '';
  const m = Math.floor(s / 60), r = Math.round(s % 60);
  return `${m}:${String(r).padStart(2, '0')}`;
};
const bytes = (n) => {
  if (!n) return '';
  const u = ['B', 'KB', 'MB', 'GB', 'TB']; let i = 0;
  while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; }
  return `${n.toFixed(n < 10 && i > 0 ? 1 : 0)} ${u[i]}`;
};
const FORMAT_LABEL = {
  insp: 'INSP · dual-fisheye', insv: 'INSV · dual-fisheye',
  gopromax360: '.360 · GoPro EAC',
};
const label = (it) => FORMAT_LABEL[it.source_format] || (it.source_format || '').replace(/^equirect_/, '').toUpperCase() + ' · equirect';

// --------------------------------------------------------------------------
// rendering
// --------------------------------------------------------------------------
function card(it) {
  const el = document.createElement('button');
  el.className = 'card';
  const ready = it.status === 'ready' || it.status === 'preview';
  const stitching = it.status === 'preview';
  const badgeClass = it.status === 'failed' || it.status === 'unsupported' ? 'bad'
    : (stitching || !ready) ? 'warn' : '';
  const badgeText = stitching ? 'camera preview'
    : ready ? (it.kind === 'video' ? 'video' : 'photo') : it.status;
  el.innerHTML = `
    <div class="strip">
      ${ready ? `<img loading="lazy" alt="" src="${mediaUrl(it.id, 'thumb')}">` : ''}
      <span class="badge ${badgeClass}">${badgeText}</span>
      ${it.kind === 'video' && it.duration ? `<span class="dur">${clock(it.duration)}</span>` : ''}
    </div>
    <div class="meta">
      <div class="n"></div>
      <div class="s"></div>
    </div>`;
  el.querySelector('.n').textContent = it.name;
  el.querySelector('.s').textContent = ready
    ? [label(it), it.camera, bytes(it.size_bytes),
       stitching ? 'full stitch in progress' : ''].filter(Boolean).join('  ·  ')
    : (it.error || 'waiting in the queue');
  el.onclick = () => ready ? openViewer(it) : (it.error ? toast(it.error, 6000) : toast('Still processing.'));
  return el;
}

function folderCard(f, onOpen) {
  const el = document.createElement('button');
  el.className = 'folder-card';
  el.innerHTML = `<span class="g">▸</span><span class="n"></span>`;
  el.querySelector('.n').textContent = f.name;
  el.onclick = () => onOpen(f.id);
  return el;
}

function renderSheet(data, { onOpenFolder, readOnly }) {
  const sheet = $('sheet');
  sheet.innerHTML = '';
  state.items = data.items;

  if (data.folders.length) {
    const h = document.createElement('p'); h.className = 'eyebrow';
    h.textContent = `Folders — ${data.folders.length}`;
    const g = document.createElement('div'); g.className = 'grid folder-grid';
    data.folders.forEach((f) => g.appendChild(folderCard(f, onOpenFolder)));
    sheet.append(h, g);
  }

  if (data.items.length) {
    const h = document.createElement('p'); h.className = 'eyebrow';
    h.textContent = `Media — ${data.items.length}`;
    const g = document.createElement('div'); g.className = 'grid';
    data.items.forEach((it) => g.appendChild(card(it)));
    sheet.append(h, g);
  }

  if (!data.folders.length && !data.items.length) {
    const e = document.createElement('div'); e.className = 'empty';
    e.innerHTML = readOnly
      ? `<h3>Nothing here yet</h3><p>This folder is empty.</p>`
      : `<h3>This folder is empty</h3>
         <p>Drop in .insp, .insv, .360 or already-stitched equirectangular files.
            Orbit flattens them to a sphere you can look around.</p>
         <button class="btn key" id="empty-add">Add media</button>`;
    sheet.append(e);
    const b = $('empty-add'); if (b) b.onclick = () => $('picker').click();
  }
}

function renderCrumb(chain, onGo) {
  const c = $('crumb'); c.innerHTML = '';
  chain.forEach((f, i) => {
    if (i) { const s = document.createElement('span'); s.className = 'sep'; s.textContent = '/'; c.appendChild(s); }
    const b = document.createElement('button');
    b.textContent = f.name; b.onclick = () => onGo(f.id);
    c.appendChild(b);
  });
}

// --------------------------------------------------------------------------
// viewer
// --------------------------------------------------------------------------
function destroyViewer() {
  if (state.viewer) { state.viewer.destroy(); state.viewer = null; }
  $('stage').innerHTML = '';
}

async function openViewer(it) {
  const full = shareToken ? it : await api(`/api/items/${it.id}`).catch(() => it);
  const derivs = (full.derivatives || []).map((d) => d.kind);

  $('viewer').classList.add('on');
  $('vtitle').textContent = it.name;
  $('vmeta').textContent = [label(it), it.camera, it.width ? `${it.width}×${it.height} source` : '',
    it.duration ? clock(it.duration) : ''].filter(Boolean).join('   ·   ');
  $('vdownload').classList.toggle('hidden', !state.allowDownload);
  $('vdownload').onclick = () => window.open(mediaUrl(it.id, 'original'), '_blank');
  $('vshare').classList.toggle('hidden', !!shareToken);
  $('vshare').onclick = () => shareDialog('item', it.id, it.name);

  destroyViewer();
  const common = { container: $('stage'), navbar: ['zoom', 'move', 'fullscreen'], plugins: [GyroscopePlugin] };

  if (it.kind === 'video') {
    const ladder = derivs.filter((k) => k.startsWith('video_')).sort().reverse();
    const sel = $('vquality');
    sel.classList.toggle('hidden', ladder.length < 2);
    sel.innerHTML = ladder.map((k) => `<option value="${k}">${k.replace('video_', '')}p</option>`).join('');
    const mount = (kind) => {
      destroyViewer();
      state.viewer = new Viewer({
        ...common,
        adapter: [EquirectangularVideoAdapter, { muted: false, autoplay: false }],
        panorama: { source: mediaUrl(it.id, kind) },
        plugins: [...common.plugins, [VideoPlugin, { progressbar: true, bigbutton: true }]],
      });
    };
    sel.onchange = () => mount(sel.value);
    mount(ladder[0] || 'video_1080');
  } else {
    $('vquality').classList.add('hidden');
    state.viewer = new Viewer({
      ...common,
      panorama: mediaUrl(it.id, derivs.includes('preview') ? 'preview' : 'master'),
    });
    // Swap in the full-resolution sphere once it has loaded in the background.
    if (derivs.includes('master') && derivs.includes('preview')) {
      const hi = new Image();
      hi.onload = () => state.viewer && state.viewer.setPanorama(hi.src, { transition: false, showLoader: false });
      hi.src = mediaUrl(it.id, 'master');
    }
  }
}

$('vclose').onclick = () => { $('viewer').classList.remove('on'); destroyViewer(); };
document.addEventListener('keydown', (e) => {
  if (e.key === 'Escape' && $('viewer').classList.contains('on')) $('vclose').click();
});

// --------------------------------------------------------------------------
// dialogs
// --------------------------------------------------------------------------
function closeDialog() { $('scrim').classList.remove('on'); $('dlg').innerHTML = ''; }
$('scrim').onclick = (e) => { if (e.target === $('scrim')) closeDialog(); };

function dialog(html) {
  $('dlg').innerHTML = html;
  $('scrim').classList.add('on');
  const first = $('dlg').querySelector('input');
  if (first) first.focus();
}

function newFolderDialog() {
  dialog(`<h2>New folder</h2><p class="sub">Inside ${state.folder.name}.</p>
    <div class="field"><label for="fn">Name</label><input id="fn" placeholder="Iceland, March"></div>
    <div class="err" id="fe"></div>
    <div class="row"><button class="btn ghost" id="fc">Cancel</button><button class="btn key" id="fk">Create folder</button></div>`);
  $('fc').onclick = closeDialog;
  $('fk').onclick = async () => {
    const body = new FormData();
    body.append('parent_id', state.folder.id);
    body.append('name', $('fn').value);
    try { await api('/api/folders', { method: 'POST', body }); closeDialog(); loadFolder(state.folder.id); }
    catch (e) { $('fe').textContent = e.message; }
  };
  $('fn').onkeydown = (e) => { if (e.key === 'Enter') $('fk').click(); };
}

function shareDialog(type, id, name) {
  dialog(`<h2>Share ${type}</h2><p class="sub">${name}</p>
    <div class="field"><label for="sp">Password (optional)</label><input id="sp" type="text" placeholder="Leave blank for no password"></div>
    <div class="field"><label for="sx">Expires</label>
      <select id="sx"><option value="0">Never</option><option value="7">In 7 days</option>
      <option value="30">In 30 days</option><option value="90">In 90 days</option></select></div>
    <label class="check"><input type="checkbox" id="sd" checked> Let people download originals</label>
    <div class="err" id="se"></div>
    <div class="row"><button class="btn ghost" id="sc">Cancel</button><button class="btn key" id="sk">Create link</button></div>`);
  $('sc').onclick = closeDialog;
  $('sk').onclick = async () => {
    const body = new FormData();
    body.append('target_type', type); body.append('target_id', id);
    body.append('label', name); body.append('password', $('sp').value);
    body.append('expires_days', $('sx').value);
    body.append('allow_download', $('sd').checked ? 1 : 0);
    try {
      const r = await api('/api/shares', { method: 'POST', body });
      const url = r.url || `${location.origin}/s/${r.token}`;
      dialog(`<h2>Link ready</h2><p class="sub">Anyone with this link can view ${name}.</p>
        <div class="linkout"><input id="lu" readonly value="${url}"><button class="btn key" id="lc">Copy</button></div>
        <p class="note">Revoke it any time from the folder menu. Viewers see the flattened
        360 versions; originals are only reachable if downloads stay on.</p>
        <div class="row"><button class="btn ghost" id="ld">Done</button></div>`);
      $('lc').onclick = () => { $('lu').select(); navigator.clipboard.writeText(url); toast('Link copied'); };
      $('ld').onclick = closeDialog;
    } catch (e) { $('se').textContent = e.message; }
  };
}

// --------------------------------------------------------------------------
// upload
// --------------------------------------------------------------------------
async function uploadFiles(files) {
  let done = 0;
  for (const f of files) {
    toast(`Uploading ${f.name} (${++done}/${files.length})`, 60000);
    const body = new FormData();
    body.append('folder_id', state.folder.id);
    body.append('file', f);
    try { await api('/api/upload', { method: 'POST', body }); }
    catch (e) { toast(`${f.name}: ${e.message}`, 6000); }
  }
  toast(`Queued ${files.length} file${files.length > 1 ? '' : ''} for processing`);
  loadFolder(state.folder.id);
}

$('add').onclick = () => $('picker').click();
$('picker').onchange = (e) => { uploadFiles([...e.target.files]); e.target.value = ''; };
['dragover', 'drop'].forEach((ev) => document.addEventListener(ev, (e) => e.preventDefault()));
document.addEventListener('drop', (e) => {
  if (!shareToken && e.dataTransfer.files.length) uploadFiles([...e.dataTransfer.files]);
});

// --------------------------------------------------------------------------
// library mode
// --------------------------------------------------------------------------
async function loadFolder(id) {
  const data = await api(`/api/folders/${id}`);
  state.folder = data.folder;
  renderCrumb(data.breadcrumb, loadFolder);
  renderSheet(data, { onOpenFolder: loadFolder, readOnly: false });
  buildTree(data.breadcrumb, data.folders);
  const busy = (data.counts.pending || 0) + (data.counts.processing || 0)
             + (data.counts.preview || 0);
  $('queue').textContent = busy ? `${busy} in queue` : 'queue idle';
  clearTimeout(state.poll);
  if (busy) state.poll = setTimeout(() => loadFolder(id), 4000);
}

function buildTree(breadcrumb, children) {
  const tree = $('tree'); tree.innerHTML = '';
  breadcrumb.forEach((f, depth) => {
    const b = document.createElement('button');
    b.className = 'tree-row';
    b.setAttribute('aria-current', String(depth === breadcrumb.length - 1));
    b.innerHTML = `<span class="tw">${'·'.repeat(depth)}▾</span><span></span>`;
    b.lastElementChild.textContent = f.name;
    b.onclick = () => loadFolder(f.id);
    tree.appendChild(b);
  });
  children.forEach((f) => {
    const b = document.createElement('button');
    b.className = 'tree-row';
    b.innerHTML = `<span class="tw">${'·'.repeat(breadcrumb.length)}▸</span><span></span>`;
    b.lastElementChild.textContent = f.name;
    b.onclick = () => loadFolder(f.id);
    tree.appendChild(b);
  });
}

$('new-folder').onclick = newFolderDialog;
$('share-folder').onclick = () => shareDialog('folder', state.folder.id, state.folder.name);
$('signout').onclick = async () => { await api('/api/auth/logout', { method: 'POST' }); location.reload(); };

// --------------------------------------------------------------------------
// share mode
// --------------------------------------------------------------------------
async function loadShare(folderId) {
  const qs = folderId ? `?folder=${folderId}` : '';
  let data;
  try {
    data = await api(`/api/s/${shareToken}${qs}`);
  } catch (e) {
    if (e.status === 403 && !state.sharePw) return askSharePassword();
    $('sheet').innerHTML = `<div class="empty"><h3>Link unavailable</h3><p>${e.message}</p></div>`;
    return;
  }
  state.allowDownload = data.allow_download;
  $('brand-sub').textContent = data.label || 'shared';

  if (data.mode === 'item') {
    $('crumb').textContent = data.item.name;
    renderSheet({ folders: [], items: [data.item] }, { onOpenFolder: () => {}, readOnly: true });
    openViewer(data.item);
    return;
  }
  state.folder = data.folder;
  renderCrumb(data.breadcrumb, loadShare);
  renderSheet(data, { onOpenFolder: loadShare, readOnly: true });
}

function askSharePassword() {
  dialog(`<h2>Password needed</h2><p class="sub">This link is protected.</p>
    <div class="field"><label for="pw">Password</label><input id="pw" type="password"></div>
    <div class="err" id="pe"></div>
    <div class="row"><button class="btn key" id="pk">Open</button></div>`);
  const go = async () => {
    state.sharePw = $('pw').value;
    try { await api(`/api/s/${shareToken}`); closeDialog(); loadShare(); }
    catch { state.sharePw = ''; $('pe').textContent = 'That password did not work.'; }
  };
  $('pk').onclick = go;
  $('pw').onkeydown = (e) => { if (e.key === 'Enter') go(); };
}

// --------------------------------------------------------------------------
// boot
// --------------------------------------------------------------------------
async function boot() {
  if (shareToken) {
    $('app').classList.remove('hidden');
    document.querySelector('.rail-foot').classList.add('hidden');
    ['new-folder', 'share-folder', 'add'].forEach((id) => $(id).classList.add('hidden'));
    return loadShare();
  }
  try {
    const user = await api('/api/auth/me');
    $('who').textContent = user.username;
    $('app').classList.remove('hidden');
    const root = await api('/api/root');
    loadFolder(root.id);
  } catch {
    $('gate').classList.remove('hidden');
  }
}

$('signin').onclick = async () => {
  const body = new FormData();
  body.append('username', $('u').value);
  body.append('password', $('p').value);
  try {
    await api('/api/auth/login', { method: 'POST', body });
    $('gate').classList.add('hidden');
    boot();
  } catch (e) { $('gate-err').textContent = e.message; }
};
$('p').onkeydown = (e) => { if (e.key === 'Enter') $('signin').click(); };

boot();
