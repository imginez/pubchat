"""
PubChat 存储层 —— SQLite 封装，宿主无关。

Store 类封装所有数据库操作：users / agents / messages / room_members / invites。
"""
import hashlib
import json
import secrets
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

CST = timezone(timedelta(hours=8))


def _hash_token(token):
    return hashlib.sha256(token.encode()).hexdigest()


SCHEMA = """
CREATE TABLE IF NOT EXISTS users(
  client_id TEXT PRIMARY KEY,
  name      TEXT NOT NULL,
  key1      TEXT NOT NULL,
  is_admin  INTEGER DEFAULT 0,
  agent_limit INTEGER DEFAULT 3,
  created_at TEXT
);
CREATE TABLE IF NOT EXISTS agents(
  client_id  TEXT PRIMARY KEY,
  name       TEXT NOT NULL,
  owner      TEXT NOT NULL,
  key2       TEXT NOT NULL,
  mode       TEXT DEFAULT 'online',
  created_at TEXT
);
CREATE TABLE IF NOT EXISTS messages(
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  room       TEXT NOT NULL,
  sender     TEXT NOT NULL,
  sender_name TEXT NOT NULL,
  body       TEXT NOT NULL,
  mentions   TEXT DEFAULT '[]',
  created_at TEXT NOT NULL,
  reply_to   INTEGER
);
CREATE INDEX IF NOT EXISTS idx_msg_room ON messages(room, id);
CREATE TABLE IF NOT EXISTS room_members(
  room_id    TEXT NOT NULL,
  user_id    TEXT NOT NULL,
  role       TEXT DEFAULT 'member',
  joined_at  TEXT,
  PRIMARY KEY (room_id, user_id)
);
CREATE TABLE IF NOT EXISTS invites(
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  token_hash  TEXT NOT NULL UNIQUE,
  role        TEXT DEFAULT 'member',
  expires_at  TEXT,
  max_uses    INTEGER DEFAULT 1,
  used        INTEGER DEFAULT 0,
  created_by  TEXT,
  created_at  TEXT,
  room_id     TEXT
);
CREATE TABLE IF NOT EXISTS rooms(
  room_id    TEXT PRIMARY KEY,
  title      TEXT NOT NULL,
  owner      TEXT NOT NULL,
  visibility TEXT DEFAULT 'invite',
  created_at TEXT NOT NULL,
  archived   INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS admin_bootstrap(
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  token_hash  TEXT NOT NULL UNIQUE,
  code_hash   TEXT NOT NULL,
  attempts    INTEGER DEFAULT 0,
  expires_at  TEXT NOT NULL,
  used        INTEGER DEFAULT 0,
  created_at  TEXT
);
CREATE TABLE IF NOT EXISTS admin_device(
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  name        TEXT NOT NULL,
  secret_hash TEXT NOT NULL UNIQUE,
  revoked     INTEGER DEFAULT 0,
  created_by  TEXT DEFAULT '',
  created_at  TEXT,
  last_seen   TEXT
);
CREATE TABLE IF NOT EXISTS admin_audit(
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  ts          TEXT NOT NULL,
  device      TEXT DEFAULT '',
  ip          TEXT DEFAULT '',
  action      TEXT DEFAULT '',
  result      TEXT DEFAULT '',
  detail      TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS admin_token(
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  token_hash  TEXT NOT NULL UNIQUE,
  expires_at  TEXT NOT NULL,
  used        INTEGER DEFAULT 0,
  kid         TEXT DEFAULT '',
  created_at  TEXT
);
CREATE TABLE IF NOT EXISTS tasks(
  id          TEXT PRIMARY KEY,
  room        TEXT NOT NULL,
  title       TEXT NOT NULL DEFAULT '',
  owner       TEXT NOT NULL DEFAULT '',
  owner_name  TEXT NOT NULL DEFAULT '',
  status      TEXT NOT NULL DEFAULT 'running',
  created_at  TEXT NOT NULL,
  updated_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_task_room ON tasks(room, updated_at);
CREATE TABLE IF NOT EXISTS task_events(
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  task_id     TEXT NOT NULL,
  agent       TEXT NOT NULL DEFAULT '',
  agent_name  TEXT NOT NULL DEFAULT '',
  kind        TEXT NOT NULL DEFAULT 'delta',
  text        TEXT NOT NULL DEFAULT '',
  ts          TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_taskev ON task_events(task_id, id);
CREATE TABLE IF NOT EXISTS agent_dm(
  a           TEXT NOT NULL,
  b           TEXT NOT NULL,
  created_by  TEXT,
  created_at  TEXT,
  archived    INTEGER DEFAULT 0,
  PRIMARY KEY(a, b)
);
"""

MEMBER_LIMIT = 0  # 0 = 不限成员数
INVITE_DEFAULT_HOURS = 24
RATE_LIMIT_WINDOW = 60
RATE_LIMIT_MAX = 10


def now_iso():
    return datetime.now(CST).isoformat(timespec="seconds")


def jd(o):
    return json.dumps(o, ensure_ascii=False)


class Store:
    def __init__(self, path):
        self.path = str(path)
        self.lock = threading.Lock()
        self.conn = sqlite3.connect(self.path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        cols = {r[1] for r in self.conn.execute("PRAGMA table_info(users)")}
        has_role = "role" in cols
        version = self.conn.execute("PRAGMA user_version").fetchone()[0]
        try:
            self.conn.execute("ALTER TABLE users ADD COLUMN onboarded INTEGER DEFAULT 0")
        except sqlite3.OperationalError:
            pass
        if not has_role:
            self.conn.execute("ALTER TABLE users ADD COLUMN role TEXT DEFAULT 'member'")
        try:
            self.conn.execute("ALTER TABLE users ADD COLUMN display_name TEXT DEFAULT ''")
        except sqlite3.OperationalError:
            pass
        try:
            self.conn.execute("ALTER TABLE users ADD COLUMN source TEXT DEFAULT 'local'")
        except sqlite3.OperationalError:
            pass
        try:
            self.conn.execute("ALTER TABLE users ADD COLUMN status TEXT DEFAULT 'active'")
        except sqlite3.OperationalError:
            pass
        try:
            self.conn.execute("ALTER TABLE invites ADD COLUMN room_id TEXT")
        except sqlite3.OperationalError:
            pass
        try:
            self.conn.execute("ALTER TABLE agents ADD COLUMN mode TEXT DEFAULT 'online'")
        except sqlite3.OperationalError:
            pass
        try:
            self.conn.execute("ALTER TABLE users ADD COLUMN agent_limit INTEGER DEFAULT 3")
        except sqlite3.OperationalError:
            pass
        # 0.8 批4：回复引用。只加列，老库解出来 reply_to 为 NULL，语义与「不是引用」一致。
        try:
            self.conn.execute("ALTER TABLE messages ADD COLUMN reply_to INTEGER")
        except sqlite3.OperationalError:
            pass
        # 0 = 不限额；存量行按新默认 3 回填，NULL/负数一律归 3
        self.conn.execute("UPDATE users SET agent_limit=3 "
                          "WHERE agent_limit IS NULL OR agent_limit<0")
        self.conn.execute("UPDATE agents SET mode='online' WHERE mode IS NULL OR mode=''")
        self.conn.execute("UPDATE invites SET max_uses=1 WHERE max_uses IS NULL OR max_uses != 1")
        if not has_role or version < 1:
            self._migrate_roles()
        self.conn.execute("PRAGMA user_version=3")
        self.conn.commit()

    def _migrate_roles(self):
        """老库的 role 列是后加的：最早创建的账号是房主，其余一律成员。"""
        rows = self.conn.execute(
            "SELECT client_id FROM users ORDER BY created_at, rowid").fetchall()
        if not rows:
            return
        first = rows[0][0]
        self.conn.execute("UPDATE users SET role='member' WHERE client_id<>?", (first,))
        self.conn.execute("UPDATE users SET role='owner' WHERE client_id=?", (first,))

    def q(self, sql, args=()):
        with self.lock:
            return self.conn.execute(sql, args).fetchall()

    def x(self, sql, args=()):
        with self.lock:
            cur = self.conn.execute(sql, args)
            self.conn.commit()
            return cur

    # -- users --
    def user(self, client_id):
        r = self.q("SELECT * FROM users WHERE client_id=?", (client_id,))
        return dict(r[0]) if r else None

    def users(self):
        return [dict(r) for r in self.q("SELECT * FROM users ORDER BY created_at")]

    def add_user(self, client_id, name, key1, is_admin=0, role="member", source="local",
                 status="active"):
        self.x("INSERT INTO users(client_id,name,key1,is_admin,created_at,role,source,status) "
               "VALUES(?,?,?,?,?,?,?,?)",
               (client_id, name, key1, is_admin, now_iso(), role, source, status))

    def profile_human(self, old_cid, new_cid, name, key1):
        with self.lock:
            c = self.conn
            rooms = [r[0] for r in c.execute("SELECT DISTINCT room FROM messages").fetchall()]
            c.execute("UPDATE users SET client_id=?, name=?, key1=?, onboarded=1 WHERE client_id=?",
                      (new_cid, name, key1, old_cid))
            if new_cid != old_cid:
                c.execute("UPDATE agents SET owner=? WHERE owner=?", (new_cid, old_cid))
                c.execute("UPDATE messages SET sender=? WHERE sender=?", (new_cid, old_cid))
                for room in rooms:
                    new_room = room
                    if room == "own-" + old_cid:
                        new_room = "own-" + new_cid
                    elif room.startswith("dm-"):
                        try:
                            a, b = room[3:].split("-", 1)
                        except ValueError:
                            continue
                        if old_cid in (a, b):
                            new_room = "dm-" + "-".join(
                                sorted([new_cid if x == old_cid else x for x in (a, b)]))
                    if new_room != room:
                        c.execute("UPDATE messages SET room=? WHERE room=?", (new_room, room))
            c.commit()

    def mark_onboarded(self, cid):
        self.x("UPDATE users SET onboarded=1 WHERE client_id=?", (cid,))

    # -- agents --
    def agent(self, client_id):
        r = self.q("SELECT * FROM agents WHERE client_id=?", (client_id,))
        return dict(r[0]) if r else None

    def agents_of(self, owner):
        return [dict(r) for r in self.q(
            "SELECT * FROM agents WHERE owner=? ORDER BY client_id", (owner,))]

    def all_agents(self):
        return [dict(r) for r in self.q("SELECT * FROM agents ORDER BY client_id")]

    def add_agent(self, client_id, name, owner, key2, mode="online"):
        self.x("INSERT INTO agents(client_id,name,owner,key2,mode,created_at) "
               "VALUES(?,?,?,?,?,?)",
               (client_id, name, owner, key2, mode, now_iso()))

    def rename_agent(self, client_id, name):
        self.x("UPDATE agents SET name=? WHERE client_id=?", (name, client_id))

    def set_agent_mode(self, client_id, mode):
        self.x("UPDATE agents SET mode=? WHERE client_id=?", (mode, client_id))

    def remove_agent(self, client_id):
        with self.lock:
            self.conn.execute("DELETE FROM room_members WHERE user_id=?", (client_id,))
            self.conn.execute("DELETE FROM agents WHERE client_id=?", (client_id,))
            self._drop_agent_dm(client_id)
            self.conn.commit()

    # -- agent_dm（智能体 ⇄ 智能体 私聊配对）--
    def agent_dm(self, a, b):
        r = self.q("SELECT * FROM agent_dm WHERE a=? AND b=?", (a, b))
        return dict(r[0]) if r else None

    def agent_dm_rows(self):
        return [dict(r) for r in self.q("SELECT * FROM agent_dm ORDER BY a, b")]

    def open_agent_dm(self, a, b, created_by):
        """幂等开一条配对；已归档的被再次打开时复活（不新增行、不丢历史消息）。"""
        self.x("INSERT OR IGNORE INTO agent_dm(a,b,created_by,created_at,archived) "
               "VALUES(?,?,?,?,0)", (a, b, created_by, now_iso()))
        self.x("UPDATE agent_dm SET archived=0 WHERE a=? AND b=?", (a, b))

    def _drop_agent_dm(self, agent_id):
        # 级联只从 remove_agent / remove_user 内部走：那两个方法已持有 self.lock，
        # 而这把锁是 threading.Lock（非重入），所以这里不再包一个「公开版」的 with self.lock
        # ——那个方法零调用方，而且一旦被谁从 remove_* 里调上就是自死锁。
        self.conn.execute("DELETE FROM agent_dm WHERE a=? OR b=?", (agent_id, agent_id))

    # -- messages --
    def add_message(self, room, sender, sender_name, body, mentions, reply_to=None):
        cur = self.x(
            "INSERT INTO messages(room,sender,sender_name,body,mentions,created_at,reply_to) "
            "VALUES(?,?,?,?,?,?,?)",
            (room, sender, sender_name, body, jd(mentions), now_iso(),
             int(reply_to) if reply_to else None))
        mid = cur.lastrowid
        r = self.q("SELECT * FROM messages WHERE id=?", (mid,))
        return self.row_to_msg(dict(r[0]))

    def message(self, mid):
        """按 id 取一条消息（引用解析用）。取不到回 None。"""
        try:
            mid = int(mid)
        except (TypeError, ValueError):
            return None
        r = self.q("SELECT * FROM messages WHERE id=?", (mid,))
        return self.row_to_msg(dict(r[0])) if r else None

    def messages_by_ids(self, ids):
        """{id: msg}：历史回包要把被引用的那条的名字与摘要一起带上，免得客户端再拉一遍。"""
        ids = sorted({int(i) for i in ids if i})
        if not ids:
            return {}
        marks = ",".join("?" * len(ids))
        rows = self.q("SELECT * FROM messages WHERE id IN (%s)" % marks, tuple(ids)) or []
        return {r["id"]: self.row_to_msg(dict(r)) for r in rows}

    def message_counts_by_sender(self):
        """按发送者统计条数（模型中转站的用量列）。只读，不改任何东西。"""
        rows = self.q("SELECT sender, COUNT(*) AS count FROM messages "
                      "GROUP BY sender ORDER BY count DESC") or []
        return [{"sender": r["sender"], "count": r["count"]} for r in rows]

    def row_to_msg(self, r):
        return {"id": r["id"], "room": r["room"], "from": r["sender"],
                "sender_name": r["sender_name"], "content": r["body"],
                "mentions": json.loads(r["mentions"] or "[]"), "ts": r["created_at"],
                "reply_to": r["reply_to"] if "reply_to" in r.keys() else None}

    def history(self, room, limit=60, since=None, before=None):
        limit = max(1, min(int(limit), 200))
        if before:
            rows = self.q("SELECT * FROM messages WHERE room=? AND id<? ORDER BY id DESC LIMIT ?",
                          (room, int(before), limit))
            return [self.row_to_msg(dict(x)) for x in reversed(rows)]
        if since:
            rows = self.q("SELECT * FROM messages WHERE room=? AND id>? ORDER BY id DESC LIMIT ?",
                          (room, int(since), limit))
        else:
            rows = self.q("SELECT * FROM messages WHERE room=? ORDER BY id DESC LIMIT ?",
                          (room, limit))
        return [self.row_to_msg(dict(x)) for x in reversed(rows)]

    def last_ts_map(self):
        rows = self.q(
            "SELECT m.room AS room, m.created_at AS ts FROM messages m "
            "JOIN (SELECT room, MAX(id) AS mid FROM messages GROUP BY room) t "
            "ON m.id = t.mid")
        return {r["room"]: r["ts"] for r in rows}

    def last_id_map(self):
        rows = self.q("SELECT room, MAX(id) AS mid FROM messages GROUP BY room")
        return {r["room"]: r["mid"] for r in rows}

    def unread_count(self, room, cursor=None, exclude=None):
        if cursor is None:
            return 0
        row = self.q("SELECT COUNT(*) AS n FROM messages "
                     "WHERE room=? AND id>? AND sender<>?",
                     (room, int(cursor), exclude or ""))[0]
        return int(row["n"] or 0)

    # -- instance / members / invites --
    def user_count(self):
        row = self.q("SELECT COUNT(*) AS n FROM users")[0]
        return int(row["n"] or 0)

    def users_with_role(self):
        rows = self.q("SELECT * FROM users ORDER BY created_at")
        out = []
        for r in rows:
            d = dict(r)
            if not d.get("role"):
                d["role"] = "member"
            d.setdefault("display_name", d.get("name", ""))
            d.setdefault("source", "local")
            out.append(d)
        return out

    def set_user_role(self, client_id, role):
        self.x("UPDATE users SET role=? WHERE client_id=?", (role, client_id))

    def set_user_status(self, client_id, status):
        self.x("UPDATE users SET status=? WHERE client_id=?", (status, client_id))

    def set_user_source(self, client_id, source):
        self.x("UPDATE users SET source=? WHERE client_id=?", (source, client_id))

    def set_user_agent_limit(self, client_id, limit):
        self.x("UPDATE users SET agent_limit=? WHERE client_id=?", (int(limit), client_id))

    def set_user_key(self, client_id, key1):
        self.x("UPDATE users SET key1=? WHERE client_id=?", (key1, client_id))

    def set_user_name(self, client_id, name):
        self.x("UPDATE users SET name=? WHERE client_id=?", (name, client_id))

    def remove_user(self, client_id):
        with self.lock:
            acids = [r[0] for r in self.conn.execute(
                "SELECT client_id FROM agents WHERE owner=?", (client_id,))]
            self.conn.execute("DELETE FROM room_members WHERE user_id=?", (client_id,))
            for acid in acids:
                self.conn.execute("DELETE FROM room_members WHERE user_id=?", (acid,))
            self.conn.execute("DELETE FROM agents WHERE owner=?", (client_id,))
            self.conn.execute("DELETE FROM users WHERE client_id=?", (client_id,))
            for acid in acids:
                self._drop_agent_dm(acid)
            self.conn.commit()

    def reset_user_key(self, client_id):
        new_key = secrets.token_urlsafe(12)
        self.x("UPDATE users SET key1=? WHERE client_id=?", (new_key, client_id))
        return new_key

    def reset_agent_key(self, agent_cid):
        new_key = secrets.token_urlsafe(12)
        # 参数顺序必须是 (新 key, client_id)：写成 (agent_cid, new_key) 会一条都不更新，
        # 返回的新 key 还是旧库里的，换 Key 一直是坏的（0.8.15 排查到）。
        self.x("UPDATE agents SET key2=? WHERE client_id=?", (new_key, agent_cid))
        return new_key

    def add_invite(self, token_hash, role, expires_at, created_by, room_id=""):
        cur = self.x(
            "INSERT INTO invites(token_hash,role,expires_at,max_uses,used,created_by,created_at,room_id) "
            "VALUES(?,?,?,1,0,?,?,?)",
            (token_hash, role, expires_at, created_by, now_iso(), room_id or None))
        return cur.lastrowid

    def find_invite_by_hash(self, token_hash):
        r = self.q("SELECT * FROM invites WHERE token_hash=?", (token_hash,))
        return dict(r[0]) if r else None

    def use_invite(self, invite_id):
        self.x("UPDATE invites SET used=used+1 WHERE id=?", (invite_id,))

    def claim_invite(self, invite_id):
        """一次性占用：只有未用过的邀请才能占用成功。"""
        cur = self.x("UPDATE invites SET used=1 WHERE id=? AND used=0", (invite_id,))
        return cur.rowcount == 1

    def remove_invite(self, invite_id):
        self.x("DELETE FROM invites WHERE id=?", (invite_id,))

    def list_invites(self):
        return [dict(r) for r in self.q(
            "SELECT * FROM invites ORDER BY id DESC")]

    def add_room_member(self, room_id, user_id, role="member"):
        self.x("INSERT OR REPLACE INTO room_members(room_id,user_id,role,joined_at) "
               "VALUES(?,?,?,?)", (room_id, user_id, role, now_iso()))

    def remove_room_member(self, room_id, user_id):
        self.x("DELETE FROM room_members WHERE room_id=? AND user_id=?",
               (room_id, user_id))

    def room_members_of(self, room_id):
        return [dict(r) for r in self.q(
            "SELECT * FROM room_members WHERE room_id=? ORDER BY joined_at",
            (room_id,))]

    def is_room_member(self, room_id, user_id):
        r = self.q("SELECT 1 FROM room_members WHERE room_id=? AND user_id=?",
                    (room_id, user_id))
        return bool(r)

    def rooms_of_user(self, user_id):
        return [r["room_id"] for r in self.q(
            "SELECT room_id FROM room_members WHERE user_id=? ORDER BY room_id",
            (user_id,))]

    def room_ids(self):
        return [r["room_id"] for r in self.q(
            "SELECT DISTINCT room_id FROM room_members ORDER BY room_id")]

    # -- rooms（房间 v2：自定义房间元数据；room_members 仍管成员）--
    def create_room(self, room_id, title, owner, visibility="invite"):
        self.x("INSERT INTO rooms(room_id,title,owner,visibility,created_at,archived) "
               "VALUES(?,?,?,?,?,0)",
               (room_id, title, owner, visibility, now_iso()))
        self.add_room_member(room_id, owner, "owner")
        return self.room(room_id)

    def room(self, room_id):
        r = self.q("SELECT * FROM rooms WHERE room_id=?", (room_id,))
        return dict(r[0]) if r else None

    def rooms_all(self, include_archived=False):
        sql = "SELECT * FROM rooms"
        if not include_archived:
            sql += " WHERE archived=0"
        sql += " ORDER BY created_at, room_id"
        return [dict(r) for r in self.q(sql)]

    def rename_room(self, room_id, title):
        cur = self.x("UPDATE rooms SET title=? WHERE room_id=?", (title, room_id))
        return cur.rowcount == 1

    def archive_room(self, room_id):
        cur = self.x("UPDATE rooms SET archived=1 WHERE room_id=?", (room_id,))
        return cur.rowcount == 1

    def count_owned_rooms(self, owner):
        r = self.q("SELECT COUNT(*) FROM rooms WHERE owner=? AND archived=0", (owner,))
        return int(r[0][0]) if r else 0

    # -- admin bootstrap / devices（只存哈希，明文只在生成时返回一次）--
    def add_bootstrap(self, token_hash, code_hash, expires_at):
        cur = self.x(
            "INSERT INTO admin_bootstrap(token_hash,code_hash,attempts,expires_at,used,created_at) "
            "VALUES(?,?,0,?,0,?)",
            (token_hash, code_hash, expires_at, now_iso()))
        return cur.lastrowid

    def find_bootstrap_by_hash(self, token_hash):
        r = self.q("SELECT * FROM admin_bootstrap WHERE token_hash=?", (token_hash,))
        return dict(r[0]) if r else None

    def bump_bootstrap_attempts(self, bid):
        """计一次错码。返回自增后的 attempts / used。"""
        self.x("UPDATE admin_bootstrap SET attempts=attempts+1 WHERE id=?", (bid,))
        r = self.q("SELECT attempts,used FROM admin_bootstrap WHERE id=?", (bid,))
        return dict(r[0]) if r else {"attempts": 0, "used": 0}

    def claim_bootstrap(self, bid):
        """一次性占用：并发下也只有一个人能换成设备凭证。"""
        cur = self.x("UPDATE admin_bootstrap SET used=1 WHERE id=? AND used=0", (bid,))
        return cur.rowcount == 1

    def burn_bootstrap(self, bid):
        self.x("UPDATE admin_bootstrap SET used=1 WHERE id=?", (bid,))

    def clear_bootstraps(self):
        """启动/重新生成时把所有未用的旧链接作废。"""
        self.x("UPDATE admin_bootstrap SET used=1 WHERE used=0")

    def add_device(self, name, secret_hash, created_by=""):
        cur = self.x(
            "INSERT INTO admin_device(name,secret_hash,revoked,created_by,created_at,last_seen) "
            "VALUES(?,?,0,?,?,?)",
            (name, secret_hash, created_by, now_iso(), now_iso()))
        return cur.lastrowid

    def device_by_hash(self, secret_hash):
        r = self.q("SELECT * FROM admin_device WHERE secret_hash=? AND revoked=0",
                   (secret_hash,))
        return dict(r[0]) if r else None

    def active_device_count(self):
        row = self.q("SELECT COUNT(*) AS n FROM admin_device WHERE revoked=0")[0]
        return int(row["n"] or 0)

    def touch_device(self, did):
        self.x("UPDATE admin_device SET last_seen=? WHERE id=?", (now_iso(), did))

    def list_devices(self):
        return [dict(r) for r in self.q(
            "SELECT id,name,revoked,created_by,created_at,last_seen "
            "FROM admin_device ORDER BY id")]

    def revoke_device(self, did):
        cur = self.x("UPDATE admin_device SET revoked=1 WHERE id=? AND revoked=0", (did,))
        return cur.rowcount == 1

    # -- audit (append-only, never contains secrets) --
    def add_audit(self, device, ip, action, result, detail=""):
        self.x(
            "INSERT INTO admin_audit(ts,device,ip,action,result,detail) "
            "VALUES(?,?,?,?,?,?)",
            (now_iso(), str(device or "")[:40], str(ip or "")[:64],
             str(action or "")[:80], str(result or "")[:20],
             str(detail or "")[:300]))

    def clear_expired_bootstraps(self):
        """启动时清一次过期行（评审 红线 #7）。"""
        now = now_iso()
        self.x("UPDATE admin_bootstrap SET used=1 WHERE used=0 AND expires_at<=?", (now,))

    def bootstrap_state(self, bid):
        """返回 bootstrap 条目的 used / attempts / expires_at，用于审计三态。"""
        r = self.q("SELECT used,attempts,expires_at FROM admin_bootstrap WHERE id=?",
                    (bid,))
        return dict(r[0]) if r else None

    # -- admin_token: one-time access tokens for local admin panel --
    def add_admin_token(self, token_hash, expires_at, kid=""):
        cur = self.x(
            "INSERT INTO admin_token(token_hash,expires_at,used,kid,created_at) "
            "VALUES(?,?,0,?,?)",
            (token_hash, expires_at, kid, now_iso()))
        return cur.lastrowid

    def find_admin_token_by_hash(self, token_hash):
        r = self.q("SELECT * FROM admin_token WHERE token_hash=?", (token_hash,))
        return dict(r[0]) if r else None

    def use_admin_token(self, tid):
        """Atomic single-use claim: returns True if this call was the first."""
        cur = self.x("UPDATE admin_token SET used=1 WHERE id=? AND used=0", (tid,))
        return cur.rowcount == 1

    def clear_expired_admin_tokens(self):
        """Startup cleanup: mark expired unused tokens as used."""
        now = now_iso()
        self.x("UPDATE admin_token SET used=1 WHERE used=0 AND expires_at<=?", (now,))

    # -- tasks / task_events（二期：任务流式进度）--
    TASK_EVENT_KEEP = 300        # 每个任务最多留这么多条流式片段
    TASK_ROOM_KEEP = 50          # 每个房间最多留这么多条任务

    def task(self, task_id):
        r = self.q("SELECT * FROM tasks WHERE id=?", (task_id,))
        return dict(r[0]) if r else None

    def upsert_task(self, task_id, room, title, owner, owner_name, status="running"):
        now = now_iso()
        old = self.task(task_id)
        if old:
            self.x("UPDATE tasks SET status=?, updated_at=?, "
                   "title=CASE WHEN ?<>'' THEN ? ELSE title END WHERE id=?",
                   (status, now, title or "", title or "", task_id))
            return self.task(task_id)
        self.x("INSERT INTO tasks(id,room,title,owner,owner_name,status,created_at,updated_at) "
               "VALUES(?,?,?,?,?,?,?,?)",
               (task_id, room, title or "", owner or "", owner_name or "", status, now, now))
        self.prune_tasks(room)
        return self.task(task_id)

    def add_task_event(self, task_id, agent, agent_name, kind, text):
        cur = self.x(
            "INSERT INTO task_events(task_id,agent,agent_name,kind,text,ts) "
            "VALUES(?,?,?,?,?,?)",
            (task_id, agent or "", agent_name or "", kind or "delta",
             (text or "")[:2000], now_iso()))
        self.prune_task_events(task_id)
        return cur.lastrowid

    def prune_task_events(self, task_id):
        rows = self.q("SELECT id FROM task_events WHERE task_id=? ORDER BY id DESC LIMIT 1 OFFSET ?",
                      (task_id, self.TASK_EVENT_KEEP))
        if rows:
            self.x("DELETE FROM task_events WHERE task_id=? AND id<=?", (task_id, rows[0]["id"]))

    def prune_tasks(self, room):
        rows = self.q("SELECT id FROM tasks WHERE room=? ORDER BY updated_at DESC LIMIT 1 OFFSET ?",
                      (room, self.TASK_ROOM_KEEP))
        for r in rows:
            self.x("DELETE FROM task_events WHERE task_id=?", (r["id"],))
            self.x("DELETE FROM tasks WHERE id=?", (r["id"],))

    def task_events(self, task_id, limit=60):
        limit = max(1, min(int(limit), 300))
        rows = self.q("SELECT * FROM task_events WHERE task_id=? ORDER BY id DESC LIMIT ?",
                      (task_id, limit))
        return [dict(r) for r in reversed(rows)]

    def row_to_task(self, r, events_limit=60):
        return {"id": r["id"], "room": r["room"], "title": r["title"],
                "owner": r["owner"], "owner_name": r["owner_name"],
                "status": r["status"], "created_at": r["created_at"],
                "updated_at": r["updated_at"],
                "events": self.task_events(r["id"], events_limit)}

    def tasks_for_rooms(self, rooms, limit=20, events_limit=60):
        """按房间取任务：进行中的全给，已结束的按更新时间取最近的。"""
        out = []
        for room in rooms:
            rows = self.q("SELECT * FROM tasks WHERE room=? ORDER BY updated_at DESC LIMIT ?",
                          (room, max(1, min(int(limit), 50))))
            out.extend(self.row_to_task(dict(r), events_limit) for r in rows)
        out.sort(key=lambda t: t["updated_at"], reverse=True)
        out.sort(key=lambda t: 0 if t["status"] == "running" else 1)
        return out
