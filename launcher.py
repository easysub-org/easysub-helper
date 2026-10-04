#!/usr/bin/env python
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 hcz1017
"""冻结/直跑入口（PyInstaller 用这个，别用 easysub_helper/__main__.py）。

为什么必须单独一个入口文件：PyInstaller 只收集"脚本里**静态可见**的 import"。
`easysub_helper/__main__.py` 里是 `from . import cli` —— 那是**运行期**的相对导入
（靠 `__package__` 兜底），静态分析看不到，于是打出来的包里根本没有 `easysub_helper` 包，
运行时报：

    ModuleNotFoundError: No module named 'easysub_helper.cli'

（这是实测：Linux onefile 产物一跑就崩，macOS/Windows 同理。）

这里改成 `from easysub_helper.cli import main` 这种静态导入，PyInstaller 就能顺着把整个包
收集进去。直接 `python launcher.py` 也等价于 `python -m easysub_helper`。
"""

import sys

from easysub_helper.cli import main

if __name__ == "__main__":
    sys.exit(main())
