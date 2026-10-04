# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 hcz1017
"""Win7 打包脚本的纯逻辑测试（在 Linux/macOS 上也能跑）。

`tools/build_win7_exe.py` 里"找 UCRT、拼 PyInstaller 参数"这两步是纯函数，所以没有 Windows
也能测。这层测试的意义：Win7 支持是**没有真机可验**的一条路径，参数一旦写错（漏 --add-binary、
DLL 找错目录），我们只会在用户机器上才知道。
"""

import io
import os
import shutil
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from tools import build_win7_exe as bw  # noqa: E402


def _write(path, text=""):
    with io.open(path, "w", encoding="utf-8") as fh:
        fh.write(text)


class FindUcrtTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="easysub-ucrt-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.sdk = os.path.join(self.tmp, "sdk")
        self.pydir = os.path.join(self.tmp, "python")
        for d in (self.sdk, self.pydir):
            os.makedirs(d)
        # SDK 里一整套 UCRT + 一个无关 DLL
        for name in ("api-ms-win-crt-runtime-l1-1-0.dll",
                     "api-ms-win-crt-stdio-l1-1-0.dll",
                     "ucrtbase.dll",
                     "unrelated.dll"):
            _write(os.path.join(self.sdk, name))
        # Python 目录里只有基础库（用来验证"先出现的目录优先"）
        _write(os.path.join(self.pydir, "ucrtbase.dll"))

    def test_finds_api_sets_and_ucrtbase(self):
        found = bw.find_ucrt_dlls([self.sdk])
        self.assertEqual(sorted(found), [
            "api-ms-win-crt-runtime-l1-1-0.dll",
            "api-ms-win-crt-stdio-l1-1-0.dll",
            "ucrtbase.dll",
        ])
        self.assertNotIn("unrelated.dll", found)

    def test_earlier_directory_wins(self):
        found = bw.find_ucrt_dlls([self.sdk, self.pydir])
        self.assertEqual(found["ucrtbase.dll"], os.path.join(self.sdk, "ucrtbase.dll"))

    def test_case_insensitive(self):
        upper = os.path.join(self.tmp, "upper")
        os.makedirs(upper)
        _write(os.path.join(upper, "UCRTBASE.DLL"))
        _write(os.path.join(upper, "API-MS-WIN-CRT-RUNTIME-L1-1-0.DLL"))
        found = bw.find_ucrt_dlls([upper])
        self.assertEqual(sorted(found), ["api-ms-win-crt-runtime-l1-1-0.dll", "ucrtbase.dll"])

    def test_missing_directories_are_ignored(self):
        self.assertEqual(bw.find_ucrt_dlls([None, "", os.path.join(self.tmp, "nope")]), {})
        self.assertEqual(bw.find_ucrt_dlls([]), {})


class CandidateDirsTest(unittest.TestCase):
    def test_sdk_redist_comes_first_and_uses_arch(self):
        dirs = bw.candidate_dirs("x86", env={"SystemRoot": r"D:\Win", "ProgramFiles(x86)": r"D:\PF"})
        self.assertTrue(dirs[0].endswith(os.path.join("Redist", "ucrt", "DLLs", "x86")), dirs[0])
        self.assertTrue(dirs[0].startswith(r"D:\PF"), dirs[0])
        self.assertTrue(any("System32" in d for d in dirs))

    def test_env_defaults_are_used(self):
        dirs = bw.candidate_dirs("x64", env={})
        self.assertTrue(dirs[0].startswith(r"C:\Program Files (x86)"), dirs[0])


class BuildCommandTest(unittest.TestCase):
    def setUp(self):
        self.dlls = {
            "ucrtbase.dll": r"C:\sdk\ucrtbase.dll",
            "api-ms-win-crt-runtime-l1-1-0.dll": r"C:\sdk\api-ms-win-crt-runtime-l1-1-0.dll",
        }
        self.cmd = bw.build_command("python.exe", self.dlls)

    def test_core_flags(self):
        for flag in ("--onefile", "--noconsole", "--collect-submodules", "--hidden-import"):
            self.assertIn(flag, self.cmd)
        self.assertEqual(self.cmd[self.cmd.index("--name") + 1], "easysub-helper")
        self.assertEqual(self.cmd[self.cmd.index("--icon") + 1], bw.ICON)
        self.assertEqual(self.cmd[-1], bw.ENTRY, "入口脚本必须是最后一个参数")

    def test_every_dll_gets_an_add_binary(self):
        pairs = [self.cmd[i + 1] for i, a in enumerate(self.cmd) if a == "--add-binary"]
        self.assertEqual(len(pairs), len(self.dlls))
        for pair in pairs:
            self.assertTrue(pair.endswith(os.pathsep + "."), pair)
        self.assertIn(r"C:\sdk\ucrtbase.dll" + os.pathsep + ".", pairs)

    def test_assets_are_added_explicitly(self):
        value = self.cmd[self.cmd.index("--add-data") + 1]
        self.assertIn("easysub_helper/assets", value)
        self.assertIn(os.pathsep, value)


class MainTest(unittest.TestCase):
    def test_list_ucrt_returns_zero(self):
        original = bw.find_ucrt_dlls
        bw.find_ucrt_dlls = lambda dirs: {"ucrtbase.dll": r"C:\x\ucrtbase.dll"}  # noqa: E731
        self.addCleanup(lambda: setattr(bw, "find_ucrt_dlls", original))
        self.assertEqual(bw.main(["--list-ucrt"]), 0)

    def test_missing_ucrt_is_an_error_not_a_silent_build(self):
        original = bw.find_ucrt_dlls
        bw.find_ucrt_dlls = lambda dirs: {}  # noqa: E731
        self.addCleanup(lambda: setattr(bw, "find_ucrt_dlls", original))
        self.assertEqual(bw.main([]), 2, "找不到 UCRT 必须报错，不能产出一个起不来的包")

    def test_dry_run_prints_a_command(self):
        original = bw.find_ucrt_dlls
        bw.find_ucrt_dlls = lambda dirs: {"ucrtbase.dll": r"C:\x\ucrtbase.dll"}  # noqa: E731
        self.addCleanup(lambda: setattr(bw, "find_ucrt_dlls", original))
        self.assertEqual(bw.main(["--dry-run"]), 0)

    def test_non_windows_build_is_refused(self):
        if os.name == "nt":
            self.skipTest("Windows 上这条没有意义")
        original = bw.find_ucrt_dlls
        bw.find_ucrt_dlls = lambda dirs: {"ucrtbase.dll": r"C:\x\ucrtbase.dll"}  # noqa: E731
        self.addCleanup(lambda: setattr(bw, "find_ucrt_dlls", original))
        self.assertEqual(bw.main([]), 2, "非 Windows 上必须拒绝真打包")


if __name__ == "__main__":
    unittest.main()
