"""
AppContext —— 宿主注入的运行时上下文。

两个宿主各自构造一个 AppContext 实例，持有 store / hub / 配置等引用，
传给 core 层的各个函数。过渡期保留 server.py 的模块级全局变量作为
deprecated alias，指向 context 实例。
"""
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Optional


@dataclass
class AppContext:
    """宿主注入的运行时上下文。"""
    store: Any = None
    hub: Any = None
    data_dir: Path = field(default_factory=lambda: Path("data"))
    web_dir: Path = field(default_factory=lambda: Path("web"))
    root_dir: Path = field(default_factory=lambda: Path("."))
    instance: Optional[Dict] = None
    rate_attempts: Dict[str, list] = field(default_factory=dict)
    config: Dict[str, Any] = field(default_factory=dict)
