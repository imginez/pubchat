# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包：PubChat 单文件 exe。

    python -m PyInstaller --noconfirm --clean packaging/pubchat.spec

环境变量：
    PUBCHAT_BUNDLE_CLOUDFLARED=1   把 cloudflared.exe 一起塞进去（体积 +55MB）
    PUBCHAT_CLOUDFLARED=<路径>      指定要打包的 cloudflared.exe

不想每次启动解压 55MB 的话，就别打包，让用户把 cloudflared.exe 放到 exe 同目录。
"""
import os
from pathlib import Path

_here = Path(SPECPATH).resolve()          # noqa: F821  可能是目录，也可能是 spec 路径
ROOT = _here.parent if _here.is_dir() else _here.parent.parent
bundle_cf = os.environ.get("PUBCHAT_BUNDLE_CLOUDFLARED", "") == "1"

datas = [(str(ROOT / "web"), "web")]
binaries = []
if bundle_cf:
    cf = os.environ.get("PUBCHAT_CLOUDFLARED", "")
    if not cf or not os.path.isfile(cf):
        raise SystemExit("PUBCHAT_BUNDLE_CLOUDFLARED=1 但没有可用的 PUBCHAT_CLOUDFLARED")
    binaries.append((cf, "."))

a = Analysis(
    [str(ROOT / "desktop.py")],
    pathex=[str(ROOT)],
    binaries=binaries,
    datas=datas,
    hiddenimports=[
        "websockets.asyncio.client",
        "websockets.asyncio.server",
        "websockets.legacy.client",
        "websockets.legacy.server",
        "qrcode",
    ],
    hookspath=[],
    runtime_hooks=[],
    excludes=[
        "unittest", "doctest", "pydoc", "pdb", "xmlrpc",
        "lib2to3", "test", "sqlite3.test",
    ],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="PubChat",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,          # 桌面版不弹黑框；命令行调试用 --no-gui 或源码跑
    disable_windowed_traceback=False,
    version=str(ROOT / "packaging" / "version_info.txt"),
)
