"""
PubChat WS 协议常量。

字段名、消息类型枚举、解析/序列化。两个宿主共用，协议不动。
"""
import re

LOBBY = "lobby"
SLUG_LIMIT = 32

# 消息类型
TYPE_AUTH = "auth"
TYPE_JOIN = "join"
TYPE_MESSAGE = "message"
TYPE_HISTORY = "history"
TYPE_ROOMS = "rooms"
TYPE_PRESENCE = "presence"
TYPE_PROFILE = "profile"
TYPE_AGENT_RENAME = "agent_rename"
TYPE_AGENT_CREATE = "agent_create"
TYPE_AGENT_REMOVE = "agent_remove"
TYPE_AGENT_SET_MODE = "agent_set_mode"
# 换二级 Key（0.8.15）：本人对自己的席位换，旧 Key 立刻失效、在线连接踢下线
TYPE_AGENT_ROTATE_KEY = "agent_rotate_key"
TYPE_ONBOARDED = "onboarded"
TYPE_PING = "ping"
TYPE_PONG = "pong"
# 房间参与者（@ 补全用）
TYPE_ROOM_MEMBERS = "room_members"
# 任务流（二期：流式进度卡片）
TYPE_TASK_STREAM = "task_stream"
TYPE_TASK_LIST = "task_list"
TYPE_TASK_STATE = "task_state"
# 输入中状态：C→S 宣布「我在回复」/「我回完了」，S→C 按房广播；不落库、不回 ack
TYPE_TYPING = "typing"
# 成员管理（仅实例 owner）：列表 / 移出服务器 / 智能体席位上限
TYPE_MEMBER_LIST = "member_list"
TYPE_MEMBER_REMOVE = "member_remove"
TYPE_MEMBER_AGENT_LIMIT = "member_agent_limit"
# 智能体 ⇄ 智能体 私聊：开一条 adm-<a>-<b> 房 / 回执
TYPE_AGENT_DM_OPEN = "agent_dm_open"
TYPE_AGENT_DM_OPENED = "agent_dm_opened"
# 0.8.24 手机端功能补齐（只走 WS，服务器公网口不转发 /api/admin/*）：
# 重置成员一级 Key、已授权设备列表与撤销、实例设置读写。
TYPE_MEMBER_RESET_KEY = "member_reset_key"
TYPE_MEMBER_KEY_RESET = "member_key_reset"
TYPE_DEVICES_LIST = "devices_list"
TYPE_DEVICES = "devices"
TYPE_DEVICE_REVOKE = "device_revoke"
TYPE_DEVICE_REVOKED = "device_revoked"
TYPE_SETTINGS_GET = "settings_get"
TYPE_SETTINGS_SET = "settings_set"
TYPE_SETTINGS = "settings"
# 0.8 批4：读回执。客户端看到某条消息就回一帧 message_ack，服务端在房内向里广播
# message_acked（带累计 acked 名单），发任务收尾的一方据此判断「真有人收到」。
TYPE_MESSAGE_ACK = "message_ack"
TYPE_MESSAGE_ACKED = "message_acked"
TYPE_DELIVERY = "delivery"

# 任务流状态
TASK_RUNNING = "running"
TASK_DONE = "done"
TASK_ERROR = "error"
TASK_STATUSES = (TASK_RUNNING, TASK_DONE, TASK_ERROR)

# @全体成员：出现这个 token 就算提及本房所有人
MENTION_ALL_TOKENS = ("@全体成员", "@所有人", "@all")
MENTION_ALL_LABEL = "全体成员"

# 错误码
ERR_BAD_LOGIN = "BAD_LOGIN"
ERR_BAD_AGENT_KEY = "BAD_AGENT_KEY"
ERR_BAD_KIND = "BAD_KIND"
ERR_AUTH_REQUIRED = "AUTH_REQUIRED"
ERR_EMPTY = "EMPTY"
ERR_NO_PERM = "NO_PERM"
ERR_READ_ONLY = "READONLY"
ERR_EMPTY_NAME = "EMPTY_NAME"
ERR_WEAK_KEY = "WEAK_KEY"
ERR_BAD_USERNAME = "BAD_USERNAME"
ERR_USERNAME_TAKEN = "USERNAME_TAKEN"
ERR_NO_AGENT = "NO_AGENT"
ERR_BAD_MODE = "BAD_MODE"
ERR_EMPTY_TOKEN = "EMPTY_TOKEN"
ERR_EXPIRED = "EXPIRED"
ERR_USED = "USED"
ERR_BAD_INVITE = "BAD_INVITE"
ERR_FULL = "FULL"
ERR_BAD_CID = "BAD_CID"
ERR_CID_TAKEN = "CID_TAKEN"
ERR_PENDING = "PENDING"
ERR_NOT_FOUND = "NOT_FOUND"
ERR_AGENT_LIMIT = "AGENT_LIMIT"
ERR_BAD_LIMIT = "BAD_LIMIT"
ERR_AGENT_DM_SELF = "AGENT_DM_SELF"
ERR_AGENT_DM_AMBIGUOUS = "AGENT_DM_AMBIGUOUS"
ERR_BAD_REPLY = "BAD_REPLY"
# 0.8.24：宿主没把 instance.json 的读写函数传进来时，设置类帧回这个码，
# 而不是抛 500 或假装成功（老宿主/老客户端混连时不至于崩）。
ERR_UNSUPPORTED = "UNSUPPORTED"
ERR_UNKNOWN = "UNKNOWN"


def valid_client_id(s):
    """登录名校验：小写字母/数字/下划线，2-32 位。"""
    return bool(s) and bool(re.fullmatch(r"[a-z0-9_]{2,32}", s))
