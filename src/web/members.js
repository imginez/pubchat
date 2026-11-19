/* PubChat 成员管理页（只在 127.0.0.1 管理口可打开） */
(function () {
  var $ = function (s) { return document.querySelector(s); };
  // 席位上限只做三选一：0＝不限额，和 WS 侧 member_agent_limit 同一套判据
  var LIMIT_OPTS = [3, 10, 0];
  var LIMIT_LABEL = { 3: '3 个', 10: '10 个', 0: '不限' };
  var state = null;

  function esc(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) {
      return ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":"&#39;"})[c];
    });
  }

  /* 头像气泡 / 状态气泡：成员与智能体都用同一套，页面才不像一排文字 + 按钮 */
  var AV_COLORS = ['#07c160', '#2f6bff', '#ff7d00', '#8b5cf6', '#0ea5e9', '#e5533d', '#12b886', '#b25e00'];
  var ROLE_OPTS = [{ id: 'member', label: '可发言' }, { id: 'readonly', label: '只读' }];

  function hashOf(s) {
    var h = 0, str = String(s || '');
    for (var i = 0; i < str.length; i++) h = (h * 31 + str.charCodeAt(i)) % 100000;
    return h;
  }
  function avatarColor(cid) { return AV_COLORS[hashOf(cid) % AV_COLORS.length]; }
  function initialOf(name) {
    var s = String(name || '').trim();
    return s ? s.slice(0, 1).toUpperCase() : '·';
  }
  function avatar(cid, name) {
    return '<span class="avx" style="background:' + avatarColor(cid) + '">' + esc(initialOf(name)) + '</span>';
  }

  function confirmAction(title, msg) {
    return new Promise(function (resolve) {
      var overlay = document.getElementById('confirmOverlay');
      if (!overlay) { resolve(confirm(title + '\n\n' + msg)); return; }
      document.getElementById('confirmTitle').textContent = title;
      document.getElementById('confirmMsg').textContent = msg;
      overlay.classList.add('show');
      var cleanup = function (result) {
        overlay.classList.remove('show');
        document.getElementById('confirmOk').onclick = null;
        document.getElementById('confirmCancel').onclick = null;
        resolve(result);
      };
      document.getElementById('confirmOk').onclick = function () { cleanup(true); };
      document.getElementById('confirmCancel').onclick = function () { cleanup(false); };
    });
  }

  function toast(msg) {
    var t = $('#toast');
    t.textContent = msg;
    t.classList.add('show');
    clearTimeout(t._timer);
    t._timer = setTimeout(function () { t.classList.remove('show'); }, 1800);
  }

  function copy(text) {
    if (navigator.clipboard && window.isSecureContext) {
      navigator.clipboard.writeText(text).then(function () { toast('已复制'); });
      return;
    }
    var ta = document.createElement('textarea');
    ta.value = text;
    ta.style.position = 'fixed';
    ta.style.opacity = '0';
    document.body.appendChild(ta);
    ta.select();
    try { document.execCommand('copy'); toast('已复制'); } catch (e) { toast('复制失败'); }
    document.body.removeChild(ta);
  }

  function makeQr(text) {
    var qr = qrcode(0, 'M');
    qr.addData(text);
    qr.make();
    return qr.createSvgTag({ cellSize: 4, margin: 3 });
  }

  function api(path, body) {
    return fetch(path, {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(body || {})
    }).then(function (r) { return r.json().then(function (j) { return { ok: r.ok, j: j }; }); })
      .then(function (res) { if (!res.ok) throw new Error(res.j.error || '操作失败'); return res.j; });
  }

  function gotoSeats() {
    if (window.parent && window.parent !== window) {
      try { window.parent.postMessage({ type: 'pubchat-goto-tab', tab: 'seats' }, location.origin); } catch (e) {}
      return;
    }
    location.href = '/admin.html?sec=seats';
  }

  function seatsLink(u) {
    if (!u.agents || !u.agents.length) return '';
    return '<button type="button" class="btn ghost sm" data-goto-seats>智能体席位 →</button>';
  }

  function rolePill(role) {
    var map = { owner: ['blue', '房主'], readonly: ['orange', '只读'], member: ['green', '可发言'] };
    var m = map[role] || map.member;
    return '<span class="bub ' + m[0] + '">' + m[1] + '</span>';
  }

  function onlinePill(isOnline) {
    return isOnline
      ? '<span class="bub green"><i class="dot"></i>在线</span>'
      : '<span class="bub grey">离线</span>';
  }

  function render() {
    $('#ver').textContent = 'v' + (state.version || '');
    var inst = state.instance || {};
    $('#instInfo').textContent = (inst.name || 'PubChat') + ' · #' + (inst.fingerprint || '');
    $('#memberCount').textContent = state.member_count + (state.member_limit ? ' / ' + state.member_limit : ' · 不限');
    renderChrome();
    renderMembers();
    renderRooms();
    renderInvites();
    renderMyAgents();
    renderSeats();
  }

  /* ------------------------------------------------ 设备条 / 回滚条 -- */
  function showDeny(detail) {
    document.body.classList.add('denied');
    var box = $('#denyBox');
    if (box) box.hidden = false;
    var d = $('#denyDetail');
    if (d) d.textContent = detail || '';
  }

  function renderChrome() {
    var on = state.mode === 'server';
    var bar = $('#devBar');
    if (bar) {
      bar.hidden = !on;
      if (on) {
        var cur = (state.devices || []).filter(function (d) { return d.current; })[0];
        $('#devCurrent').textContent = cur ? ('#' + cur.id + ' ' + cur.name) : '未知设备';
      }
    }
    renderRollback();
  }

  var rollbackTimer = null;

  function renderRollback() {
    var bar = $('#rollbackBar');
    if (!bar) return;
    var rb = state.rollback;
    if (!rb || !rb.available) {
      bar.hidden = true;
      if (rollbackTimer) { clearInterval(rollbackTimer); rollbackTimer = null; }
      return;
    }
    bar.hidden = false;
    $('#rollbackText').textContent = '刚执行了「' + (rb.action || '修改') + '」操作，可在倒计时内回滚。';
    var expires = new Date(rb.expires_at);
    function tick() {
      var diff = Math.max(0, Math.floor((expires - new Date()) / 1000));
      $('#rollbackCount').textContent = Math.floor(diff / 60) + ':' +
        (diff % 60 < 10 ? '0' : '') + (diff % 60);
      if (diff <= 0) {
        bar.hidden = true;
        if (rollbackTimer) { clearInterval(rollbackTimer); rollbackTimer = null; }
        load();
      }
    }
    tick();
    // 快照可能被新的改名/审批操作替换：重建定时器，别让旧 expires 关着新快照
    if (rollbackTimer) { clearInterval(rollbackTimer); rollbackTimer = null; }
    rollbackTimer = setInterval(tick, 1000);
  }

  $('#rollbackBtn').addEventListener('click', function () {
    confirmAction('回滚修改', '确定把实例名与加入审批恢复到上次修改之前？').then(function (ok) {
      if (!ok) return;
      api('/api/admin/rollback', {}).then(function () {
        toast('已回滚');
        load();
      }).catch(function (e) { toast(e.message); });
    });
  });

  function renderMembers() {
    var filter = ($('#searchInput').value || '').toLowerCase();
    var users = state.users || [];
    if (filter) {
      users = users.filter(function (u) {
        return (u.name || '').toLowerCase().indexOf(filter) >= 0 ||
               (u.client_id || '').toLowerCase().indexOf(filter) >= 0;
      });
    }
    users.sort(function (a, b) {
    if (a.online && !b.online) return -1;
      if (!a.online && b.online) return 1;
      if (a.role === 'owner' && b.role !== 'owner') return -1;
      if (a.role !== 'owner' && b.role === 'owner') return 1;
      return (a.name || '').localeCompare(b.name || '');
    });

    $('#memberList').innerHTML = users.map(function (u) {
      var isSelf = u.is_admin && u.role === 'owner';
      var isPending = u.status === 'pending';
      var lim = (typeof u.agent_limit === 'number') ? u.agent_limit : 3;
      var pills = (isPending ? '<span class="bub orange">待批准</span>' : '') +
        rolePill(u.role) +
        (isPending ? '' : onlinePill(u.online)) +
        '<span class="bub grey">' + esc(u.source === 'public' ? '远程加入' : '本地创建') + '</span>' +
        '<span class="bub grey">' + (u.agent_count || 0) + ' 个智能体</span>';
      var seats = '';
      if (!isPending) {
        seats = '<div class="mb-a">' +
          '<span class="lim">席位 ' + (u.agent_count || 0) + '/' +
          (lim === 0 ? '不限' : lim) + '</span>' +
          LIMIT_OPTS.map(function (o) {
            return '<button type="button" class="chip' + (o === lim ? ' on' : '') +
              '" data-limit="' + o + '" data-limit-for="' + esc(u.client_id) + '">' +
              LIMIT_LABEL[o] + '</button>';
          }).join('') + '</div>';
      }
      var actions = '';
      if (isPending) {
        actions = '<div class="mb-a">' +
          '<button class="btn sm round" data-approve="' + esc(u.client_id) + '">批准</button>' +
          '<button class="btn danger sm round" data-reject="' + esc(u.client_id) + '">拒绝</button>' +
          '</div>';
      } else if (isSelf) {
        actions = '<div class="mb-a"><span class="muted">这是你的管理员账号</span></div>';
      } else {
        actions = '<div class="mb-a">' +
          '<div class="chips">' +
          ROLE_OPTS.map(function (r) {
            return '<button type="button" class="chip' + (u.role === r.id ? ' on' : '') +
              '" data-set-role="' + esc(u.client_id) + '" data-role="' + r.id + '">' +
              r.label + '</button>';
          }).join('') + '</div>' +
          '<button class="btn ghost sm round" data-reset="' + esc(u.client_id) + '">重置 Key</button>' +
          '<button class="btn danger sm round" data-remove="' + esc(u.client_id) + '">踢出</button>' +
          '</div>';
      }
      return '<article class="member" data-cid="' + esc(u.client_id) + '">' +
        avatar(u.client_id, u.name) +
        '<div class="mb">' +
        '<div class="mb-t"><b>' + esc(u.name) + '</b>' +
        '<span class="at">@' + esc(u.client_id) + '</span>' + pills + '</div>' +
        seats + actions +
        '</div></article>';
    }).join('') || '<div class="muted">没有找到成员。</div>';

    $('#memberList').querySelectorAll('[data-goto-seats]').forEach(function (btn) {
      btn.addEventListener('click', gotoSeats);
    });

    $('#memberList').querySelectorAll('[data-approve]').forEach(function (btn) {
      btn.addEventListener('click', function () {
        var cid = this.getAttribute('data-approve');
        api('/api/admin/member/approve', { client_id: cid })
          .then(function () { toast('已批准'); load(); })
          .catch(function (e) { toast(e.message); });
      });
    });

    $('#memberList').querySelectorAll('[data-reject]').forEach(function (btn) {
      btn.addEventListener('click', function () {
        var cid = this.getAttribute('data-reject');
        confirmAction('拒绝加入', '确定拒绝 ' + cid + ' 的加入申请？').then(function (ok) {
          if (!ok) return;
          api('/api/admin/member/reject', { client_id: cid })
            .then(function () { toast('已拒绝'); load(); })
            .catch(function (e) { toast(e.message); });
        });
      });
    });

    $('#memberList').querySelectorAll('[data-set-role]').forEach(function (btn) {
      btn.addEventListener('click', function () {
        if (this.classList.contains('on')) return;
        var cid = this.getAttribute('data-set-role');
        var role = this.getAttribute('data-role');
        api('/api/admin/member/role', { client_id: cid, role: role })
          .then(function () { toast('角色已更新'); load(); })
          .catch(function (e) { toast(e.message); load(); });
      });
    });

    $('#memberList').querySelectorAll('[data-limit]').forEach(function (btn) {
      btn.addEventListener('click', function () {
        var cid = btn.getAttribute('data-limit-for');
        var limit = parseInt(btn.getAttribute('data-limit'), 10);
        btn.disabled = true;
        api('/api/admin/member/limit', { client_id: cid, limit: limit })
          .then(function () {
            toast(cid + ' 的席位上限已设为' + (limit === 0 ? '不限' : ' ' + limit));
            load();
          })
          .catch(function (e) { btn.disabled = false; toast(e.message); });
      });
    });

    $('#memberList').querySelectorAll('[data-reset]').forEach(function (btn) {
      btn.addEventListener('click', function () {
        var cid = this.getAttribute('data-reset');
        confirmAction('重置 Key', '确定重置 ' + cid + ' 的一级 Key？该成员需要用新 Key 重新登录。').then(function (ok) {
          if (!ok) return;
          api('/api/admin/member/reset-key', { client_id: cid })
            .then(function (j) { toast('新 Key: ' + j.key1); load(); })
            .catch(function (e) { toast(e.message); });
        });
      });
    });

    $('#memberList').querySelectorAll('[data-remove]').forEach(function (btn) {
      btn.addEventListener('click', function () {
        var cid = this.getAttribute('data-remove');
        confirmAction('踢出成员', '确定踢出 ' + cid + '？该成员的所有智能体也会被删除，在线连接立即断开。').then(function (ok) {
          if (!ok) return;
          api('/api/admin/member/remove', { client_id: cid })
            .then(function () { toast('已踢出'); load(); })
            .catch(function (e) { toast(e.message); });
        });
      });
    });
  }

  function selectedRoom() {
    var rid = $('#roomSelect').value;
    var rooms = state.rooms || [];
    for (var i = 0; i < rooms.length; i++) {
      if (rooms[i].room_id === rid) return rooms[i];
    }
    return null;
  }

  function renderRooms() {
    var rooms = state.rooms || [];
    var sel = $('#roomSelect');
    var prev = sel.value;
    sel.innerHTML = rooms.length
      ? rooms.map(function (r) {
          return '<option value="' + esc(r.room_id) + '">' + esc(r.title) + '</option>';
        }).join('')
      : '<option value="">（没有可配置的房间）</option>';
    if (prev && rooms.some(function (r) { return r.room_id === prev; })) sel.value = prev;
    renderRoomDetail();
  }

  function renderRoomDetail() {
    var r = selectedRoom();
    var current = $('#roomCurrent');
    if (!r) {
      current.innerHTML = '<span class="muted">没有可配置的房间。</span>';
      $('#roomCandidates').innerHTML = '';
      return;
    }
    current.innerHTML = r.members.length
      ? r.members.map(function (m) {
          return '<span class="room-member">' + esc(m.name) +
            ' <span class="muted">@' + esc(m.client_id) + '</span>' +
            '<button class="rm-x" data-room-remove="' + esc(m.client_id) + '" title="移出">×</button></span>';
        }).join('')
      : '<span class="muted">还没有第二级成员（房主默认可进）。</span>';
    current.querySelectorAll('[data-room-remove]').forEach(function (btn) {
      btn.addEventListener('click', function () {
        api('/api/admin/room/members', {
          action: 'remove', room: r.room_id, client_id: this.getAttribute('data-room-remove')
        }).then(function () { toast('已移出'); load(); })
          .catch(function (e) { toast(e.message); });
      });
    });

    var memberIds = {};
    r.members.forEach(function (m) { memberIds[m.client_id] = true; });
    var checked = {};
    $('#roomCandidates').querySelectorAll('input:checked').forEach(function (c) {
      checked[c.value] = true;
    });
    var cands = (state.users || []).filter(function (u) {
      return !memberIds[u.client_id] && u.client_id !== r.owner &&
        (u.status || 'active') === 'active';
    });
    $('#roomCandidates').innerHTML = cands.map(function (u) {
      return '<label class="room-cand"><input type="checkbox" value="' + esc(u.client_id) + '"' +
        (checked[u.client_id] ? ' checked' : '') + '> ' + esc(u.name) +
        ' <span class="muted">@' + esc(u.client_id) + '</span></label>';
    }).join('') || '<span class="muted">没有可加入的成员。</span>';
  }

  function renderInvites() {
    var invs = state.invites || [];
    if (!invs.length) {
      $('#invitesList').innerHTML = '<div class="muted" style="margin-top:8px">没有邀请码记录。</div>';
      return;
    }
    $('#invitesList').innerHTML = '<div class="muted" style="margin:14px 0 4px">邀请码记录</div>' +
      invs.map(function (inv) {
        var exp = inv.expires_at ? new Date(inv.expires_at) : null;
        var status = inv.status || (inv.used ? 'used' : 'active');
        var label = status === 'used' ? '已使用'
          : status === 'expired' ? '已过期'
          : inv.role === 'readonly' ? '只读 · 未用' : '可发言 · 未用';
        var cls = status === 'used' ? 'grey'
          : status === 'expired' ? 'red'
          : inv.role === 'readonly' ? 'orange' : 'green';
        return '<div class="inv-item">' +
          '<span class="bub ' + cls + '">' + esc(label) + '</span>' +
          '<span class="muted">' + (exp ? '到期 ' + esc(exp.toLocaleString()) : '长期有效') + '</span>' +
          '<span class="muted">一次性</span>' +
          '<button class="btn danger sm round" data-del-inv="' + parseInt(inv.id, 10) + '">删除</button>' +
          '</div>';
      }).join('');
    $('#invitesList').querySelectorAll('[data-del-inv]').forEach(function (btn) {
      btn.addEventListener('click', function () {
        var id = this.getAttribute('data-del-inv');
        confirmAction('删除邀请码', '确定删除这条邀请码？已发出的链接会失效。').then(function (ok) {
          if (!ok) return;
          api('/api/admin/invite', { action: 'delete', id: parseInt(id) })
            .then(function () { load(); })
            .catch(function (e) { toast(e.message); });
        });
      });
    });
  }

  document.querySelectorAll('#invHours .chip').forEach(function (btn) {
    btn.addEventListener('click', function () {
      document.querySelectorAll('#invHours .chip').forEach(function (b) {
        b.classList.toggle('on', b === btn);
      });
    });
  });

  /* 角色用气泡选（select 仍保留但隐藏，generateInvites 读的还是它） */
  document.querySelectorAll('#invRoleSeg .chip').forEach(function (btn) {
    btn.addEventListener('click', function () {
      document.querySelectorAll('#invRoleSeg .chip').forEach(function (b) {
        b.classList.toggle('on', b === btn);
      });
      var sel = document.getElementById('invRole');
      if (sel) sel.value = btn.getAttribute('data-role');
    });
  });

  /* 数量步进器 */
  (function () {
    var minus = document.getElementById('invMinus');
    var plus = document.getElementById('invPlus');
    var inp = document.getElementById('invCount');
    if (!inp) return;
    function bump(d) {
      inp.value = Math.max(1, Math.min(20, (parseInt(inp.value, 10) || 1) + d));
    }
    if (minus) minus.addEventListener('click', function () { bump(-1); });
    if (plus) plus.addEventListener('click', function () { bump(1); });
  })();

  function showInviteResult(invs, mode) {
    var isLink = mode === 'link';
    $('#invResultTitle').textContent = isLink
      ? '邀请链接（整段发给朋友，打开即加入；每个只能用一次）：'
      : '邀请码（只在生成时显示一次，每个只能用一次）：';
    $('#invList').innerHTML = invs.map(function (inv, i) {
      var head = '<div class="mb-p">' +
        '<span class="bub green">#' + (i + 1) + '</span>' +
        '<span class="bub ' + (inv.role === 'readonly' ? 'orange' : 'blue') + '">' +
        (inv.role === 'readonly' ? '只读' : '可发言') + '</span>' +
        '<span class="bub grey">一次性</span></div>';
      var body;
      if (isLink) {
        var qrHtml = '';
        try {
          qrHtml = '<div class="qr" style="margin-top:10px">' + makeQr(inv.url) + '</div>' +
            '<div class="muted" style="text-align:center;margin-top:5px">扫码加入</div>';
        } catch (e) {}
        body = '<div class="invite-url"><input readonly value="' + esc(inv.url) + '">' +
          '<button class="btn ghost sm round" data-copy="' + esc(inv.url) + '">复制链接</button></div>' + qrHtml;
      } else {
        body = '<div class="invite-url"><input readonly value="' + esc(inv.token) + '">' +
          '<button class="btn ghost sm round" data-copy="' + esc(inv.token) + '">复制码</button></div>';
      }
      return '<div class="inv-block">' + head + body + '</div>';
    }).join('');
    $('#invList').querySelectorAll('[data-copy]').forEach(function (copyBtn) {
      copyBtn.addEventListener('click', function () {
        copy(this.getAttribute('data-copy'));
      });
    });
    $('#invResult').classList.add('show');
  }

  function generateInvites(btn, mode) {
    btn.disabled = true;
    var role = $('#invRole').value;
    var picked = $('#invHours .chip.on');
    var hours = picked ? parseInt(picked.getAttribute('data-hours'), 10) : 24;
    var count = parseInt($('#invCount').value) || 1;

    api('/api/admin/reachability', {}).then(function (r) {
      if (r.has_public === false && r.note) {
        $('#reachWarn').textContent = r.note;
        $('#reachWarn').style.display = '';
      } else {
        $('#reachWarn').style.display = 'none';
      }
      return api('/api/admin/invite', { action: 'create', role: role, hours: hours, count: count });
    }).then(function (j) {
      var invs = j.invites || (j.invite ? [j.invite] : []);
      showInviteResult(invs, mode);
      if (j.note) {
        $('#reachWarn').textContent = j.note;
        $('#reachWarn').style.display = '';
      }
      load();
    }).catch(function (e) {
      toast(e.message);
    }).finally(function () {
      btn.disabled = false;
    });
  }

  $('#genInvBtn').addEventListener('click', function () { generateInvites(this, 'code'); });
  $('#genInvLinkBtn').addEventListener('click', function () { generateInvites(this, 'link'); });

  function renderMyAgents() {
    var box = $('#myAgents');
    if (!box) return;
    var users = state.users || [];
    var me = users.filter(function (u) { return u.role === 'owner'; })[0] ||
             users.filter(function (u) { return (u.agents || []).length; })[0];
    var agents = (me && me.agents) || [];
    var cnt = $('#myAgentCount');
    if (cnt) cnt.textContent = agents.length + ' 个';
    if (!agents.length) {
      box.innerHTML = '<div class="muted">你名下还没有智能体。回聊天页点左下角「＋ 接入我的智能体」，' +
        '选好模式一 / 模式二再复制指令。</div>';
      return;
    }
    box.innerHTML = agents.map(function (a) {
      var inbox = a.mode === 'inbox';
      return '<div class="agent">' +
        '<span class="avx bot">🤖</span>' +
        '<div class="mb" style="flex:1">' +
        '<div class="mb-t"><b>' + esc(a.name || a.client_id) + '</b>' +
        '<span class="at">' + esc(a.client_id) + '</span></div>' +
        '<div class="mb-p"><span class="bub ' + (inbox ? 'orange' : 'green') + '">' +
        (inbox ? '模式二 · 离线收件' : '模式一 · 值守在线') + '</span>' +
        '<span class="bub grey">二级 Key</span></div></div>' +
        '<button type="button" class="btn ghost sm round" data-copy-agent="' + esc(a.client_id) + '">复制接入指令</button>' +
        '</div>';
    }).join('');
    box.querySelectorAll('[data-copy-agent]').forEach(function (btn) {
      btn.addEventListener('click', function () {
        var cid = this.getAttribute('data-copy-agent');
        confirmAction('复制接入指令', '会为 ' + cid + ' 重置一个新的二级 Key（旧 Key 立刻失效），并把用户名/Key/实例地址打包复制。继续？').then(function (ok) {
          if (!ok) return;
          api('/api/admin/agent/reset-key', { client_id: cid }).then(function (j) {
            var txt = '【PubChat 智能体接入】\n用户名：' + cid + '\n二级 Key：' + (j.key2 || '') + '\n实例地址：' + location.origin + '\n（用二级 Key 以 agent 身份连这个实例的 WS）';
            copy(txt);
            toast('接入指令已复制');
          }).catch(function (e) { toast(e.message); });
        });
      });
    });
  }

  $('#searchInput').addEventListener('input', function () { renderMembers(); });

  $('#addForm').addEventListener('submit', function (e) {
    e.preventDefault();
    $('#addErr').textContent = '';
    $('#addBtn').disabled = true;
    api('/api/admin/user', {
      name: $('#addName').value.trim(),
      client_id: $('#addCid').value.trim().toLowerCase()
    }).then(function (j) {
      $('#addName').value = '';
      $('#addCid').value = '';
      toast('已创建 ' + ((j.user && j.user.name) || ''));
      load();
    }).catch(function (err) {
      $('#addErr').textContent = err.message;
    }).finally(function () { $('#addBtn').disabled = false; });
  });

  $('#roomSelect').addEventListener('change', renderRoomDetail);
  $('#roomRefreshBtn').addEventListener('click', function () { load(); });
  $('#addRoomMembersBtn').addEventListener('click', function () {
    var r = selectedRoom();
    if (!r) return;
    var ids = [];
    $('#roomCandidates').querySelectorAll('input:checked').forEach(function (c) {
      ids.push(c.value);
    });
    if (!ids.length) { toast('先勾选要加入的成员'); return; }
    api('/api/admin/room/members', { action: 'add', room: r.room_id, client_ids: ids })
      .then(function () { toast('已加入'); load(); })
      .catch(function (e) { toast(e.message); });
  });

  function load() {
    fetch('/api/state', { cache: 'no-store' }).then(function (r) {
      return r.json().then(function (j) { return { status: r.status, ok: r.ok, j: j || {} }; });
    }).then(function (res) {
      if (res.status === 401 || res.status === 403) {
        showDeny((res.j && res.j.error) || (res.status === 401
          ? '这台设备还没有认领管理权'
          : '管理接口只接受 HTTPS 反代或本机回环访问'));
        return;
      }
      if (!res.ok) throw new Error((res.j && res.j.error) || '读取状态失败');
      document.body.classList.remove('denied');
      var box = $('#denyBox');
      if (box) box.hidden = true;
      state = res.j;
      render();
    }).catch(function (e) {
      if (state) {
        $('#memberList').innerHTML = '<div class="err">读取状态失败，刷新重试。</div>';
      } else {
        showDeny('读取状态失败：' + ((e && e.message) || '网络错误') +
                 '。请确认页面是从已认领设备的浏览器打开。');
      }
    });
  }

  function renderSeats() {
    var box = $('#seatsList');
    if (!box) return;
    var modes = state.agent_modes || [];
    var rows = [];
    (state.users || []).forEach(function (u) {
      (u.agents || []).forEach(function (a) { rows.push({ u: u, a: a }); });
    });
    var cnt = $('#seatCount');
    if (cnt) cnt.textContent = rows.length + ' 个';
    box.innerHTML = rows.map(function (r) {
      var opts = modes.map(function (m) {
        return '<button type="button" class="chip' + (r.a.mode === m.id ? ' on' : '') +
          '" data-mode-for="' + esc(r.a.client_id) + '" data-mode="' + esc(m.id) + '">' +
          esc(m.label) + '</button>';
      }).join('');
      return '<div class="agent">' +
        '<span class="avx bot">🤖</span>' +
        '<div class="mb" style="flex:1">' +
        '<div class="mb-t"><b>' + esc(r.a.name) + '</b>' +
        '<span class="at">' + esc(r.a.client_id) + '</span></div>' +
        '<div class="mb-p"><span class="bub blue">归属 ' + esc(r.u.name) + '</span>' +
        '<span class="bub grey">' + esc(r.u.client_id) + '</span></div></div>' +
        '<div class="chips" style="margin-left:auto">' + opts + '</div>' +
        '<button type="button" class="btn ghost sm round" data-reset-key="' + esc(r.a.client_id) + '">重置二级 Key</button>' +
        '<div class="akey">' + esc(r.a.key2) + '</div></div>';
    }).join('') || '<div class="muted">还没有智能体席位。点聊天页左下角「＋ 接入我的智能体」或搜索框右边的「＋」就能加。</div>';

    box.querySelectorAll('[data-reset-key]').forEach(function (btn) {
      btn.addEventListener('click', function () {
        var cid = this.getAttribute('data-reset-key');
        confirmAction('重置智能体 Key', '确定重置智能体 ' + cid + ' 的二级 Key？旧 Key 立刻失效，正在用它的智能体要重新配置。').then(function (ok) {
          if (!ok) return;
          api('/api/admin/agent/reset-key', { client_id: cid })
            .then(function (j) { toast('新二级Key: ' + j.key2); load(); })
            .catch(function (e) { toast(e.message); });
        });
      });
    });

    box.querySelectorAll('[data-mode-for]').forEach(function (btn) {
      btn.addEventListener('click', function () {
        if (this.classList.contains('on')) return;
        var cid = this.getAttribute('data-mode-for'), mode = this.getAttribute('data-mode'), s = this;
        s.disabled = true;
        api('/api/admin/agent/mode', { client_id: cid, mode: mode })
          .then(function () { toast('已切换'); load(); })
          .catch(function (e) { toast(e.message); load(); })
          .finally(function () { s.disabled = false; });
      });
    });
  }

  load();
  setInterval(load, 5000);
})();
