/* PubChat 设备凭证注入（服务器模式）。
 *
 * 认领成功后 device_secret 存在本 origin 的 localStorage 里；这个脚本在
 * 页面自己的脚本之前加载，给同源的 /api/* 请求补上 Authorization:
 * Bearer <secret>。本机桌面宿主用回环 + Host/Origin 校验，不认这个头，
 * 补上也无害。
 *
 * 覆盖面是整个 /api/：管理口在 0.7 起对所有端点都要设备凭证（只有
 * /api/admin/token/verify 与 /healthz 例外），桌面外壳那一族页面
 * （shell / setup / instances / server / about）原先只发 /api/state 与
 * /api/admin/*，其余端点一律 401，整个管理面就是空白。
 *
 * Red Line #4: 只走 Bearer，不写 Cookie；secret 不出现在 URL 里。
 */
(function () {
  'use strict';

  var KEY = 'pubchat.device.secret';
  var rawFetch = window.fetch ? window.fetch.bind(window) : null;

  function get() {
    try { return localStorage.getItem(KEY) || ''; } catch (e) { return ''; }
  }

  function set(secret) {
    try {
      if (secret) localStorage.setItem(KEY, secret);
      else localStorage.removeItem(KEY);
    } catch (e) { /* 隐私模式写不进去就当没有 */ }
  }

  function clear() { set(''); }

  /* 0.8.17：管理权跟账号走，不跟机器走（需求方：不要认领那套）。
     没有设备 secret 时用当前登录账号的一级 Key 当 Bearer；服务端只认 role=owner 的账号。 */
  function ownerKey() {
    try { return localStorage.getItem('pubchat.key') || ''; } catch (e) { return ''; }
  }
  function credential() { return get() || ownerKey(); }

  function needsAuth(url) {
    var u;
    try {
      u = new URL(url, location.href);
    } catch (e) {
      return false;
    }
    if (u.origin !== location.origin) return false;
    return u.pathname.indexOf('/api/') === 0;
  }

  if (rawFetch) {
    window.fetch = function (input, init) {
      var url = typeof input === 'string' ? input : (input && input.url) || '';
      var secret = credential();
      if (secret && needsAuth(url)) {
        var opts = {};
        if (init) {
          for (var k in init) {
            if (Object.prototype.hasOwnProperty.call(init, k)) opts[k] = init[k];
          }
        }
        var h = new Headers(opts.headers || {});
        if (!h.has('Authorization')) h.set('Authorization', 'Bearer ' + secret);
        opts.headers = h;
        init = opts;
      }
      return rawFetch(input, init);
    };
  }

  window.PubChatDevice = { key: KEY, get: get, set: set, clear: clear };
})();
