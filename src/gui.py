#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""PubChat 桌面控制台。

一个 tkinter 窗口把常用操作收在一起：看局域网地址和二维码、开临时/固定
公网地址、打开管理页、导入旧数据库。服务本身跑在后台线程里，关掉窗口才退出。

不引入第三方 UI 依赖；二维码用 qrcode 生成矩阵，直接画在 Canvas 上。
"""
import threading
import webbrowser

import qrcode
import tkinter as tk
from tkinter import filedialog, messagebox

import server

BG = "#f5f6f8"
CARD = "#ffffff"
INK = "#1d2129"
MUTED = "#86909c"
LINE = "#e5e6eb"
BRAND = "#2f6bff"
OK = "#00a870"
ERR = "#d93026"
FONT = ("Microsoft YaHei UI", 10)
FONT_SM = ("Microsoft YaHei UI", 9)
FONT_MD = ("Microsoft YaHei UI", 12, "bold")
FONT_TITLE = ("Microsoft YaHei UI", 21, "bold")
MONO = ("Consolas", 10)


def show_error(title, text):
    root = tk.Tk()
    root.withdraw()
    messagebox.showerror(title, text, parent=root)
    root.destroy()


def _button(parent, text, command, primary=True, width=0):
    b = tk.Button(
        parent, text=text, command=command,
        bg=BRAND if primary else "#f2f3f5",
        fg="#ffffff" if primary else INK,
        activebackground="#2557d6" if primary else "#e5e6eb",
        activeforeground="#ffffff" if primary else INK,
        relief="flat", bd=0, padx=14, pady=7, cursor="hand2",
        font=FONT_SM, highlightthickness=0,
    )
    if width:
        b.configure(width=width)
    return b


def _entry(parent, width=34, show=None):
    e = tk.Entry(parent, width=width, font=FONT, relief="flat", bd=0,
                 highlightthickness=1, highlightbackground=LINE,
                 highlightcolor=BRAND, insertbackground=INK, bg="#fbfbfc")
    if show:
        e.configure(show=show)
    return e


class App:
    def __init__(self, root, port, admin_port, version, data_dir, tunnel,
                 stop_event, import_db, lan_ips):
        self.root = root
        self.port = port
        self.admin_port = admin_port
        self.version = version
        self.data_dir = data_dir
        self.tunnel = tunnel
        self.stop_event = stop_event
        self.import_db = import_db
        self.lan_ips = lan_ips
        self._qr_cache = {}
        self._form_mode = None
        self._busy = False

        root.title("PubChat 控制台")
        root.configure(bg=BG)
        # 133% 缩放的 1280x800 屏上，900x780 会被 Tk 放大到屏幕外；
        # 880x700 缩放后约 1170x930，底部按钮始终可见。
        root.geometry("880x700")
        root.minsize(760, 640)
        try:
            root.iconname("PubChat")
        except Exception:
            pass

        self._build()
        self._show_mode(tunnel.mode)
        self._refresh()
        self.root.after(1000, self.refresh)
        root.protocol("WM_DELETE_WINDOW", self.close)

    # ------------------------------------------------------------------ UI --
    def _build(self):
        root = self.root
        wrap = tk.Frame(root, bg=BG)
        wrap.pack(fill="both", expand=True, padx=24, pady=(20, 16))

        head = tk.Frame(wrap, bg=BG)
        head.pack(fill="x", pady=(0, 14))
        tk.Label(head, text="PubChat", bg=BG, fg=INK, font=FONT_TITLE).pack(side="left")
        tk.Label(head, text="  v" + self.version, bg=BG, fg=MUTED, font=FONT_SM).pack(
            side="left", anchor="s", pady=(0, 5))
        self.online_label = tk.Label(head, text="● 运行中", bg=BG, fg=OK, font=FONT)
        self.online_label.pack(side="right")

        row = tk.Frame(wrap, bg=BG)
        row.pack(fill="both", expand=True)
        row.columnconfigure(0, weight=1, uniform="c")
        row.columnconfigure(1, weight=1, uniform="c")
        row.rowconfigure(0, weight=1)

        self._build_lan(row)
        self._build_public(row)
        self._build_footer(wrap)

    def _card(self, parent, title, column):
        outer = tk.Frame(parent, bg=CARD, highlightbackground=LINE, highlightthickness=1)
        outer.grid(row=0, column=column, sticky="nsew", padx=(0, 8) if column == 0 else (8, 0))
        body = tk.Frame(outer, bg=CARD, padx=18, pady=16)
        body.pack(fill="both", expand=True)
        tk.Label(body, text=title, bg=CARD, fg=INK, font=FONT_MD).pack(anchor="w")
        return body

    def _build_lan(self, row):
        body = self._card(row, "同一 Wi-Fi 下访问", 0)
        tk.Label(body, text="手机/平板打开下面地址，或直接扫码。", bg=CARD, fg=MUTED,
                 font=FONT_SM).pack(anchor="w", pady=(4, 10))

        self.lan_urls = []
        for _ in range(3):
            line = tk.Frame(body, bg=CARD)
            line.pack(fill="x", pady=2)
            lbl = tk.Label(line, text="", bg=CARD, fg=INK, font=MONO, anchor="w",
                           justify="left", wraplength=330)
            lbl.pack(side="left", fill="x", expand=True)
            btn = _button(line, "复制", lambda l=lbl: self.copy(l.cget("text")), primary=False)
            btn.pack(side="right")
            self.lan_urls.append((line, lbl))

        qrbox = tk.Frame(body, bg=CARD)
        qrbox.pack(anchor="w", pady=(12, 0))
        self.lan_qr = tk.Canvas(qrbox, width=170, height=170, bg=CARD,
                                highlightthickness=0)
        self.lan_qr.pack()
        self.lan_hint = tk.Label(body, text="", bg=CARD, fg=MUTED, font=FONT_SM,
                                 wraplength=360, justify="left")
        self.lan_hint.pack(anchor="w", pady=(8, 0))

    def _build_public(self, row):
        body = self._card(row, "公网地址（给外面的人）", 1)
        tk.Label(body, text="临时地址重启就变；固定域名重启不变。", bg=CARD, fg=MUTED,
                 font=FONT_SM).pack(anchor="w", pady=(4, 10))

        modes = tk.Frame(body, bg=CARD)
        modes.pack(fill="x")
        self.mode_var = tk.StringVar(value=self.tunnel.mode)
        for value, text in (("quick", "临时地址"), ("named", "固定域名"),
                            ("manual", "已有地址")):
            tk.Radiobutton(
                modes, text=text, value=value, variable=self.mode_var,
                command=lambda: self._show_mode(self.mode_var.get()),
                bg=CARD, fg=INK, activebackground=CARD, selectcolor=CARD,
                font=FONT_SM, highlightthickness=0, cursor="hand2",
            ).pack(side="left", padx=(0, 10))

        self.form_box = tk.Frame(body, bg=CARD)
        self.form_box.pack(fill="x", pady=(10, 0))

        self.frames = {"quick": self._quick_form(self.form_box),
                       "named": self._named_form(self.form_box),
                       "manual": self._manual_form(self.form_box)}
        for f in self.frames.values():
            f.grid(row=0, column=0, sticky="nsew")
        self.form_box.columnconfigure(0, weight=1)

        tk.Frame(body, bg=LINE, height=1).pack(fill="x", pady=(14, 12))
        self.pub_state = tk.Label(body, text="", bg=CARD, fg=MUTED, font=FONT_SM,
                                  wraplength=360, justify="left")
        self.pub_state.pack(anchor="w")
        self.pub_url = tk.Label(body, text="", bg=CARD, fg=INK, font=MONO,
                                wraplength=360, justify="left")
        self.pub_url.pack(anchor="w", pady=(4, 0))
        self.pub_qr = tk.Canvas(body, width=138, height=138, bg=CARD,
                                highlightthickness=0)
        self.pub_qr.pack(anchor="w", pady=(10, 0))

    def _quick_form(self, parent):
        f = tk.Frame(parent, bg=CARD)
        self.quick_note = tk.Label(f, text="", bg=CARD, fg=MUTED, font=FONT_SM,
                                   wraplength=360, justify="left")
        self.quick_note.pack(anchor="w", pady=(0, 8))
        btns = tk.Frame(f, bg=CARD)
        btns.pack(anchor="w")
        self.quick_btn = _button(btns, "开启临时地址", self.toggle_quick)
        self.quick_btn.pack(side="left")
        return f

    def _named_form(self, parent):
        f = tk.Frame(parent, bg=CARD)
        tk.Label(f, text="Cloudflare 隧道 Token", bg=CARD, fg=MUTED, font=FONT_SM).pack(anchor="w")
        self.token_entry = _entry(f, width=40, show="•")
        self.token_entry.pack(fill="x", pady=(3, 2))
        self.token_hint = tk.Label(f, text="", bg=CARD, fg=MUTED, font=("Microsoft YaHei UI", 8))
        self.token_hint.pack(anchor="w")
        tk.Label(f, text="固定域名（含 https://）", bg=CARD, fg=MUTED,
                 font=FONT_SM).pack(anchor="w", pady=(8, 0))
        self.named_url_entry = _entry(f, width=40)
        self.named_url_entry.pack(fill="x", pady=(3, 0))
        self.auto_var = tk.BooleanVar(value=bool(self.tunnel.auto))
        tk.Checkbutton(f, text="启动 PubChat 时自动开启", variable=self.auto_var,
                       bg=CARD, activebackground=CARD, selectcolor=CARD, fg=INK,
                       font=FONT_SM, highlightthickness=0, cursor="hand2").pack(
            anchor="w", pady=(6, 0))
        btns = tk.Frame(f, bg=CARD)
        btns.pack(anchor="w", pady=(8, 0))
        _button(btns, "保存并开启", self.save_named).pack(side="left")
        self.named_stop_btn = _button(btns, "停止", lambda: self.bg(self.tunnel.stop),
                                      primary=False)
        self.named_stop_btn.pack(side="left", padx=6)
        tk.Label(f, text="Token 只存本机；域名解析和证书由你自己的 Cloudflare 账号管理。",
                 bg=CARD, fg=MUTED, font=("Microsoft YaHei UI", 8),
                 wraplength=360, justify="left").pack(anchor="w", pady=(8, 0))
        return f

    def _manual_form(self, parent):
        f = tk.Frame(parent, bg=CARD)
        tk.Label(f, text="公网地址（反向代理/其他隧道给你的地址）", bg=CARD, fg=MUTED,
                 font=FONT_SM).pack(anchor="w")
        self.manual_url_entry = _entry(f, width=40)
        self.manual_url_entry.pack(fill="x", pady=(3, 0))
        btns = tk.Frame(f, bg=CARD)
        btns.pack(anchor="w", pady=(8, 0))
        _button(btns, "保存并显示", self.save_manual).pack(side="left")
        tk.Label(f, text="程序只负责展示和生成二维码，不检查这个地址能否打开。",
                 bg=CARD, fg=MUTED, font=("Microsoft YaHei UI", 8),
                 wraplength=360, justify="left").pack(anchor="w", pady=(8, 0))
        return f

    def _build_footer(self, parent):
        card = tk.Frame(parent, bg=CARD, highlightbackground=LINE, highlightthickness=1)
        card.pack(fill="x", pady=(14, 0))
        body = tk.Frame(card, bg=CARD, padx=18, pady=12)
        body.pack(fill="x")

        left = tk.Frame(body, bg=CARD)
        left.pack(side="left", fill="x", expand=True)
        self.status_line = tk.Label(left, text="", bg=CARD, fg=MUTED, font=FONT_SM)
        self.status_line.pack(anchor="w")
        self.data_line = tk.Label(left, text="数据目录：" + str(self.data_dir), bg=CARD,
                                  fg=MUTED, font=("Microsoft YaHei UI", 8),
                                  wraplength=420, justify="left")
        self.data_line.pack(anchor="w", pady=(3, 0))

        right = tk.Frame(body, bg=CARD)
        right.pack(side="right")
        _button(right, "打开管理页", self.open_admin, primary=False).pack(side="left", padx=4)
        _button(right, "打开聊天页", self.open_chat, primary=False).pack(side="left", padx=4)
        _button(right, "导入旧数据", self.import_clicked, primary=False).pack(side="left", padx=4)
        _button(right, "退出", self.close, primary=True).pack(side="left", padx=(10, 0))

    # --------------------------------------------------------------- 表单 --
    def _show_mode(self, mode):
        if mode not in self.frames:
            mode = "quick"
        self.mode_var.set(mode)
        self.frames[mode].tkraise()
        self._form_mode = mode
        if mode == "named":
            self.named_url_entry.delete(0, "end")
            self.named_url_entry.insert(0, self.tunnel.public_url or "")
            self.token_entry.delete(0, "end")
            self.token_hint.configure(text="已保存 Token（留空保持不变）" if self.tunnel.token
                                      else "在 Cloudflare Zero Trust 里创建隧道后复制")
            self.auto_var.set(bool(self.tunnel.auto))
        elif mode == "manual":
            self.manual_url_entry.delete(0, "end")
            self.manual_url_entry.insert(0, self.tunnel.public_url or "")
        self._refresh()

    def toggle_quick(self):
        if self.tunnel.mode != "quick":
            try:
                self.tunnel.save_config(mode="quick")
            except Exception as exc:
                messagebox.showerror("切换失败", str(exc), parent=self.root)
                return
            self._show_mode("quick")
        st = self.tunnel.status()
        action = self.tunnel.stop if st.get("state") == "on" else self.tunnel.start
        self.bg(action)

    def save_named(self):
        token = self.token_entry.get().strip()
        url = self.named_url_entry.get().strip()
        if not token and not self.tunnel.token:
            messagebox.showerror("缺少 Token", "请先粘贴 Cloudflare 隧道 Token。", parent=self.root)
            return
        try:
            self.tunnel.save_config(mode="named", token=token or None, public_url=url,
                                    auto=self.auto_var.get())
        except Exception as exc:
            messagebox.showerror("保存失败", str(exc), parent=self.root)
            return
        self.token_entry.delete(0, "end")
        self.token_hint.configure(text="已保存 Token（留空保持不变）")
        self._form_mode = "named"
        self.bg(self.tunnel.start)

    def save_manual(self):
        url = self.manual_url_entry.get().strip()
        try:
            self.tunnel.save_config(mode="manual", public_url=url)
        except Exception as exc:
            messagebox.showerror("保存失败", str(exc), parent=self.root)
            return
        self._show_mode("manual")
        self.refresh()

    # --------------------------------------------------------------- 动作 --
    def bg(self, fn):
        if self._busy:
            return
        self._busy = True
        self.refresh()

        def work():
            err = None
            try:
                fn()
            except Exception as exc:
                err = exc

            def done():
                self._busy = False
                if err is not None:
                    messagebox.showerror("操作失败", str(err), parent=self.root)
                self._refresh()
            self.root.after(0, done)

        threading.Thread(target=work, daemon=True).start()

    def copy(self, text):
        if not text:
            return
        try:
            self.root.clipboard_clear()
            self.root.clipboard_append(text)
            self.root.update_idletasks()
        except Exception as exc:
            messagebox.showerror("复制失败", str(exc), parent=self.root)

    def open_admin(self):
        webbrowser.open("http://127.0.0.1:%d/" % self.admin_port)

    def open_chat(self):
        webbrowser.open("http://127.0.0.1:%d/" % self.port)

    def import_clicked(self):
        path = filedialog.askopenfilename(
            parent=self.root, title="选择旧的 pubchat.db",
            filetypes=[("SQLite 数据库", "*.db"), ("所有文件", "*.*")])
        if not path:
            return
        if not messagebox.askyesno(
                "导入旧数据",
                "会用这个库接管账号和消息；当前库会先备份。\n\n%s\n\n继续？" % path,
                parent=self.root):
            return

        def work():
            backup = self.import_db(path)
            server.boot()
            self.root.after(0, lambda: messagebox.showinfo(
                "导入完成",
                "已接管：%s\n%s" % (path, "旧库备份：%s" % backup if backup else "（原来没有库）"),
                parent=self.root))
            self.root.after(0, self._refresh)

        self.bg(work)

    def close(self):
        st = self.tunnel.status()
        if st.get("state") in ("on", "starting"):
            if not messagebox.askyesno("退出 PubChat", "公网隧道会一起断开，确定退出？",
                                       parent=self.root):
                return
        self.stop_event.set()
        self.root.destroy()

    # --------------------------------------------------------------- 刷新 --
    def refresh(self):
        try:
            self._refresh()
        except Exception:
            pass
        self.root.after(1000, self.refresh)

    def _refresh(self):
        users = server.STORE.users()
        online = len(server.hub.clients)
        self.online_label.configure(text="● 运行中 · 在线 %d" % online,
                                    fg=OK if users else "#ff7d00")
        self.status_line.configure(
            text="对外端口 %d · 管理口 %d · %s" % (
                self.port, self.admin_port, "已初始化" if users else "等待初始化"))

        ips = self.lan_ips()
        urls = ["http://%s:%d/" % (ip, self.port) for ip in ips] or \
               ["http://127.0.0.1:%d/" % self.port]
        for i, (line, lbl) in enumerate(self.lan_urls):
            if i < len(urls):
                lbl.configure(text=urls[i])
                if not line.winfo_manager():
                    line.pack(fill="x", pady=2)
            else:
                line.pack_forget()
        first = urls[0]
        self._draw_qr(self.lan_qr, first)
        self.lan_hint.configure(text="扫码打开：" + first)

        st = self.tunnel.status()
        state = st.get("state")
        detail = st.get("detail") or ""
        if self._form_mode == "quick":
            self.quick_note.configure(text=detail)
            self.quick_btn.configure(
                text="关闭临时地址" if state == "on" else
                     ("申请中…" if state == "starting" else "开启临时地址"))
            self.quick_btn.configure(state="disabled" if (self._busy or state == "starting")
                                     else "normal")
        if self._form_mode == "named":
            self.named_stop_btn.configure(state="normal" if state in ("on", "starting")
                                          else "disabled")

        self.pub_state.configure(text=detail)
        url = st.get("url")
        if url:
            self.pub_url.configure(text=url)
        else:
            self.pub_url.configure(text="")
        self._draw_qr(self.pub_qr, url)

    def _draw_qr(self, canvas, text):
        name = str(canvas)
        key = (name, text)
        if not text:
            canvas.delete("all")
            canvas.configure(height=1)
            for k in [k for k in self._qr_cache if k[0] == name]:
                self._qr_cache.pop(k, None)
            return
        if self._qr_cache.get(key) == text and canvas.find_all():
            return
        self._qr_cache[key] = text
        try:
            qr = qrcode.QRCode(border=2, error_correction=qrcode.constants.ERROR_CORRECT_M)
            qr.add_data(text)
            qr.make(fit=True)
            matrix = qr.get_matrix()
        except Exception:
            canvas.delete("all")
            return
        size = 170 if canvas is self.lan_qr else 138
        n = len(matrix)
        cell = max(2, size // n)
        total = cell * n
        x0 = (size - total) // 2
        y0 = (size - total) // 2
        canvas.delete("all")
        canvas.configure(width=size, height=size)
        canvas.create_rectangle(0, 0, size, size, fill="#ffffff", outline="")
        for y, row in enumerate(matrix):
            for x, on in enumerate(row):
                if on:
                    canvas.create_rectangle(x0 + x * cell, y0 + y * cell,
                                            x0 + (x + 1) * cell, y0 + (y + 1) * cell,
                                            fill="#000000", outline="")


def run(port, admin_port, version, data_dir, tunnel, stop_event, import_db, lan_ips):
    root = tk.Tk()
    App(root, port, admin_port, version, data_dir, tunnel, stop_event, import_db, lan_ips)
    root.mainloop()
