#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""PubChat 单窗口外壳：用系统自带的 WebView2 显示「聊天 / 设置」。

优先走 EdgeChromium（Windows 10/11 自带 WebView2 运行时），
没有运行时就返回 False，由 desktop.py 回退到 tkinter 控制台。
"""
from pathlib import Path


def run(admin_port, data_dir, version="", stop_event=None, tunnel=None, debug=False):
    """打开窗口并阻塞到用户关闭。成功返回 True；环境不支持返回 False。"""
    try:
        import webview
    except Exception as exc:
        print("[pubchat] 内置窗口不可用（pywebview 未安装）：%s" % exc, flush=True)
        return False

    url = "http://127.0.0.1:%d/shell.html" % admin_port
    title = "PubChat" + (" v" + version if version else "")
    width, height = 1180, 820
    try:
        scr = webview.screens[0]
        width = min(width, max(860, int(scr.width) - 80))
        height = min(height, max(560, int(scr.height) - 120))
    except Exception:
        pass
    storage = None
    try:
        storage = Path(data_dir) / "webview"
        storage.mkdir(parents=True, exist_ok=True)
    except OSError:
        storage = None

    try:
        window = webview.create_window(
            title, url,
            width=width, height=height, min_size=(860, 560),
            background_color="#f5f6f8",
            text_select=True,
        )
        if window is None:
            return False
        if stop_event is not None:
            try:
                window.events.closed += lambda: stop_event.set()
            except Exception:
                pass
        if tunnel is not None:
            def _confirm_close():
                try:
                    on = (tunnel.status() or {}).get("state") == "on"
                except Exception:
                    on = False
                if not on:
                    return True
                try:
                    import ctypes
                    r = ctypes.windll.user32.MessageBoxW(
                        None,
                        "关闭窗口会停止 PubChat，并断开当前公网地址。\n确定要关闭吗？",
                        "PubChat", 0x04 | 0x30 | 0x100)
                    return r == 6
                except Exception:
                    return True
            try:
                window.events.closing += _confirm_close
            except Exception:
                pass
        webview.start(
            gui="edgechromium",
            debug=bool(debug),
            private_mode=False,
            storage_path=str(storage) if storage else None,
        )
        return True
    except Exception as exc:
        print("[pubchat] 内置窗口启动失败：%s" % exc, flush=True)
        return False
