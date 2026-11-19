/* 运行时推导 WS 地址：页面在根路径或子路径下都能用 */
(function () {
  var prefix = location.pathname.replace(/\/[^\/]*$/, '');
  var proto = location.protocol === 'https:' ? 'wss://' : 'ws://';
  window.PUB_CONFIG = {
    ws: proto + location.host + prefix + '/ws',
    historyLimit: 60,

    /* ---------- 左侧会话分组 ----------
       一条 = 一个分组目录。改这里就够了，不用碰 app.js。
         id          : 写进 localStorage 的键名，别随便改（改了折叠状态会丢）
         label       : 标题文字
         rooms       : 把哪些 room.kind 收进这一组；'*' 表示"其余所有类型"
         defaultOpen : 默认展开(true)/折叠(false)；不写就用 groupsDefaultExpanded
         roster      : 名单组（不是会话组）。'agents'=我的智能体，'members'=全部成员
                       名单组没有房间和未读，只用来展示成员和快捷跳转；行首圆点=在线状态
       未匹配到任何组的类型自动落到 '*' 那组，再没有就落到最后一组。 */
    groups: [
      { id: 'contact',  label: '联系人',   rooms: ['dm'],      defaultOpen: true },
      { id: 'group',    label: '群聊',     rooms: ['lobby'],   defaultOpen: true },
      // 智能体⇄智能体私聊（S2）：与手机端 uni 侧同名分组，默认收起，未读红点挂在分组头。
      // agentDm: true = 这一组带「＋ 和智能体单聊」入口，且空组也保留（否则第一次开不了房）。
      { id: 'agentsdm', label: '智能体协作', rooms: ['agent_dm'], defaultOpen: false, agentDm: true },
      { id: 'other',    label: '其他',     rooms: ['own', '*'], defaultOpen: true },
      { id: 'myagents', label: '我的智能体', roster: 'agents',  defaultOpen: true },
      { id: 'members',  label: '成员',     roster: 'members',  defaultOpen: true }
    ],
    groupsDefaultExpanded: true, // 上面没写 defaultOpen 的组，用这个
    hideEmptyGroups: true,       // 空分组不显示（搜索时也只显示有命中的组）

    /* 拖拽排序：触屏设备默认关闭（触屏上拖拽和滚动打架），需要就改 true */
    dragOnTouch: false
  };
})();
