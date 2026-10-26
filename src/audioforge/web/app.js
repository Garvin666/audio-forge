/* audio-forge 前端：无第三方依赖，直接跑在浏览器的 ES2020 上。 */
(() => {
  'use strict';

  const $ = (id) => document.getElementById(id);
  const el = {
    drop: $('drop'), picker: $('picker'), files: $('files'),
    target: $('target'), quality: $('quality'), tags: $('tags'),
    go: $('go'), clear: $('clear'), cancel: $('cancel'),
    bar: $('bar'), msg: $('msg'),
    panel: $('panel'), list: $('list'), ttl: $('ttl'),
    status: $('status'), ffnote: $('ffnote'),
  };

  const state = {
    files: [],
    health: null,
    busy: false,
    phase: 'idle',        // idle | uploading | converting
    progress: { done: 0, total: 0 },
    startedAt: 0,
    xhr: null,
    poll: null,
    tick: null,
  };

  // 令牌：优先取 ?token=xxx（取完从地址栏抹掉），回退 sessionStorage。
  // Basic 认证模式下这里为空，浏览器自己带 Authorization，不需要令牌。
  const TOKEN = (() => {
    const q = new URLSearchParams(location.search);
    const fromUrl = q.get('token');
    if (fromUrl) {
      sessionStorage.setItem('af_token', fromUrl);
      q.delete('token');
      const rest = q.toString();
      history.replaceState(null, '', location.pathname + (rest ? '?' + rest : ''));
      return fromUrl;
    }
    return sessionStorage.getItem('af_token') || '';
  })();

  function withAuth(headers) {
    const h = headers || {};
    if (TOKEN) h['Authorization'] = 'Bearer ' + TOKEN;
    return h;
  }

  function downloadUrl(url) {
    if (!TOKEN) return url;
    return url + (url.includes('?') ? '&' : '?') + 'token=' + encodeURIComponent(TOKEN);
  }

  const size = (n) => n >= 1048576
    ? (n / 1048576).toFixed(1) + ' MB'
    : Math.max(1, Math.round(n / 1024)) + ' KB';

  const isNcm = (name) => /\.ncm$/i.test(name);

  const maxUploadMb = () => {
    const lim = state.health && state.health.limits;
    return (lim && lim.max_upload_mb) || 0;
  };

  /* ---------------- 能力探测 ---------------- */

  async function boot() {
    try {
      const r = await fetch('/api/health', { cache: 'no-store', headers: withAuth() });
      if (r.status === 401) {
        el.status.textContent = '需要凭证';
        el.status.className = 'badge badge-warn';
        el.ffnote.textContent = TOKEN
          ? '令牌无效或已更换：请用带 ?token=新令牌 的地址重新打开本页'
          : '该服务已启用令牌认证：请在地址后加 ?token=你的令牌 再打开';
        el.go.disabled = true;
        return;
      }
      const h = await r.json();
      state.health = h;
      applyHealth(h);
    } catch (err) {
      el.status.textContent = '服务未响应';
      el.status.className = 'badge badge-warn';
      el.ffnote.textContent = '无法连接后端：' + err.message;
    }
  }

  function applyHealth(h) {
    const canTranscode = !!(h.capabilities && h.capabilities.transcode);
    const n = (h.formats || []).length;

    el.status.textContent = canTranscode ? `就绪 · ${n} 种目标格式` : '仅 NCM 通道';
    el.status.className = 'badge ' + (canTranscode ? 'badge-ok' : 'badge-warn');

    for (const fmt of h.formats || []) {
      const opt = document.createElement('option');
      opt.value = fmt;
      opt.textContent = fmt.toUpperCase();
      if (!canTranscode) opt.disabled = true;
      el.target.appendChild(opt);
    }
    if (!canTranscode) {
      el.quality.disabled = true;
      el.ffnote.textContent = '未检测到 ffmpeg：只能解密 .ncm（输出其原始 mp3/flac），通用转码已停用';
    } else {
      // ffmpeg -version 的第一行很长，只取版本号
      const m = /version\s+([0-9][\w.\-]*)/.exec(h.ffmpeg || '');
      el.ffnote.textContent = 'ffmpeg ' + (m ? m[1] : '已就绪') + ' · 通用转码可用';
    }
    if (h.limits && h.limits.job_ttl_minutes) el.ttl.textContent = h.limits.job_ttl_minutes;
    if (maxUploadMb()) {
      el.ffnote.textContent += ` · 单次上限 ${maxUploadMb()} MB`;
    }
    el.picker.accept = (h.formats || []).map((f) => '.' + f).join(',') + ',.ncm';
  }

  /* ---------------- 文件列表 ---------------- */

  function addFiles(list) {
    for (const f of list) {
      const dup = state.files.some((x) => x.name === f.name && x.size === f.size);
      if (!dup) state.files.push(f);
    }
    renderFiles();
  }

  function renderFiles() {
    el.files.textContent = '';
    state.files.forEach((f, i) => {
      const li = document.createElement('li');

      const nm = document.createElement('span');
      nm.className = 'nm';
      nm.textContent = f.name;

      const sz = document.createElement('span');
      sz.className = 'sz';
      sz.textContent = size(f.size);

      const rm = document.createElement('button');
      rm.className = 'rm';
      rm.type = 'button';
      rm.title = '移除';
      rm.textContent = '×';
      rm.onclick = () => { state.files.splice(i, 1); renderFiles(); };

      li.append(nm, sz, rm);
      el.files.appendChild(li);
    });
    syncButtons();
  }

  function syncButtons() {
    const n = state.files.length;
    el.go.disabled = state.busy || n === 0;
    el.clear.disabled = state.busy || n === 0;
    el.cancel.hidden = !state.busy;
    el.cancel.disabled = !state.busy;
    el.go.textContent = state.busy ? '处理中…' : (n > 1 ? `开始转换（${n} 个）` : '开始转换');
  }

  /* ---------------- 提交 ---------------- */

  function setBar(ratio) {
    el.bar.hidden = false;
    const v = Math.max(0, Math.min(1, ratio));
    el.bar.firstElementChild.style.width = v * 100 + '%';
    el.bar.setAttribute('aria-valuenow', String(Math.round(v * 100)));
  }

  function say(text, kind) {
    el.msg.hidden = !text;
    el.msg.textContent = text || '';
    el.msg.className = 'msg' + (kind ? ' ' + kind : '');
  }

  function precheck() {
    const target = el.target.value;
    if (target !== 'auto') return null;
    const others = state.files.filter((f) => !isNcm(f.name));
    if (others.length) {
      return `「${others[0].name}」不是 NCM 文件，请先选择目标格式（当前为自动）。`;
    }
    return null;
  }

  function oversize() {
    const mb = maxUploadMb();
    if (!mb) return null;
    const total = state.files.reduce((n, f) => n + f.size, 0);
    if (total <= mb * 1048576) return null;
    return `这批共 ${size(total)}，超过服务端单次上限 ${mb} MB —— 请分批上传（现在拦下，省得传完才 413）。`;
  }

  function newJobId() {
    if (window.crypto && crypto.randomUUID) return crypto.randomUUID().replace(/-/g, '');
    let out = '';
    for (let i = 0; i < 32; i += 1) out += '0123456789abcdef'[Math.floor(Math.random() * 16)];
    return out;
  }

  function stopTimers() {
    if (state.poll) { clearInterval(state.poll); state.poll = null; }
    if (state.tick) { clearInterval(state.tick); state.tick = null; }
  }

  function elapsed() {
    return Math.max(0, Math.round((Date.now() - state.startedAt) / 1000));
  }

  async function pollJob(jobId) {
    if (!state.busy) return;
    try {
      const r = await fetch('/api/job/' + jobId, { cache: 'no-store', headers: withAuth() });
      if (r.status !== 200) return;          // 还在上传 / 尚未解析，忽略
      const j = await r.json();
      if (!j.ok) return;
      state.phase = 'converting';
      state.progress = { done: j.done || 0, total: j.total || 0 };
      const ratio = 0.85 + 0.1 * (state.progress.total ? state.progress.done / state.progress.total : 0);
      setBar(ratio);
      if (state.progress.total) {
        say(`正在转换 ${state.progress.done}/${state.progress.total} · ${elapsed()}s`);
      }
    } catch (_) {
      /* 轮询抖动不影响主流程 */
    }
  }

  function startTimers(jobId) {
    state.startedAt = Date.now();
    state.poll = setInterval(() => pollJob(jobId), 1000);
    state.tick = setInterval(() => {
      if (!state.busy) return;
      if (state.phase === 'uploading') {
        say(`正在上传… ${elapsed()}s`);
      } else if (state.phase === 'converting') {
        const p = state.progress;
        say(p.total ? `正在转换 ${p.done}/${p.total} · ${elapsed()}s` : `正在转换… ${elapsed()}s`);
      }
    }, 1000);
  }

  function submit() {
    if (state.busy || state.files.length === 0) return;
    const bad = precheck() || oversize();
    if (bad) { say(bad, 'err'); return; }

    const sent = state.files.slice();
    const jobId = newJobId();
    const fd = new FormData();
    sent.forEach((f) => fd.append('files', f, f.name));
    fd.append('target', el.target.value);
    fd.append('quality', el.quality.value);
    if (el.tags && el.tags.checked) fd.append('tags', '1');
    fd.append('job', jobId);

    state.busy = true;
    state.phase = 'uploading';
    state.progress = { done: 0, total: sent.length };
    syncButtons();
    say('正在上传…');
    setBar(0);

    const xhr = new XMLHttpRequest();
    state.xhr = xhr;
    xhr.open('POST', '/api/convert');
    if (TOKEN) xhr.setRequestHeader('Authorization', 'Bearer ' + TOKEN);
    xhr.upload.onprogress = (e) => {
      if (e.lengthComputable) setBar((e.loaded / e.total) * 0.8);
    };
    xhr.upload.onload = () => {
      state.phase = 'converting';
      say('上传完成，正在转换…');
      setBar(0.85);
    };
    xhr.onerror = () => finish('网络错误：后端可能已停止运行。', 'err');
    xhr.onabort = () => finish('已取消。服务端可能仍在处理这一批，结果不会自动下载。', 'err');
    xhr.onload = () => {
      let data = null;
      try { data = JSON.parse(xhr.responseText); } catch (_) { /* 保留 null */ }
      const detail = data && data.error ? data.error : `HTTP ${xhr.status}`;
      const rid = data && data.id ? `（错误号 ${data.id}）` : '';
      if (xhr.status === 401) {
        finish('令牌无效或已过期：请用带 ?token= 的地址重新打开本页。', 'err');
        return;
      }
      if (xhr.status === 429) { finish('请求被拒绝：' + detail, 'err'); return; }
      if (xhr.status !== 200 || !data) {
        finish('转换失败：' + detail + rid, 'err');
        return;
      }
      if (!data.ok) { finish('转换失败：' + (data.error || '未知错误') + rid, 'err'); return; }
      setBar(1);
      const rows = data.results || [];
      renderResults(rows);
      const failed = rows.filter((r) => !r.ok);
      say(`完成 ${rows.length - failed.length} 个${failed.length ? `，失败 ${failed.length} 个` : ''}。`,
          failed.length ? 'err' : '');
      // 失败的放回列表：用户改完参数直接再点一次即可，不用重新挑文件
      state.files = failed.length ? sent.filter((f) => failed.some((r) => r.source === f.name)) : [];
      state.busy = false;
      stopTimers();
      renderFiles();
      state.phase = 'idle';
      setTimeout(() => { el.bar.hidden = true; setBar(0); }, 1200);
    };
    startTimers(jobId);
    xhr.send(fd);
  }

  function finish(text, kind) {
    state.busy = false;
    state.phase = 'idle';
    state.xhr = null;
    stopTimers();
    syncButtons();
    say(text, kind);
    el.bar.hidden = true;
    setBar(0);
  }

  /* ---------------- 结果 ---------------- */

  function renderResults(rows) {
    el.list.textContent = '';
    for (const r of rows) {
      const li = document.createElement('li');
      const info = document.createElement('div');
      info.className = 'info';

      const title = document.createElement('b');
      title.textContent = r.ok ? r.name : r.source;
      info.appendChild(title);

      const meta = document.createElement('small');
      if (r.ok) {
        const bits = [size(r.size), r.note || ''].filter(Boolean);
        meta.textContent = `${r.source} → ${bits.join(' · ')}`;
        if (r.warning) {
          const warn = document.createElement('span');
          warn.className = 'warn';
          warn.textContent = ' ⚠ ' + r.warning;
          meta.appendChild(warn);
        }
      } else {
        li.className = 'fail';
        meta.className = 'bad';
        meta.textContent = r.error || '失败';
      }
      info.appendChild(meta);
      li.appendChild(info);

      if (r.ok && r.url) {
        const a = document.createElement('a');
        a.className = 'dl';
        a.href = downloadUrl(r.url);
        a.setAttribute('download', r.name || '');
        a.textContent = '下载';
        li.appendChild(a);
      }
      el.list.appendChild(li);
    }
    el.panel.hidden = rows.length === 0;
    if (rows.length) el.panel.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
  }

  /* ---------------- 事件 ---------------- */

  el.drop.addEventListener('click', () => el.picker.click());
  el.drop.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); el.picker.click(); }
  });
  el.picker.addEventListener('change', () => {
    addFiles(el.picker.files);
    el.picker.value = '';
  });

  for (const ev of ['dragenter', 'dragover']) {
    el.drop.addEventListener(ev, (e) => { e.preventDefault(); el.drop.classList.add('over'); });
  }
  for (const ev of ['dragleave', 'drop']) {
    el.drop.addEventListener(ev, (e) => { e.preventDefault(); el.drop.classList.remove('over'); });
  }
  el.drop.addEventListener('drop', (e) => {
    if (e.dataTransfer && e.dataTransfer.files) addFiles(e.dataTransfer.files);
  });

  el.go.addEventListener('click', submit);
  el.cancel.addEventListener('click', () => {
    if (state.xhr) state.xhr.abort();
  });
  el.clear.addEventListener('click', () => {
    state.files = [];
    renderFiles();
    el.list.textContent = '';
    el.panel.hidden = true;
    say('');
  });

  window.addEventListener('dragover', (e) => e.preventDefault());
  window.addEventListener('drop', (e) => e.preventDefault());

  boot();
})();
