# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 hcz1017
"""Python 3.6 兼容门禁。

为什么卡 3.6：Win7 上最后一个可用的 CPython 是 3.8，但构建机/CI 镜像常常只有更老的
解释器；源码只用 3.6 能解析的语法，就能在任意老环境里跑起来（发布 Win7 产物再单独用 3.8 构建）。

两道检查：
  1. 用 `ast.parse(feature_version=(3,6))` 逐文件解析（能抓出海象运算符、f-string `=`、
     仅位置参数等 3.8+ 语法）；
  2. 扫禁用 API（那些**能解析但运行时才炸**的：dataclass、asyncio.run、
     subprocess.run(capture_output=)、str.removeprefix、functools.cache…）。
"""

import ast
import io
import os
import unittest

ROOT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "easysub_helper")

#: (子串, 说明)。注意 asyncio.get_running_loop 是通过 getattr 兜底用的，所以这里查的是
#: 直接调用形式 "asyncio.get_running_loop("。
BANNED = (
    ("from __future__ import annotations", "3.7+"),
    ("@dataclass", "3.7+"),
    ("dataclasses import", "3.7+"),
    ("asyncio.run(", "3.7+"),
    ("asyncio.get_running_loop(", "3.7+（需 getattr 兜底）"),
    ("capture_output=", "3.7+"),
    ("text=True", "3.7+（用 universal_newlines=True）"),
    ("functools.cache", "3.9+"),
    (".removeprefix(", "3.9+"),
    (".removesuffix(", "3.9+"),
    ("math.lcm", "3.9+"),
    ("importlib.metadata", "3.8+"),
    ("web.AppKey", "aiohttp 3.9+"),
    ("str | None", "3.10+ 的类型联合写法"),
)


def _sources():
    for base, _dirs, files in os.walk(ROOT):
        for name in files:
            if name.endswith(".py"):
                yield os.path.join(base, name)


class SyntaxTest(unittest.TestCase):
    def test_all_files_parse_as_python36(self):
        checked = 0
        for path in _sources():
            with io.open(path, encoding="utf-8") as fh:
                source = fh.read()
            try:
                ast.parse(source, filename=path, feature_version=(3, 6))
            except SyntaxError as exc:
                self.fail("{}:{} 不是 3.6 可解析的语法: {}".format(path, exc.lineno, exc.msg))
            checked += 1
        self.assertGreater(checked, 5, "没扫到源文件，路径可能变了")


class BannedApiTest(unittest.TestCase):
    def test_no_banned_apis(self):
        hits = []
        for path in _sources():
            with io.open(path, encoding="utf-8") as fh:
                for lineno, line in enumerate(fh, 1):
                    stripped = line.strip()
                    if stripped.startswith("#") or stripped.startswith('"'):
                        continue
                    for needle, why in BANNED:
                        if needle in line:
                            hits.append("{}:{} {}（{}）".format(path, lineno, needle, why))
        self.assertEqual(hits, [], "发现 3.6 上不可用的 API：\n" + "\n".join(hits))


if __name__ == "__main__":
    unittest.main()
