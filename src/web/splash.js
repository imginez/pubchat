/* 开屏动画（S3，桌面与手机共用同一份样式与退场逻辑）
 *
 * 标为什么写在 HTML 里而不是这里造：脚本注入的覆盖层自己就要等脚本执行完才存在，
 * 那段空档正是要盖住的白屏。所以页面里预留 #pcSplash 这一小段标记，本文件只负责
 * 样式、计时、点击跳过与 ready() 提前退场；页面没预留时（例如以后新加的入口）
 * 才回退成脚本自己造一个。
 *
 * 规则：最长 1200ms；宿主数据到货调 PubChatSplash.ready() 可提前退；点任意处即跳过；
 * prefers-reduced-motion 不放动效。
 */
(function () {
  'use strict';

  var MAX_MS = 1200;
  var MIN_MS = 260;          // 比这更短就成了一闪而过的抖，不如不播
  var KEY = 'pc-splash';
  var reduced = false;
  try {
    reduced = window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  } catch (e) {}
  /* 外壳与手机首页是把聊天 H5 装在 iframe 里切来切去的：那不是冷启动，不放开屏。
     取不到 top（跨域沙箱）时同样按被嵌入处理，宁可不放也不要每帧都闪一次。 */
  var embedded = false;
  try { embedded = window.top !== window.self; } catch (e) { embedded = true; }

  var style = document.createElement('style');
  // 底色跟随页面自己的 --bg：退场淡出时不能出现一次色差
  style.textContent =
    '.' + KEY + '{position:fixed;inset:0;z-index:2147483000;display:flex;flex-direction:column;' +
    'align-items:center;justify-content:center;gap:14px;cursor:pointer;background:var(--bg,#f2f4f7);' +
    'color:#1d2129;font:15px/1.6 -apple-system,BlinkMacSystemFont,"PingFang SC","Microsoft YaHei",sans-serif;' +
    'transition:opacity .18s ease}' +
    '.' + KEY + '.out{opacity:0;pointer-events:none}' +
    '.' + KEY + ' .mk{width:56px;height:56px;border-radius:14px;background:#07c160;color:#fff;' +
    'display:flex;align-items:center;justify-content:center;font-size:30px;font-weight:700;' +
    'box-shadow:0 8px 24px rgba(7,193,96,.24)}' +
    '.' + KEY + ' .nm{font-size:19px;font-weight:600;letter-spacing:.02em}' +
    '.' + KEY + ' .sb{font-size:13px;color:#86909c;margin-top:-8px}' +
    '.' + KEY + ' .ld{width:18px;height:18px;border-radius:50%;border:2px solid #d6dae1;' +
    'border-top-color:#07c160;animation:' + KEY + '-spin .8s linear infinite}' +
    '@keyframes ' + KEY + '-spin{to{transform:rotate(360deg)}}' +
    '@media (prefers-reduced-motion: reduce){.' + KEY + ' .ld{animation:none}}';
  (document.head || document.documentElement).appendChild(style);

  function fallback() {
    var el = document.createElement('div');
    el.className = KEY;
    el.id = 'pcSplash';
    el.innerHTML = '<div class="mk">P</div><div class="nm">PubChat</div>' +
      '<div class="sb">自托管的聊天室</div><div class="ld"></div>';
    document.body.appendChild(el);
    return el;
  }

  /* 本文件在 <head> 里同步执行：样式先落地，body 里的 #pcSplash 一被解析出来就是带样的，
     第一次绘制不会是白屏。真正的接线放到 DOMContentLoaded。 */
  var el = null;
  var gone = false;
  var pending = false;
  var started = Date.now();

  function hide() {
    if (gone || !el) return;
    gone = true;
    el.classList.add('out');
    if (reduced) { if (el.parentNode) el.parentNode.removeChild(el); return; }
    setTimeout(function () { if (el.parentNode) el.parentNode.removeChild(el); }, 220);
  }

  /* 页面脚本挂在 body 末尾，执行时 DOMContentLoaded 还没发到本组件：
     这时 el 尚未接线，直接 return 会把「第一屏已经就绪」这个信号丢掉，
     开屏就白撑到 MAX_MS。所以先记成 pending，wire() 一接上就退。 */
  function ready() {
    if (gone) return;
    if (!el) { pending = true; return; }
    var left = MIN_MS - (Date.now() - started);
    if (left > 0) setTimeout(hide, left); else hide();
  }

  function wire() {
    if (embedded) {                 // 被外壳嵌着：摘掉预留标记，静默不播
      gone = true;
      var pre = document.getElementById('pcSplash');
      if (pre && pre.parentNode) pre.parentNode.removeChild(pre);
      return;
    }
    el = document.getElementById('pcSplash') || fallback();
    el.setAttribute('role', 'status');
    el.setAttribute('aria-live', 'polite');
    el.addEventListener('click', hide);
    if (reduced || gone || pending) hide();     // ready() 可能先于 DOMContentLoaded 被调
  }

  if (document.body) wire();
  else document.addEventListener('DOMContentLoaded', wire);
  if (reduced) ready();
  else setTimeout(hide, MAX_MS);

  window.PubChatSplash = { ready: ready, hide: hide };
})();
