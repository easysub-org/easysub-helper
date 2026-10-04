# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 hcz1017
"""协议消息构造/校验测试。"""

import unittest

from easysub_helper import i18n, protocol


class MessageTest(unittest.TestCase):
    def test_hello_shape(self):
        msg = protocol.hello("1.0.0", 16000, 1, 20, ["system", "mic"], "parec", "mon", 16000, False,
                             lang="zh_CN")
        self.assertEqual(msg["type"], "hello")
        self.assertEqual(msg["api"], protocol.API_VERSION)
        self.assertEqual(msg["format"], "f32le")
        self.assertEqual(msg["frameMs"], 20)
        self.assertEqual(msg["lang"], "zh_CN")
        self.assertIsNone(protocol.hello("1", 16000, 1, 20, [], None, None, None, False)["nativeRate"])
        # 总开关（默认暂停）必须一路传到页面：hello 与 state 都带 paused
        self.assertFalse(msg["paused"])
        self.assertTrue(protocol.hello("1", 16000, 1, 20, [], None, None, None, False, paused=True)["paused"])

    def test_error_and_level(self):
        err = protocol.error(protocol.ERR_BAD_CODE, "x", fatal=True)
        self.assertEqual((err["type"], err["code"], err["fatal"]), ("error", "bad_code", True))
        lv = protocol.level(0.1234567, 0.7654321)
        self.assertEqual(lv["rms"], 0.123457)

    def test_state_carries_paused(self):
        msg = protocol.state(False, None, None, None, paused=True)
        self.assertEqual(msg["type"], "state")
        self.assertFalse(msg["capturing"])
        self.assertTrue(msg["paused"])
        self.assertFalse(protocol.state(True, "system", "parec", "mon")["paused"])

    def test_paused_error_code_is_stable(self):
        # 前端按这个 code 渲染"去助手窗口点启动"，所以它是对外契约，别改名
        self.assertEqual(protocol.ERR_PAUSED, "paused")

    def test_dumps_is_compact_utf8(self):
        text = protocol.dumps({"a": "中文"})
        self.assertNotIn(" ", text)
        self.assertIn("中文", text)


class ParseTest(unittest.TestCase):
    def test_accepts_valid(self):
        self.assertEqual(protocol.parse_client_message('{"type":"start","source":"mic"}')["source"], "mic")
        self.assertEqual(protocol.parse_client_message('{"type":"stop"}')["type"], "stop")
        self.assertEqual(protocol.parse_client_message(b'{"type":"ping","t":7}')["t"], 7)

    def test_rejects_invalid(self):
        # "devices" 曾经存在，现在助手只认 start/stop/ping（设备列举已删）
        for raw in ("not json", "[]", '{"type":"nope"}', '{"type":"devices"}',
                    '{"type":"start","source":"desktop"}',
                    '{"type":"start","device":5}', '{"type":"ping","t":"soon"}', b"\xff\xfe"):
            with self.assertRaises(protocol.BadMessage, msg="应拒绝: %r" % (raw,)):
                protocol.parse_client_message(raw)

    def test_errors_are_localized(self):
        saved = i18n.get_language()
        try:
            i18n.set_language("en")
            with self.assertRaises(protocol.BadMessage) as ctx:
                protocol.parse_client_message("nope")
            self.assertNotIn("合法", str(ctx.exception))
            i18n.set_language("zh_CN")
            with self.assertRaises(protocol.BadMessage) as ctx:
                protocol.parse_client_message("nope")
            self.assertIn("合法", str(ctx.exception))
        finally:
            i18n.set_language(saved)

    def test_error_codes_are_stable(self):
        # 前端按 code 渲染自己的文案：这些值不能随手改
        self.assertEqual(protocol.ERR_NOT_PAIRED, "not_paired")
        self.assertEqual(protocol.ERR_BAD_CODE, "bad_code")
        self.assertEqual(protocol.ERR_CODE_EXPIRED, "code_expired")
        self.assertEqual(protocol.ERR_LOCKED, "locked")
        self.assertEqual(protocol.ERR_NO_CODE, "no_code")
        self.assertEqual(protocol.ERR_CAPTURE_FAILED, "capture_failed")


if __name__ == "__main__":
    unittest.main()
