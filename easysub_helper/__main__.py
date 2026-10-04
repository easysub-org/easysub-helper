# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 hcz1017
"""`python -m easysub_helper` 入口。

注意：这里刻意不 import soundcard——它在 import 期会连音频服务，且有 sys.argv 的坑
（见 audio/soundcard_backend.py 的 import_soundcard），只能在真正需要时懒加载。
"""

import os
import sys

# 同样允许直接跑文件（`python easysub_helper/__main__.py`）：补上包上下文，
# 否则下面的相对 import 会失败。见 cli.py 里同一段说明。
if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    __package__ = "easysub_helper"

from .cli import main

if __name__ == "__main__":
    sys.exit(main())
