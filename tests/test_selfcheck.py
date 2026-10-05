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
        """--windowed/--noconsole 产物没有 stdout：只看退出码，打印不能炸。"""
        code = selfcheck.main(checks=[("a", lambda: None)], out=None)
        self.assertEqual(code, 0)


class DefaultChecksTest(unittest.TestCase):
    """默认四项在本机必须全过——它们就是打包作业唯一的"包没坏"证据。"""

    def test_all_default_checks_pass(self):
        out = io.StringIO()
        results = selfcheck.run(out=out)
        failed = [(name, detail) for name, ok, detail in results if not ok]
        self.assertEqual(failed, [], "默认自检项失败：%r\n%s" % (failed, out.getvalue()))
        names = [name for name, _, _ in results]
        for expected in ("tkinter", "assets", "resampler", "http+ws round trip"):
            self.assertIn(expected, names)

    def test_frame_size_contract(self):
        # 16k 单声道 f32le 20ms = 320 样本 = 1280 字节（页面按这个切帧）
        self.assertEqual(selfcheck.FRAME_BYTES, 1280)


if __name__ == "__main__":
    unittest.main()
