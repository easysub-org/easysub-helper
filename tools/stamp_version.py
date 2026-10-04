#!/usr/bin/env python
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 hcz1017
"""发版时把 Release 版本号写进源码（`__version__` 与 pyproject）。

为什么需要：发布 tag 是 v1.8.0，而产物里的 `--version` 恒为 0.1.0 —— 排障时对不上号
（独立审查发现）。CI 在 `workflow_dispatch` 带版本号时调用本脚本，把两个地方一起盖章。
"""

import io
import re
import sys


def stamp(version):
    init_p = "easysub_helper/__init__.py"
    text = io.open(init_p, encoding="utf-8").read()
    new, n1 = re.subn(r"(__version__\s*=\s*)"[^"]+"", r'\1"%s"' % version, text, count=1)
    if n1 != 1:
        raise SystemExit("错误：没在 %s 里找到 __version__" % init_p)
    io.open(init_p, "w", encoding="utf-8").write(new)

    pyproject_p = "pyproject.toml"
    text = io.open(pyproject_p, encoding="utf-8").read()
    new, n2 = re.subn(r'(?m)^(version\s*=\s*)"[^"]+"', r'\1"%s"' % version, text, count=1)
    if n2 != 1:
        raise SystemExit("错误：没在 %s 里找到 version" % pyproject_p)
    io.open(pyproject_p, "w", encoding="utf-8").write(new)
    print("版本号已盖章：%s" % version)


if __name__ == "__main__":
    if len(sys.argv) != 2 or not re.match(r"^\d+\.\d+\.\d+", sys.argv[1] or ""):
        print("用法：python tools/stamp_version.py x.y.z[...]（发行版本号）")
        raise SystemExit(2)
    stamp(sys.argv[1])
