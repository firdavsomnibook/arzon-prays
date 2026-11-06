// Arzon Prays — Firebase ulagichi (login + Firestore REST + katta fayllar)
// Ilova avval Claude bulutida ishlagan: bu fayl xuddi o'sha interfeysni (window.claude.use('db' | 'assets'))
// Firebase orqali beradi, shuning uchun ilovaning asosiy kodi deyarli o'zgarmaydi.
(function () {
  'use strict';
  const CFG = {
    apiKey: 'AIzaSyA50NZ2snvTz25g1f1xQ5L4d7T1FJhdmXc',
    projectId: 'arzon-prays',
  };
  const FS = 'https://firestore.googleapis.com/v1/projects/' + CFG.projectId + '/databases/(default)/documents';
  const AUTH = 'https://identitytoolkit.googleapis.com/v1/accounts:signInWithPassword?key=' + CFG.apiKey;
  const TOKEN = 'https://securetoken.googleapis.com/v1/token?key=' + CFG.apiKey;
  const LS_RT = 'fb_rt', LS_EMAIL = 'fb_email', LS_STAFF = 'ap_staff', LS_ADMIN = 'fb_admin';
  const SIGNUP = 'https://identitytoolkit.googleapis.com/v1/accounts:signUp?key=' + CFG.apiKey;
  const CHUNK = 900000; // bitta hujjatga sig'adigan base64 bo'lak (Firestore chegarasi ~1 MB)

  const st = { idToken: null, exp: 0, rt: null, email: null, uid: null, staff: '' };
  const lsGet = (k) => { try { return localStorage.getItem(k); } catch (e) { return null; } };
  const lsSet = (k, v) => { try { if (v == null) localStorage.removeItem(k); else localStorage.setItem(k, v); } catch (e) { /* ignore */ } };

  function err(code, message) { const e = new Error(message || code); e.code = code; return e; }
  function httpErr(status, text) {
    if (status === 401 || status === 403) return err('not_granted', 'ruxsat yo\'q (' + status + ')');
    if (status === 429) return err('rate_limited', 'juda ko\'p so\'rov');
    if (status === 413) return err('too_large', 'juda katta');
    if (status >= 500) return err('unavailable', 'server ' + status);
    return err('invalid_argument', 'HTTP ' + status + ' ' + (text || '').slice(0, 200));
  }

  // ---------- kirish ----------
  async function signIn(email, password) {
    let r;
    try { r = await fetch(AUTH, { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify({ email, password, returnSecureToken: true }) }); }
    catch (e) { throw err('unavailable', 'Internet yo\'q'); }
    const j = await r.json().catch(() => ({}));
    if (!r.ok) {
      const m = (j.error && j.error.message) || '';
      if (/INVALID|EMAIL_NOT_FOUND|PASSWORD/.test(m)) throw err('bad_login', 'Email yoki parol noto\'g\'ri');
      if (/TOO_MANY/.test(m)) throw err('rate_limited', 'Juda ko\'p urinish. Birozdan keyin qayta urining');
      throw err('unavailable', m || ('HTTP ' + r.status));
    }
    setTokens(j.idToken, j.refreshToken, +j.expiresIn, j.email, j.localId);
    lsSet(LS_ADMIN, '1');
  }
  async function signInAnon() {
    let r;
    try { r = await fetch(SIGNUP, { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify({ returnSecureToken: true }) }); }
    catch (e) { throw err('unavailable', 'Internet yo\'q'); }
    const j = await r.json().catch(() => ({}));
    if (!r.ok) throw err('unavailable', ((j.error && j.error.message) || ('HTTP ' + r.status)) === 'ADMIN_ONLY_OPERATION' ? 'Firebase\'da "Anonymous" kirish yoqilmagan' : ((j.error && j.error.message) || 'xato'));
    setTokens(j.idToken, j.refreshToken, +j.expiresIn, null, j.localId); lsSet(LS_ADMIN, null); lsSet(LS_EMAIL, null); st.email = null;
  }
  function setTokens(id, rt, expiresIn, email, uid) {
    st.idToken = id; st.rt = rt; st.exp = Date.now() + (expiresIn - 120) * 1000;
    if (email) st.email = email; if (uid) st.uid = uid;
    lsSet(LS_RT, rt); if (st.email) lsSet(LS_EMAIL, st.email);
  }
  async function refresh() {
    if (!st.rt) throw err('not_granted', 'kirish kerak');
    let r;
    try { r = await fetch(TOKEN, { method: 'POST', headers: { 'content-type': 'application/x-www-form-urlencoded' }, body: 'grant_type=refresh_token&refresh_token=' + encodeURIComponent(st.rt) }); }
    catch (e) { throw err('unavailable', 'Internet yo\'q'); }
    const j = await r.json().catch(() => ({}));
    if (!r.ok) { if (r.status === 400) { lsSet(LS_RT, null); st.rt = null; } throw err(r.status === 400 ? 'not_granted' : 'unavailable', 'sessiya tugadi'); }
    setTokens(j.id_token, j.refresh_token, +j.expires_in, null, j.user_id);
  }
  async function token() { if (!st.idToken || Date.now() > st.exp) await refresh(); return st.idToken; }

  // ---------- Firestore REST ----------
  async function call(method, url, body, again) {
    const t = await token();
    let r;
    try { r = await fetch(url, { method, headers: Object.assign({ authorization: 'Bearer ' + t }, body ? { 'content-type': 'application/json' } : {}), body: body ? JSON.stringify(body) : undefined }); }
    catch (e) { throw err('unavailable', 'Internet yo\'q'); }
    if (r.status === 401 && !again) { st.exp = 0; return call(method, url, body, true); }
    return r;
  }
  const enc = (path) => path.split('/').map(encodeURIComponent).join('/');
  // har bir hujjat bitta "j" maydonida JSON matn sifatida saqlanadi (ichma-ich massivlar Firestore'da taqiqlangani uchun)
  function toFields(obj) { const f = { j: { stringValue: JSON.stringify(obj) }, u: { timestampValue: new Date().toISOString() } }; if (obj && typeof obj.status === 'string') f.s = { stringValue: obj.status }; return f; }
  function fromFields(f) {
    if (!f) return {};
    if (f.j && typeof f.j.stringValue === 'string') { try { return JSON.parse(f.j.stringValue); } catch (e) { return {}; } }
    const out = {}; for (const k of Object.keys(f)) out[k] = val(f[k]); return out;
  }
  function val(v) {
    if ('stringValue' in v) return v.stringValue;
    if ('integerValue' in v) return +v.integerValue;
    if ('doubleValue' in v) return v.doubleValue;
    if ('booleanValue' in v) return v.booleanValue;
    if ('timestampValue' in v) return v.timestampValue;
    if ('nullValue' in v) return null;
    if ('arrayValue' in v) return (v.arrayValue.values || []).map(val);
    if ('mapValue' in v) { const o = {}; const f = v.mapValue.fields || {}; for (const k of Object.keys(f)) o[k] = val(f[k]); return o; }
    return null;
  }
  const snap = (id, data) => ({ id, exists: data !== undefined, data: () => data });
  async function getDoc(path) {
    const r = await call('GET', FS + '/' + enc(path));
    if (r.status === 404) return snap(path.split('/').pop(), undefined);
    if (!r.ok) throw httpErr(r.status, await r.text());
    const j = await r.json(); return snap(path.split('/').pop(), fromFields(j.fields));
  }
  async function setDoc(path, data) {
    const r = await call('PATCH', FS + '/' + enc(path), { fields: toFields(data) });
    if (!r.ok) throw httpErr(r.status, await r.text());
  }
  async function delDoc(path) {
    const r = await call('DELETE', FS + '/' + enc(path));
    if (!r.ok && r.status !== 404) throw httpErr(r.status, await r.text());
  }
  async function listColl(name) {
    const docs = []; let page = '';
    for (let i = 0; i < 50; i++) {
      const r = await call('GET', FS + '/' + enc(name) + '?pageSize=300' + (page ? '&pageToken=' + encodeURIComponent(page) : ''));
      if (r.status === 404) break;
      if (!r.ok) throw httpErr(r.status, await r.text());
      const j = await r.json();
      for (const d of j.documents || []) { const id = d.name.split('/').pop(); const data = fromFields(d.fields); docs.push({ id, data: () => data }); }
      if (!j.nextPageToken) break; page = j.nextPageToken;
    }
    return { docs, size: docs.length };
  }
  const db = {
    doc: (path) => ({ id: path.split('/').pop(), path, get: () => getDoc(path), set: (d) => setDoc(path, d), delete: () => delDoc(path) }),
    collection: (name) => ({ get: () => listColl(name) }),
  };

  // ---------- katta fayllar: gzip + base64 bo'laklarga bo'lib saqlanadi ----------
  function b64(u8) { let s = ''; for (let i = 0; i < u8.length; i += 0x8000) s += String.fromCharCode.apply(null, u8.subarray(i, i + 0x8000)); return btoa(s); }
  function unb64(s) { const bin = atob(s); const u = new Uint8Array(bin.length); for (let i = 0; i < bin.length; i++) u[i] = bin.charCodeAt(i); return u; }
  async function gz(blob) {
    if (typeof CompressionStream === 'undefined') return { z: 0, u8: new Uint8Array(await blob.arrayBuffer()) };
    const s = blob.stream().pipeThrough(new CompressionStream('gzip'));
    return { z: 1, u8: new Uint8Array(await new Response(s).arrayBuffer()) };
  }
  async function gunzip(u8) { const s = new Blob([u8]).stream().pipeThrough(new DecompressionStream('gzip')); return new Uint8Array(await new Response(s).arrayBuffer()); }
  async function putChunks(coll, id, b64s, meta) {
    const n = Math.max(1, Math.ceil(b64s.length / CHUNK));
    for (let i = 0; i < n; i++) await setDoc(coll + '/' + id + '__' + i, { b: b64s.slice(i * CHUNK, (i + 1) * CHUNK) });
    await setDoc(coll + '/' + id, Object.assign({ n }, meta));
    return n;
  }
  async function getChunks(coll, id) {
    const m = await getDoc(coll + '/' + id); if (!m.exists) throw err('not_found', 'fayl topilmadi');
    const meta = m.data(); const parts = [];
    for (let i = 0; i < meta.n; i++) { const c = await getDoc(coll + '/' + id + '__' + i); if (!c.exists) throw err('not_found', 'fayl bo\'lagi topilmadi'); parts.push(c.data().b); }
    return { meta, u8: unb64(parts.join('')) };
  }
  const assets = {
    async upload(blob) {
      const id = 'a' + Date.now().toString(36) + Math.random().toString(36).slice(2, 8);
      const { z, u8 } = await gz(blob);
      await putChunks('blobs', id, b64(u8), { z, size: u8.length, at: new Date().toISOString() });
      return { id };
    },
    async delete(id) {
      const m = await getDoc('blobs/' + id); const n = m.exists ? (m.data().n || 1) : 0;
      await delDoc('blobs/' + id); for (let i = 0; i < n; i++) await delDoc('blobs/' + id + '__' + i);
    },
  };
  async function blobJSON(id) {
    const { meta, u8 } = await getChunks('blobs', id);
    const raw = meta.z ? await gunzip(u8) : u8;
    return JSON.parse(new TextDecoder().decode(raw));
  }
  // Telegramdan kelgan asl fayl (xlsx/xls/pdf) — bot "tgfiles/<sid>__i" bo'laklariga yozadi
  async function tgFile(sid) { const { meta, u8 } = await getChunks('tgfiles', sid); return { meta, buf: u8.buffer }; }

  // ---------- kirish oynasi ----------
  function loginUI(msg, adminMode) {
    return new Promise((resolve) => {
      const wrap = document.createElement('div');
      wrap.id = 'fbLogin';
      wrap.innerHTML = `<style>
        #fbLogin{position:fixed;inset:0;z-index:9999;display:grid;place-items:center;background:var(--bg,#f3f4f1);font:15px system-ui,sans-serif;color:var(--ink,#18201b);padding:16px}
        #fbLogin form{width:100%;max-width:360px;background:#fff;border:1px solid #dde2dc;border-radius:16px;padding:24px;box-shadow:0 8px 30px #0001}
        #fbLogin h1{font-size:22px;margin:0 0 4px;color:#1f6b45}#fbLogin p{margin:0 0 16px;color:#6b746e;font-size:13px}
        #fbLogin label{display:block;font-size:13px;margin:10px 0 4px;color:#4a534d}
        #fbLogin input{width:100%;box-sizing:border-box;padding:11px 12px;border:1px solid #c9d0c9;border-radius:10px;font:inherit;background:#fff;color:#18201b}
        #fbLogin .go{margin-top:16px;width:100%;padding:12px;border:0;border-radius:10px;background:#1f6b45;color:#fff;font:600 15px system-ui;cursor:pointer}
        #fbLogin .go:disabled{opacity:.6}#fbLogin .e{color:#c62828;font-size:13px;margin-top:10px;min-height:18px}
        #fbLogin .sw{margin-top:12px;border:0;background:none;color:#1f6b45;text-decoration:underline;cursor:pointer;font-size:13px;padding:0}
      </style>
      <form autocomplete="on"><h1>Arzon Prays</h1><p>by Shifoat</p><div class="f"></div>
        <button class="go" type="submit">Kirish</button><div class="e" role="alert"></div>
        <button class="sw" type="button"></button></form>`;
      document.body.appendChild(wrap);
      const f = wrap.querySelector('form'); const e = wrap.querySelector('.e'); const box = wrap.querySelector('.f'); const sw = wrap.querySelector('.sw');
      let admin = !!adminMode;
      const draw = () => {
        box.innerHTML = admin
          ? `<label>Admin email<input name="email" type="email" required autocomplete="username"></label><label>Parol<input name="password" type="password" required autocomplete="current-password"></label>`
          : `<label>Ismingiz<input name="staff" type="text" required maxlength="40" autocomplete="name" placeholder="masalan: Shohruh"></label>`;
        sw.textContent = admin ? '← Ism bilan kirish' : 'Admin sifatida kirish (parol bilan)';
        if (admin) { f.email.value = lsGet(LS_EMAIL) || ''; (f.email.value ? f.password : f.email).focus(); }
        else { f.staff.value = lsGet(LS_STAFF) || ''; f.staff.focus(); }
      };
      sw.onclick = () => { admin = !admin; e.textContent = ''; draw(); };
      draw(); if (msg) e.textContent = msg;
      f.onsubmit = async (ev) => {
        ev.preventDefault(); e.textContent = ''; const b = f.querySelector('.go'); b.disabled = true; b.textContent = 'Tekshirilmoqda…';
        try {
          if (admin) { await signIn(f.email.value.trim(), f.password.value); }
          else { const n = f.staff.value.trim().replace(/\s+/g, ' '); if (!n) throw err('bad', 'Ismingizni yozing'); await signInAnon(); lsSet(LS_STAFF, n); st.staff = n; }
          if (admin) { st.staff = lsGet(LS_STAFF) || 'Admin'; }
          wrap.remove(); resolve();
        } catch (x) { e.textContent = x.message || String(x); b.disabled = false; b.textContent = 'Kirish'; }
      };
    });
  }

  async function start() {
    st.rt = lsGet(LS_RT); st.email = lsGet(LS_EMAIL); st.staff = lsGet(LS_STAFF) || '';
    if (st.rt) {
      try { await refresh(); }
      catch (e) { if (e.code === 'unavailable') { /* internet yo'q: baribir urinib ko'ramiz */ } else await loginUI('Sessiya tugagan, qayta kiring.'); }
    } else await loginUI();
    window.claude = { use: async (name) => (name === 'db' ? db : name === 'assets' ? assets : null) };
  }

  window.FB = {
    blobJSON, tgFile,
    get email() { return st.email; },
    get staff() { return st.staff; },
    setStaff(n) { n = String(n || '').trim().replace(/\s+/g, ' ').slice(0, 40); if (!n) return; st.staff = n; lsSet(LS_STAFF, n); },
    get isAdmin() { return lsGet(LS_ADMIN) === '1' || !!st.email; },
    logout() { lsSet(LS_RT, null); lsSet(LS_ADMIN, null); st.rt = null; st.idToken = null; location.reload(); },
    async adminLogin() { await loginUI('', true); location.reload(); },
    _test: { toFields, fromFields },
  };
  window.__fbReady = start();
})();
