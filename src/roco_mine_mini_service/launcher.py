"""Unified entry point: config-driven headless mode or the PySide6 GUI.

python gui.py
  -> reads config.yaml
  -> password credentials present  : headless background service (no GUI)
  -> otherwise                     : PySide6 GUI which manages the service
"""

from __future__ import annotations

import sys
from pathlib import Path

from .config import load_config


def run() -> int:
    """Decide between the headless background mode and the GUI."""

    config = load_config()
    if config.password_login and config.has_password_credentials:
        from .headless import run_headless

        return run_headless(config)

    return _run_gui()


def _run_gui() -> int:
    # The GUI module imports PySide6, which is only needed on this path.
    # Put the project root on sys.path so `import gui` works from anywhere.
    try:
        import PySide6  # noqa: F401 - probe before launching the GUI
    except ModuleNotFoundError:
        print(
            "缺少图形界面依赖 PySide6。\n"
            "请先安装：uv sync   （或在当前 Python 环境执行：pip install PySide6）\n"
            "然后重新运行：uv run gui.py"
        )
        return 1
    root = Path.cwd().resolve()
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    import main

    return main.launch_gui()
