# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 hcz1017
"""Linux 子进程后端的命令行构造（不真的起进程，CI 上也能跑）。"""

import sys
import unittest

from easysub_helper.audio.subprocess_backend import DEFAULT_MONITOR, SubprocessBackend


class ArgvTest(unittest.TestCase):
    def test_parec_system_uses_default_monitor(self):
        be = SubprocessBackend(tool="parec", source="system", rate=16000, frame_ms=20)
        argv = be.build_argv()
        self.assertIn("--device=" + DEFAULT_MONITOR, argv)
        self.assertIn("--format=float32le", argv)
        self.assertIn("--rate=16000", argv)
        self.assertIn("--channels=1", argv)

    def test_parec_mic_omits_device(self):
        be = SubprocessBackend(tool="parec", source="mic", rate=48000, frame_ms=20)
        argv = be.build_argv()
        self.assertFalse([a for a in argv if a.startswith("--device=")], "麦克风应走工具默认输入源")
        self.assertIn("--rate=48000", argv)

    def test_explicit_device_wins(self):
        be = SubprocessBackend(tool="parec", source="system", device="MySink.monitor")
        self.assertIn("--device=MySink.monitor", be.build_argv())

    def test_pw_record(self):
        be = SubprocessBackend(tool="pw-record", source="system", rate=16000)
        argv = be.build_argv()
        self.assertIn("--target", argv)
        self.assertIn("-", argv, "pw-record 需要显式输出到 stdout")
        self.assertEqual(be.name, "pw-record")

    def test_latency_defaults_to_frame_ms(self):
        be = SubprocessBackend(tool="parec", source="system", frame_ms=20)
        self.assertEqual(be.latency_ms, 20)
        self.assertIn("--latency-msec=20", be.build_argv())

    @unittest.skipIf(sys.platform.startswith("win"), "Linux 专用")
    def test_find_tool_on_linux(self):
        from easysub_helper.audio.subprocess_backend import find_tool

        # 本机（开发/CI 容器）可能都没有；这里只要求"要么找到，要么干净地返回 None"
        tool = find_tool()
        self.assertTrue(tool is None or tool in ("parec", "pw-record"))


if __name__ == "__main__":
    unittest.main()
