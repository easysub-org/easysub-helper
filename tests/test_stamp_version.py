# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 hcz1017
"""发版版本号盖章的测试。

背景：第一版脚本有一个 **SyntaxError**（正则里引号嵌套写错），整个文件无法被加载——
而它只在 `workflow_dispatch` 发版时才跑，日常 CI 全绿，差点带着坏脚本发版。所以这里：
① 用临时仓库真跑 `stamp_in`；② 专门 import 这个脚本（防 SyntaxError 漏网）。
"""

import importlib.util
import io
import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

SPEC = importlib.util.spec_from_file_location("stamp_version", os.path.join(ROOT, "tools", "stamp_version.py"))
stamp_version = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(stamp_version)          # **能 import 本身就是一条测试**（SyntaxError 会在这里炸）


class StampInTest(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="easysub-stamp-")
        os.makedirs(os.path.join(self.root, "easysub_helper"))
        with io.open(os.path.join(self.root, "easysub_helper", "__init__.py"), "w", encoding="utf-8") as fh:
            fh.write('__version__ = "0.1.0"\n')
        with io.open(os.path.join(self.root, "pyproject.toml"), "w", encoding="utf-8") as fh:
            fh.write('[project]\nname = "easysub-helper"\nversion = "0.1.0"\n')

    def _read(self, rel):
        with io.open(os.path.join(self.root, rel), encoding="utf-8") as fh:
            return fh.read()

    def test_stamps_both_files(self):
        changed = stamp_version.stamp_in(self.root, "1.8.0")
        self.assertEqual(changed, ["easysub_helper/__init__.py", "pyproject.toml"])
        self.assertIn('__version__ = "1.8.0"', self._read("easysub_helper/__init__.py"))
        self.assertIn('version = "1.8.0"', self._read("pyproject.toml"))

    def test_missing_files_raise(self):
        with self.assertRaises(OSError):
            stamp_version.stamp_in(os.path.join(self.root, "nope"), "1.8.0")

    def test_main_rejects_non_numeric_input(self):
        # 拒绝矩阵：非三段 / 多余参数 / 空参
        self.assertEqual(stamp_version.main(["s.py", "beta"]), 2)
        self.assertEqual(stamp_version.main(["s.py", "1.2"]), 2)
        self.assertEqual(stamp_version.main(["s.py", "1.2.3.4"]), 2)
        self.assertEqual(stamp_version.main(["s.py", "1.8.0", "extra"]), 2)
        self.assertEqual(stamp_version.main(["s.py"]), 2)

    def test_main_success_path_stamps_files(self):
        """成功路径的回归网：第一版 main() 在成功路径上 NameError，测试全绿照样发不出版
        （拒绝路径提前 return，把它绕过去了）。root 注入口让这条可以对着临时仓库真跑。"""
        code = stamp_version.main(["stamp_version.py", "1.8.0"], root=self.root)
        self.assertEqual(code, 0)
        self.assertIn('__version__ = "1.8.0"', self._read("easysub_helper/__init__.py"))
        self.assertIn('version = "1.8.0"', self._read("pyproject.toml"))


if __name__ == "__main__":
    unittest.main()
