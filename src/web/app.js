/* ============================================================
   公共聊天室前端（零构建）
   协议：pubchat
     auth        {type:'auth', kind:'user', username, key, last_read:{slug:id}}
     authenticated  ← {me:{client_id,name,onboarded}, rooms:[{slug,kind,title,last_ts,unread}], agents:[{client_id,name,key2}]}
     message     → {type:'message', room, content}   ← 同结构广播
     history     → {type:'history', room, limit, since} ← {room, messages}
     presence    ← {members:[...], online:[...]}
     profile     → {type:'profile', name, username, key}  ← {type:'account', username, name, key, relogin}
     agent_rename→ {type:'agent_rename', client_id, name} ← {type:'agent_renamed', client_id, name}
     agent_set_mode→ {type:'agent_set_mode', client_id, mode} ← {type:'agent_mode', client_id, mode, mode_label, prompts}

   左侧会话列表（2026-11-05 改造）：按 service 端 room.kind 分组
     dm → 联系人 / lobby → 群聊 / own 及其它 → 其他
   分组映射写在 config.js 的 PUB_CONFIG.groups，本文件只实现机制。
   ============================================================ */
(function () {
  'use strict';

  var cfg = window.PUB_CONFIG || {};
  var LIMIT = cfg.historyLimit || 60;
  var LS_USER = 'pubchat.user';
  var LS_KEY = 'pubchat.key';
  /* 左侧分组相关的本地存储（都带 pubchat. 前缀，避免和同域其他页面打架） */
  var LS_COLLAPSE = 'pubchat.groups.collapsed'; // {groupId:true=折叠}
  var LS_PIN = 'pubchat.groups.pin';            // {slug:1}
  var LS_MUTE = 'pubchat.groups.mute';          // {slug:1}
  var LS_ORDER = 'pubchat.groups.order';        // {groupId:[slug,...]} 拖拽后的顺序
  var LS_LAST = 'pubchat.groups.lastTs';        // {slug:毫秒} 本地兜底的最近活跃时间
  var LS_READ = 'pubchat.lastRead.';            // 前缀，键 = pubchat.lastRead.<登录名>：{slug:最后读到的消息 id}
  var LS_GUIDE = 'pubchat.guide.seen.v1';       // 这台设备看过四步引导了吗（与服务端 onboarded 分开）

  /* config.js 没配 groups 时的兜底（保证老 config.js 也不至于白屏） */
  var DEFAULT_GROUPS = [
    { id: 'contact', label: '联系人', rooms: ['dm'] },
    { id: 'group', label: '群聊', rooms: ['lobby'] },
    { id: 'agentsdm', label: '智能体协作', rooms: ['agent_dm'], defaultOpen: false, agentDm: true },
    { id: 'other', label: '其他', rooms: ['own', '*'] }
  ];

  var state = {
    ws: null, connected: false, retry: 0, retryTimer: null, reconnectNow: false,
    lastPong: 0,
    me: null, rooms: [], agents: [], agentModes: [], newAgentMode: 'online',
    connectMode: 'online', connectPending: '', connectText: '',
    members: {}, online: {},
    msgs: {}, active: null, unread: {}, seen: {}, lastRead: {},
    lastKey: '', lastUser: '',
    /* —— 左侧分组 —— */
    query: '',                 // 搜索词
    collapsed: {},             // {groupId:bool}
    pinned: {}, muted: {}, order: {}, lastTs: {},
    pendingAgentDm: '',        // 我发起的开房请求（own|peer），回包或报错即清
    selectAfterRooms: '',      // 房间列表回包后要自动切过去的 slug
    firstRunGuide: false,      // 首启：资料步走完再弹引导
    guideShown: false,         // 本次会话已经自动弹过引导了，重连不再弹第二次
    replyTo: null,             // 待发送的引用（一条消息对象），发送后清空
    acked: {},                 // {room: {id:true}} 已上报过读回执的，防重复发帧
  };

  function $(s) { return document.querySelector(s); }
  function el(t, c, x) { var n = document.createElement(t); if (c) n.className = c; if (x != null) n.textContent = x; return n; }
  function esc(s) { return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) { return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]; }); }
  function pad(n) { return String(n).padStart(2, '0'); }
  function dayKey(ts) { var x = new Date(ts); return x.getFullYear() + '-' + x.getMonth() + '-' + x.getDate(); }
  function dayLabel(ts) {
    var x = new Date(ts), n = new Date();
    if (dayKey(ts) === dayKey(n.getTime())) return '今天';
    if (dayKey(ts) === dayKey(n.getTime() - 864e5)) return '昨天';
    return (x.getMonth() + 1) + '月' + x.getDate() + '日';
  }
  function hm(ts) { var x = new Date(ts); return pad(x.getHours()) + ':' + pad(x.getMinutes()); }
  function colorOf(id) { var h = 0; for (var i = 0; i < id.length; i++) h = (h * 31 + id.charCodeAt(i)) % 360; return 'hsl(' + h + ' 55% 55%)'; }
  function initialOf(n) { return (n || '?').trim().slice(0, 1).toUpperCase(); }

  /* 开屏动画退场：第一屏有内容可看了就收（组件自己兜 1.2s 上限） */
  function splashDone() {
    if (window.PubChatSplash) { window.PubChatSplash.ready(); return; }
    // 组件本身没加载上（资源被某一层白名单挡住）时，页面里预留的覆盖层必须自己摘掉
    var s = document.getElementById('pcSplash');
    if (s && s.parentNode) s.parentNode.removeChild(s);
  }
  function nameOf(id) { return state.members[id] ? state.members[id].name : id; }
  function toast(t) {
    var n = $('#toast'); n.textContent = t; n.classList.remove('hidden');
    clearTimeout(toast._t); toast._t = setTimeout(function () { n.classList.add('hidden'); }, 2200);
  }
  function randomKey() {
    var s = 'abcdefghijkmnpqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789', out = '';
    var buf = new Uint32Array(16);
    if (window.crypto && window.crypto.getRandomValues) window.crypto.getRandomValues(buf);
    else for (var j = 0; j < 16; j++) buf[j] = Math.floor(Math.random() * 4294967296);
    for (var i = 0; i < 16; i++) out += s[buf[i] % s.length];
    return out;
  }
  function lsGet(k, d) { try { var v = localStorage.getItem(k); return v == null ? d : JSON.parse(v); } catch (e) { return d; } }
  function lsSet(k, v) { try { localStorage.setItem(k, JSON.stringify(v)); } catch (e) {} }

  /* ============================================================
     单窗口壳联动（v2）：多实例聚合
     壳给每个实例一个 iframe（登录态按 origin 隔离），这里只负责上报
     房间/未读数，并响应「打开某个房间」。独立打开（手机/浏览器）时全部空转。
     ============================================================ */
  var HOST_IID = (function () {
    var m = location.search.match(/[?&]iid=([^&]+)/);
    var v = m ? decodeURIComponent(m[1]) : '';
    return v || '';
  })();
  var HOST_REF = '';
  try { if (document.referrer) HOST_REF = new URL(document.referrer).origin; } catch (e) {}
  var INST_NAME = '';
  var hostTimer = null;

  function embeddedInShell() { return window.parent && window.parent !== window; }

  function hostPost(obj) {
    if (!embeddedInShell()) return;
    obj.v = 2;
    obj.iid = HOST_IID;
    try { window.parent.postMessage(obj, HOST_REF || '*'); } catch (e) {}
  }

  function hostRooms() {
    return state.rooms.map(function (r) {
      return { slug: r.slug, title: r.title, kind: r.kind,
               unread: state.unread[r.slug] || 0,
               muted: !!state.muted[r.slug],
               lastTs: state.lastTs[r.slug] || 0 };
    });
  }

  function postHostState() {
    if (!embeddedInShell()) return;
    hostPost({ type: 'pubchat-host-state', name: INST_NAME, connected: !!state.connected,
               active: state.active, rooms: hostRooms(), notices: hostNotices() });
  }

  /* 公告历史存在实例自己的源上（对外口），外壳的「关于」是另一个源读不到，
     所以顺着这条已经在跑的状态回包把它带上去，壳里不用另开一次跨源请求。 */
  function hostNotices() {
    return window.PubChatNotice ? PubChatNotice.list().slice(0, 5) : [];
  }

  /* 未读数变化很密集（每来一条消息都变），节流 300ms 再报，别刷爆壳 */
  function scheduleHostState(now) {
    if (!embeddedInShell()) return;
    if (now) {
      clearTimeout(hostTimer); hostTimer = null;
      postHostState();
      return;
    }
    if (hostTimer) return;
    hostTimer = setTimeout(function () { hostTimer = null; postHostState(); }, 300);
  }

  /* 实例名从公网口的只读接口取；取不到就用页面标题，不阻塞任何流程 */
  (function loadInstanceName() {
    var prefix = location.pathname.replace(/\/[^\/]*$/, '');
    fetch(prefix + '/api/instance', { cache: 'no-store' })
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (j) { if (j && j.ok && j.name) { INST_NAME = j.name; scheduleHostState(); } })
      .catch(function () {});
  })();

  window.addEventListener('message', function (e) {
    if (!embeddedInShell() || e.source !== window.parent) return;
    var d = e.data || {};
    if (d.type === 'pubchat-host-hello') { scheduleHostState(true); return; }
    if (d.type === 'pubchat-host-me') { openMe(); hostPost({ type: 'pubchat-host-me-ok' }); return; }
    if (d.type === 'pubchat-open-mgmt') { openBind(); hostPost({ type: 'pubchat-open-mgmt-ok' }); return; }
    if (d.type === 'pubchat-open-add') { openConnect(); hostPost({ type: 'pubchat-open-add-ok' }); return; }
    if (d.type === 'pubchat-open-room' && d.room) {
      var ok = !!findRoom(d.room);
      if (ok) selectRoom(d.room);
      hostPost({ type: 'pubchat-room-opened', room: d.room, ok: ok });
    }
  });

  /* —— 已读游标：按登录名分开存，同一浏览器换账号不串 —— */
  var readSaveTimer = null;
  function readKey() { return LS_READ + (state.lastUser || ''); }
  function loadRead() { state.lastRead = lsGet(readKey(), {}) || {}; }
  function saveRead() {
    clearTimeout(readSaveTimer);
    readSaveTimer = setTimeout(function () { lsSet(readKey(), state.lastRead); }, 400);
  }
  function markRead(room, id) {
    if (!room || id == null) return;
    if ((state.lastRead[room] || 0) >= id) return;
    state.lastRead[room] = id;
    saveRead();
  }

  /* ---------- 连接 ---------- */
  function setConn(ok, txt) { $('#connDot').className = 'dot ' + (ok ? 'on' : 'off'); $('#connText').textContent = txt; }

  function connect() {
    clearTimeout(state.retryTimer);
    setConn(false, '连接中…');
    var ws;
    try { ws = new WebSocket(cfg.ws); } catch (e) { showBanner('连接失败：' + e.message); return; }
    state.ws = ws;

    ws.onopen = function () {
      if (ws !== state.ws) return;
      state.lastPong = Date.now();
      loadRead();                       // 带上各房间读指针，服务端据此算未读数
      ws.send(JSON.stringify({ type: 'auth', kind: 'user',
        username: state.lastUser, key: state.lastKey, last_read: state.lastRead }));
    };
    ws.onmessage = function (ev) {
      if (ws !== state.ws) return;
      var f; try { f = JSON.parse(ev.data); } catch (e) { return; }
      handle(f);
    };
    ws.onclose = function () {
      if (ws !== state.ws) return;
      state.connected = false;
      scheduleHostState();
      if (state.reconnectNow) {
        state.reconnectNow = false; state.retry = 0;
        setTimeout(connect, 300);
        return;
      }
      if (state.me) { setConn(false, '已断开，重连中…'); scheduleRetry(); }
    };
    ws.onerror = function () { try { ws.close(); } catch (e) {} };
  }

  function scheduleRetry() {
    state.retry = Math.min(state.retry + 1, 6);
    var wait = Math.min(1000 * Math.pow(1.6, state.retry), 15000);
    clearTimeout(state.retryTimer);
    state.retryTimer = setTimeout(connect, wait);
    setConn(false, '断开，' + Math.round(wait / 1000) + 's 后重试');
  }

  /* iOS/Android 软键盘会压住底部输入框：按可视视口收紧聊天区高度 */
  (function () {
    var vv = window.visualViewport;
    if (!vv) return;
    var app = $('#app');
    function fit() {
      app.style.height = vv.height + 'px';
      app.style.transform = vv.offsetTop ? 'translateY(' + vv.offsetTop + 'px)' : '';
    }
    vv.addEventListener('resize', fit);
    vv.addEventListener('scroll', fit);
    fit();
  })();

  /* 手机切回前台：系统常把旧 socket 留成“假在线”，发个 ping 确认，没回音就重连 */
  document.addEventListener('visibilitychange', function () {
    if (document.visibilityState !== 'visible' || !state.me) return;
    if (!state.ws || state.ws.readyState !== 1) {
      state.retry = 0;
      clearTimeout(state.retryTimer);
      connect();
      return;
    }
    var seen = state.lastPong;
    send({ type: 'ping' });
    setTimeout(function () {
      if (document.visibilityState !== 'visible' || !state.me) return;
      if (state.lastPong === seen) { try { state.ws.close(); } catch (e) {} }
    }, 4000);
  });

  function send(o) {
    if (!state.ws || state.ws.readyState !== 1) return false;
    state.ws.send(JSON.stringify(o)); return true;
  }

  /* ---------- 帧 ---------- */
  function handle(f) {
    if (f.type === 'pong') { state.lastPong = Date.now(); return; }
    if (f.type === 'authenticated') {
      state.connected = true; state.retry = 0;
      state.me = f.me; state.rooms = f.rooms || []; state.agents = f.agents || [];
      state.agentModes = f.agent_modes || [];
      localStorage.setItem(LS_USER, state.lastUser);
      localStorage.setItem(LS_KEY, state.lastKey);
      setConn(true, '已连接');
      hideBanner();
      var av = $('#myAv'); av.textContent = initialOf(state.me.name);
      av.style.background = colorOf(state.me.client_id);
      $('#myName').textContent = state.me.name;
      $('#app').hidden = false;
      $('#login').classList.add('hidden');
      applyServerUnread();
      seedLastTs();
      renderSidebar();
      splashDone();
      if (state.selectAfterRooms && findRoom(state.selectAfterRooms)) {
        var pend = state.selectAfterRooms; state.selectAfterRooms = '';
        selectRoom(pend);                     // 刚开的智能体私聊房：列表一到就切过去
      } else if (!state.active) {
        var first = state.rooms.filter(function (r) { return r.kind === 'lobby'; })[0] || state.rooms[0];
        if (first) selectRoom(first.slug);
      } else {
        loadRoom(state.active);         // 重连后把断线期间漏的消息补上
      }
      if (state.me.kind === 'human') {
        if (state.me.onboarded === false) {
          state.firstRunGuide = true;         // 资料设完（或明确稍后）才弹引导
          openProfile(true);
        } else if (scanned && scanned.first) {
          /* 部署完成弹窗点「进入聊天室」：直接进改名字/改密码环节，不弹「加入聊天室」教程。
             只弹一次：保存资料后服务端会重新推 ready，不清标记会无限重复弹。 */
          scanned.first = false;
          openProfile(true);
        } else if (!state.guideShown && !guideSeen()) {
          /* 本机部署的房主在 /api/setup 里就是 onboarded 的：资料没什么要重填，
             但他一次也没看过四步引导——这台设备第一次进聊天时补上。 */
          state.guideShown = true;
          openGuide();
        }
      }
      scheduleHostState();
      return;
    }
    if (f.type === 'presence') {
      state.members = {}; state.online = {};
      (f.members || []).forEach(function (m) { state.members[m.client_id] = m; if (m.online) state.online[m.client_id] = true; });
      renderSidebar();
      return;
    }
    if (f.type === 'message') { addMessage(f.room, f); return; }
    if (f.type === 'history') {
      (f.messages || []).forEach(function (m) {
        addMessage(f.room, { id: m.id, room: f.room, from: m.from, sender_name: m.sender_name,
          content: m.content, mentions: m.mentions, ts: m.ts }, true);
      });
      if (f.room === state.active) renderStream();
      return;
    }
    if (f.type === 'rooms-changed') {
      send({ type: 'rooms', last_read: state.lastRead });
      return;
    }
    if (f.type === 'room_created') {
      closeSheet('roomSheet');
      applyRooms(f.rooms);
      if (f.room && f.room.slug) selectRoom(f.room.slug);
      toast('房间已创建');
      return;
    }
    if (f.type === 'room_renamed') {
      closeSheet('roomSheet');
      applyRooms(f.rooms);
      toast('房间名已更新');
      return;
    }
    if (f.type === 'agent_dm_opened') {
      // 服务端随后广播 rooms-changed；这里只认领"我发起的那一次"，
      // 别人家智能体开的房（我作为主人也会收到）不能抢我的视图。
      if (state.pendingAgentDm) {
        var want = state.pendingAgentDm.split('|');
        if (want.indexOf(f.a) < 0 || want.indexOf(f.b) < 0) return;
        // 对不上的一律不清 pending：别人家的回执抢先清掉钩子，自己那条随后到达时
        // 就没主了，弹层一直挂着不跳页。与手机 home.vue 同一口径。
        state.pendingAgentDm = '';
        closeSheet('agentDmSheet');
        if (f.room) {
          state.selectAfterRooms = f.room;      // 房间列表要等 rooms 回包，先记着
          send({ type: 'rooms', last_read: state.lastRead });
          toast(f.created ? '私聊房已建好' : '已回到之前的私聊房');
        }
      }
      return;
    }
    if (f.type === 'room_archived' || f.type === 'room_left') {
      closeSheet('roomMenuSheet');
      applyRooms(f.rooms);
      if (state.active === f.room) selectRoom('lobby');
      toast(f.type === 'room_archived' ? '房间已归档' : '已退出房间');
      return;
    }
    if (f.type === 'room_invites') {
      renderRoomInvites(f);
      return;
    }
    if (f.type === 'account') {
      if (!f.ok) return;
      state.lastUser = f.username; state.lastKey = f.key;
      localStorage.setItem(LS_USER, f.username);
      localStorage.setItem(LS_KEY, f.key);
      if (state.me) { state.me.client_id = f.username; state.me.name = f.name; state.me.onboarded = true; }
      $('#myName').textContent = f.name;
      var a2 = $('#myAv'); a2.textContent = initialOf(f.name); a2.style.background = colorOf(f.username);
      closeProfile();
      toast('已保存，正在重新登录…');
      state.reconnectNow = true;
      try { state.ws.close(); } catch (e) { state.reconnectNow = false; connect(); }
      return;
    }
    if (f.type === 'agent_renamed') {
      state.agents.forEach(function (a) { if (a.client_id === f.client_id) a.name = f.name; });
      if (!$('#bindSheet').classList.contains('hidden')) openBind();
      toast('已改名：' + f.name);
      return;
    }
    if (f.type === 'agent_created') {
      if (f.agent && f.agent.client_id
          && !state.agents.some(function (a) { return a.client_id === f.agent.client_id; })) {
        if (!f.agent.mode) f.agent.mode = 'online';
        state.agents.push(f.agent);
      }
      if (!$('#bindSheet').classList.contains('hidden')) openBind();
      var madeAgent = f.agent;
      if (state.connectPending && madeAgent && madeAgent.client_id) {
        state.connectPending = '';
        var go = $('#connectGo');
        if (go) { go.disabled = false; go.textContent = '生成接入指令'; }
        showConnectResult(madeAgent);
      }
      toast('已加一个智能体系位：' + ((madeAgent && madeAgent.name) || ''));
      return;
    }
    if (f.type === 'agent_mode') {
      state.agents.forEach(function (a) {
        if (a.client_id !== f.client_id) return;
        a.mode = f.mode; a.mode_label = f.mode_label;
        if (f.prompts) a.prompts = f.prompts;
      });
      if (!$('#bindSheet').classList.contains('hidden')) openBind();
      renderSidebar();
      toast('接入方式已切换：' + (f.mode_label || f.mode));
      return;
    }
    if (f.type === 'agent_key_rotated') {
      (state.agents || []).forEach(function (a) { if (a.client_id === f.client_id) a.key2 = f.key2; });
      if (!$('#bindSheet').classList.contains('hidden')) openBind();
      toast('二级 Key 已更换，旧 Key 立刻失效');
      return;
    }
    if (f.type === 'agent_removed') {
      state.agents = state.agents.filter(function (a) { return a.client_id !== f.client_id; });
      if (!$('#bindSheet').classList.contains('hidden')) openBind();
      toast('已删除智能体系位：' + (f.client_id || ''));
      return;
    }
    if (f.type === 'onboarded') return;
    if (f.type === 'error') {
      if (f.code === 'BAD_LOGIN' || f.code === 'BAD_AGENT_KEY') {
        setConn(false, '登录失败');
        state.me = null;
        localStorage.removeItem(LS_KEY);
        $('#login').classList.remove('hidden');
        $('#app').hidden = true;
        $('#loginErr').textContent = f.message || '用户名或 Key 不对';
        try { state.ws.close(); } catch (e) {}
        return;
      }
      if (!state.me) return;
      if (state.pendingAgentDm && /^(AGENT_DM_AMBIGUOUS|AGENT_DM_SELF|NO_AGENT|NO_PERM|AGENT_LIMIT)$/.test(f.code || '')) {
        state.pendingAgentDm = '';            // 开房失败只提示一次，不留一条常驻横幅
        toast(f.message || f.code);
        return;
      }
      if ($('#profileSheet').classList.contains('hidden') === false) {
        $('#pfErr').textContent = f.message || f.code; $('#pfSave').disabled = false; return;
      }
      showBanner('服务端：' + (f.message || f.code));
      return;
    }
  }

  /* ---------- 消息 ---------- */
  function addMessage(room, m, quiet) {
    if (!room || m.id == null) return;
    if (m.ts) {
      var t = Date.parse(m.ts);
      if (t && t > (state.lastTs[room] || 0)) { state.lastTs[room] = t; lsSet(LS_LAST, state.lastTs); }
    }
    if (!state.msgs[room]) state.msgs[room] = [];
    if (!state.seen[room]) state.seen[room] = {};
    if (state.seen[room][m.id]) return;
    state.seen[room][m.id] = true;
    state.msgs[room].push(m);
    state.msgs[room].sort(function (a, b) { return a.id - b.id; });
    var ro = findRoom(room);              // 房间的 last_id 要跟着新消息走，否则"标为已读"会停在登录那一刻
    if (ro && (ro.last_id == null || m.id > ro.last_id)) ro.last_id = m.id;
    if (room === state.active) { markRead(room, m.id); if (!quiet) renderStream(); }
    else if (!quiet) { state.unread[room] = (state.unread[room] || 0) + 1; renderSidebar(); }
    if (!quiet) { notifyMine(room, m); scheduleHostState(); }
  }

  /* 与「我」有关的消息（@我 / 私聊 / 智能体房）→ 交给壳去弹提醒。
     规则：自己发的不算；聊天页正开着这个房间且窗口有焦点时不打扰；
     公共大厅只有 @到我 才算。 */
  function notifyMine(room, m) {
    var me = state.me && state.me.client_id;
    if (!me || !m || !m.from || m.from === me) return;
    if (room === state.active && document.hasFocus()) return;
    var mentioned = (m.mentions || []).indexOf(me) >= 0;
    var privateish = /^(dm|own|adm)-/.test(room || '');
    if (!mentioned && !privateish) return;
    hostPost({
      type: 'pubchat-notify',
      title: (m.sender_name || m.from) + (mentioned ? ' @了你' : ' 发来消息'),
      body: String(m.content || '').replace(/\s+/g, ' ').slice(0, 120),
      room: room
    });
  }

  /* ============================================================
     左侧会话分组
     ============================================================ */
  function groupsCfg() {
    var g = (cfg.groups && cfg.groups.length) ? cfg.groups : DEFAULT_GROUPS;
    return g.map(function (x, i) {
      return { id: x.id || ('g' + i), label: x.label || x.id || '分组',
               rooms: x.rooms || ['*'], roster: x.roster || null,
               defaultOpen: x.defaultOpen, agentDm: !!x.agentDm };
    });
  }

  function groupOf(kind) {
    var gs = groupsCfg(), star = null;
    for (var i = 0; i < gs.length; i++) {
      if (gs[i].rooms.indexOf(kind) >= 0) return gs[i].id;
      if (gs[i].rooms.indexOf('*') >= 0 && !star) star = gs[i].id;
    }
    return star || (gs.length ? gs[gs.length - 1].id : 'other');
  }

  function loadSideState() {
    state.pinned = lsGet(LS_PIN, {}) || {};
    state.muted = lsGet(LS_MUTE, {}) || {};
    state.order = lsGet(LS_ORDER, {}) || {};
    state.lastTs = lsGet(LS_LAST, {}) || {};
    var saved = lsGet(LS_COLLAPSE, null);
    var c = {};
    groupsCfg().forEach(function (g) {
      var defOpen = (typeof g.defaultOpen === 'boolean') ? g.defaultOpen : (cfg.groupsDefaultExpanded !== false);
      c[g.id] = saved && typeof saved[g.id] === 'boolean' ? saved[g.id] : !defOpen;
    });
    state.collapsed = c;
  }

  /* 服务端 authenticated 里带的 last_ts 是权威值，比本地记的准，取大的那个 */
  function seedLastTs() {
    var changed = false;
    state.rooms.forEach(function (r) {
      if (!r.last_ts) return;
      var t = Date.parse(r.last_ts);
      if (t && t > (state.lastTs[r.slug] || 0)) { state.lastTs[r.slug] = t; changed = true; }
    });
    if (changed) lsSet(LS_LAST, state.lastTs);
  }

  /* 服务端按读指针算的未读数是权威值；没有游标的房间以当前最新一条为基线（视为已读） */
  function applyServerUnread() {
    state.unread = {};
    state.rooms.forEach(function (r) {
      if (state.lastRead[r.slug] == null) {
        // 没游标的房间以当前最新一条为基线；空房间记 0，这样它以后来的第一条才算未读
        state.lastRead[r.slug] = r.last_id != null ? r.last_id : 0;
        saveRead();
      } else if (r.unread) {
        state.unread[r.slug] = r.unread;
      }
    });
    scheduleHostState();
  }

  function convList() {
    return state.rooms.map(function (r) {
      return {
        slug: r.slug, title: r.title, kind: r.kind,
        group: groupOf(r.kind),
        lastTs: state.lastTs[r.slug] || 0,
        unread: state.unread[r.slug] || 0,
        pinned: !!state.pinned[r.slug],
        muted: !!state.muted[r.slug]
      };
    });
  }

  /* 名单组：我的智能体 / 全部成员。不是会话，不参与未读、排序和拖拽 */
  function rosterList(kind) {
    var out = [], me = state.me && state.me.client_id;
    Object.keys(state.members).forEach(function (id) {
      var m = state.members[id];
      if (!m) return;
      if (kind === 'agents') {
        if (m.kind !== 'agent') return;
        if (m.owner && me && m.owner !== me) return;   // 只看自己名下的
      } else if (m.kind === 'agent') return;           // 成员组只列人
      out.push({ id: id, name: m.name || id, kind: m.kind, owner: m.owner,
                 online: !!state.online[id], me: id === me });
    });
    out.sort(function (a, b) {
      if (a.online !== b.online) return a.online ? -1 : 1;   // 在线的排前面
      return String(a.name).localeCompare(String(b.name));
    });
    return out;
  }

  /* 和某人对聊的房号（与服务端 pair() 一致：两个 client_id 排序后拼接） */
  function dmSlugWith(id) {
    var me = state.me && state.me.client_id;
    if (!me || !id || me === id) return null;
    var p = [me, id].sort();
    return 'dm-' + p[0] + '-' + p[1];
  }

  /* 组内顺序：置顶 → 拖拽过的顺序 → 最近活跃时间倒序 → 标题 */
  function sortConv(gid, list) {
    var ord = state.order[gid] || [], idx = {};
    ord.forEach(function (s, i) { idx[s] = i; });
    return list.slice().sort(function (a, b) {
      if (a.pinned !== b.pinned) return a.pinned ? -1 : 1;
      var ia = idx[a.slug], ib = idx[b.slug];
      if (ia != null && ib != null && ia !== ib) return ia - ib;
      if (ia != null && ib == null) return -1;
      if (ia == null && ib != null) return 1;
      if (b.lastTs !== a.lastTs) return b.lastTs - a.lastTs;
      return String(a.title).localeCompare(String(b.title));
    });
  }

  function touchDragDisabled() {
    if (cfg.dragOnTouch === true) return false;
    return !!(window.matchMedia && window.matchMedia('(hover: none)').matches);
  }

  function renderSidebar() {
    var host = $('#sideList');
    if (!host) return;
    var q = String(state.query || '').trim().toLowerCase();
    var searching = !!q;
    var all = convList();
    var gs = groupsCfg();
    var shown = 0;
    host.textContent = '';

    gs.forEach(function (g) {
      var isRoster = !!g.roster;
      // 会话组按 room.kind 归组；名单组按成员表取
      var mine = isRoster ? rosterList(g.roster)
                          : all.filter(function (c) { return c.group === g.id; });
      var list = searching
        ? mine.filter(function (c) { return String(c.name || c.title).toLowerCase().indexOf(q) >= 0; })
        : mine;
      if (searching && !list.length) return;                       // 搜索时没命中的组整组不显示
      // 空组默认不显示；但带开房入口的那组必须留着，否则第一次没房可点，入口也就没了
      if (!searching && !list.length && cfg.hideEmptyGroups !== false && !g.agentDm) return;
      shown += list.length;

      var open = searching ? true : !state.collapsed[g.id];        // 搜索时强制展开
      var grp = el('div', 'grp' + (open ? ' open' : '') + (isRoster ? ' roster-grp' : ''));
      grp.dataset.gid = g.id;

      var activeConv = isRoster ? null
        : mine.filter(function (c) { return c.slug === state.active; })[0];
      var hasActive = !isRoster && !!activeConv;
      var unread = isRoster ? 0
        : mine.reduce(function (s, c) { return s + (c.muted ? 0 : c.unread); }, 0);

      var h = el('button', 'grp-h');
      h.type = 'button';
      h.setAttribute('aria-expanded', open ? 'true' : 'false');
      h.append(el('span', 'chev', '›'), el('span', 'grp-t', g.label));
      if (!open && hasActive && activeConv) h.append(el('span', 'grp-active', activeConv.title)); // 折叠了也要看得见当前会话
      if (!open && isRoster && mine.length) h.append(el('span', 'grp-active', rosterHint(g.roster, mine)));
      h.append(el('span', 'grp-n', String(searching ? list.length : mine.length)));
      if (unread) h.append(el('span', 'grp-un', unread > 99 ? '99+' : String(unread)));
      h.onclick = function () {
        if (searching) { toast('清空搜索后可以折叠'); return; }
        state.collapsed[g.id] = !state.collapsed[g.id];
        lsSet(LS_COLLAPSE, state.collapsed);
        renderSidebar();
      };
      if (g.agentDm) {
        // 入口挂在分组头上；不能嵌进 h（button 里再套 button 是非法 DOM，浏览器会拆结构）
        var head = el('div', 'grp-head');
        var add = el('button', 'grp-add');
        add.type = 'button';
        add.id = 'agentDmAdd';
        add.textContent = '＋';
        add.title = '和智能体单聊';
        add.setAttribute('aria-label', '和智能体单聊');
        add.onclick = openAgentDmSheet;
        head.append(h, add);
        grp.append(head);
      } else {
        grp.append(h);
      }

      if (open) grp.append(isRoster ? rosterItems(list, g.id) : convItems(sortConv(g.id, list), g.id));
      host.append(grp);
    });

    if (searching && !shown) host.append(el('div', 'side-empty', '没有匹配的会话'));
    if (!searching && !shown) host.append(el('div', 'side-empty', '还没有会话'));
  }

  function convItems(list, gid) {
    var box = el('div', 'items');
    box.dataset.gid = gid;
    if (!list.length) { box.append(el('div', 'item ghost', '（暂无）')); return box; }
    var noDrag = touchDragDisabled();
    list.forEach(function (c) {
      var cls = 'item' + (c.slug === state.active ? ' on' : '') + (c.pinned ? ' pinned' : '');
      var row = el('div', cls);
      row.dataset.slug = c.slug;
      if (!noDrag) row.draggable = true;
      if (c.pinned) { var pd = el('i', 'pindot'); pd.title = '已置顶'; row.append(pd); }
      var av = el('span', 'av', initialOf(c.title));
      av.style.background = c.kind === 'dm' ? colorOf(c.slug) : '#64748b';
      row.append(av, el('span', 'nm', c.title));
      if (c.unread) {
        if (c.muted) { var md = el('i', 'mutedot'); md.title = '有未读（免打扰）'; row.append(md); }
        else row.append(el('span', 'badge', c.unread > 99 ? '99+' : String(c.unread)));
      }
      row.onclick = function () { selectRoom(c.slug); };
      row.oncontextmenu = function (e) { e.preventDefault(); openCtx(e, c); };
      if (!noDrag) bindDrag(row, c, gid);
      box.append(row);
    });
    return box;
  }

  /* 折叠的名单组：标题右侧补一句在线情况，别只剩一个数字 */
  function rosterHint(kind, list) {
    var on = list.filter(function (m) { return m.online; }).length;
    return on + '/' + list.length + ' 在线';
  }

  /* 名单行：圆点=在线；点人进私聊，点智能体往输入框里 @ 它 */
  function rosterItems(list, gid) {
    var box = el('div', 'items roster');
    box.dataset.gid = gid;
    if (!list.length) { box.append(el('div', 'item ghost', '（暂无）')); return box; }
    var me = state.me && state.me.client_id;
    list.forEach(function (m) {
      var row = el('div', 'item' + (m.id === me ? ' me' : ''));
      row.dataset.cid = m.id;
      var av = el('span', 'av', initialOf(m.name));
      av.style.background = colorOf(m.id);
      row.append(av, el('span', 'nm', m.name + (m.kind === 'agent' ? ' · 助手' : '')));
      var mine = null;
      if (m.kind === 'agent') {
        mine = state.agents.filter(function (a) { return a.client_id === m.id; })[0] || null;
        if (mine) {
          var mb = el('span', 'roster-mode', MODE_SHORT[modeOf(mine)] || '');
          mb.title = '接入方式：' + modeLabelOf(mine) + '（点这里切换）';
          mb.onclick = function (e) { e.stopPropagation(); openBind(); };
          row.append(mb);
        }
      }
      row.append(el('i', 'dot' + (m.online ? ' on' : '')));
      if (m.kind === 'agent') {
        row.title = '点一下在输入框 @ ' + m.name + (mine ? '；右侧徽标可切换接入方式' : '');
        row.onclick = function () { insert('@' + m.name + ' '); toast('已插入 @' + m.name); };
      } else if (m.id !== me) {
        row.title = '和 ' + m.name + ' 私聊';
        row.onclick = function () {
          var slug = dmSlugWith(m.id);
          if (slug && findRoom(slug)) selectRoom(slug);
          else toast('你们还没有私聊房间');
        };
      } else {
        row.title = '这是你自己';
      }
      box.append(row);
    });
    return box;
  }

  /* —— 拖拽排序（组内） —— */
  var drag = { slug: null, gid: null };

  function clearDropHints() {
    [].forEach.call(document.querySelectorAll('.item.drop-before,.item.drop-after'), function (n) {
      n.classList.remove('drop-before', 'drop-after');
    });
  }

  function bindDrag(row, c, gid) {
    row.addEventListener('dragstart', function (e) {
      drag.slug = c.slug; drag.gid = gid;
      row.classList.add('dragging');
      if (e.dataTransfer) { e.dataTransfer.effectAllowed = 'move'; try { e.dataTransfer.setData('text/plain', c.slug); } catch (x) {} }
    });
    row.addEventListener('dragend', function () {
      row.classList.remove('dragging'); drag.slug = null; drag.gid = null; clearDropHints();
    });
    row.addEventListener('dragover', function (e) {
      if (!drag.slug || drag.gid !== gid || drag.slug === c.slug) return;
      e.preventDefault();
      if (e.dataTransfer) e.dataTransfer.dropEffect = 'move';
      var r = row.getBoundingClientRect();
      clearDropHints();
      row.classList.add(e.clientY < r.top + r.height / 2 ? 'drop-before' : 'drop-after');
    });
    row.addEventListener('drop', function (e) {
      if (!drag.slug || drag.gid !== gid || drag.slug === c.slug) return;
      e.preventDefault();
      var r = row.getBoundingClientRect();
      moveInGroup(gid, drag.slug, c.slug, e.clientY >= r.top + r.height / 2);
    });
  }

  function moveInGroup(gid, from, to, after) {
    var box = document.querySelector('.items[data-gid="' + gid + '"]');
    if (!box) return;
    var order = [].map.call(box.querySelectorAll('.item[data-slug]'), function (n) { return n.dataset.slug; });
    var fi = order.indexOf(from);
    if (fi < 0) return;
    order.splice(fi, 1);
    var ti = order.indexOf(to);
    if (ti < 0) return;
    order.splice(after ? ti + 1 : ti, 0, from);
    state.order[gid] = order;
    lsSet(LS_ORDER, state.order);
    renderSidebar();
  }

  /* —— 右键菜单 —— */
  function closeCtx() { var m = document.getElementById('ctxMenu'); if (m) m.remove(); }

  function openCtx(e, c) {
    closeCtx();
    var m = el('div', 'ctx'); m.id = 'ctxMenu';
    function item(label, fn, danger) {
      var b = el('button', danger ? 'danger' : '', label);
      b.type = 'button';
      b.onclick = function (ev) { ev.stopPropagation(); closeCtx(); fn(); };
      m.append(b);
    }
    item(c.pinned ? '取消置顶' : '置顶', function () {
      if (c.pinned) delete state.pinned[c.slug]; else state.pinned[c.slug] = 1;
      lsSet(LS_PIN, state.pinned); renderSidebar();
    });
    item(c.muted ? '取消免打扰' : '免打扰', function () {
      if (c.muted) delete state.muted[c.slug]; else state.muted[c.slug] = 1;
      lsSet(LS_MUTE, state.muted); renderSidebar();
    });
    item('标为已读', function () { markReadLatest(c.slug); });
    document.body.append(m);
    var r = m.getBoundingClientRect();
    m.style.left = Math.max(4, Math.min(e.clientX, window.innerWidth - r.width - 6)) + 'px';
    m.style.top = Math.max(4, Math.min(e.clientY, window.innerHeight - r.height - 6)) + 'px';
  }

  /* ---------- 房间 ---------- */
  function selectRoom(slug) {
    state.active = slug; state.unread[slug] = 0;
    clearReply();   // 引用不跨房：服务端只认同房消息，换房留着旧引用只会发不出去
    var r = findRoom(slug);
    var gid = groupOf(r ? r.kind : '');
    if (state.collapsed[gid]) {            // 进到折叠组里的会话：自动展开
      state.collapsed[gid] = false;
      lsSet(LS_COLLAPSE, state.collapsed);
    }
    $('#app').classList.add('open');
    $('#roomTitle').textContent = r ? r.title : slug;
    renderSidebar(); renderStream(true);
    scheduleHostState();
    loadRoom(slug);
  }

  function findRoom(s) { for (var i = 0; i < state.rooms.length; i++) if (state.rooms[i].slug === s) return state.rooms[i]; return null; }
  function roomLastId(s) { var r = findRoom(s); return r && r.last_id != null ? r.last_id : null; }

  /* ---------- 房间 v2：建房 / 改名 / 归档 / 邀请 / 退房 ---------- */
  function applyRooms(rooms) {
    state.rooms = rooms || [];
    renderSidebar();
    scheduleHostState();
  }

  function activeRoom() { return findRoom(state.active); }

  function closeSheet(id) {
    var n = $('#' + id); if (n) n.classList.add('hidden');
    // 引导有四条关闭路径（下一步到底 / 跳过 / ✕ / 点背景与返回），全走这里落本机标记
    if (id === 'guideSheet') { try { localStorage.setItem(LS_GUIDE, '1'); } catch (e) {} }
  }

  function openRoomSheet(mode, room) {
    if (!state.me) { toast('请先登录'); return; }
    state.roomSheetMode = mode;
    state.roomEditing = room ? room.slug : '';
    $('#roomSheetTitle').textContent = mode === 'rename' ? '改房间名' : '创建房间';
    $('#roomSheetTip').textContent = mode === 'rename'
      ? '改名后成员会立刻看到新名字。'
      : '房间默认仅邀请。创建后你是房主，可生成一次性邀请链接给朋友。';
    $('#roomName').value = mode === 'rename' ? (room && room.title || '') : '';
    $('#roomSubmit').textContent = mode === 'rename' ? '保存' : '创建';
    $('#roomSheet').classList.remove('hidden');
    setTimeout(function () { $('#roomName').focus(); }, 60);
  }

  function submitRoom() {
    var name = ($('#roomName').value || '').trim();
    if (name.length < 2) { toast('房间名至少 2 个字'); return; }
    if (state.roomSheetMode === 'rename') {
      if (!send({ type: 'room_rename', room: state.roomEditing, title: name })) {
        toast('未连接');
      }
    } else if (!send({ type: 'room_create', title: name })) {
      toast('未连接');
    }
  }

  /* ---------- S2 智能体⇄智能体开房入口 ---------- */
  function agentOptions() {
    // 我的席位排在前面；别人名下的智能体也列出来（自己两个席位互聊同样要能开）
    var mine = {}, out = [];
    (state.agents || []).forEach(function (a) {
      if (!a || !a.client_id || mine[a.client_id]) return;
      mine[a.client_id] = true;
      out.push({ client_id: a.client_id, name: a.name || a.client_id, mine: true });
    });
    Object.keys(state.members || {}).forEach(function (id) {
      var m = state.members[id];
      if (!m || m.kind !== 'agent' || mine[id]) return;
      out.push({ client_id: id, name: m.name || id, mine: false,
                 owner: m.owner_name || m.owner || '' });
    });
    return out;
  }

  function fillAgentSelect(sel, items, withMine) {
    sel.innerHTML = '';
    items.forEach(function (a) {
      var o = document.createElement('option');
      o.value = a.client_id;
      o.textContent = a.name + (a.mine ? '（我的）' : (a.owner ? ' · ' + a.owner : ''));
      sel.appendChild(o);
    });
    if (withMine) {
      var first = items.filter(function (a) { return a.mine; })[0];
      if (first) sel.value = first.client_id;
    }
  }

  function openAgentDmSheet() {
    if (!state.me) { toast('请先登录'); return; }
    var opts = agentOptions();
    if (!opts.filter(function (a) { return a.mine; }).length) {
      toast('你还没有智能体席位，先去「我的 → 智能体」绑定一个'); return;
    }
    if (opts.length < 2) { toast('这个实例里还没有第二个智能体'); return; }
    fillAgentSelect($('#agentDmMine'), opts, true);
    fillAgentSelect($('#agentDmPeer'), opts, false);
    var peer = opts.filter(function (a) { return !a.mine; })[0] || opts[1];
    $('#agentDmPeer').value = peer.client_id;
    $('#agentDmSheet').classList.remove('hidden');
  }

  function submitAgentDm() {
    var own = $('#agentDmMine').value, peer = $('#agentDmPeer').value;
    if (!own || !peer) { toast('两个都要选'); return; }
    if (own === peer) { toast('不能和自己的席位私聊'); return; }
    // own_client_id 一直带上：多席位时服务端不带上会回 AGENT_DM_AMBIGUOUS
    if (!send({ type: 'agent_dm_open', client_id: peer, own_client_id: own })) {
      toast('未连接'); return;
    }
    state.pendingAgentDm = own + '|' + peer;
    toast('正在开房…');
  }

  function openRoomMenu() {
    var r = activeRoom();
    if (!r) { toast('先选一个房间'); return; }
    if (r.kind !== 'shared') { toast('大厅和个人房不需要管理'); return; }
    var me = state.me && state.me.client_id;
    var isOwner = !!(r.owner && r.owner === me) ||
                  !!(state.me && state.me.role === 'owner');
    $('#roomMenuTitle').textContent = r.title || '房间';
    $('#roomInviteBtn').hidden = !isOwner;
    $('#roomRenameBtn').hidden = !isOwner;
    $('#roomArchiveBtn').hidden = !isOwner;
    $('#roomLeaveBtn').hidden = isOwner;
    $('#roomMenuSheet').classList.remove('hidden');
  }

  var inviteHours = 24;
  function setInviteHours(h) {
    inviteHours = h;
    var box = $('#roomInviteHours');
    if (box) {
      Array.prototype.forEach.call(box.querySelectorAll('.chip'), function (b) {
        b.classList.toggle('on', b.getAttribute('data-hours') === String(h));
      });
    }
    var tip = $('#roomInviteTip');
    if (tip) {
      tip.textContent = h === 0
        ? '每条链接一次性、长期有效（不过期）。对方打开后填名字和登录名即可加入并自动进房。'
        : '每条链接一次性、' + h + ' 小时有效。对方打开后填名字和登录名即可加入并自动进房。';
    }
  }

  function openRoomInvite() {
    var r = activeRoom();
    if (!r || r.kind !== 'shared') return;
    $('#roomInviteList').textContent = '';
    setInviteHours(inviteHours);
    $('#roomInviteSheet').classList.remove('hidden');
  }

  function inviteRow(url) {
    var row = el('div', 'room-invite');
    var a = el('input', 'room-invite-url');
    a.value = url; a.readOnly = true;
    var btns = el('div', 'room-invite-btns');
    var copy = el('button', 'ghost', '复制');
    copy.type = 'button';
    copy.onclick = function () {
      try { a.select(); document.execCommand('copy'); toast('已复制'); }
      catch (e) { toast('复制失败'); }
    };
    btns.append(copy);
    if (typeof qrcode === 'function') {
      try {
        var qr = qrcode(0, 'M');
        qr.addData(url);
        qr.make();
        var box = el('div', 'room-invite-qr');
        box.innerHTML = qr.createSvgTag({ cellSize: 3, margin: 2 });
        row.append(box);
      } catch (e) {}
    }
    row.prepend(a); row.append(btns);
    return row;
  }

  function genRoomInvites(n) {
    var r = activeRoom();
    if (!r || r.kind !== 'shared') return;
    if (!send({ type: 'room_invite', room: r.slug, count: n, hours: inviteHours })) toast('未连接');
  }

  function renderRoomInvites(f) {
    var box = $('#roomInviteList');
    if (!box) return;
    box.textContent = '';
    var prefix = location.pathname.replace(/\/[^\/]*$/, '');
    (f.invites || []).forEach(function (it) {
      var url = location.origin + prefix + 'join.html#t=' + encodeURIComponent(it.token);
      box.append(inviteRow(url));
    });
    if (!(f.invites || []).length) box.append(el('div', 'sheet-tip', '生成后把链接发给朋友。'));
  }


  /* 拉消息：首次打开先取最近一屏做上下文，再按读指针补断线期间漏掉的那段（按 id 去重） */
  var CATCHUP_LIMIT = 200;
  function loadRoom(slug) {
    var arr = state.msgs[slug] || [];
    var last = arr.length ? arr[arr.length - 1].id : null;
    if (last == null) send({ type: 'history', room: slug, limit: LIMIT });
    var cur = last != null ? last : state.lastRead[slug];
    var rid = roomLastId(slug);
    if (cur != null && rid != null && rid > cur) {
      send({ type: 'history', room: slug, limit: CATCHUP_LIMIT, since: cur });
    }
  }

  /* 右键"标为已读"：直接跳到服务端该房最新一条 */
  function markReadLatest(slug) {
    var id = roomLastId(slug);
    if (id == null) { var arr = state.msgs[slug] || []; id = arr.length ? arr[arr.length - 1].id : null; }
    if (id != null) markRead(slug, id);
    state.unread[slug] = 0;
    renderSidebar();
    scheduleHostState();
  }

  /* ---------- 渲染消息 ---------- */
  function renderStream(forceBottom) {
    var box = $('#stream'); var msgs = state.msgs[state.active] || [];
    var nearBottom = box.scrollHeight - box.scrollTop - box.clientHeight < 80;
    var oldTop = box.scrollTop;
    box.textContent = '';
    if (!state.active) { box.append(el('div', 'empty', '选一个房间开始说话')); return; }
    if (!msgs.length) {
      box.append(el('div', 'empty', '还没有消息。'));
      setNewMsg(false);
      return;
    }
    var prev = null;
    msgs.forEach(function (m) {
      var k = dayKey(m.ts);
      if (k !== prev) { prev = k; var d = el('div', 'day'); d.append(el('span', '', dayLabel(m.ts))); box.append(d); }
      box.append(msgNode(m));
    });
    ackVisible(state.active, msgs);
    if (forceBottom || nearBottom) {
      box.scrollTop = box.scrollHeight;
      setNewMsg(false);
    } else {
      box.scrollTop = oldTop;
      setNewMsg(true);
    }
  }

  function setNewMsg(on) {
    var b = $('#newMsg');
    if (b) b.classList.toggle('hidden', !on);
  }

  function ackVisible(room, msgs) {
    // 读回执只在"这条真被渲染出来了"时上报；自己发的不算（服务端要的是别人收到）。
    if (!room || !state.me) return;
    var done = state.acked[room] || (state.acked[room] = {});
    var ids = [];
    msgs.forEach(function (m) {
      if (!m.id || m.from === state.me.client_id || done[m.id]) return;
      done[m.id] = true;
      ids.push(m.id);
    });
    if (ids.length) send({ type: 'message_ack', room: room, ids: ids.slice(-50) });
  }

  function setReply(m) {
    state.replyTo = m;
    var bar = $('#quoteBar');
    if (!bar) return;
    $('#quoteBarText').textContent = (m.sender_name || '') + '：' + (m.content || '').slice(0, 60);
    bar.classList.remove('hidden');
    $('#input').focus();
  }

  function clearReply() {
    state.replyTo = null;
    var bar = $('#quoteBar');
    if (bar) bar.classList.add('hidden');
  }

  function quoteNode(m) {
    // 引用块全程 textContent（走 el），不进 innerHTML——外来字符串一律不许拼 HTML。
    var q = el('div', 'quote');
    q.append(el('span', 'q-who', m.reply_to_name ? ('↩ ' + m.reply_to_name) : '↩ 引用已不存在'));
    if (m.reply_to_excerpt) q.append(el('span', 'q-text', m.reply_to_excerpt));
    return q;
  }

  function msgNode(m) {
    var mine = state.me && m.from === state.me.client_id;
    var row = el('div', 'msg' + (mine ? ' mine' : ''));
    var av = el('span', 'av', initialOf(m.sender_name)); av.style.background = colorOf(m.from);
    var body = el('div', 'body');
    var meta = el('div', 'meta');
    meta.append(el('b', '', mine ? '我' : m.sender_name), el('span', 'msg-time', hm(m.ts)));
    var known = state.members[m.from];
    if (known && known.kind === 'agent') meta.append(el('span', 'agent-tag', '助手'));
    var bubble = el('div', 'bubble'); bubble.innerHTML = highlight(m.content || '');
    body.append(meta);
    if (m.reply_to) body.append(quoteNode(m));
    body.append(bubble);
    bindMsgMenu(bubble, m);
    row.append(av, body);
    return row;
  }

  var pressTimer = null;

  function bindMsgMenu(node, m) {
    node.addEventListener('contextmenu', function (e) {
      e.preventDefault(); openMsgMenu(e.clientX, e.clientY, m);
    });
    node.addEventListener('touchstart', function (e) {
      var t = e.touches && e.touches[0];
      if (!t) return;
      clearTimeout(pressTimer);
      pressTimer = setTimeout(function () { openMsgMenu(t.clientX, t.clientY, m); }, 520);
    }, { passive: true });
    ['touchend', 'touchmove', 'touchcancel'].forEach(function (ev) {
      node.addEventListener(ev, function () { clearTimeout(pressTimer); }, { passive: true });
    });
  }

  function openMsgMenu(x, y, m) {
    state.menuMsg = m;
    var menu = $('#msgMenu');
    if (!menu) return;
    menu.classList.remove('hidden');
    var r = menu.getBoundingClientRect();
    menu.style.left = Math.max(6, Math.min(x, window.innerWidth - r.width - 6)) + 'px';
    menu.style.top = Math.max(6, Math.min(y, window.innerHeight - r.height - 6)) + 'px';
  }

  function closeMsgMenu() {
    var menu = $('#msgMenu');
    if (menu) menu.classList.add('hidden');
  }

  /* 原生返回：覆盖层 → 房间 → 交给壳/系统。返回 true 表示本次返回已被消费。 */
  function pubchatBack() {
    var sheets = ['guideSheet', 'bindSheet', 'profileSheet', 'roomSheet', 'roomMenuSheet',
                  'roomInviteSheet', 'agentDmSheet', 'meSheet'];
    for (var i = 0; i < sheets.length; i++) {
      var el = $('#' + sheets[i]);
      if (el && !el.classList.contains('hidden')) { el.classList.add('hidden'); return true; }
    }
    var menu = $('#msgMenu');
    if (menu && !menu.classList.contains('hidden')) { closeMsgMenu(); return true; }
    var app = $('#app');
    if (app && app.classList.contains('open') &&
        window.matchMedia && window.matchMedia('(max-width:760px)').matches) {
      app.classList.remove('open');
      return true;
    }
    return false;
  }
  window.pubchatBack = pubchatBack;

  function copyText(text) {
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(text).then(function () { toast('已复制'); },
        function () { fallbackCopy(text); });
      return;
    }
    fallbackCopy(text);
  }

  function fallbackCopy(text) {
    var ta = document.createElement('textarea');
    ta.value = text; document.body.appendChild(ta); ta.select();
    try { document.execCommand('copy'); toast('已复制'); }
    catch (e) { toast('复制失败'); }
    document.body.removeChild(ta);
  }

  function highlight(text) {
    var html = esc(text);
    Object.keys(state.members).forEach(function (id) {
      var nm = state.members[id].name;
      if (!nm) return;
      var safe = nm.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
      html = html.replace(new RegExp('@' + safe, 'g'), '<span class="mention">@' + esc(nm) + '</span>');
    });
    return html;
  }

  function showBanner(t) { var b = $('#banner'); b.textContent = t; b.classList.remove('hidden'); }
  function hideBanner() { $('#banner').classList.add('hidden'); }

  /* ---------- 发送 ---------- */
  function doSend() {
    var ta = $('#input'), text = ta.value.trim();
    if (!text || !state.active || !state.connected) return;
    var frame = { type: 'message', room: state.active, content: text };
    if (state.replyTo) frame.reply_to = state.replyTo.id;
    if (!send(frame)) { showBanner('未连接'); return; }
    ta.value = ''; grow();
    clearReply();
  }

  function grow() {
    var t = $('#input'); t.style.height = 'auto';
    t.style.height = Math.min(t.scrollHeight, 150) + 'px';
    $('#send').disabled = !t.value.trim() || !state.connected;
  }

  /* ---------- 提及（输入 @ 自动弹层，点 @ 按钮也开） ---------- */
  var mi = { open: false, query: '', atPos: -1, idx: 0 };
  function mentionRows() {
    var q = String(mi.query || '').trim().toLowerCase();
    var rows = Object.keys(state.members).map(function (id) {
      var m = state.members[id] || {};
      return { client_id: id, name: m.name || id, online: !!m.online };
    }).filter(function (m) {
      if (!q) return true;
      return m.name.toLowerCase().indexOf(q) >= 0 ||
        String(m.client_id).toLowerCase().indexOf(q) >= 0;
    });
    rows.sort(function (a, b) {
      if (a.online !== b.online) return a.online ? -1 : 1;
      return a.name.localeCompare(b.name);
    });
    return rows;
  }
  function closeMention() {
    mi.open = false; mi.query = ''; mi.atPos = -1; mi.idx = 0;
    var p = document.getElementById('mentionPop');
    if (p) p.remove();
  }
  function drawMention() {
    var pop = document.getElementById('mentionPop');
    if (!pop) {
      pop = el('div', 'mention-pop'); pop.id = 'mentionPop';
      // 点候选不抢焦点，否则 caret 丢失
      pop.onmousedown = function (e) { e.preventDefault(); };
      document.body.append(pop);
    }
    pop.innerHTML = '';
    var rows = mentionRows();
    if (mi.idx > rows.length - 1) mi.idx = Math.max(0, rows.length - 1);
    rows.forEach(function (r, i) {
      var b = el('button');
      if (i === mi.idx) b.className = 'act';
      var av = el('span', 'av', initialOf(r.name));
      av.style.background = colorOf(r.client_id);
      b.append(av, document.createTextNode(r.name));
      b.onclick = function () { pickMention(r); };
      pop.append(b);
    });
    if (!rows.length) pop.append(el('div', 'item', '没有匹配的成员'));
    var box = $('#composer').getBoundingClientRect();
    pop.style.left = Math.min(box.left, Math.max(8, window.innerWidth - 220)) + 'px';
    pop.style.top = 'auto';
    pop.style.bottom = (window.innerHeight - box.top + 8) + 'px';
  }
  // 光标前是不是一个还没打完的 @xxx
  function detectMention() {
    var t = $('#input');
    if (!t) return;
    var v = t.value || '';
    var cur = t.selectionStart == null ? v.length : t.selectionStart;
    var before = v.slice(0, cur);
    var at = before.lastIndexOf('@');
    var bad = function () { if (mi.open) closeMention(); };
    if (at < 0) return bad();
    var prev = at > 0 ? before.charAt(at - 1) : '';
    if (prev && !/\s/.test(prev)) return bad();   // 邮箱之类不算
    var q = before.slice(at + 1);
    if (/[\s\n]/.test(q)) return bad();           // 打了空格 = 这次 @ 说完了
    if (q.length > 20) return bad();
    if (!mi.open) mi.idx = 0;
    mi.open = true; mi.atPos = at; mi.query = q;
    if (mi.idx > mentionRows().length - 1) mi.idx = 0;
    drawMention();
  }
  function pickMention(row) {
    if (!row) return;
    var t = $('#input');
    var v = t.value || '';
    var before = v.slice(0, mi.atPos);
    var after = v.slice(mi.atPos);
    var m = /^@\S*/.exec(after);
    var rest = m ? after.slice(m[0].length) : after;
    var ins = '@' + row.name + ' ';
    t.value = before + ins + rest;
    var caret = (before + ins).length;
    closeMention();
    t.focus();
    try { t.setSelectionRange(caret, caret); } catch (e) {}
    grow();
  }
  function showMention() {
    closeMention();
    var t = $('#input');
    mi.open = true; mi.query = ''; mi.idx = 0;
    mi.atPos = t.selectionStart == null ? (t.value || '').length : t.selectionStart;
    drawMention();
  }
  function insert(txt) {
    var t = $('#input'); var pos = t.selectionStart == null ? t.value.length : t.selectionStart;
    t.value = t.value.slice(0, pos) + txt + t.value.slice(pos);
    t.focus(); t.selectionStart = t.selectionEnd = pos + txt.length; grow();
  }

  /* ---------- 绑定页 ---------- */
  /* 接入文案由服务端按模式下发（core/agent_modes.py），这里只补当前实例地址 */
  var MODE_FALLBACK = [{ id: 'online', label: '值守在线（模式一）' },
                       { id: 'inbox', label: '离线收件（模式二）' }];
  var MODE_SHORT = { online: '模式一', inbox: '模式二' };
  var KEY_WARN = '此串含二级密钥，只发给自己的智能体，不要贴到群里';

  function agentModes() {
    return state.agentModes && state.agentModes.length ? state.agentModes : MODE_FALLBACK;
  }
  function modeOf(a) { return a.mode === 'inbox' ? 'inbox' : 'online'; }
  function modeLabelOf(a) {
    var m = modeOf(a);
    if (a.mode_label && m === a.mode) return a.mode_label;
    var hit = agentModes().filter(function (x) { return x.id === m; })[0];
    return hit ? hit.label : m;
  }
  function promptFor(a) {
    var p = a.prompts && a.prompts[modeOf(a)];
    if (!p) return '服务端没有下发接入文案，请升级服务端后再试。';
    return p.replace('{url}', cfg.ws);
  }
  var canManageAgents = function () {
    return !!(state.me && state.me.kind === 'human' && state.me.role !== 'readonly');
  };

  function buildModeBar(a) {
    var bar = el('div', 'modebar');
    bar.append(el('span', 'mode-cap', '接入方式'));
    var seg = el('div', 'mode');
    var locked = !canManageAgents();
    agentModes().forEach(function (m) {
      var b = el('button', 'mode-btn' + (modeOf(a) === m.id ? ' on' : ''), m.label);
      b.type = 'button';
      b.disabled = locked;
      b.title = locked ? '只读成员不能改接入方式' : '选中哪种，下面就显示哪种接入指令';
      b.onclick = function () {
        if (modeOf(a) === m.id) return;
        if (!send({ type: 'agent_set_mode', client_id: a.client_id, mode: m.id })) {
          toast('未连接'); return;
        }
        b.disabled = true;
        setTimeout(function () { b.disabled = locked; }, 1500);
      };
      seg.append(b);
    });
    bar.append(seg, el('span', 'mode-now', '当前：' + modeLabelOf(a)));
    return bar;
  }

  function labelOfModeId(id) {
    var hit = agentModes().filter(function (x) { return x.id === id; })[0];
    return hit ? hit.label : id;
  }

  function renderNewAgentBar() {
    var bar = $('#newAgentBar');
    if (!bar) return;
    bar.textContent = '';
    bar.append(el('span', 'mode-cap', '新建接入方式'));
    var seg = el('div', 'mode');
    var locked = !canManageAgents();
    agentModes().forEach(function (m) {
      var b = el('button', 'mode-btn' + (state.newAgentMode === m.id ? ' on' : ''), m.label);
      b.type = 'button';
      b.disabled = locked;
      b.title = locked ? '只读成员不能加智能体' : '新建的席位按这个接入方式生成接入指令';
      b.onclick = function () {
        state.newAgentMode = m.id;
        renderNewAgentBar();
      };
      seg.append(b);
    });
    bar.append(seg, el('span', 'mode-now', '当前：' + labelOfModeId(state.newAgentMode)));
  }

  /* ---------- 接入我的智能体：先选模式，再生成并复制 ----------
     以前「＋ → 添加智能体」直接按 online 建了一个再让人去别处看，用户要的是先选模式、
     选了才建、建完立刻把那句话复制走。这里新建的那条走 state.connectPending 关联回执。 */
  var CONNECT_INFO = {
    online: { badge: '模式一', title: '值守在线',
              desc: '智能体常驻在线，@ 到它立刻收到。适合能一直开着的智能体。',
              note: '（适合 DSH、Claude Code 等能常驻的智能体）' },
    inbox:  { badge: '模式二', title: '离线收件',
              desc: '智能体按需启动，上线后自己读收件箱。适合偶尔用一下的智能体。',
              note: '（适合豆包、千问等 inbox 应用的工作模式）' }
  };

  function connectModeInfo(id) { return CONNECT_INFO[id] || CONNECT_INFO.online; }

  function renderConnectModes() {
    var host = $('#connectModes');
    if (!host) return;
    host.textContent = '';
    var locked = !canManageAgents();
    agentModes().forEach(function (m) {
      var info = connectModeInfo(m.id);
      var on = state.connectMode === m.id;
      var card = el('button', 'mode-card' + (on ? ' on' : ''));
      card.type = 'button';
      card.dataset.mode = m.id;
      card.disabled = locked;
      card.title = locked ? '只读成员不能接智能体' : m.label;
      card.append(el('span', 'mc-badge', info.badge),
                  el('span', 'mc-t', info.title),
                  el('span', 'mc-d', info.desc),
                  el('span', 'mc-note', info.note));
      card.onclick = function () {
        if (state.connectMode === m.id) return;
        state.connectMode = m.id;
        renderConnectModes();
      };
      host.append(card);
    });
  }

  function resetConnect() {
    state.connectPending = '';
    var box = $('#connectResult');
    if (box) box.classList.add('hidden');
    var foot = $('#connectFoot');
    if (foot) foot.classList.remove('hidden');
    var go = $('#connectGo');
    if (go) { go.disabled = false; go.textContent = '生成接入指令'; }
    var nm = $('#connectName');
    if (nm) nm.value = '';
    renderConnectModes();
  }

  function openConnect() {
    if (!state.me) { toast('请先登录'); return; }
    // 弹窗不叠弹窗：引导/我的/新建如果开着，先收起来（不动「引导已看」标记）
    ['guideSheet', 'meSheet', 'newSheet', 'bindSheet'].forEach(function (id) {
      var n = document.getElementById(id);
      if (n) n.classList.add('hidden');
    });
    resetConnect();
    $('#connectSheet').classList.remove('hidden');
  }

  function showConnectResult(a) {
    var text = promptFor(a);
    state.connectText = text;
    $('#connectAgentName').textContent = (a.name || a.client_id) + ' · ' + (a.client_id || '');
    $('#connectCode').textContent = text;
    $('#connectResult').classList.remove('hidden');
    $('#connectFoot').classList.add('hidden');
    copyText(text);            // 选完模式就把指令放到剪贴板，用户直接粘给智能体
  }

  function connectGo() {
    if (!canManageAgents()) { toast('只读成员不能接智能体'); return; }
    var go = $('#connectGo');
    state.connectPending = state.connectMode;
    var nmEl = $('#connectName');
    var nm = nmEl ? (nmEl.value || '').trim().slice(0, 20) : '';
    var frame = { type: 'agent_create', mode: state.connectMode };
    if (nm) frame.name = nm;
    if (!send(frame)) {
      state.connectPending = ''; toast('未连接'); return;
    }
    go.disabled = true; go.textContent = '生成中…';
    setTimeout(function () {
      if (!state.connectPending) return;
      state.connectPending = '';
      go.disabled = false; go.textContent = '生成接入指令';
      toast('没等到回执，再点一次试试');
    }, 6000);
  }

  /* ---------- 我的（顶栏「我的」的唯一落点：账号 + 智能体，复用原有两个 sheet） ---------- */
  function openMe() {
    if (state.me) {
      $('#meName').textContent = state.me.name || '未登录';
      $('#meSub').textContent = '@' + (state.me.client_id || '') + (INST_NAME ? ' · ' + INST_NAME : '');
      var av = $('#meAv2');
      av.textContent = initialOf(state.me.name || '我');
      av.style.background = colorOf(state.me.client_id || '');
      $('#meAgentsSub').textContent = state.agents.length
        ? state.agents.length + ' 个席位 · 绑定与接入方式'
        : '还没有席位，点这里绑定';
    }
    $('#meSheet').classList.remove('hidden');
  }

  /* 我的智能体：按手机版版式做卡片（头像 / 在线徽标 / 服务器 / 二级 Key 打码 / 四个按钮）。
     0.8.15 起这是「管理」的唯一落点——云端实例也用它，不再走服务器那张管理员页。 */
  function maskKey(k) {
    k = k || '';
    if (k.length <= 6) return k;
    return k.slice(0, 3) + '••••' + k.slice(-3);
  }
  function wsUrlFor() {
    return (location.protocol === 'https:' ? 'wss://' : 'ws://') + location.host + '/ws';
  }

  function openBind() {
    renderNewAgentBar();
    var host = $('#bindList'); host.textContent = '';
    if (!state.agents.length) {
      host.append(el('div', 'agc-empty', '还没有智能体席位。点上面的「新建」选模式一/模式二就能接入。'));
    }
    (state.agents || []).forEach(function (a) {
      var on = !!(state.online && state.online[a.client_id]);
      var card = el('div', 'agcard');

      var top = el('div', 'agc-top');
      var av = el('div', 'agc-av', initialOf(a.name || a.client_id));
      av.style.background = colorOf(a.client_id);
      var who = el('div', 'agc-who');
      who.append(el('div', 'agc-name', a.name || a.client_id),
                 el('div', 'agc-id', '@' + a.client_id));
      top.append(av, who, el('span', 'agc-badge' + (on ? ' on' : ''), on ? '在线' : '离线'));

      var srv = el('div', 'agc-row');
      srv.append(el('span', 'agc-k', '服务器'), el('code', 'agc-v', wsUrlFor()));

      var keyRow = el('div', 'agc-row');
      var keyVal = el('code', 'agc-v', maskKey(a.key2));
      var eye = el('button', 'mini', '显示');
      var shown = false;
      eye.onclick = function () {
        shown = !shown;
        keyVal.textContent = shown ? (a.key2 || '') : maskKey(a.key2);
        eye.textContent = shown ? '隐藏' : '显示';
      };
      keyRow.append(el('span', 'agc-k', '二级 Key'), keyVal, eye);

      var acts = el('div', 'agc-acts');
      var copyBtn = el('button', 'mini primary', '复制接入信息');
      copyBtn.onclick = function () {
        var text = promptFor(a);
        if (navigator.clipboard) navigator.clipboard.writeText(text).then(function () { toast('已复制，发给它就行'); }, function () { toast('复制失败，请手动选'); });
        else toast('请手动选择复制');
      };
      var renameBtn = el('button', 'mini', '改名');
      renameBtn.onclick = function () {
        var v = window.prompt('给它起个名字（@ 这个名字就能叫到它）', a.name || '');
        if (v === null) return;
        v = v.trim();
        if (!v) { toast('名字不能为空'); return; }
        if (v === a.name) return;
        if (!send({ type: 'agent_rename', client_id: a.client_id, name: v })) toast('未连接');
      };
      var rotBtn = el('button', 'mini', '换 Key');
      rotBtn.onclick = function () {
        if (!window.confirm('给「' + (a.name || a.client_id) + '」换一个新的二级 Key？\n\n旧 Key 立刻失效，正在连的会被踢下线，要用新 Key 重新接。')) return;
        if (!send({ type: 'agent_rotate_key', client_id: a.client_id })) toast('未连接');
      };
      var del = el('button', 'mini danger', '删除');
      del.onclick = function () {
        if (!window.confirm('删除「' + (a.name || a.client_id) + '」？\n\n二级 Key 立刻失效；正在连的会被踢下线；聊天记录保留。')) return;
        if (!send({ type: 'agent_remove', client_id: a.client_id })) toast('未连接');
      };
      acts.append(copyBtn, renameBtn, rotBtn, del);

      card.append(top, srv, keyRow, acts, buildModeBar(a));
      host.append(card);
    });
    $('#bindSheet').classList.remove('hidden');
  }

  /* ---------- 我的账号 ---------- */
  /* ---------- 新手引导（S3-c）：四步文案与手机端共用 web/guide.js ---------- */
  var guideIndex = 0;
  function guideSeen() { try { return localStorage.getItem(LS_GUIDE) === '1'; } catch (e) { return true; } }
  function renderGuide() {
    var steps = window.PubChatGuide.steps('desktop');
    var s = steps[guideIndex];
    $('#guideTitle').textContent = s.t;
    $('#guideBody').innerHTML = s.b;
    $('#guideDots').innerHTML = window.PubChatGuide.dots(steps.length, guideIndex);
    $('#guideNext').textContent = guideIndex === steps.length - 1 ? '开始使用' : '下一步';
  }
  function openGuide() {
    if (!window.PubChatGuide) return;
    state.guideShown = true;               // 一次会话最多自动弹一次
    guideIndex = 0;
    renderGuide();
    $('#guideSheet').classList.remove('hidden');
  }
  function nextGuide() {
    if (guideIndex < window.PubChatGuide.steps('desktop').length - 1) { guideIndex++; renderGuide(); }
    else closeSheet('guideSheet');
  }
  /* 首启顺序：先设资料（名字/Key），这一步走完才谈引导。
     关掉资料层的四条路（保存回执 / 稍后 / ✕ / 点背景）都走 closeProfile，别漏。 */
  function closeProfile() { closeSheet('profileSheet'); maybeOpenGuide(); }
  function maybeOpenGuide() {
    if (!state.firstRunGuide) return;
    state.firstRunGuide = false;
    openGuide();
  }

  function openProfile(first) {
    if (!state.me || state.me.kind !== 'human') { toast('智能体身份不能改资料'); return; }
    $('#profileTitle').textContent = first ? '第一次进来，先设置一下' : '我的账号';
    $('#profileTip').textContent = first
      ? '名字和登录信息现在是管理员发的，你可以改成自己的；点保存后会用新信息重新登录。'
      : '改完点保存，会用新信息自动重新登录一次。';
    $('#pfName').value = state.me.name || '';
    $('#pfUser').value = state.me.client_id || '';
    $('#pfKey').value = '';
    $('#pfKey').placeholder = '留空则不改动';
    $('#pfErr').textContent = '';
    $('#profileSheet').classList.remove('hidden');
    $('#pfName').focus();
  }

  function saveProfile() {
    var name = $('#pfName').value.trim();
    var user = $('#pfUser').value.trim().toLowerCase();
    var key = $('#pfKey').value.trim();
    var err = $('#pfErr');
    if (!name) { err.textContent = '显示名不能为空'; return; }
    if (!/^[a-z0-9_]{2,32}$/.test(user)) { err.textContent = '登录名只能小写字母/数字/下划线，2-32 位'; return; }
    if (key && key.length < 8) { err.textContent = '一级 key 至少 8 位'; return; }
    if (!key) key = state.lastKey;   // 留空表示不改口令
    err.textContent = '';
    if (!send({ type: 'profile', name: name, username: user, key: key })) { err.textContent = '未连接，稍后重试'; return; }
    $('#pfSave').disabled = true;
    setTimeout(function () { $('#pfSave').disabled = false; }, 2000);
  }

  /* ---------- 登录 ---------- */
  function doLogin(e) {
    e.preventDefault();
    state.lastUser = $('#username').value.trim().toLowerCase();
    state.lastKey = $('#key').value.trim();
    $('#loginErr').textContent = '';
    if (!state.lastUser || !state.lastKey) return;
    $('#loginBtn').disabled = true;
    connect();
    setTimeout(function () { $('#loginBtn').disabled = false; }, 1200);
  }

  /* ---------- 事件 ---------- */
  $('#loginForm').addEventListener('submit', doLogin);
  $('#composer').addEventListener('submit', function (e) { e.preventDefault(); doSend(); });
  $('#input').addEventListener('input', function () { grow(); detectMention(); });
  $('#input').addEventListener('click', detectMention);
  $('#input').addEventListener('keyup', function (e) {
    if (e.key !== 'ArrowDown' && e.key !== 'ArrowUp' && e.key !== 'Enter') detectMention();
  });
  $('#input').addEventListener('keydown', function (e) {
    if (mi.open) {
      if (e.key === 'ArrowDown') { e.preventDefault(); mi.idx = (mi.idx + 1) % Math.max(1, mentionRows().length); drawMention(); return; }
      if (e.key === 'ArrowUp') { e.preventDefault(); mi.idx = (mi.idx - 1 + Math.max(1, mentionRows().length)) % Math.max(1, mentionRows().length); drawMention(); return; }
      if (e.key === 'Enter' || e.key === 'Tab') {
        e.preventDefault();
        pickMention(mentionRows()[mi.idx]);
        return;
      }
      if (e.key === 'Escape') { e.preventDefault(); closeMention(); return; }
    }
    if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); doSend(); }
    if (e.key === 'Escape') closeMention();
  });
  $('#mentionBtn').addEventListener('click', function (e) { e.stopPropagation(); showMention(); });
  document.addEventListener('click', function (e) {
    if (!e.target.closest('.mention-pop') && e.target.id !== 'mentionBtn' && e.target.id !== 'input') closeMention();
    if (!e.target.closest('.ctx')) closeCtx();
    if (!e.target.closest('#msgMenu')) closeMsgMenu();
  });
  document.addEventListener('keydown', function (e) { if (e.key === 'Escape') { closeCtx(); } });
  /* 侧栏搜索：有命中就强制展开该组，清空后恢复原有折叠状态 */
  $('#sideSearch').addEventListener('input', function () {
    state.query = this.value || '';
    renderSidebar();
    var host = $('#sideList'); if (host && state.query) host.scrollTop = 0;
  });
  $('#sideSearch').addEventListener('keydown', function (e) {
    if (e.key === 'Escape') { this.value = ''; state.query = ''; renderSidebar(); this.blur(); }
  });
  window.addEventListener('resize', closeCtx);
  window.addEventListener('pagehide', function () { lsSet(readKey(), state.lastRead); });
  $('#reload').addEventListener('click', function () {
    if (!state.active) return;
    state.msgs[state.active] = []; state.seen[state.active] = {};
    send({ type: 'history', room: state.active, limit: LIMIT });
    renderStream();
  });
  $('#back').addEventListener('click', function () { $('#app').classList.remove('open'); });
  $('#newRoom').addEventListener('click', function () { $('#newSheet').classList.remove('hidden'); });
  $('#closeNew').addEventListener('click', function () { closeSheet('newSheet'); });
  $('#newSheet').addEventListener('click', function (e) { if (e.target.id === 'newSheet') closeSheet('newSheet'); });
  $('#newAgentBtn').addEventListener('click', function () {
    closeSheet('newSheet');
    openConnect();
  });
  $('#sideConnect').addEventListener('click', openConnect);
  $('#closeConnect').addEventListener('click', function () { closeSheet('connectSheet'); });
  $('#connectSheet').addEventListener('click', function (e) { if (e.target.id === 'connectSheet') closeSheet('connectSheet'); });
  $('#connectGo').addEventListener('click', connectGo);
  $('#connectAgain').addEventListener('click', resetConnect);
  $('#connectCopy').addEventListener('click', function () {
    if (state.connectText) copyText(state.connectText);
  });
  $('#newInviteBtn').addEventListener('click', function () {
    closeSheet('newSheet');
    // 聊天页在独立源上，邀请界面在管理页签里：交给外壳切页（shell 收到后打开管理→成员与邀请）
    try { window.parent.postMessage({ type: 'pubchat-open-invite' }, '*'); } catch (e) {}
  });
  $('#newRoomBtn').addEventListener('click', function () {
    closeSheet('newSheet');
    openRoomSheet('create');
  });
  $('#roomMenuBtn').addEventListener('click', openRoomMenu);
  $('#closeRoomSheet').addEventListener('click', function () { closeSheet('roomSheet'); });
  $('#roomCancel').addEventListener('click', function () { closeSheet('roomSheet'); });
  $('#roomSubmit').addEventListener('click', submitRoom);
  $('#closeAgentDm').addEventListener('click', function () { closeSheet('agentDmSheet'); });
  $('#agentDmCancel').addEventListener('click', function () { closeSheet('agentDmSheet'); });
  $('#agentDmSheet').addEventListener('click', function (e) { if (e.target.id === 'agentDmSheet') closeSheet('agentDmSheet'); });
  $('#agentDmGo').addEventListener('click', submitAgentDm);
  $('#roomName').addEventListener('keydown', function (e) { if (e.key === 'Enter') { e.preventDefault(); submitRoom(); } });
  $('#roomSheet').addEventListener('click', function (e) { if (e.target.id === 'roomSheet') closeSheet('roomSheet'); });
  $('#closeRoomMenu').addEventListener('click', function () { closeSheet('roomMenuSheet'); });
  $('#roomMenuSheet').addEventListener('click', function (e) { if (e.target.id === 'roomMenuSheet') closeSheet('roomMenuSheet'); });
  $('#roomInviteBtn').addEventListener('click', function () { closeSheet('roomMenuSheet'); openRoomInvite(); });
  $('#roomRenameBtn').addEventListener('click', function () {
    var r = activeRoom(); closeSheet('roomMenuSheet'); openRoomSheet('rename', r);
  });
  $('#roomArchiveBtn').addEventListener('click', function () {
    var r = activeRoom(); if (!r) return;
    if (!window.confirm('归档「' + (r.title || r.slug) + '」？成员将看不到这个房间，消息保留。')) return;
    closeSheet('roomMenuSheet');
    send({ type: 'room_archive', room: r.slug });
  });
  $('#roomLeaveBtn').addEventListener('click', function () {
    var r = activeRoom(); if (!r) return;
    if (!window.confirm('退出「' + (r.title || r.slug) + '」？')) return;
    closeSheet('roomMenuSheet');
    send({ type: 'room_leave', room: r.slug });
  });
  $('#closeRoomInvite').addEventListener('click', function () { closeSheet('roomInviteSheet'); });
  $('#roomInviteSheet').addEventListener('click', function (e) { if (e.target.id === 'roomInviteSheet') closeSheet('roomInviteSheet'); });
  $('#roomInviteGen').addEventListener('click', function () { genRoomInvites(1); });
  $('#roomInviteGen5').addEventListener('click', function () { genRoomInvites(5); });
  Array.prototype.forEach.call(document.querySelectorAll('#roomInviteHours .chip'), function (b) {
    b.addEventListener('click', function () {
      setInviteHours(parseInt(b.getAttribute('data-hours'), 10));
    });
  });
  $('#newMsg').addEventListener('click', function () {
    var s = $('#stream'); if (s) s.scrollTop = s.scrollHeight;
    setNewMsg(false);
  });
  $('#stream').addEventListener('scroll', function () {
    var s = this;
    if (s.scrollHeight - s.scrollTop - s.clientHeight < 80) setNewMsg(false);
  });
  $('#msgCopy').addEventListener('click', function () {
    if (state.menuMsg) copyText(state.menuMsg.content || '');
    closeMsgMenu();
  });
  $('#msgMention').addEventListener('click', function () {
    var m = state.menuMsg;
    if (m) {
      var ta = $('#input');
      ta.value = '@' + (m.sender_name || '') + ' ' + ta.value;
      ta.focus(); grow();
    }
    closeMsgMenu();
  });
  $('#msgReply').addEventListener('click', function () {
    if (state.menuMsg) setReply(state.menuMsg);
    closeMsgMenu();
  });
  $('#quoteCancel').addEventListener('click', clearReply);
  $('#addAgent').addEventListener('click', function () {
    if (!send({ type: 'agent_create', mode: state.newAgentMode })) { toast('未连接'); return; }
    var b = this;
    b.disabled = true;
    setTimeout(function () { b.disabled = false; }, 1200);
  });
  $('#closeBind').addEventListener('click', function () { $('#bindSheet').classList.add('hidden'); });
  $('#bindSheet').addEventListener('click', function (e) { if (e.target.id === 'bindSheet') $('#bindSheet').classList.add('hidden'); });
  $('#meCard').addEventListener('click', openMe);
  $('#closeMe').addEventListener('click', function () { closeSheet('meSheet'); });
  $('#meSheet').addEventListener('click', function (e) { if (e.target.id === 'meSheet') closeSheet('meSheet'); });
  $('#meAcct').addEventListener('click', function () { closeSheet('meSheet'); openProfile(false); });
  // 管理归管理页（0.8.16）：在壳里就问一句能不能切过去，不在壳里（浏览器）就还弹本页的卡片
  $('#meAgents').addEventListener('click', function () {
    closeSheet('meSheet');
    if (embeddedInShell()) { hostPost({ type: 'pubchat-goto-manage' }); return; }
    openBind();
  });
  $('#meGuide').addEventListener('click', function () { closeSheet('meSheet'); openGuide(); });
  $('#closeGuide').addEventListener('click', function () { closeSheet('guideSheet'); });
  $('#guideSheet').addEventListener('click', function (e) { if (e.target.id === 'guideSheet') closeSheet('guideSheet'); });
  $('#guideNext').addEventListener('click', nextGuide);
  $('#guideSkip').addEventListener('click', function () { closeSheet('guideSheet'); });
  $('#closeProfile').addEventListener('click', closeProfile);
  $('#profileSheet').addEventListener('click', function (e) { if (e.target.id === 'profileSheet') closeProfile(); });
  $('#pfSave').addEventListener('click', saveProfile);
  $('#pfGen').addEventListener('click', function () { $('#pfKey').value = randomKey(); $('#pfKey').placeholder = ''; });
  $('#pfLater').addEventListener('click', function () { closeProfile(); send({ type: 'onboarded' }); });
  ['#pfName', '#pfUser', '#pfKey'].forEach(function (s) {
    $(s).addEventListener('keydown', function (e) { if (e.key === 'Enter') { e.preventDefault(); saveProfile(); } });
  });

  /* ---------- 启动 ---------- */
  /* 扫码登录：管理页生成的链接把账号放在 #u=&k=（只走浏览器本地，不发给服务器） */
  function takeLoginHash() {
    var h = (location.hash || '').slice(1);
    if (!h) return null;
    var map = {};
    h.split('&').forEach(function (kv) {
      var i = kv.indexOf('=');
      if (i < 0) return;
      try { map[decodeURIComponent(kv.slice(0, i))] = decodeURIComponent(kv.slice(i + 1)); } catch (e) {}
    });
    var u = (map.u || '').trim().toLowerCase();
    var k = (map.k || '').trim();
    var first = (map.first || '') === '1';
    try { history.replaceState(null, '', location.pathname + location.search); } catch (e) { location.hash = ''; }
    return (u && k) ? { u: u, k: k, first: first } : null;
  }

  loadSideState();
  var scanned = takeLoginHash();
  var u = localStorage.getItem(LS_USER), k = localStorage.getItem(LS_KEY);
  if (scanned) { u = scanned.u; k = scanned.k; }
  if (u) $('#username').value = u;
  if (u && k) { state.lastUser = u; state.lastKey = k; connect(); }
  else splashDone();          // 没凭证：登录框已经画出来了，不用再盖着
  setTimeout(splashDone, 1500);   // 连不上服务器时也不能把人关在开屏里
  /* 更新公告（S4）：启动就拉。204、坏 JSON、断网全部静默——
     公告是顺带的事，不能挡住进软件，所以这里不接任何错误提示。
     拉完再报一次状态，外壳的「关于」才一定拿得到这条历史（不靠后面的事件碰巧触发）。 */
  if (window.PubChatNotice) PubChatNotice.boot().then(function () { scheduleHostState(); });
  grow();
})();
