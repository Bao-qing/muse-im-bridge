/*
 * 在已登录的 https://muse.ai 页面打开 F12 -> Console，整段粘贴运行回车。
 *
 * 它会自动提取需要的 cookies，并弹框提示你确认/补全，最后直接下载成 cookies.json。
 * 下载后将文件放入本项目根目录即可。
 */
(async () => {
  let cookies = {};

  // 1. 尝试从 cookieStore 获取非 HttpOnly cookies
  if (window.cookieStore) {
    try {
      const list = await cookieStore.getAll();
      for (const c of list) {
        if (c.domain && c.domain.includes('muse.ai')) {
          cookies[c.name] = c.value;
        }
      }
    } catch (e) {}
  }

  // 2. 如果缺少关键的 HttpOnly cookie（hatch_sess 等），弹窗提示一键粘贴整行 Cookie 头
  if (!cookies.hatch_sess) {
    const raw = prompt(
      '【一步导出】因浏览器安全限制，HttpOnly Cookie 无法通过纯 JS 自动读取：\n' +
      '请在 F12 -> Network (网络) 随便点一个 muse.ai 请求，\n' +
      '在 Request Headers 找到 Cookie 整行值粘贴到这里：'
    );
    if (!raw) {
      console.log('用户取消');
      return;
    }
    // 兼容粘贴整行
    cookies = { cookie: raw.trim() };
  }

  // 3. 触发下载为 cookies.json
  const text = JSON.stringify(cookies, null, 2) + '\n';
  const a = document.createElement('a');
  a.href = URL.createObjectURL(new Blob([text], { type: 'application/json' }));
  a.download = 'cookies.json';
  document.body.appendChild(a);
  a.click();
  a.remove();
  URL.revokeObjectURL(a.href);

  console.log('✅ 已触发下载 cookies.json，放入项目根目录即可生效！');
})();
