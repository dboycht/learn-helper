# -*- coding: utf-8 -*-
"""源码运行入口（不打包时用）。

    py -3.10 backend\\main.py --port 0

冻结后（PyInstaller）入口同样是本文件：``pyinstaller backend/main.py``。
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from learn_helper.runtime import run  # noqa: E402

if __name__ == '__main__':
    run()
