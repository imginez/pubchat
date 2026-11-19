/* 更新公告（S4）：桌面与手机共用同一份 H5，所以这一份组件两端都吃。
   服务端语义由 hosts/server.py（公网）与 hosts/local.py（本机对外口）实现：
   没有 data/notice.json 或文件为空 → 204；有 → 200 原样透传，不加工。
   客户端只认 version / title / body / published_at 四个字段，
   并且**任何失败都静默**——公告是顺带的事，绝不能挡住人进软件。 */
(function (global) {
  var KEY = 'pcNotice';
  var LS_MUTE = 'pubchat.notice.muted.v1';    // 「以后不再弹出」按版本记
  var LS_LIST = 'pubchat.notice.list.v1';     // 本机看过的公告，给「关于」页列历史
  var LIST_MAX = 20;

  function lsGet(k, d) {
    try { var v = localStorage.getItem(k); return v ? JSON.parse(v) : d; } catch (e) { return d; }
  }
  function lsSet(k, v) { try { localStorage.setItem(k, JSON.stringify(v)); } catch (e) {} }
  function lsRaw(k) { try { return localStorage.getItem(k); } catch (e) { return null; } }
  /* 版本号是纯字符串：存的时候不能再 JSON.stringify 一遍，否则读回来带一层引号，
     「同版本不再弹」永远比不中（真浏览器那遍才抓到）。 */
  function lsPut(k, v) { try { localStorage.setItem(k, v); } catch (e) {} }

  function text(v) { return typeof v === 'string' ? v : (v == null ? '' : String(v)); }

  /* 服务端是原样透传的，字段缺不缺它不管——这里补齐成一定有的形状。
     没有 version 就当没有公告：按版本静默这件事会失去依据，不如不弹。 */
  function normalize(o) {
    if (!o || typeof o !== 'object' || Array.isArray(o)) return null;
    var v = text(o.version).trim();
    if (!v) return null;
    return {
      version: v,
      title: text(o.title).trim() || '更新公告',
      body: text(o.body),
      published_at: text(o.published_at).trim()
    };
  }

  function record(n) {
    var list = lsGet(LS_LIST, []);
    if (!Array.isArray(list)) list = [];
    list = list.filter(function (x) { return !x || x.version !== n.version; });
    list.unshift(n);
    if (list.length > LIST_MAX) list.length = LIST_MAX;
    lsSet(LS_LIST, list);
  }

  function when(iso) {
    if (!iso) return '';
    var t = Date.parse(iso);
    if (isNaN(t)) return iso;                 // 服务端写的是字符串，认不出来就原样给
    var d = new Date(t);
    function p(x) { return (x < 10 ? '0' : '') + x; }
    return d.getFullYear() + '-' + p(d.getMonth() + 1) + '-' + p(d.getDate()) +
           ' ' + p(d.getHours()) + ':' + p(d.getMinutes());
  }

  function styleOnce() {
    if (document.getElementById(KEY + '-style')) return;
    var css = '.' + KEY + '{position:fixed;inset:0;z-index:9000;display:flex;' +
      'align-items:center;justify-content:center;background:rgba(17,24,39,.34);padding:20px}' +
      '.' + KEY + '-card{background:#fff;border-radius:14px;max-width:420px;width:100%;' +
      'padding:20px 20px 16px;box-shadow:0 16px 40px rgba(16,24,40,.22);' +
      'font:14px/1.6 -apple-system,BlinkMacSystemFont,"Segoe UI","Microsoft YaHei",sans-serif;' +
      'color:#111827}' +
      '.' + KEY + '-hd{display:flex;gap:8px;align-items:baseline;margin-bottom:6px}' +
      '.' + KEY + '-ver{background:#e7f7ef;color:#0b8f4a;border-radius:6px;padding:1px 7px;' +
      'font-size:12px;font-weight:600}' +
      '.' + KEY + '-date{color:#6b7280;font-size:12px}' +
      '.' + KEY + '-card h3{margin:0 0 8px;font-size:17px;line-height:1.4}' +
      '.' + KEY + '-body{white-space:pre-wrap;word-break:break-word;color:#374151;' +
      'max-height:46vh;overflow:auto}' +
      '.' + KEY + '-mute{display:flex;gap:6px;align-items:center;margin:14px 0 12px;' +
      'color:#6b7280;font-size:13px;cursor:pointer}' +
      '.' + KEY + '-ok{display:block;width:100%;border:0;border-radius:10px;background:#10b981;' +
      'color:#fff;font-size:15px;font-weight:600;padding:10px 0;cursor:pointer}' +
      '@media (prefers-reduced-motion: reduce){.' + KEY + '{animation:none}}';
    var s = document.createElement('style');
    s.id = KEY + '-style';
    s.textContent = css;
    (document.head || document.documentElement).appendChild(s);
  }

  /* 首启流程（资料步 / 四步引导）正开着的时候不叠公告：
     新人的第一次更重要。这条公告仍然记进本机历史，「关于」里随时看得到。 */
  function firstRunBusy() {
    var ids = ['profileSheet', 'guideSheet'];
    for (var i = 0; i < ids.length; i++) {
      var n = document.getElementById(ids[i]);
      if (n && !n.classList.contains('hidden')) return true;
    }
    return false;
  }

  function show(n) {
    styleOnce();
    var old = document.getElementById(KEY);
    if (old && old.parentNode) old.parentNode.removeChild(old);
    var wrap = document.createElement('div');
    wrap.className = KEY;
    wrap.id = KEY;
    wrap.setAttribute('role', 'dialog');
    wrap.setAttribute('aria-modal', 'true');
    wrap.setAttribute('aria-labelledby', KEY + 'Title');

    var card = document.createElement('div');
    card.className = KEY + '-card';

    var hd = document.createElement('div');
    hd.className = KEY + '-hd';
    var ver = document.createElement('span');
    ver.className = KEY + '-ver';
    ver.textContent = 'v' + n.version;
    hd.appendChild(ver);
    if (n.published_at) {
      var d = document.createElement('span');
      d.className = KEY + '-date';
      d.textContent = when(n.published_at);
      hd.appendChild(d);
    }
    card.appendChild(hd);

    var h3 = document.createElement('h3');
    h3.id = KEY + 'Title';
    h3.textContent = n.title;
    card.appendChild(h3);

    var body = document.createElement('div');
    body.className = KEY + '-body';
    body.textContent = n.body;          // 全部走 textContent：公告正文永远不当 HTML 解析
    card.appendChild(body);

    var mute = document.createElement('label');
    mute.className = KEY + '-mute';
    var box = document.createElement('input');
    box.type = 'checkbox';
    box.id = KEY + 'Mute';
    mute.appendChild(box);
    mute.appendChild(document.createTextNode(' 以后不再弹出这个版本'));
    card.appendChild(mute);

    var ok = document.createElement('button');
    ok.type = 'button';
    ok.className = KEY + '-ok';
    ok.id = KEY + 'Ok';
    ok.textContent = '知道了';
    card.appendChild(ok);

    wrap.appendChild(card);
    document.body.appendChild(wrap);

    function close() {
      if (box.checked) lsPut(LS_MUTE, n.version);
      if (wrap.parentNode) wrap.parentNode.removeChild(wrap);
      document.removeEventListener('keydown', onKey);
    }
    function onKey(e) { if (e.key === 'Escape') close(); }
    ok.addEventListener('click', close);
    wrap.addEventListener('click', function (e) { if (e.target === wrap) close(); });
    document.addEventListener('keydown', onKey);
    ok.focus();
    return wrap;
  }

  function basePrefix() {
    return (global.PUBCHAT_BASE != null) ? global.PUBCHAT_BASE
                                         : location.pathname.replace(/\/[^\/]*$/, '');
  }

  /* 返回一个 promise，测试可以直接 await；产品侧不关心结果。 */
  function boot(base) {
    var url = (base == null ? basePrefix() : base) + '/api/notice';
    return fetch(url, { cache: 'no-store' })
      .then(function (r) {
        if (r.status === 204) return { status: 'none' };
        if (!r.ok) return { status: 'error', http: r.status };
        return r.json().then(function (j) { return { status: 'ok', raw: j }; },
                              function () { return { status: 'error', why: 'json' }; });
      })
      .then(function (res) {
        if (res.status !== 'ok') return res;
        var n = normalize(res.raw);
        if (!n) return { status: 'none' };
        record(n);
        if (lsRaw(LS_MUTE) === n.version) return { status: 'muted', notice: n };
        if (firstRunBusy()) return { status: 'busy', notice: n };
        show(n);
        return { status: 'shown', notice: n };
      })
      .catch(function () { return { status: 'error' }; });   // 断网、DNS、CORS 全走这条
  }

  global.PubChatNotice = {
    boot: boot,
    list: function () {
      var l = lsGet(LS_LIST, []);
      return Array.isArray(l) ? l : [];
    },
    mutedVersion: function () { return lsRaw(LS_MUTE) || ''; },
    normalize: normalize,
    when: when            // 「关于」页也载这份组件，日期格式两端只能有一套
  };
})(window);
