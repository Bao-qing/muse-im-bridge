/*
 * 在已登录的 https://muse.ai 页面的 DevTools Console 里整段粘贴运行，
 * 自动换一份新的 .mint.json 并下载到本地。
 *
 * 原理（2026-10-27 从网页 JS 里确认）：
 *   POST /api/hatch/token  {vmAddress: "wss://<vm_id>.metaaivm.com/", vmName: <vm_id>}
 *     -> {token, notary_token}
 *   响应里没有 notary_token 时，再打 POST /api/hatch/noise-notary-token {vmName}
 *   cookie 由浏览器自动带上（HttpOnly，脚本不需要、也读不到）。
 *
 * vm_id 自动从 IndexedDB 的 hatch-chat-cache 里读；读不到会弹框让你填。
 */
(async () => {
  const TOKEN_PATH = '/api/hatch/token';
  const NOTARY_PATH = '/api/hatch/noise-notary-token';

  const findVmId = async () => {
    if (window.__MUSE_VM_ID__) return window.__MUSE_VM_ID__;
    const re = /([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\.metaaivm\.com/i;
    try {
      const dbs = await indexedDB.databases();
      for (const info of dbs) {
        if (!info.name) continue;
        const db = await new Promise((res, rej) => {
          const r = indexedDB.open(info.name);
          r.onsuccess = () => res(r.result);
          r.onerror = () => rej(r.error);
        });
        for (const store of Array.from(db.objectStoreNames)) {
          const vals = await new Promise((res) => {
            const r = db.transaction(store, 'readonly').objectStore(store).getAll();
            r.onsuccess = () => res(r.result);
            r.onerror = () => res([]);
          });
          const hit = JSON.stringify(vals).match(re);
          if (hit) { db.close(); return hit[1]; }
        }
        db.close();
      }
    } catch (e) { console.warn('IndexedDB 读取失败：', e); }
    return prompt('没找到 vm_id，请粘贴（例如 c72b5f2b-...）:')?.trim() || null;
  };

  const post = async (path, body) => {
    const res = await fetch(path, {
      method: 'POST',
      credentials: 'same-origin',
      cache: 'no-store',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    if (!res.ok) {
      let detail = '';
      try { detail = (await res.json()).error || ''; } catch {}
      throw new Error(`${path} -> HTTP ${res.status} ${detail}`);
    }
    return res.json();
  };

  const vmId = await findVmId();
  if (!vmId) { console.error('没有 vm_id，取消'); return; }

  let data = await post(TOKEN_PATH, {
    vmAddress: `wss://${vmId}.metaaivm.com/`,
    vmName: vmId,
  });
  if (!data.token || !data.notary_token) {
    const extra = await post(NOTARY_PATH, { vmName: vmId });
    data = { token: data.token, notary_token: data.notary_token || extra.notaryToken };
  }
  if (!data.token || !data.notary_token) {
    console.error('换 token 失败，返回：', data);
    return;
  }

  const text = JSON.stringify(
    { token: data.token, notary_token: data.notary_token }, null, 2) + '\n';
  const a = document.createElement('a');
  a.href = URL.createObjectURL(new Blob([text], { type: 'application/json' }));
  a.download = '.mint.json';
  a.click();
  URL.revokeObjectURL(a.href);

  // 只打印长度和 vm_id，不把凭证打到控制台
  console.log('已下载 .mint.json', {
    vm_id: vmId,
    token: data.token.length,
    notary_token: data.notary_token.length,
  });
})();
