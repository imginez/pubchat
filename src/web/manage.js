/* PubChat 管理页（0.8.16）
   一套 UI 同时服务本机部署与服务器部署：只走 WS + 用户身份（用户名 + 一级 Key），
   不碰管理口那套设备凭证。添加智能体仍然在聊天页的弹窗里，这里只管「已有的」。
*/
(function () {
  'use strict';

  var $ = function (id) { return document.getElementById(id); };
  function el(t, c, x) { var n = document.createElement(t); if (c) n.className = c; if (x != null) n.textContent = x; return n; }
  function colorOf(id) {
    var h = 0; id = id || '';
    for (var i = 0; i < id.length; i++) h = (h * 31 + id.charCodeAt(i)) % 360;
    return 'hsl(' + h + ',52%,46%)';
  }
  function initialOf(n) { return (n || '?').trim().slice(0, 1).toUpperCase(); }
  var toastTimer = 0;
  function toast(t) {
    var box = $('toast'); box.textContent = t; box.classList.add('show');
    clearTimeout(toastTimer); toastTimer = setTimeout(function () { box.classList.remove('show'); }, 2200);
  }
  function copy(text, ok) {
    if (navigator.clipboard) {
      navigator.clipboard.writeText(text).then(function () { toast(ok || '已复制'); },
        function () { toast('复制失败，请手动选'); });
    } else { toast('请手动选择复制'); }
  }
  function maskKey(k) { k = k || ''; return k.length <= 6 ? k : k.slice(0, 3) + '••••' + k.slice(-3); }
  function wsUrl() {
    var p = location.protocol === 'https:' ? 'wss://' : 'ws://';
    return p + location.host + '/ws';
  }
  function store(k) { try { return localStorage.getItem(k) || ''; } catch (e) { return ''; } }

  var state = { me: null, agents: [], rooms: [], members: [], memberList: null,
                online: {}, modes: [], owner: false, invites: [] };
  var sock = null, retry = 0;

  /* ---------------- 连接 ---------------- */
  function connect() {
    var user = store('pubchat.user'), key = store('pubchat.key');
    if (!user || !key) { $('conn').textContent = '没有登录信息，请先回聊天页登录'; return; }
    try { sock = new WebSocket(wsUrl()); } catch (e) { $('conn').textContent = '连不上'; return; }
    sock.onopen = function () {
      sock.send(JSON.stringify({ type: 'auth', kind: 'user', username: user, key: key, last_read: 0 }));
    };
    sock.onmessage = function (ev) {
      var f; try { f = JSON.parse(ev.data); } catch (e) { return; }
      onFrame(f);
    };
    sock.onclose = function () {
      $('conn').textContent = '已断开，重连中…'; $('conn').classList.remove('on');
      retry = Math.min(retry + 1, 6);
      setTimeout(connect, 800 * retry);
    };
  }
  function send(o) {
    if (!sock || sock.readyState !== 1) { toast('还没连上'); return false; }
    sock.send(JSON.stringify(o)); return true;
  }

  function onFrame(f) {
    if (f.type === 'authenticated') {
      state.me = f.me; state.agents = f.agents || []; state.rooms = f.rooms || [];
      state.modes = f.agent_modes || [];
      state.owner = (f.me && f.me.role === 'owner');
      $('conn').textContent = '已连接 · ' + (f.me.name || f.me.client_id);
      $('conn').classList.add('on');
      send({ type: 'member_list' });
      renderAll();
      return;
    }
    if (f.type === 'presence') {
      var on = {};
      (f.members || []).forEach(function (m) { if (m.online) on[m.client_id] = 1; });
      state.online = on; state.presence = f.members || [];
      renderMembers(); renderAgents(); return;
    }
    if (f.type === 'member_list') { state.memberList = f.members || []; renderMembers(); return; }
    if (f.type === 'member_removed') { toast('已移出成员'); state.memberList = null; send({ type: 'member_list' }); return; }
    if (f.type === 'member_agent_limit_set') { send({ type: 'member_list' }); return; }
    if (f.type === 'agent_created') {
      if (!state.agents.some(function (a) { return a.client_id === f.agent.client_id; })) state.agents.push(f.agent);
      renderAgents(); return;
    }
    if (f.type === 'agent_renamed') {
      state.agents.forEach(function (a) { if (a.client_id === f.client_id) a.name = f.name; });
      renderAgents(); return;
    }
    if (f.type === 'agent_mode') {
      state.agents.forEach(function (a) {
        if (a.client_id !== f.client_id) return;
        a.mode = f.mode; a.mode_label = f.mode_label; if (f.prompts) a.prompts = f.prompts;
      });
      renderAgents(); return;
    }
    if (f.type === 'agent_key_rotated') {
      state.agents.forEach(function (a) { if (a.client_id === f.client_id) a.key2 = f.key2; });
      renderAgents(); toast('二级 Key 已更换，旧 Key 立刻失效'); return;
    }
    if (f.type === 'agent_removed') {
      state.agents = state.agents.filter(function (a) { return a.client_id !== f.client_id; });
      renderAgents(); toast('已删除该智能体'); return;
    }
    if (f.type === 'instance_invites') {
      state.invites = (f.invites || []).concat(state.invites).slice(0, 12);
      renderInvite(); return;
    }
    if (f.type === 'rooms-changed') { send({ type: 'rooms' }); return; }
    if (f.type === 'rooms') { state.rooms = f.rooms || []; renderRooms(); return; }
    if (f.type === 'error') { toast(f.message || f.code || '出错了'); return; }
  }

  /* ---------------- 我的智能体 ---------------- */
  function promptText(a) {
    var p = (a.prompts && a.prompts[a.mode]) || '';
    return (p || ('wsUrl: ' + wsUrl() + '\nclientId: ' + a.client_id + '\nkey: ' + (a.key2 || '')))
      + '\n\n（服务器：' + wsUrl() + ' · clientId：' + a.client_id + '）';
  }
  function renderAgents() {
    var host = $('agentCards'); host.textContent = '';
    $('nAgents').textContent = state.agents.length ? state.agents.length + ' 个席位' : '';
    if (!state.agents.length) {
      host.append(el('div', 'mg-empty', '还没有智能体。点右上角「添加智能体」选模式，或到聊天页加号里接入。'));
      return;
    }
    state.agents.forEach(function (a) {
      var on = !!state.online[a.client_id];
      var card = el('div', 'mgcard');
      var top = el('div', 'mgc-top');
      var av = el('div', 'mgc-av', initialOf(a.name || a.client_id));
      av.style.background = colorOf(a.client_id);
      var who = el('div', 'mgc-who');
      who.append(el('div', 'mgc-name', a.name || a.client_id), el('div', 'mgc-sub', '@' + a.client_id));
      top.append(av, who, el('span', 'mg-badge' + (on ? ' on' : ''), on ? '在线' : '离线'));

      var rowS = el('div', 'mg-row');
      rowS.append(el('span', 'k', '服务器'), el('span', 'v', wsUrl()));

      var rowK = el('div', 'mg-row');
      var kv = el('span', 'v', maskKey(a.key2));
      var shown = false;
      var eye = el('button', 'mg-btn', '显示');
      eye.onclick = function () { shown = !shown; kv.textContent = shown ? (a.key2 || '') : maskKey(a.key2); eye.textContent = shown ? '隐藏' : '显示'; };
      rowK.append(el('span', 'k', '二级 Key'), kv, eye);

      var acts = el('div', 'mg-acts');
      var bCopy = el('button', 'mg-btn primary', '复制接入信息');
      bCopy.onclick = function () { copy(promptText(a), '接入信息已复制'); };
      var bName = el('button', 'mg-btn', '改名');
      bName.onclick = function () {
        var v = window.prompt('给它起个名字（@ 这个名字就能叫到它）', a.name || '');
        if (v === null) return; v = v.trim();
        if (!v) { toast('名字不能为空'); return; }
        if (v === a.name) return;
        send({ type: 'agent_rename', client_id: a.client_id, name: v });
      };
      var bKey = el('button', 'mg-btn', '换 Key');
      bKey.onclick = function () {
        if (!window.confirm('给「' + (a.name || a.client_id) + '」换一个新的二级 Key？\n\n旧 Key 立刻失效，正在连的会被踢下线。')) return;
        send({ type: 'agent_rotate_key', client_id: a.client_id });
      };
      var bDel = el('button', 'mg-btn danger', '删除');
      bDel.onclick = function () {
        if (!window.confirm('删除「' + (a.name || a.client_id) + '」？\n\n二级 Key 立刻失效；聊天记录保留。')) return;
        send({ type: 'agent_remove', client_id: a.client_id });
      };
      acts.append(bCopy, bName, bKey, bDel);

      card.append(top, rowS, rowK, acts);
      host.append(card);
    });
  }

  /* ---------------- 成员 ---------------- */
  function renderMembers() {
    var host = $('memberCards'); host.textContent = '';
    var rows = state.memberList || (state.presence || []).filter(function (m) { return m.kind !== 'agent'; });
    $('nMembers').textContent = rows.length ? rows.length + ' 人' : '';
    if (!rows.length) {
      host.append(el('div', 'mg-empty', state.owner ? '还没有成员' : '只有实例主人能看到完整成员名单'));
      return;
    }
    rows.forEach(function (m) {
      var on = !!(m.online || state.online[m.client_id]);
      var card = el('div', 'mgcard');
      var top = el('div', 'mgc-top');
      var av = el('div', 'mgc-av', initialOf(m.name || m.client_id));
      av.style.background = colorOf(m.client_id);
      var who = el('div', 'mgc-who');
      who.append(el('div', 'mgc-name', m.name || m.client_id), el('div', 'mgc-sub', '@' + m.client_id));
      top.append(av, who, el('span', 'mg-badge role', m.role === 'owner' ? '主人' : (m.role === 'readonly' ? '只读' : '成员')),
                 el('span', 'mg-badge' + (on ? ' on' : ''), on ? '在线' : '离线'));
      card.append(top);
      if (m.agent_limit != null) {
        var r = el('div', 'mg-row');
        r.append(el('span', 'k', '席位上限'), el('span', 'v', (m.agent_limit || 0) + ' 个'));
        card.append(r);
      }
      if (state.owner && m.role !== 'owner') {
        var acts = el('div', 'mg-acts');
        var bLimit = el('button', 'mg-btn', '改席位上限');
        bLimit.onclick = function () {
          var v = window.prompt('给「' + (m.name || m.client_id) + '」设几个智能体席位？（0 = 按默认）', String(m.agent_limit || 0));
          if (v === null) return;
          var n = parseInt(v, 10);
          if (isNaN(n) || n < 0) { toast('请输入 0 或正整数'); return; }
          send({ type: 'member_agent_limit', client_id: m.client_id, limit: n });
        };
        var bKick = el('button', 'mg-btn danger', '移出');
        bKick.onclick = function () {
          if (!window.confirm('把「' + (m.name || m.client_id) + '」移出这个聊天室？\n\n他名下的智能体会一起踢下线，聊天记录保留。')) return;
          send({ type: 'member_remove', client_id: m.client_id });
        };
        acts.append(bLimit, bKick);
        card.append(acts);
      }
      host.append(card);
    });
  }

  /* ---------------- 邀请 ---------------- */
  function renderInvite() {
    var card = $('inviteCard'); card.textContent = '';
    if (!state.owner) {
      card.append(el('div', 'mg-empty', '只有实例主人能生成邀请链接。'));
      return;
    }
    var form = el('div', 'mg-form');
    var hours = el('select');
    [['24', '24 小时'], ['48', '48 小时'], ['0', '永久']].forEach(function (o) {
      var opt = el('option', null, o[1]); opt.value = o[0]; hours.append(opt);
    });
    var count = el('select');
    [1, 2, 3, 5].forEach(function (n) { var opt = el('option', null, n + ' 条'); opt.value = String(n); count.append(opt); });
    var go = el('button', 'mg-btn primary', '生成邀请链接');
    go.onclick = function () {
      send({ type: 'instance_invite', hours: parseInt(hours.value, 10), count: parseInt(count.value, 10) });
    };
    form.append(el('span', 'k', '有效期'), hours, el('span', 'k', '数量'), count, go);
    card.append(form);
    if (!state.invites.length) {
      card.append(el('div', 'mg-row', '邀请链接给同学后，他用浏览器打开就能注册进这个聊天室。'));
    }
    state.invites.forEach(function (it) {
      var full = location.origin + (it.url || '');
      var line = el('div', 'mg-link');
      line.append(el('code', null, full));
      var b = el('button', 'mg-btn', '复制');
      b.onclick = function () { copy(full, '邀请链接已复制'); };
      line.append(b);
      card.append(line);
    });
  }

  /* ---------------- 房间 ---------------- */
  function renderRooms() {
    var host = $('roomCards'); host.textContent = '';
    $('nRooms').textContent = state.rooms.length ? state.rooms.length + ' 个' : '';
    if (!state.rooms.length) { host.append(el('div', 'mg-empty', '还没有房间')); return; }
    state.rooms.forEach(function (r) {
      var card = el('div', 'mgcard');
      var top = el('div', 'mgc-top');
      var av = el('div', 'mgc-av', initialOf(r.title || r.slug));
      av.style.background = colorOf(r.slug);
      var who = el('div', 'mgc-who');
      who.append(el('div', 'mgc-name', r.title || r.slug), el('div', 'mgc-sub', r.slug));
      var kindText = { lobby: '公共', own: '个人房', dm: '私聊', room: '共享房' }[r.kind] || r.kind || '';
      top.append(av, who, el('span', 'mg-badge', kindText));
      card.append(top);
      if (r.kind === 'room') {
        var acts = el('div', 'mg-acts');
        var bRename = el('button', 'mg-btn', '改名');
        bRename.onclick = function () {
          var v = window.prompt('房间新名字', r.title || '');
          if (v === null || !v.trim()) return;
          send({ type: 'room_rename', slug: r.slug, title: v.trim() });
        };
        var bArch = el('button', 'mg-btn danger', '归档');
        bArch.onclick = function () {
          if (!window.confirm('归档房间「' + (r.title || r.slug) + '」？')) return;
          send({ type: 'room_archive', slug: r.slug });
        };
        acts.append(bRename, bArch);
        card.append(acts);
      }
      host.append(card);
    });
  }

  /* ---------------- 实例 ---------------- */
  function renderInst() {
    var card = $('instCard'); card.textContent = '';
    var rows = [
      ['地址', location.origin],
      ['WebSocket', wsUrl()],
      ['当前身份', state.me ? (state.me.name + '（@' + state.me.client_id + '）') : '—'],
      ['在线', String(Object.keys(state.online).length) + ' 个连接'],
      ['部署', /(^|\.)127\.0\.0\.1|localhost/.test(location.hostname) ? '本机部署' : '服务器部署']
    ];
    rows.forEach(function (p) {
      var r = el('div', 'mg-row');
      r.append(el('span', 'k', p[0]), el('span', 'v', p[1]));
      card.append(r);
    });
  }

  function renderAll() { renderAgents(); renderMembers(); renderInvite(); renderRooms(); renderInst(); }

  /* ---------------- 入口 ---------------- */
  $('addAgent').onclick = function () {
    // 添加智能体走聊天页那个弹窗（需求方：原来的弹窗已经够用），这里只切回去。
    try { window.parent.postMessage({ v: 2, type: 'pubchat-goto-add-agent' }, '*'); } catch (e) {}
    toast('添加智能体在聊天页：点加号 → 接入我的智能体');
  };
  $('newRoom').onclick = function () {
    var v = window.prompt('新房间叫什么？（建好后可在房间里拉人）', '');
    if (v === null || !v.trim()) return;
    send({ type: 'room_create', title: v.trim() });
  };

  // 本机部署才显示「本机服务」入口（公网地址/设备/扫码/自启在老的本机设置页里）
  var isLocal = /^(127\.0\.0\.1|localhost|\[::1\])$/.test(location.hostname) ||
                /^192\.168\.|^10\.|^172\.(1[6-9]|2\d|3[01])\./.test(location.hostname);
  if (isLocal) {
    $('localSec').hidden = false;
    $('openLocal').onclick = function () {
      try { window.parent.postMessage({ v: 2, type: 'pubchat-goto-settings' }, '*'); } catch (e) {}
    };
  }

  if (window.parent && window.parent !== window) document.documentElement.className += ' embed';
  connect();
})();
