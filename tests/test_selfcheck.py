# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 hcz1017
"""`--selftest` 的测试。

这个自检本身是"产线问题的报警器"，所以它自己也要有测试：
  * 检查项怎么注入、失败怎么变成非 0 退出码（否则 CI 会绿着放过一个坏包）；
  * 默认四项在本机环境（含 tkinter、assets、重采样、本机 HTTP+WS 环路）真的能过。
"""

import io
import unittest

from easysub_helper import selfcheck


class ResultTest(unittest.TestCase):
    def test_all_pass_reports_ok_and_zero(self):
        out = io.StringIO()
        code = selfcheck.main(checks=[("a", lambda: None), ("b", lambda: None)], out=out)
        self.assertEqual(code, 0)
        text = out.getvalue()
        self.assertIn("[ok]   a", text)
        self.assertIn("selftest OK (2 checks)", text)

    def test_any_failure_gives_nonzero(self):
        def boom():
            raise RuntimeError("打包漏了 tkinter")

        out = io.StringIO()
        code = selfcheck.main(checks=[("good", lambda: None), ("bad", boom)], out=out)
        self.assertEqual(code, 1, "自检失败必须让打包作业红")
        text = out.getvalue()
        self.assertIn("[FAIL] bad", text)
        self.assertIn("打包漏了 tkinter", text)
        self.assertIn("selftest FAILED (1/2)", text)

    def test_results_carry_detail(self):
        results = selfcheck.run(checks=[("bad", lambda: 1 / 0)], out=io.StringIO())
        self.assertEqual([(name, ok) for name, ok, _ in results], [("bad", False)])
        self.assertIn("ZeroDivisionError", results[0][2])

    def test_emit_tolerates_missing_stdout(self):
        """--windowed/--noconsole 产物没有 stdout：只看退出码，打印不能炸。

        坑（独立审查抓的）：不能只传 out=None —— run() 会把它回退成 sys.stdout，
        于是 `_emit` 的 None 分支根本没被执行（假信心）。这里真的把 sys.stdout 打成 None。
        """
        import sys
        from unittest import mock

        with mock.patch.object(sys, "stdout", None):
            code = selfcheck.main(checks=[("a", lambda: None)])
        self.assertEqual(code, 0)


class DefaultChecksTest(unittest.TestCase):
    """默认四项必须过——它们就是打包作业唯一的"包没坏"证据。

    唯一的例外是 tkinter：CI 的**单元测试**环境里没装它（setup-python 的 Linux CPython 不带
    tkinter，GUI 测试那一步也因此整批 skip）。环境缺它不能算代码坏了，所以这里在有 tkinter
    时严格断言、没有时只要求"该检查被如实报告出来"；真正的失败路径由下面
    test_missing_tkinter_is_reported 用注入的方式覆盖（与环境无关）。
    """

    def test_all_default_checks_pass(self):
        out = io.StringIO()
        results = selfcheck.run(out=out)
        failed = [(name, detail) for name, ok, detail in results if not ok]
        self.assertEqual(failed, [], "默认自检项失败：%r\n%s" % (failed, out.getvalue()))
        names = [name for name, _, _ in results]
        for expected in ("tkinter", "assets", "resampler", "http+ws round trip"):
            self.assertIn(expected, names)

    def test_missing_tkinter_is_reported(self):
        """tkinter 检查必须有牙：模拟"模块导不进来"时必须报失败（除非是无头 Linux 的例外）。"""
        import builtins

        real_import = builtins.__import__

        def fake_import(name, *a, **kw):
            if name == "tkinter" or name.startswith("tkinter."):
                raise ImportError("No module named 'tkinter'")
            return real_import(name, *a, **kw)

        builtins.__import__ = fake_import
        try:
            if selfcheck._headless_linux():
                with self.assertRaises(selfcheck.Warn):
                    selfcheck.check_tkinter()
            else:
                with self.assertRaises(RuntimeError):
                    selfcheck.check_tkinter()
        finally:
            builtins.__import__ = real_import

    def test_tkinter_check_ignores_display(self):
        """判定必须只看**模块**在不在：无显示≠缺 tkinter（本仓第一次加检查时就误诊过）。

        做法：把"无头判定"强制为 True（等效无 DISPLAY），在 tkinter 可导入的前提下调用
        check_tkinter()——必须**正常返回**。若哪天有人改回依赖 gui.tkinter_module()（它在无显示
        Linux 上返回 None），这里会抛 Warn，测试立刻红。
        """
        try:
            import tkinter  # noqa: F401
        except Exception:
            self.skipTest("本机没有 tkinter，这条测不了")

        original = selfcheck._headless_linux
        selfcheck._headless_linux = lambda: True
        try:
            selfcheck.check_tkinter()          # 不抛异常 = 按"模块存在"判定，与显示无关
        finally:
            selfcheck._headless_linux = original

    def test_warn_does_not_fail_the_run(self):
        def warn_check():
            raise selfcheck.Warn("环境不适用")

        out = io.StringIO()
        code = selfcheck.main(checks=[("warny", warn_check)], out=out)
        self.assertEqual(code, 0)
        self.assertIn("[warn] warny", out.getvalue())
        self.assertIn("selftest OK (1 checks)", out.getvalue())

    def test_require_tkinter_turns_the_warning_into_a_failure(self):
        """打包冒烟用 --require-tkinter 时，"无头环境缺 tkinter"必须变红。

        独立审查指出：Warn 豁免是为了让没图形环境的构建机不误报，但打包作业要回答的是
        "**这个产物**有没有 tkinter"——实测三平台打包 runner 的 CPython 都带 tkinter，
        所以产物里缺它一定是打包漏了。
        """
        def warn_tkinter():
            raise selfcheck.Warn("headless Linux, tkinter missing")

        strict = io.StringIO()
        self.assertEqual(selfcheck.main(checks=[("tkinter", warn_tkinter)], out=strict,
                                        require_tkinter=True), 1)
        self.assertIn("FAILED", strict.getvalue())
        # 不带该开关时（本地/单元测试环境）仍然是提醒，不算失败
        lenient = io.StringIO()
        self.assertEqual(selfcheck.main(checks=[("tkinter", warn_tkinter)], out=lenient), 0)
        self.assertIn("[warn]", lenient.getvalue())

    def test_missing_assets_are_reported(self):
        from easysub_helper import gui

        original = gui.assets_dir
        gui.assets_dir = lambda: None
        try:
            with self.assertRaises(RuntimeError):
                selfcheck.check_assets()
        finally:
            gui.assets_dir = original

    def test_frame_size_contract(self):
        # 16k 单声道 f32le 20ms = 320 样本 = 1280 字节（页面按这个切帧）
        self.assertEqual(selfcheck.FRAME_BYTES, 1280)


if __name__ == "__main__":
    unittest.main()
