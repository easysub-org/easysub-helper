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


def _soften_console_encoding():
    """stdout/stderr 在"编码不了"时不崩（与 cli._soften_console_encoding 同一套）。

    为什么这里也要：CI 的 Windows runner 上 stdout 是 cp1252，用法提示里的中文
    （"用法：…三段数字"）一 print 就 UnicodeEncodeError → 测试 ERROR（实测踩过）。
    tools/ 下独立脚本不能 import easysub_helper（打包外运行），所以复制一小份。
    """
    for name in ("stdout", "stderr"):
        stream = getattr(sys, name, None)
        if stream is None:
            try:
                setattr(sys, name, open(os.devnull, "w", encoding="utf-8", errors="replace"))
            except OSError:
                pass
            continue
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(encoding="utf-8", errors="replace")
                continue
            except (ValueError, OSError):
                pass
        try:
            setattr(sys, name, io.TextIOWrapper(stream.buffer, encoding="utf-8", errors="replace"))
        except (AttributeError, ValueError, OSError):
            pass

#: `__version__ = "…"` 的赋值行
_INIT_RE = re.compile(r'(__version__\s*=\s*)"[^"]*"')
#: pyproject 的 `version = "…"`（只盖 [project] 里那一行；匹配行首，避免误伤别的键）
_PYPROJECT_RE = re.compile(r'(?m)^(version\s*=\s*)"[^"]+"')


def stamp_in(root, version):
    """在 `root` 指向的仓库里盖章；返回改过的文件相对路径列表。"""
    changed = []

    init_p = root + "/easysub_helper/__init__.py"
    with io.open(init_p, encoding="utf-8") as fh:
        text = fh.read()
    new, n1 = _INIT_RE.subn(lambda m: m.group(1) + '"%s"' % version, text, count=1)
    if n1 != 1:
        raise ValueError("没在 %s 里找到 __version__ 赋值" % init_p)
    with io.open(init_p, "w", encoding="utf-8") as fh:
        fh.write(new)
    changed.append("easysub_helper/__init__.py")

    py_p = root + "/pyproject.toml"
    with io.open(py_p, encoding="utf-8") as fh:
        text = fh.read()
    new, n2 = _PYPROJECT_RE.subn(lambda m: m.group(1) + '"%s"' % version, text, count=1)
    if n2 != 1:
        raise ValueError("没在 %s 里找到 version 字段" % py_p)
    with io.open(py_p, "w", encoding="utf-8") as fh:
        fh.write(new)
    changed.append("pyproject.toml")
    return changed


def main(argv, root=None):
    _soften_console_encoding()
    # 只接受 x.y[.z…] 且至少三段（发版号不该有 "1.2" 这种残缺写法）；多余参数直接拒绝，
    # 不然 "1.2 3" 这类手滑会被当成正常输入（复审就真踩过一次）
    if len(argv) != 2 or not re.match(r"^\d+\.\d+\.\d+$", argv[1] or ""):
        print("用法：python tools/stamp_version.py x.y.z（发行版本号，恰好一段参数、三段数字）")
        return 2
    if root is None:
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    try:
        changed = stamp_in(root, argv[1])
    except (ValueError, OSError) as exc:
        print("::error::%s" % exc, file=sys.stderr)
        return 1
    print("版本号已盖章：%s（%s）" % (argv[1], ", ".join(changed)))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
