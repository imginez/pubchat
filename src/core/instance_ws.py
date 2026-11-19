"""
实例管理类 WS 帧（0.8.24：手机端功能补齐）。

背景：手机端（uni-client / APK）连的是服务器**公网口**，而公网口只转发页面与
/ws；`/api/admin/*` 只在回环管理口上（见 hosts/server.py::make_process_request）。
所以手机想「重置成员一级 Key / 看并撤销已授权设备 / 改实例设置」只能走 WS 帧。

权限判据与 core/member_admin.py 同口径：`role == "owner"` 的人类账号，
is_admin 不管用（管理权跟账号走，不跟机器走）。

实例设置（name / require_approval / allow_room_create）存在 instance.json，
WS 层本身够不着，由宿主通过 inst_getter / inst_saver 注入；宿主没注入时
settings_get / settings_set 回 ERR_UNSUPPORTED，绝不假装成功。
"""
from core.protocol import (ERR_EMPTY_NAME, ERR_NO_PERM, ERR_NOT_FOUND,
                           ERR_UNSUPPORTED)
from core.rooms import me_of

SETTINGS_KEYS = ("name", "require_approval", "allow_room_create")
NAME_MAX = 24


def _is_owner(ident):
    return ident.get("kind") == "human" and ident.get("role") == "owner"


def _deny(hub, ws, message="只有服务器主人能做这个操作"):
    return hub.send(ws, {"type": "error", "code": ERR_NO_PERM, "message": message})


def _as_bool(value, default=False):
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value).strip().lower()
    if text in ("1", "true", "yes", "on", "开", "开启"):
        return True
    if text in ("0", "false", "no", "off", "关", "关闭"):
        return False
    return default


def device_rows(store, this_device_id=None):
    """store.list_devices() → WS 回执用的行。

    表里没有 platform 列，档位按设备名猜一个（设备名本来就从 UA 取），
    猜不到留空——手机上显示时按空值跳过，不编数据。
    """
    out = []
    for d in store.list_devices():
        name = d.get("name") or ""
        out.append({
            "id": d.get("id"),
            "name": name,
            "platform": _platform_of(name),
            "last_seen": d.get("last_seen") or "",
            "created_at": d.get("created_at") or "",
            "active": not bool(d.get("revoked")),
            "this_device": bool(this_device_id is not None
                                and d.get("id") == this_device_id),
        })
    return out


def _platform_of(name):
    low = (name or "").lower()
    for key, label in (("iphone", "iOS"), ("ipad", "iOS"), ("ios", "iOS"),
                       ("android", "Android"), ("鸿蒙", "HarmonyOS"),
                       ("windows", "Windows"), ("mac", "macOS"),
                       ("linux", "Linux")):
        if key in low:
            return label
    return ""


def settings_payload(inst):
    inst = inst or {}
    return {
        "type": "settings",
        "name": inst.get("name") or "PubChat",
        "require_approval": bool(inst.get("require_approval")),
        "allow_room_create": bool(inst.get("allow_room_create", True)),
        "public_url": inst.get("public_url") or "",
    }


# ----------------------------- 重置成员一级 Key ----------------------------- #

async def handle_member_reset_key(ws, store, hub, ident, data, log=None):
    """owner 重置某成员的一级 Key：旧 Key 立刻失效，新 Key 只在这一次回执里出现。"""
    if not _is_owner(ident):
        return await _deny(hub, ws, "只有服务器主人能重置成员的一级 Key")
    cid = (data.get("client_id") or "").strip().lower()
    if not cid or not store.user(cid):
        return await hub.send(ws, {"type": "error", "code": ERR_NOT_FOUND,
                                   "message": "没有这个成员"})
    new_key = store.reset_user_key(cid)
    if log:
        log("member reset key", cid, "by", me_of(ident))
    await hub.send(ws, {"type": "member_key_reset", "client_id": cid,
                        "key1": new_key})


# ------------------------------ 已授权设备 ------------------------------ #

async def handle_devices_list(ws, store, hub, ident, data=None, log=None,
                              this_device_id=None):
    if not _is_owner(ident):
        return await _deny(hub, ws, "只有服务器主人能看设备列表")
    items = device_rows(store, this_device_id)
    if log:
        log("devices list", "by", me_of(ident), "n=%d" % len(items))
    await hub.send(ws, {"type": "devices", "items": items,
                        "device_max": 10})


async def handle_device_revoke(ws, store, hub, ident, data, log=None,
                               this_device_id=None):
    """撤销一台已授权设备。只接受整数 id；假 id / 已撤销一律 NOT_FOUND，不抛异常。"""
    if not _is_owner(ident):
        return await _deny(hub, ws, "只有服务器主人能撤销设备")
    raw = data.get("id")
    try:
        did = int(raw)
    except (TypeError, ValueError):
        return await hub.send(ws, {"type": "error", "code": ERR_NOT_FOUND,
                                   "message": "设备 id 不合法"})
    if this_device_id is not None and did == this_device_id:
        return await _deny(hub, ws, "不能撤销正在用的这台设备")
    if not store.revoke_device(did):
        return await hub.send(ws, {"type": "error", "code": ERR_NOT_FOUND,
                                   "message": "设备不存在，或已经吊销过了"})
    if log:
        log("device revoke", "#%d" % did, "by", me_of(ident))
    await hub.send(ws, {"type": "device_revoked", "id": did})


# ------------------------------- 实例设置 ------------------------------- #

async def handle_settings_get(ws, store, hub, ident, data=None, log=None,
                              inst_getter=None):
    if not _is_owner(ident):
        return await _deny(hub, ws, "只有服务器主人能看服务器设置")
    if inst_getter is None:
        return await hub.send(ws, {"type": "error", "code": ERR_UNSUPPORTED,
                                   "message": "这个宿主管不到实例设置"})
    try:
        inst = inst_getter() or {}
    except Exception:
        inst = {}
    await hub.send(ws, settings_payload(inst))


async def handle_settings_set(ws, store, hub, ident, data, log=None,
                              inst_getter=None, inst_saver=None):
    """改三项设置。只认 SETTINGS_KEYS；没带的字段保持原值。"""
    if not _is_owner(ident):
        return await _deny(hub, ws, "只有服务器主人能改服务器设置")
    if inst_getter is None or inst_saver is None:
        return await hub.send(ws, {"type": "error", "code": ERR_UNSUPPORTED,
                                   "message": "这个宿主管不到实例设置"})
    try:
        inst = dict(inst_getter() or {})
    except Exception:
        inst = {}

    if "name" in data and data.get("name") is not None:
        name = str(data.get("name") or "").strip()[:NAME_MAX]
        if not name:
            return await hub.send(ws, {"type": "error", "code": ERR_EMPTY_NAME,
                                       "message": "服务器名称不能为空"})
        inst["name"] = name
    if "require_approval" in data:
        inst["require_approval"] = _as_bool(data.get("require_approval"))
    if "allow_room_create" in data:
        inst["allow_room_create"] = _as_bool(data.get("allow_room_create"), True)

    try:
        inst_saver(inst)
    except Exception as exc:
        if log:
            log("settings save failed", me_of(ident), exc)
        return await hub.send(ws, {"type": "error", "code": ERR_UNSUPPORTED,
                                   "message": "写实例设置失败，请稍后再试"})
    if log:
        log("settings set", "by", me_of(ident),
            "name=%r approval=%s room_create=%s"
            % (inst.get("name"), bool(inst.get("require_approval")),
               bool(inst.get("allow_room_create", True))))
    await hub.send(ws, settings_payload(inst))
