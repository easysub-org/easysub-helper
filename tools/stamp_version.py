#!/usr/bin/env python
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 hcz1017
"""发版时把 Release 版本号写进源码（`__version__` 与 pyproject）。

为什么需要：发布 tag 是 v1.8.0，而产物里的 `--version` 恒为 0.1.0 —— 排障时对不上号
（独立审查发现）。CI 在 `workflow_dispatch` 带版本号时调用本脚本，把两个地方一起盖章。

用法：python tools/stamp_version.py x.y.z[...]（发行版本号，须以数字开头）
"""

import io
import os
import re
import sys

#: `__version__ = "…"` 的赋值行
_INIT_RE = re.compile(r'(__version__\s*=\s*)"[^"]*"')
#: pyproject 的 `version = "…"`（只盖 [project] 里那一行；匹配行首，避免误伤别的键）
_PYPROJECT_RE = re.compile(r'(?m)^(version\s*=\s*)"[^"]+"')


def stamp_in(root, version):
    """在 `root` 指向的仓库里盖章；返回改过的文件相对路径列表。"""
    changed = []

    init_p = root + "/easysub_helper/__init__.py"
    text = io.open(init_p, encoding="utf-8").read()
    new, n1 = _INIT_RE.subn(lambda m: m.group(1) + '"%s"' % version, text, count=1)
    if n1 != 1:
        raise ValueError("没在 %s 里找到 __version__ 赋值" % init_p)
    io.open(init_p, "w", encoding="utf-8").write(new)
    changed.append("easysub_helper/__init__.py")

    py_p = root + "/pyproject.toml"
    text = io.open(py_p, encoding="utf-8").read()
    new, n2 = _PYPROJECT_RE.subn(lambda m: m.group(1) + '"%s"' % version, text, count=1)
    if n2 != 1:
        raise ValueError("没在 %s 里找到 version 字段" % py_p)
    io.open(py_p, "w", encoding="utf-8").write(new)
    changed.append("pyproject.toml")
    return changed


def main(argv):
    # 只接受 x.y[.z…] 且至少三段（发版号不该有 "1.2" 这种残缺写法）；多余参数直接拒绝，
    # 不然 "1.2 3" 这类手滑会被当成正常输入（复审就真踩过一次）
    if len(argv) != 2 or not re.match(r"^\d+\.\d+\.\d+$", argv[1] or ""):
        print("用法：python tools/stamp_version.py x.y.z（发行版本号，恰好一段参数、三段数字）")
        return 2
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    try:
        changed = stamp_in(root, version)
    except (ValueError, OSError) as exc:
        print("::error::%s" % exc, file=sys.stderr)
        return 1
    print("版本号已盖章：%s（%s）" % (version, ", ".join(changed)))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
