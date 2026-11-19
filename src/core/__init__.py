"""
PubChat core —— 宿主无关的协议、存储、权限、认证逻辑。

两个宿主（hosts/server.py 公网多实例、hosts/local.py 单 exe 本机版）
共用这一份核心，各自只负责端口绑定、GUI、隧道等宿主专属逻辑。
"""
from core.context import AppContext

__all__ = ["AppContext"]
