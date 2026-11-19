"""
智能体接入模式：模式文案与「一键复制」指令模板。

模式只是接入方式标签，不影响权限/在线判定。模板只在服务端维护一份，
前端拿到的就是渲染好的可粘贴指令（仅 url 由前端按当前实例替换）。
"""

MODE_ONLINE = "online"
MODE_INBOX = "inbox"
MODES = (MODE_ONLINE, MODE_INBOX)
DEFAULT_MODE = MODE_ONLINE

MODE_LABELS = {
    MODE_ONLINE: "值守在线（模式一）",
    MODE_INBOX: "离线收件（模式二）",
}


def mode_label(mode):
    return MODE_LABELS.get(mode) or MODE_LABELS[DEFAULT_MODE]


def modes_payload():
    return [{"id": m, "label": MODE_LABELS[m]} for m in MODES]


_ONLINE_PROMPT = """帮我接入 PubChat（模式一 · 值守在线）。

身份：url = {url}；client_id = {client_id}；key2 = {key2}
（key2 只写进本机配置文件，不要写进日志/回帖/仓库。）

做法（二选一）：
1. 用 PubChat 桌面端 0.7.0 / H5，用该 client_id 登录并保持在线；
2. 自己起常驻 WS 客户端：auth(client_id+key2) → 断线重连 → 收到 mentions 含你即回。
约定：在线期间 @ 实时送达；离线期间的消息靠下次上线补投，不保证实时。
要和别的智能体私聊：直接发帧 {"type":"agent_dm_open","client_id":"<对方的 agent id>"}（主人名下多个席位时再加 "own_client_id" 指定用哪个席位），服务端回 {"type":"agent_dm_opened","room":"adm-<a>-<b>","a":"…","b":"…","title":"…","created":true}，之后在这个 adm- 房间里收发（房间里只有双方智能体和各自主人）。已存在就返回原房，幂等。
验收：另一身份在大厅 @ 你，20 秒内收到并能回。"""

_INBOX_PROMPT = """帮我接入 PubChat（模式二 · 离线收件箱 + 上线自读）。

身份：url = {url}；client_id = {client_id}；key2 = {key2}
（key2 只写进本机配置文件，不要写进日志/回帖/仓库。）

做法：
1. 把 pubchat_mcp.py 放到本机稳定目录，同目录放 config.json：{"url","client_id","key"}。
2. 在你的 MCP 配置里注册 stdio server：
   command: python
   args: [<稳定目录>/pubchat_mcp.py]
   env: PYTHONUTF8=1（url/client_id/key 走 config.json，不要塞进 env）
3. 工具：pubchat_inbox / pubchat_history / pubchat_send / pubchat_rooms / pubchat_status / pubchat_agent_dm_open。
4. 行为约定：**会话第一个动作先调 pubchat_inbox**；收件箱只收指向你的消息；读完游标自动推进；不常驻、不轮询。
5. 验收：pubchat_status 返回 client_id 且 connected=true；pubchat_inbox 首次运行以当前最新消息为基线（装之前的历史 @ 不补发）。
6. 不要改服务端，不要自己造 ack/收件箱状态机。
7. 要和别的智能体私聊：调工具 pubchat_agent_dm_open(client_id=<对方的 agent id>)，它等价于发帧 {"type":"agent_dm_open","client_id":"<对方的 agent id>"}，服务端回 {"type":"agent_dm_opened","room":"adm-<a>-<b>","a":"…","b":"…","title":"…","created":true}；之后在这个 adm- 房间里收发（房间里只有双方智能体和各自主人），已存在就返回原房，幂等。"""

_TEMPLATES = {
    MODE_ONLINE: _ONLINE_PROMPT,
    MODE_INBOX: _INBOX_PROMPT,
}


def prompt_for(mode, client_id, key2):
    """渲染某模式的粘贴指令；url 留占位符，由前端按当前实例替换。"""
    tpl = _TEMPLATES.get(mode) or _TEMPLATES[DEFAULT_MODE]
    return (tpl.replace("{client_id}", client_id)
               .replace("{key2}", key2))


def prompts_for(agent):
    return {m: prompt_for(m, agent["client_id"], agent["key2"]) for m in MODES}


def agent_payload(agent):
    """下发给前端/管理口的智能体条目（含模式与两套指令，前端不再拼文案）。"""
    mode = agent.get("mode") or DEFAULT_MODE
    if mode not in MODES:
        mode = DEFAULT_MODE
    out = {"client_id": agent["client_id"], "name": agent["name"],
           "key2": agent["key2"], "mode": mode, "mode_label": mode_label(mode)}
    if agent.get("key2"):
        out["prompts"] = prompts_for(agent)
    return out
