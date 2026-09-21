"""打包入口（PyInstaller 用这个文件，不要直接用 `app.py`）。

⚠️ **必须单独有个入口脚本**（2026-09-21 实测踩到）：
`app.py` 内部用的是相对导入（`from .backend_client import ...`）。直接把它交给
PyInstaller 时，它会被当作**顶层脚本**执行（`__name__ == '__main__'`，没有 `__package__`），
于是 `ImportError: attempted relative import with no known parent package`
——打包出来的 exe **双击就闪退**，而且因为 `console=False`，连错误都看不到。

这个文件只做一件事：用**绝对导入**把包拉起，再调到 `frontend.app.main()`。
源码运行（`py -3.12 -m frontend.app`）和打包运行（`LearnHelperUI.exe`）都走同一份界面代码。
"""
from __future__ import annotations

import os
import sys

# 冻结后 `sys.path[0]` 是 PyInstaller 的临时解包目录，源码运行时是仓库根。
# 两种情况下都保证 `frontend` 这个包可被导入。
if __package__ in (None, ''):
    _repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if _repo not in sys.path:
        sys.path.insert(0, _repo)

from frontend.app import main  # noqa: E402

if __name__ == '__main__':
    sys.exit(main())
