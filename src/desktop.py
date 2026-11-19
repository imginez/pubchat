#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""PubChat 本机版入口（已弃用薄壳，实现在 hosts/local.py）。"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from hosts.local import (  # noqa: E402,F401
    APP,
    VERSION,
    AdminHTTP,
    app_dir,
    bundle_dir,
    check_reachability,
    fingerprint,
    handle_join,
    install_db_file,
    lan_ips,
    main,
    make_process_request,
    make_user,
    resolve_data_dir,
    resolve_web_dir,
    rooms_state,
    start_admin,
    users_state,
    waiting_page,
)

if __name__ == "__main__":
    sys.exit(main())
