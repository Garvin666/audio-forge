/* audio-forge 前端：无第三方依赖，直接跑在浏览器的 ES2020 上。 */
(() => {
  'use strict';

  const $ = (id) => document.getElementById(id);
  const el = {
    drop: $('drop'), picker: $('picker'), files: $('files'),
    target: $('target'), quality: $('quality'),
    go: $('go'), clear: $('clear'), bar: $('bar'), msg: $('msg'),
    panel: $('panel'), list: $('list'), ttl: $('ttl'),
    status: $('status'), ffnote: $('ffnote'),
  };

  const state = { files: [], health: null, busy: false };

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
    el.go.textContent = state.busy ? '处理中…' : (n > 1 ? `开始转换（${n} 个）` : '开始转换');
  }

  /* ---------------- 提交 ---------------- */

  function setBar(ratio) {
    el.bar.hidden = false;
    el.bar.firstElementChild.style.width = Math.max(0, Math.min(1, ratio)) * 100 + '%';
  }

  function say(text, isErr) {
    el.msg.hidden = !text;
    el.msg.textContent = text || '';
    el.msg.className = 'msg' + (isErr ? ' err' : '');
  }

  function precheck() {
    const target = el.target.value;
    if (target !== 'auto') return null;
    const others = state.files.filter((f) => !isNcm(f.name));
    if (others.length) {
      return `「${others[0].name}」不是 NCM 文件，请先选择目标格式（当前为自动）。`;
    }
    if (state.health && !state.health.capabilities.transcode && state.files.length === 0) {
      return '未检测到 ffmpeg，通用转码不可用。';
    }
    return null;
  }

  function submit() {
    if (state.busy || state.files.length === 0) return;
    const bad = precheck();
    if (bad) { say(bad, true); return; }

    const fd = new FormData();
    state.files.forEach((f) => fd.append('files', f, f.name));
    fd.append('target', el.target.value);
    fd.append('quality', el.quality.value);

    state.busy = true;
    syncButtons();
    say('正在上传…');
    setBar(0);

    const xhr = new XMLHttpRequest();
    xhr.open('POST', '/api/convert');
    if (TOKEN) xhr.setRequestHeader('Authorization', 'Bearer ' + TOKEN);
    xhr.upload.onprogress = (e) => {
      if (e.lengthComputable) setBar((e.loaded / e.total) * 0.8);
    };
    xhr.upload.onload = () => {
      say('上传完成，正在转换…');
      setBar(0.9);
    };
    xhr.onerror = () => finish('网络错误：后端可能已停止运行。', true);
    xhr.onload = () => {
      let data = null;
      try { data = JSON.parse(xhr.responseText); } catch (_) { /* 保留 null */ }
      const detail = data && data.error ? data.error : `HTTP ${xhr.status}`;
      if (xhr.status === 401) {
        finish('令牌无效或已过期：请用带 ?token= 的地址重新打开本页。', true);
        return;
      }
      if (xhr.status === 429) { finish('请求被拒绝：' + detail, true); return; }
      if (xhr.status !== 200 || !data) {
        finish('转换失败：' + detail, true);
        return;
      }
      if (!data.ok) { finish('转换失败：' + (data.error || '未知错误'), true); return; }
      setBar(1);
      renderResults(data.results || []);
      const okN = (data.results || []).filter((r) => r.ok).length;
      const failN = (data.results || []).length - okN;
      say(`完成 ${okN} 个${failN ? `，失败 ${failN} 个` : ''}。`, failN > 0);
      state.files = [];
      renderFiles();
      state.busy = false;
      syncButtons();
      setTimeout(() => { el.bar.hidden = true; setBar(0); }, 1200);
    };
    xhr.send(fd);
  }

  function finish(text, isErr) {
    state.busy = false;
    syncButtons();
    say(text, isErr);
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
