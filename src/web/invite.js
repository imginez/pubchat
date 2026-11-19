/*
 * 用邀请链接加入别人的聊天室（设置页 / 首启向导共用）。
 *
 * 为什么不在壳里 iframe 远端的 join.html：加入成功后要拿到对方返回的一级 Key，
 * 而跨源 iframe 读不到对方页面的内容。所以这里自己连一次 WebSocket（协议与
 * join.html 完全一致），拿到凭据后把远端地址登记成本机实例，再由调用方向壳
 * 发 pubchat-join-remote。
 */
(function () {
  'use strict';

  function parseLink(raw) {
    var text = String(raw || '').trim();
    if (!text) throw new Error('请粘贴邀请链接');
    if (text.indexOf('://') < 0) text = 'https://' + text;
    var u;
    try { u = new URL(text); } catch (e) { throw new Error('链接格式不对'); }
    var scheme = u.protocol.replace(':', '').toLowerCase();
    if (scheme !== 'http' && scheme !== 'https') {
      throw new Error('只支持 http:// 或 https:// 链接');
    }
    var hash = u.hash ? u.hash.slice(1) : '';
    var token = '';
    try { token = new URLSearchParams(hash).get('t') || ''; } catch (e) {}
    if (!token) {
      var m = hash.match(/(?:^|&)t=([^&]+)/);
      token = m ? decodeURIComponent(m[1]) : '';
    }
    if (!token) throw new Error('链接里没有邀请码（应形如 …/join.html#t=…）');

    // 反代子路径要保留：/pub/join.html → 实例根 /pub/，WS 在 /pub/ws
    var path = u.pathname || '/';
    var basePath = path.replace(/[^\/]*$/, '');
    var root = u.origin + basePath;
    return {
      token: token,
      host: u.host,
      base: root.replace(/\/$/, '') + '/',
      ws: (scheme === 'https' ? 'wss://' : 'ws://') + u.host + basePath + 'ws'
    };
  }

  function apiPost(obj) {
    return fetch('/api/instances', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(obj)
    }).then(function (r) {
      return r.json().then(function (j) { return { ok: r.ok, j: j || {} }; });
    });
  }

  /* 登记成实例；已经登记过就找回来（重复加入同一台服务器不该报错）。 */
  function register(base, name) {
    var url = base.replace(/\/$/, '');
    return apiPost({ action: 'add', url: url, name: name }).then(function (res) {
      if (res.ok && res.j.ok && res.j.instance) return res.j.instance;
      var msg = res.j.error || '';
      if (msg.indexOf('已经添加过') >= 0) {
        return fetch('/api/instances', { cache: 'no-store' })
          .then(function (r) { return r.json(); })
          .then(function (j) {
            var items = (j && j.instances) || [];
            for (var i = 0; i < items.length; i++) {
              if (items[i].kind === 'remote' &&
                  String(items[i].url || '').toLowerCase() === url.toLowerCase()) return items[i];
            }
            throw new Error('这台服务器已在列表里，但没找到对应实例');
          });
      }
      throw new Error(msg || '登记实例失败');
    });
  }

  /**
   * 连远端、用邀请码加入。
   * opts: { link, name, clientId, onStage(text), onDone(info), onFail(err) }
   * info: { base, instance, name, client_id, key1, pending }
   */
  function join(opts) {
    var info;
    try { info = parseLink(opts.link); } catch (e) { opts.onFail(e); return; }
    var name = String(opts.name || '').trim();
    var cid = String(opts.clientId || '').trim().toLowerCase();
    if (!name) { opts.onFail(new Error('请填写你的显示名')); return; }
    if (!/^[a-z0-9_]{2,32}$/.test(cid)) {
      opts.onFail(new Error('登录名只能小写字母/数字/下划线，2-32 位')); return;
    }

    var ws, settled = false, timer;
    function fail(err) {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      try { if (ws) ws.close(); } catch (e) {}
      opts.onFail(err instanceof Error ? err : new Error(String(err)));
    }
    function succeed(msg, pending) {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      try { ws.close(); } catch (e) {}
      if (opts.onStage) opts.onStage(pending ? '已提交，正在登记实例…' : '加入成功，正在登记实例…');
      // 实例名交给后端从主机名推导；这里别塞加入者自己的显示名，否则侧栏分组会叫成「Ming」
      register(info.base, '').then(function (inst) {
        opts.onDone({
          base: info.base,
          instance: inst,
          name: String(msg.name || name),
          client_id: String(msg.client_id || cid),
          key1: String(msg.key1 || ''),
          pending: !!pending
        });
      }).catch(function (err) {
        // 账号已经建好了，只是本地没记住地址；提示用户手动添加实例，别让人以为白加入
        opts.onFail(new Error('已加入，但登记实例失败：' + err.message +
                              '（可手动添加 ' + info.base + '）'));
      });
    }

    if (opts.onStage) opts.onStage('正在连接 ' + info.host + ' …');
    try {
      ws = new WebSocket(info.ws);
    } catch (e) {
      fail(new Error('无法发起连接：' + e.message));
      return;
    }
    timer = setTimeout(function () { fail(new Error('连接超时，请检查网络或链接是否有效')); }, 15000);

    ws.onopen = function () {
      if (opts.onStage) opts.onStage('已连接，正在提交加入申请…');
      try {
        ws.send(JSON.stringify({ type: 'join', token: info.token, name: name, client_id: cid }));
      } catch (e) {
        fail(new Error('发送失败：' + e.message));
      }
    };
    ws.onmessage = function (ev) {
      var msg;
      try { msg = JSON.parse(ev.data); } catch (e) { return; }
      if (msg.type === 'joined' && msg.ok) {
        succeed(msg, false);
      } else if (msg.type === 'pending' && msg.ok) {
        succeed(msg, true);
      } else if (msg.type === 'error') {
        fail(new Error(msg.message || '加入失败'));
      }
    };
    ws.onerror = function () { fail(new Error('连接失败，请检查地址和网络')); };
    ws.onclose = function () { fail(new Error('连接被服务器关闭')); };
  }

  window.PubChatInvite = { parseLink: parseLink, join: join, register: register };
})();
