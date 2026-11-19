#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Windows 平台专属的「桌面快捷方式 / 开机自启 / 数据目录迁移」辅助。

与聊天核心完全无关，单独成文件，hosts/local.py 只调几个函数。
设计稿 §6.3。
"""
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path


APP = "PubChat"


# --------------------------------------------------------------------------- #
# 路径
# --------------------------------------------------------------------------- #
def _startup_dir():
    """%APPDATA%\\Microsoft\\Windows\\Start Menu\\Programs\\Startup"""
    appdata = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
    return Path(appdata) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"


def _desktop_dir():
    """桌面目录（用户级）"""
    profile = os.environ.get("USERPROFILE") or str(Path.home())
    return Path(profile) / "Desktop"


def _exe_path():
    """当前运行的 exe 路径（打包后是 exe，开发时是 python + 脚本）"""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve()
    return Path(sys.argv[0]).resolve()


def _lnk_name():
    return "%s.lnk" % APP


# --------------------------------------------------------------------------- #
# 开机自启（Startup 文件夹 .lnk，不碰注册表）
# --------------------------------------------------------------------------- #
def autostart_enabled():
    """以磁盘实际状态为准，不信内存标记。"""
    return (_startup_dir() / _lnk_name()).is_file()


def set_autostart(enable, data_dir=None):
    """
    创建或删除 Startup 文件夹的 .lnk。
    - 不碰注册表、不要管理员权限
    - 文案必须写明「开机自启会常驻后台进程」
    - PyInstaller windowed 进程里调 powershell 会闪黑框，用 CREATE_NO_WINDOW
    """
    lnk = _startup_dir() / _lnk_name()
    if not enable:
        try:
            lnk.unlink()
        except FileNotFoundError:
            pass
        return True, ""
    try:
        lnk.parent.mkdir(parents=True, exist_ok=True)
        exe = _exe_path()
        # 带 --data-dir 参数启动，确保用正确的数据目录
        args = [str(exe)]
        if data_dir:
            args.extend(["--data-dir", str(data_dir)])
        return _create_lnk(lnk, args, "开机自动运行 %s（常驻后台进程）" % APP)
    except Exception as exc:
        return False, "创建开机自启快捷方式失败：%s" % exc


# --------------------------------------------------------------------------- #
# 桌面快捷方式
# --------------------------------------------------------------------------- #
def shortcut_exists():
    """以磁盘实际状态为准。"""
    return (_desktop_dir() / _lnk_name()).is_file()


def set_shortcut(enable, data_dir=None):
    """
    创建或删除桌面 .lnk。
    - 创建走 PowerShell WScript.Shell
    - PyInstaller windowed 进程里调 powershell 会闪黑框，用 CREATE_NO_WINDOW
    """
    lnk = _desktop_dir() / _lnk_name()
    if not enable:
        try:
            lnk.unlink()
        except FileNotFoundError:
            pass
        return True, ""
    try:
        # 桌面目录可能被 OneDrive 重定向或压根没建，先确保存在
        lnk.parent.mkdir(parents=True, exist_ok=True)
        exe = _exe_path()
        args = [str(exe)]
        if data_dir:
            args.extend(["--data-dir", str(data_dir)])
        return _create_lnk(lnk, args, "启动 %s" % APP)
    except Exception as exc:
        return False, "创建桌面快捷方式失败：%s" % exc


def _create_lnk(lnk_path, args, description=""):
    """
    用 PowerShell 的 WScript.Shell 创建 .lnk。
    -WindowStyle Hidden 防止闪黑框。
    """
    target = args[0]
    arguments = " ".join('"%s"' % a for a in args[1:]) if len(args) > 1 else ""
    # PowerShell 脚本：创建 WScript.Shell → 设置快捷方式属性 → 保存
    ps_script = r'''
$ws = New-Object -ComObject WScript.Shell
$sc = $ws.CreateShortcut('%s')
$sc.TargetPath = '%s'
$sc.Arguments = '%s'
$sc.Description = '%s'
$sc.WorkingDirectory = '%s'
$sc.Save()
''' % (
        str(lnk_path).replace("'", "''"),
        target.replace("'", "''"),
        arguments.replace("'", "''"),
        description.replace("'", "''"),
        str(Path(target).parent).replace("'", "''"),
    )
    result = subprocess.run(
        ["powershell", "-NoProfile", "-WindowStyle", "Hidden", "-Command", ps_script],
        capture_output=True, timeout=10,
        creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
    )
    # PowerShell 在中文 Windows 上吐的是 GBK，text=True 会直接在读线程里炸
    # （炸完 stderr 变 None，真正的错误原因就看不见了）。自己按两种编码兜。
    def _dec(raw):
        if not raw:
            return ""
        for enc in ("utf-8", "gbk"):
            try:
                return raw.decode(enc)
            except UnicodeDecodeError:
                continue
        return raw.decode("utf-8", "replace")

    out_err = _dec(result.stderr).strip()
    if result.returncode != 0:
        return False, "PowerShell 创建快捷方式失败：%s" % (out_err or "返回码 %s" % result.returncode)
    if not lnk_path.is_file():
        return False, "快捷方式文件未生成%s" % ("：%s" % out_err if out_err else "")
    return True, ""


# --------------------------------------------------------------------------- #
# 数据目录迁移
# --------------------------------------------------------------------------- #
def _default_data_base():
    base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    return Path(base) / APP


def _pointer_path():
    """指针文件：在固定位置 %LOCALAPPDATA%\\PubChat\\location.json"""
    return _default_data_base() / "location.json"


def read_pointer():
    """
    读指针文件，返回目标路径或 None。
    指针不存在 / 损坏 / 目标不可用 分别返回不同的错误信息。
    """
    ptr = _pointer_path()
    if not ptr.is_file():
        return None, "指针文件不存在", None
    try:
        with open(ptr, "r", encoding="utf-8") as f:
            loc = json.load(f)
        target = Path(loc.get("data_dir", "")).expanduser().resolve()
    except (json.JSONDecodeError, KeyError) as exc:
        return None, "数据目录指针文件损坏：%s" % exc, None
    except OSError as exc:
        return None, "读取指针文件失败：%s" % exc, None

    if not target.is_dir():
        return None, "数据目录指针指向的位置不存在：%s" % target, target
    if not os.access(str(target), os.W_OK):
        return None, "数据目录指针指向的位置不可写：%s" % target, target
    return target, None, target


def write_pointer(target):
    """把指针写到 %LOCALAPPDATA%\\PubChat\\location.json。"""
    ptr = _pointer_path()
    ptr.parent.mkdir(parents=True, exist_ok=True)
    data = {"data_dir": str(target)}
    with open(ptr, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def pick_data_dir(new_dir, current_db_path=None):
    """
    迁移数据目录到新位置。

     三条硬要求：
    1. 指针存在但目标不可用 → 报错并让人重选，绝不静默回落默认目录新建空库
    2. 迁移不能用 shutil.copy2 裸拷 .db（WAL 模式会丢数据）
       必须用 SQLite backup API（VACUUM INTO 或 conn.backup()）
    3. 指针文件和默认库同目录（都在 %LOCALAPPDATA%\\PubChat）

    流程：
    1. 校验目标：不能是源的同源/子目录，必须不存在或空目录
    2. 关闭当前数据库连接
    3. 用 VACUUM INTO 迁移（SQLite 自己的 backup，WAL 安全）
    4. 校验 users/agents/messages 行数一致
    5. 改指针
    6. 删源（任一步失败回滚、不删源）

    返回 (ok, error_message, new_db_path)
    """
    if current_db_path is None:
        # 从 server 模块拿当前数据目录（延迟导入避免循环）
        import server
        current_dir = server.DATA
        current_db = current_dir / "pubchat.db"
    else:
        current_db = Path(current_db_path)
        current_dir = current_db.parent

    new_dir = Path(new_dir).expanduser().resolve()

    # 校验 1：目标不能等于源
    if new_dir == current_dir:
        return False, "目标目录与当前目录相同", None

    # 校验 2：目标不能在源目录里面（防止复制到一半自己覆盖自己）
    try:
        new_dir.relative_to(current_dir)
        return False, "目标目录不能是当前目录的子目录", None
    except ValueError:
        pass  # 正常：new_dir 不是 current_dir 的子目录

    # 校验 3：目标必须不存在或空目录
    if new_dir.exists():
        if not new_dir.is_dir():
            return False, "目标路径已存在且不是目录", None
        if any(new_dir.iterdir()):
            return False, "目标目录不为空", None

    if not current_db.is_file():
        return False, "当前数据库文件不存在：%s" % current_db, None

    # 创建目标目录
    try:
        new_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return False, "创建目标目录失败：%s" % exc, None

    new_db = new_dir / "pubchat.db"

    # 迁移：用 SQLite 的 VACUUM INTO（WAL 安全）
    import server
    try:
        # 先关闭当前连接
        if server.STORE and server.STORE.conn:
            server.STORE.conn.close()
            server.STORE.conn = None

        # 用 VACUUM INTO 迁移（比 conn.backup() 更可靠，能处理 WAL）
        vacuum_sql = "VACUUM INTO '%s'" % str(new_db).replace("'", "''")
        src_conn = sqlite3.connect(str(current_db))
        try:
            src_conn.execute(vacuum_sql)
        finally:
            src_conn.close()

        if not new_db.is_file():
            raise OSError("VACUUM INTO 未生成目标文件")

        # 校验行数一致
        if not _verify_db_row_count(current_db, new_db):
            raise OSError("迁移后行数不一致")

        # 改指针（指针文件和默认库同目录，都在 %LOCALAPPDATA%\\PubChat）
        write_pointer(new_dir)

        # 复核指针仍指新目录
        target, err, _ = read_pointer()
        if err or target != new_dir:
            raise OSError("指针写入后验证失败：%s" % err)

        # 迁移成功，删源（保留 -wal 和 -shm 如果有的话）。
        # 删源失败不能触发回滚：指针已经指向新目录了，这时候再清目标就是把
        # 用户的数据两头都没了。留几个旧文件只是占地方，报出来让人自己清。
        leftovers = _remove_db_files(current_dir)
        note = ""
        if leftovers:
            note = "数据已迁到 %s，但旧目录里还剩 %s，可以手动删除" % (
                new_dir, "、".join(leftovers))

        # 更新 server.DATA
        server.DATA = new_dir

        return True, note, str(new_db)

    except Exception as exc:
        # 失败回滚：不删源，指针也不改（write_pointer 之前的任何异常都还没动指针；
        # 之后的异常只可能是删源，已经单独兜住了）
        _remove_db_files(new_dir)  # 清理可能不完整的目标
        try:
            new_dir.rmdir()  # 如果空了就删目录
        except OSError:
            pass
        # 重新打开原来的数据库。这一步自己再挂也不能把迁移的失败原因盖掉，
        # 否则调用方拿到的是个 traceback，用户看到的是「程序崩了」。
        try:
            server.boot()
        except Exception as boot_exc:
            return False, "迁移失败：%s；原库重开也失败（%s），数据仍在 %s" % (
                exc, boot_exc, current_dir), None
        return False, "迁移失败（已回滚）：%s" % exc, None


def _verify_db_row_count(db1, db2):
    """校验两个数据库的 users/agents/messages 行数一致。"""
    try:
        conn1 = sqlite3.connect(str(db1))
        conn2 = sqlite3.connect(str(db2))
        for table in ("users", "agents", "messages"):
            try:
                c1 = conn1.execute("SELECT COUNT(*) FROM %s" % table).fetchone()[0]
                c2 = conn2.execute("SELECT COUNT(*) FROM %s" % table).fetchone()[0]
                if c1 != c2:
                    conn1.close()
                    conn2.close()
                    return False
            except sqlite3.OperationalError:
                # 表不存在，跳过
                pass
        conn1.close()
        conn2.close()
        return True
    except Exception:
        return False


def _remove_db_files(directory):
    """删除目录下的 pubchat.db / -wal / -shm 文件，返回删不掉的 Filename 列表。"""
    d = Path(directory)
    leftovers = []
    for name in ("pubchat.db", "pubchat.db-wal", "pubchat.db-shm"):
        p = d / name
        try:
            p.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            leftovers.append(name)
    return leftovers
