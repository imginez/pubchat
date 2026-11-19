#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""CI 用的可移植子集（Linux / Windows 都能跑）。

tests/smoke.py 是完整冒烟，但它依赖本机的东西：
- `android/`（APK 源码不在本仓库）→ APK 静态项必然 FAIL
- `powershell`、Startup 目录、`%APPDATA%` → Windows 快捷方式/数据迁移项必然 FAIL
- 起真实实例的 E2E 段（run() / e2e_*）跑得久，留在本地与发版前跑

这里只挑**纯逻辑、纯静态文件**的用例：协议常量、房间权限真值表、成员与邀请、
智能体私聊、密钥与安全边界、前端硬约束的静态部分。跑得快（几秒），适合每次 push。

用法：python tests/ci.py
"""
import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("smoke", ROOT / "smoke.py")
smoke = importlib.util.module_from_spec(spec)
spec.loader.exec_module(smoke)  # 有 __main__ 守卫，import 不会跑用例

# 只放平台无关的纯静态/纯逻辑单元。
# 排除并说明原因：
#   unit_paths / unit_pointer_errors / unit_data_migration  → 依赖 %APPDATA% 与 Windows 目录语义
#   unit_frontend_locks / unit_windows_shortcuts            → 依赖 Startup 目录、powershell、.lnk
#   unit_mobile_native / unit_update_check                  → 依赖 android/（APK 源码不在本仓库）
CI_UNITS = [
    "unit_version_compare",
    "unit_instances",
    "unit_settings_defaults",
    "unit_role_migration",
    "unit_rooms_v2",
    "unit_rooms_ws",
    "unit_room_join",
    "unit_rooms_admin",
    "unit_member_admin",
    "unit_invite_hours",
    "unit_agent_dm",
    "unit_probe_stages",
    "unit_stage2_security",
    "unit_stage3_ui",
]


def main():
    for name in CI_UNITS:
        fn = getattr(smoke, name, None)
        if fn is None:
            print("  MISSING %s" % name)
            smoke.bad(name, "用例函数不存在")
            continue
        print("== %s ==" % name, flush=True)
        try:
            fn()
        except Exception:
            import traceback
            traceback.print_exc()
            smoke.bad(name, "抛异常")

    print("")
    print("通过 %d 项，失败 %d 项" % (len(smoke.PASS), len(smoke.FAIL)))
    if smoke.FAIL:
        for name, detail in smoke.FAIL:
            print("  FAIL %s%s" % (name, (" :: %s" % detail) if detail else ""))
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
