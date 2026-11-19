/* PubChat 管理页（只在 127.0.0.1 可打开） */
(function () {
  var $ = function (s) { return document.querySelector(s); };
  var state = null;
  var scanIdx = 0;
  var viewMode = null;       // 用户点了但还没保存的模式（避免轮询把界面弹回去）

  function esc(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) {
      return ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[c];
    });
  }

  function confirmAction(title, msg) {
    return new Promise(function (resolve) {
      var overlay = $('#confirmOverlay');
      $('#confirmTitle').textContent = title;
      $('#confirmMsg').textContent = msg;
      overlay.classList.add('show');
      var cleanup = function (result) {
        overlay.classList.remove('show');
        $('#confirmOk').onclick = null;
        $('#confirmCancel').onclick = null;
        resolve(result);
      };
      $('#confirmOk').onclick = function () { cleanup(true); };
      $('#confirmCancel').onclick = function () { cleanup(false); };
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

  function render() {
    $('#ver').textContent = 'v' + state.version;

    renderScan();
    renderTunnel(state.tunnel);
    renderSeats();
    renderDevices();
    renderRollback();
    renderServer();
  }

  /* ------------------------------------------------------- 服务器模式 -- */
  function serverMode() { return state && state.mode === 'server'; }

  function showDeny(detail) {
    document.body.classList.add('denied');
    var box = $('#denyBox');
    if (box) box.hidden = false;
    var d = $('#denyDetail');
    if (d) d.textContent = detail || '';
  }

  function showDeniedOff() {
    document.body.classList.remove('denied');
    var box = $('#denyBox');
    if (box) box.hidden = true;
  }

  function renderServer() {
    var on = serverMode();
    document.documentElement.classList.toggle('mode-server', on);
    var bar = $('#devBar');
    var sec = $('#serverSection');
    var off = $('#serverUnavailable');
    if (off) off.hidden = on;
    if (!on) {
      if (bar) bar.hidden = true;
      if (sec) sec.style.display = 'none';
      return;
    }
    if (bar) bar.hidden = false;
    var cur = (state.devices || []).filter(function (d) { return d.current; })[0];
    $('#devCurrent').textContent = cur ? ('#' + cur.id + ' ' + cur.name) : '未知设备';
    $('#devBarHint').textContent = cur
      ? '当前设备不在这里吊销自己（防自锁）；换机请用「生成新认领链接」。最后活动 ' + formatTime(cur.last_seen)
      : '这台浏览器的设备凭证不在服务器列表里，可能已被吊销';
    if (sec) {
      sec.style.display = '';
      $('#srvName').value = (state.instance && state.instance.name) || '';
      $('#srvFp').textContent = (state.instance && state.instance.fingerprint) || '-';
      $('#srvPorts').textContent = state.port + '（对外） / ' + state.admin_port + '（管理）';
      $('#srvData').textContent = state.data_dir || '-';
      $('#srvApproval').checked = !!(state.instance && state.instance.require_approval);
    }
  }

  function adminPost(path, body) {
    return fetch(path, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body || {})
    }).then(function (r) {
      return r.json().then(function (j) {
        if (!r.ok || !j.ok) throw new Error((j && j.error) || '操作失败');
        return j;
      });
    });
  }

  $('#srvRename').addEventListener('click', function () {
    if (!serverMode()) return;
    var name = $('#srvName').value.trim();
    var old = (state.instance && state.instance.name) || '';
    if (!name) { toast('实例名不能为空'); return; }
    if (name === old) { toast('名字没有变化'); return; }
    confirmAction('修改实例名',
      '把实例名从「' + old + '」改成「' + name + '」？所有客户端会看到新名字。15 分钟内可回滚。')
      .then(function (ok) {
        if (!ok) return;
        adminPost('/api/admin/instance', { name: name }).then(function () {
          toast('已改名');
          load();
        }).catch(function (e) { toast(e.message); });
      });
  });

  $('#srvApproval').addEventListener('change', function () {
    if (!serverMode()) return;
    var want = this.checked, box = this;
    confirmAction('加入审批',
      want ? '开启后，新成员用邀请码加入会先进入待审列表，需要房主批准才能登录。确定开启？'
           : '关闭后，新成员用邀请码加入可以直接登录。确定关闭？')
      .then(function (ok) {
        if (!ok) { box.checked = !want; return; }
        adminPost('/api/admin/instance', { require_approval: want }).then(function () {
          toast(want ? '已开启加入审批' : '已关闭加入审批');
          load();
        }).catch(function (e) { box.checked = !want; toast(e.message); });
      });
  });

  /* ---------------------------------------------------------- 扫码登录 -- */
  // 扫码用的基地址，按可信度排序：远端实例地址 > 隧道地址 > 公网/局域网地址 > 本机地址
  function bestBase() {
    var r = String(state.remote_url || '').replace(/\/+$/, '');
    if (r) return r + '/';
    var t = state.tunnel;
    if (t && t.state === 'on' && t.url) return t.url;
    var lan = (state.addresses && state.addresses.lan && state.addresses.lan[0]) || '';
    if (lan) return lan;
    return (state.addresses && state.addresses.local) || '';
  }

  function currentScanUser() {
    var users = state.users || [];
    if (!users.length) return null;
    if (scanIdx >= users.length) scanIdx = 0;
    return users[scanIdx];
  }

  function renderScan() {
    // 远端（云端）实例：本机不出码没意义，直接用远端实例地址出码，手机扫了就能打开那台服务器。
    // 以前这里写「扫不开」把功能做死了（需求方报「生成二维码当前不可用」），已改成照样出码。
    var remote = String(state.remote_url || '').replace(/\/+$/, '');
    if (state.mode === 'remote' && !remote) {
      $('#scanUser').innerHTML = '<option value="">远端地址还没填</option>';
      $('#scanUser').disabled = true;
      $('#qr').innerHTML = '<div class="muted">还不知道远端实例地址，先到「服务器设置」里填公网地址，这里就能出码。</div>';
      $('#qrHint').innerHTML = '';
      $('#qrWarn').hidden = true;
      return;
    }
    $('#scanUser').disabled = false;

    var users = state.users || [];
    var sel = $('#scanUser');
    if (scanIdx >= users.length) scanIdx = 0;
    sel.innerHTML = users.map(function (u, i) {
      return '<option value="' + i + '"' + (i === scanIdx ? ' selected' : '') + '>' +
        esc(u.name) + ' · ' + esc(u.client_id) + '</option>';
    }).join('') || '<option value="">还没有成员</option>';

    var base = bestBase();
    var u = currentScanUser();
    var text = base;
    try {
      $('#qr').innerHTML = makeQr(text);
    } catch (e) {
      $('#qr').innerHTML = '<div class="muted">二维码生成失败，直接复制上面的地址。</div>';
    }
    $('#qrHint').textContent = '扫码打开：' + base;
    $('#qrWarn').hidden = false;
    $('#qrWarn').textContent = '安全提示：二维码不含 Key，请手动输入登录凭证。';
  }

  $('#scanUser').addEventListener('change', function () {
    scanIdx = parseInt(this.value, 10) || 0;
    renderScan();
  });

  /* ------------------------------------------------------------ 公网 -- */
  function showPanel(mode) {
    ['quick', 'named', 'manual'].forEach(function (m) {
      $('#pub' + m.charAt(0).toUpperCase() + m.slice(1)).classList.toggle('active', m === mode);
      var r = document.querySelector('input[name="pubMode"][value="' + m + '"]');
      if (r) r.checked = m === mode;
    });
  }

  function renderTunnel(t) {
    // 服务器部署：地址由服务器与 nginx 决定，程序改不了；只做只读展示，别把整块藏起来变空白。
    if (serverMode()) {
      var note = $('#pubServerNote');
      var localBox = $('#pubLocalBox');
      var pub = (state.addresses && state.addresses.lan && state.addresses.lan[0]) ||
        state.remote_url || '';
      if (note) note.hidden = false;
      if (localBox) localBox.hidden = true;
      var f = $('#pubServerUrl');
      if (f && document.activeElement !== f) f.value = pub;
      return;
    }
    var noteL = $('#pubServerNote');
    var localL = $('#pubLocalBox');
    if (noteL) noteL.hidden = true;
    if (localL) localL.hidden = false;

    t = t || { state: 'off', mode: 'quick', detail: '' };
    var mode = viewMode || t.mode || 'quick';
    showPanel(mode);

    var on = t.state === 'on' && t.url;
    var pill = $('#tunnelPill');
    pill.textContent = on ? '已开启' :
      (t.state === 'starting' ? '连接中' : (t.state === 'error' ? '出错' : '未启用'));
    pill.className = 'pill' + (on ? ' on' : '');

    if (on) {
      $('#tunnelText').innerHTML = '<a href="' + esc(t.url) + '" target="_blank" rel="noreferrer">' +
        esc(t.url) + '</a>';
      $('#tunnelQr').style.display = '';
      $('#tunnelQrHint').textContent = t.mode === 'quick' ? '外部的人扫这个码（临时地址）' : '外部的人扫这个码';
      try { $('#tunnelQr').innerHTML = makeQr(t.url); }
      catch (e) { $('#tunnelQr').style.display = 'none'; $('#tunnelQrHint').textContent = ''; }
    } else {
      $('#tunnelText').textContent = t.detail || '未开启';
      $('#tunnelQr').style.display = 'none';
      $('#tunnelQr').innerHTML = '';
      $('#tunnelQrHint').textContent = '';
    }

    var btn = $('#tunnelBtn');
    btn.disabled = t.state === 'starting';
    btn.textContent = (t.mode === 'quick' && on) ? '关闭临时地址' :
      (t.state === 'starting' && t.mode === 'quick' ? '申请中…' : '开启临时地址');
    if (t.available === false) {
      $('#tunnelHelp').innerHTML = '需要 cloudflared.exe：' +
        '<a href="https://github.com/cloudflare/cloudflared/releases/latest" target="_blank" rel="noreferrer">下载</a>' +
        ' 后放到程序同目录，再点开启。';
    } else {
      $('#tunnelHelp').textContent = t.mode === 'quick'
        ? '这是临时地址，下次启动会换新的；关掉程序即断开。' : '';
    }

    var namedStop = $('#namedStop');
    namedStop.disabled = !(t.mode === 'named' && (t.state === 'on' || t.state === 'starting'));

    var namedUrl = $('#publicUrlInput');
    var manualUrl = $('#manualUrlInput');
    if (document.activeElement !== namedUrl) namedUrl.value = t.public_url || '';
    if (document.activeElement !== manualUrl) manualUrl.value = t.public_url || '';
    $('#tokenInput').placeholder = t.has_token
      ? '已保存 Token（留空保持不变）' : '粘贴 Cloudflare 隧道 Token';
    $('#autoStart').checked = !!t.auto;

    $('#tunnelBanner').textContent = t.mode === 'named'
      ? '固定域名需要在 Cloudflare Zero Trust 里建一个命名隧道，把公网主机名指到 http://127.0.0.1:' + state.port +
        '，再把 Token 填到上面。地址重启不变，Token 只存本机。'
      : (t.mode === 'manual'
        ? '这个地址由你自己的反向代理或别的隧道提供，程序只负责展示和生成二维码。'
        : '临时地址由 Cloudflare 快速隧道提供，零账号、零配置，关掉程序就失效。要长期使用，选「固定域名」。');
  }

  function postTunnel(body) {
    return fetch('/api/admin/tunnel', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body)
    }).then(function (r) {
      return r.json().then(function (j) {
        if (!r.ok) throw new Error(j.error || '操作失败');
        return j;
      });
    });
  }

  document.querySelectorAll('input[name="pubMode"]').forEach(function (r) {
    r.addEventListener('change', function () {
      viewMode = this.value;
      showPanel(viewMode);
    });
  });

  var pubCopyBtn = $('#pubServerCopy');
  if (pubCopyBtn) {
    pubCopyBtn.addEventListener('click', function () {
      var f = $('#pubServerUrl');
      copy((f && f.value) || '');
    });
  }

  $('#tunnelBtn').addEventListener('click', function () {
    var t = state.tunnel || {};
    var btn = this;
    btn.disabled = true;
    var chain;
    if (t.mode !== 'quick') {
      chain = postTunnel({ action: 'config', mode: 'quick' }).then(function () {
        return postTunnel({ action: 'start' });
      });
    } else {
      chain = postTunnel({ action: (t.state === 'on' && t.url) ? 'stop' : 'start' });
    }
    chain.then(function () {
      viewMode = null;
      toast('已切换');
      load();
    }).catch(function (e) {
      toast(e.message || '操作失败');
      btn.disabled = false;
    });
  });

  $('#namedSave').addEventListener('click', function () {
    var token = $('#tokenInput').value.trim();
    var url = $('#publicUrlInput').value.trim();
    if (!token && !(state.tunnel && state.tunnel.has_token)) {
      toast('请先填 Cloudflare 隧道 Token');
      return;
    }
    var btn = this;
    btn.disabled = true;
    postTunnel({ action: 'config', mode: 'named', token: token || null,
                 public_url: url, auto: $('#autoStart').checked })
      .then(function () { return postTunnel({ action: 'start' }); })
      .then(function () {
        $('#tokenInput').value = '';
        viewMode = null;
        toast('已保存，正在连接…');
        load();
      })
      .catch(function (e) { toast(e.message || '保存失败'); })
      .finally(function () { btn.disabled = false; });
  });

  $('#namedStop').addEventListener('click', function () {
    if (!(state.tunnel && state.tunnel.mode === 'named')) {
      toast('先点「保存并开启」');
      return;
    }
    postTunnel({ action: 'stop' }).then(load).catch(function (e) { toast(e.message || '停止失败'); });
  });

  $('#manualSave').addEventListener('click', function () {
    var url = $('#manualUrlInput').value.trim();
    var btn = this;
    btn.disabled = true;
    postTunnel({ action: 'config', mode: 'manual', public_url: url })
      .then(function () {
        viewMode = null;
        toast('已保存');
        load();
      })
      .catch(function (e) { toast(e.message || '保存失败'); })
      .finally(function () { btn.disabled = false; });
  });

  /* -------------------------------------------------------- 智能体席位 -- */
  function renderSeats() {
    var modes = state.agent_modes || [];
    var rows = [];
    state.users.forEach(function (u) {
      (u.agents || []).forEach(function (a) { rows.push({ u: u, a: a }); });
    });

    $('#seatsList').innerHTML = rows.map(function (r) {
      var opts = modes.map(function (m) {
        return '<option value="' + esc(m.id) + '"' + (r.a.mode === m.id ? ' selected' : '') +
          '>' + esc(m.label) + '</option>';
      }).join('');
      return '<div class="agent">' +
        '<span class="aid">' + esc(r.a.name) + ' · ' + esc(r.a.client_id) + '</span>' +
        '<span class="owner">归属 ' + esc(r.u.name) + '（' + esc(r.u.client_id) + '）</span>' +
        '<label class="amode">接入方式 <select class="mode-sel" data-cid="' + esc(r.a.client_id) + '">' + opts + '</select></label>' +
        '<button type="button" class="btn ghost sm" data-reset-key="' + esc(r.a.client_id) + '">重置二级Key</button>' +
        '<div class="akey">' + esc(r.a.key2) + '</div></div>';
    }).join('') || '<div class="muted">还没有智能体席位。到「成员与 Key」页签给成员加体系位。</div>';

    $('#seatsList').querySelectorAll('[data-reset-key]').forEach(function (btn) {
      btn.addEventListener('click', function () {
        var cid = this.getAttribute('data-reset-key');
        confirmAction('重置智能体 Key', '确定重置智能体 ' + cid + ' 的二级 Key？旧 Key 立刻失效，正在用它的智能体要重新配置。')
          .then(function (ok) {
            if (!ok) return;
            adminPost('/api/admin/agent/reset-key', { client_id: cid })
              .then(function (j) { toast('新二级Key: ' + j.key2); load(); })
              .catch(function (e) { toast(e.message); });
          });
      });
    });

    $('#seatsList').querySelectorAll('.mode-sel').forEach(function (sel) {
      sel.addEventListener('change', function () {
        var cid = this.getAttribute('data-cid'), mode = this.value, box = this;
        box.disabled = true;
        fetch('/api/admin/agent/mode', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ client_id: cid, mode: mode })
        }).then(function (r) { return r.json().then(function (j) { return { ok: r.ok, j: j }; }); })
          .then(function (res) {
            if (!res.ok) throw new Error(res.j.error || '切换失败');
            toast('已切换 ' + cid + ' 为 ' + modeLabelOf(mode));
            load();
          })
          .catch(function (e) { toast(e.message); load(); })
          .finally(function () { box.disabled = false; });
      });
    });
  }

  function modeLabelOf(id) {
    var hit = (state.agent_modes || []).filter(function (m) { return m.id === id; })[0];
    return hit ? hit.label : id;
  }

  /* ------------------------------------------------------------ 设备 -- */
  function formatTime(ts) {
    if (!ts) return '-';
    try {
      var d = new Date(ts);
      return d.toLocaleString('zh-CN', { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' });
    } catch (e) { return ts; }
  }

  function renderDevices() {
    var devices = state.devices || [];
    var section = $('#deviceSection');
    if (!section) return;
    var off = $('#devicesUnavailable');
    if (off) off.hidden = !(state.mode !== 'server' && !devices.length);
    // 本机桌面宿主不显示设备管理（没有设备凭证概念）
    if (state.mode !== 'server' && !devices.length) {
      section.style.display = 'none';
      return;
    }
    section.style.display = '';
    $('#devices').innerHTML = devices.map(function (d) {
      var cls = 'device' + (d.current ? ' current' : '') + (d.revoked ? ' revoked' : '');
      var pills = '';
      if (d.current) pills += '<span class="pill current">当前设备</span>';
      if (d.revoked) pills += '<span class="pill revoked">已吊销</span>';
      var actions = '';
      if (!d.revoked && !d.current) {
        actions = '<div class="actions"><button class="btn danger sm" data-revoke="' + d.id + '">吊销</button></div>';
      }
      return '<div class="' + cls + '" data-id="' + d.id + '">' +
        '<div class="top"><span class="name">' + esc(d.name) + '</span>' + pills + '</div>' +
        '<div class="meta">添加于 ' + formatTime(d.created_at) +
        (d.last_seen ? ' · 最后活动 ' + formatTime(d.last_seen) : '') +
        '</div>' + actions + '</div>';
    }).join('') || '<div class="muted">还没有授权的设备。</div>';

    $('#devices').querySelectorAll('[data-revoke]').forEach(function (btn) {
      btn.addEventListener('click', function () {
        var id = parseInt(this.getAttribute('data-revoke'));
        confirmAction('吊销设备', '确定吊销这台设备的管理权限？吊销后该设备需要重新认领才能管理本实例。').then(function (ok) {
          if (!ok) return;
          fetch('/api/admin/devices', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ action: 'revoke', id: id })
          }).then(function (r) { return r.json(); })
            .then(function (j) {
              if (!j.ok) throw new Error(j.error || '吊销失败');
              toast('已吊销');
              if (j.self) {
                // 吊销了自己，清理本地凭证
                if (window.PubChatDevice) window.PubChatDevice.clear();
                setTimeout(function () { location.reload(); }, 1000);
              } else {
                load();
              }
            })
            .catch(function (e) { toast(e.message); });
        });
      });
    });
  }

  /* ---------------------------------------------------------- 回滚 -- */
  var rollbackTimer = null;

  function renderRollback() {
    var box = $('#rollbackBox');
    if (!box) return;
    var rb = state.rollback;
    if (!rb || !rb.available) {
      box.innerHTML = '';
      if (rollbackTimer) { clearInterval(rollbackTimer); rollbackTimer = null; }
      return;
    }
    var expires = new Date(rb.expires_at);
    function updateCountdown() {
      var now = new Date();
      var diff = Math.max(0, Math.floor((expires - now) / 1000));
      var min = Math.floor(diff / 60);
      var sec = diff % 60;
      var el = box.querySelector('.countdown');
      if (el) el.textContent = min + ':' + (sec < 10 ? '0' : '') + sec;
      if (diff <= 0) {
        box.innerHTML = '';
        if (rollbackTimer) { clearInterval(rollbackTimer); rollbackTimer = null; }
        load();
      }
    }
    box.innerHTML = '<div class="rollback-banner">' +
      '<span class="msg">刚执行了「' + esc(rb.action) + '」操作，可在倒计时内回滚。</span>' +
      '<span class="countdown"></span>' +
      '<button class="btn ghost sm" id="rollbackBtn">回滚</button>' +
      '</div>';
    updateCountdown();
    // 快照可能被新的改名/审批操作替换：重建定时器，别让旧 expires 关着新快照
    if (rollbackTimer) { clearInterval(rollbackTimer); rollbackTimer = null; }
    rollbackTimer = setInterval(updateCountdown, 1000);
    $('#rollbackBtn').addEventListener('click', function () {
      confirmAction('回滚操作', '确定要回滚「' + rb.action + '」吗？这会恢复之前的设置。').then(function (ok) {
        if (!ok) return;
        fetch('/api/admin/rollback', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({})
        }).then(function (r) { return r.json(); })
          .then(function (j) {
            if (!j.ok) throw new Error(j.error || '回滚失败');
            toast('已回滚');
            load();
          })
          .catch(function (e) { toast(e.message); });
      });
    });
  }

  /* -------------------------------------------------------- 认领链接 -- */
  $('#newClaimBtn').addEventListener('click', function () {
    var btn = this;
    btn.disabled = true;
    fetch('/api/admin/devices', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ action: 'invite' })
    }).then(function (r) { return r.json(); })
      .then(function (j) {
        if (!j.ok) throw new Error(j.error || '生成失败');
        var box = $('#claimResult');
        box.style.display = '';
        box.innerHTML = '<div class="warn" style="background:#e8f0ff;color:#1d2129;border:1px solid #b3cdff">' +
          '<b>认领链接（5 分钟内有效，仅一次）：</b><br>' +
          '<code style="word-break:break-all;font-size:12px">' + esc(j.link) + '</code><br><br>' +
          '<b>6 位验证码：</b><span style="font-size:18px;font-weight:600;letter-spacing:2px">' + esc(j.code) + '</span><br>' +
          '<span class="muted">到期：' + esc(j.expires_at) + '</span>' +
          '</div>';
        try {
          var qr = qrcode(0, 'M');
          qr.addData(j.link);
          qr.make();
          box.innerHTML += '<div class="qr" style="margin-top:10px">' + qr.createSvgTag({ cellSize: 4, margin: 3 }) + '</div>';
        } catch (e) {}
      })
      .catch(function (e) { toast(e.message); })
      .finally(function () { btn.disabled = false; });
  });

  function load() {
    fetch('/api/state', { cache: 'no-store' }).then(function (r) {
      return r.json().then(function (j) { return { status: r.status, ok: r.ok, j: j || {} }; });
    }).then(function (res) {
      if (res.status === 401) {
        showDeny((res.j && res.j.error) || '这台设备还没有认领管理权');
        return;
      }
      if (res.status === 403) {
        showDeny((res.j && res.j.error) || '管理接口只接受 HTTPS 反代或本机回环访问');
        return;
      }
      if (!res.ok) throw new Error((res.j && res.j.error) || '读取状态失败');
      showDeniedOff();
      state = res.j;
      render();
      if (state.mode === 'server' && !load._poll) {
        load._poll = setInterval(load, 5000);
      }
      if (state.tunnel && state.tunnel.state === 'starting') {
        clearTimeout(load._t);
        load._t = setTimeout(load, 1200);
      }
    }).catch(function (e) {
      showDeny('读取状态失败：' + (e && e.message ? e.message : '网络错误') + '。请确认页面是通过 nginx 的 HTTPS 反代打开，且已认领设备。');
    });
  }

  load();
})();
