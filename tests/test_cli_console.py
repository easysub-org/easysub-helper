# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 hcz1017
"""控制台编码：中文输出不许把进程搞崩，也不许被静默吞掉。

背景（CI 实测）：Windows 上输出被重定向到文件/管道时用的是系统代码页（常见 cp1252），
`python -m easysub_helper --help` 直接在 `argparse.print_help()` 里抛 UnicodeEncodeError；
而代理版横幅因为 `_eprint()` 吞异常不崩，却把**每一行中文都丢了**（端口、配对码全没）。
这里用 `PYTHONIOENCODING=cp1252` 在任意平台上复现同一条路径。
"""

import io
import os
import subprocess
import sys
import unittest

from easysub_helper import cli

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class SoftenStreamsTest(unittest.TestCase):
    def test_utf8_replace_is_applied(self):
        raw = io.BytesIO()
        stream = io.TextIOWrapper(raw, encoding="cp1252")
        original = sys.stdout
        sys.stdout = stream
        try:
            cli._soften_console_encoding()
            self.assertEqual(sys.stdout.encoding.lower().replace("-", ""), "utf8")
            self.assertEqual(sys.stdout.errors, "replace")
            sys.stdout.write("易字幕\n")      # 关键：不许抛
            sys.stdout.flush()
        finally:
            sys.stdout = original
        self.assertIn("易字幕".encode("utf-8"), raw.getvalue())

    def test_none_streams_get_a_black_hole(self):
        """--windowed/--noconsole 冻结后 sys.stdout 是 None：必须补上黑洞，否则 argparse 会崩。"""
        original_out, original_err = sys.stdout, sys.stderr
        sys.stdout = None
        sys.stderr = None
        try:
            cli._soften_console_encoding()
            self.assertIsNotNone(sys.stdout, "None 的 stdout 应被换成黑洞流")
            self.assertIsNotNone(sys.stderr)
            sys.stdout.write("随便写点什么\n")   # 不许抛
            # --version 走 argparse 的 version action，正常退出方式是 SystemExit(0)
            with self.assertRaises(SystemExit) as ctx:
                cli.main(["--version"])          # 冻结产物最常见的排障命令
            self.assertEqual(ctx.exception.code, 0)
        finally:
            sys.stdout, sys.stderr = original_out, original_err


class Cp1252SubprocessTest(unittest.TestCase):
    """真正复现 CI：把子进程的 stdout 编码钉成 cp1252。"""

    def _run(self, *argv):
        env = dict(os.environ)
        env["PYTHONIOENCODING"] = "cp1252"
        return subprocess.run(
            [sys.executable, "-m", "easysub_helper"] + list(argv),
            cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )

    def test_help_does_not_crash(self):
        proc = self._run("--help")
        self.assertEqual(proc.returncode, 0, proc.stderr.decode("utf-8", "replace"))
        self.assertNotIn(b"UnicodeEncodeError", proc.stderr)
        self.assertIn(b"--port", proc.stdout)

    def test_help_keeps_the_chinese(self):
        proc = self._run("--help")
        self.assertIn("音源".encode("utf-8"), proc.stdout, "重定向时中文应以 UTF-8 写出而不是丢掉")

    def test_version_does_not_crash(self):
        self.assertEqual(self._run("--version").returncode, 0)


if __name__ == "__main__":
    unittest.main()
