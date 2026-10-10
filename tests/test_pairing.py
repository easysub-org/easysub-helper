# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 hcz1017
"""配对码/设备令牌测试。

配对是本机服务的唯一入口，参数（有效期、失败限速、令牌哈希）都必须有测试盯着——
配对码只有 6 位，没有限速就等于没有认证。
"""

import os
import shutil
import tempfile
import time
import unittest

from easysub_helper import pairing


class CodeTest(unittest.TestCase):
    def setUp(self):
        self.mgr = pairing.PairingManager(persist=False)

    def test_code_charset_and_format(self):
        code = self.mgr.new_code()
        self.assertEqual(len(code), pairing.CODE_LEN)
        for ch in code:
            self.assertIn(ch, pairing.CODE_ALPHABET, "配对码不该出现易看错的字符: %r" % ch)
        for bad in ("0", "O", "1", "I", "L"):
            self.assertNotIn(bad, pairing.CODE_ALPHABET)
        self.assertEqual(pairing.format_code(code), code[:3] + "-" + code[3:])

    def test_verify_accepts_formatted_and_lowercase(self):
        code = self.mgr.new_code()
        for variant in (code, pairing.format_code(code), code.lower(), " " + code + " ",
                        pairing.format_code(code).lower()):
            self.mgr._attempts = []
            self.mgr._locked_until = 0.0
            ok, reason = self.mgr.verify(variant)
            self.assertTrue(ok, "应接受 %r，实际 reason=%s" % (variant, reason))

    def test_verify_rejects_wrong_code(self):
        self.mgr.new_code()
        ok, reason = self.mgr.verify("ZZZZZZ")
        self.assertFalse(ok)
        self.assertEqual(reason, pairing.ERR_BAD_CODE)

    def test_lockout_after_repeated_failures(self):
        self.mgr.new_code()
        for _ in range(pairing.MAX_ATTEMPTS):
            ok, _reason = self.mgr.verify("ZZZZZZ")
            self.assertFalse(ok)
        ok, reason = self.mgr.verify("ZZZZZZ")
        self.assertEqual(reason, pairing.ERR_LOCKED)
        # 锁定期间**连正确码也拒**（否则暴力破解可以借正确码绕过限速语义）
        ok, reason = self.mgr.verify(self.mgr._code)
        self.assertFalse(ok)
        self.assertEqual(reason, pairing.ERR_LOCKED)
        self.assertGreater(self.mgr.state()["lockedForSec"], 0)

    def test_new_code_clears_lockout(self):
        self.mgr.new_code()
        for _ in range(pairing.MAX_ATTEMPTS):
            self.mgr.verify("ZZZZZZ")
        self.assertEqual(self.mgr.verify("ZZZZZZ")[1], pairing.ERR_LOCKED)
        code = self.mgr.new_code()
        self.assertEqual(self.mgr.verify(code), (True, None))

    def test_expiry(self):
        mgr = pairing.PairingManager(persist=False, code_ttl=0.01)
        mgr.new_code()
        time.sleep(0.05)
        ok, reason = mgr.verify("ANY")
        self.assertFalse(ok)
        self.assertEqual(reason, pairing.ERR_EXPIRED)
        self.assertFalse(mgr.state()["valid"])

    def test_no_code_yet(self):
        ok, reason = pairing.PairingManager(persist=False).verify("ABC123")
        self.assertFalse(ok)
        self.assertEqual(reason, pairing.ERR_NO_CODE)

    def test_new_code_invalidates_old(self):
        old = self.mgr.new_code()
        self.mgr.new_code()
        self.assertFalse(self.mgr.verify(old)[0])

    def test_error_message_is_localized_and_nonempty(self):
        self.mgr.new_code()
        for reason in (pairing.ERR_BAD_CODE, pairing.ERR_EXPIRED, pairing.ERR_NO_CODE,
                       pairing.ERR_LOCKED):
            text = self.mgr.error_message(reason)
            self.assertTrue(text)
            self.assertNotEqual(text, "server.err." + reason)


class TokenTest(unittest.TestCase):
    def test_issue_and_validate(self):
        mgr = pairing.PairingManager(persist=False)
        token = mgr.issue_token("chrome")
        self.assertTrue(mgr.token_valid(token))
        self.assertFalse(mgr.token_valid(token + "x"))
        self.assertFalse(mgr.token_valid(""))
        self.assertFalse(mgr.token_valid(None))
        self.assertEqual(len(mgr.list_devices()), 1)
        self.assertEqual(mgr.list_devices()[0]["label"], "chrome")

    def test_list_devices_includes_digest_and_forget_one(self):
        """列表要带 digest，且能**只解绑一个**（窗口里「解绑」按钮靠它）。

        坑（用户可用性审查）：`list_devices()/forget_all()` 以前是死代码，界面上没有入口，
        用户想撤销某个已配对设备只能自己去删 pairing.json。
        """
        mgr = pairing.PairingManager(persist=False)
        mgr.issue_token("chrome")
        mgr.issue_token("firefox")
        items = mgr.list_devices()
        self.assertEqual(len(items), 2)
        for entry in items:
            self.assertIn("digest", entry, "列表项必须带 digest，否则界面无法定向解绑")
            self.assertTrue(entry["digest"])
        victim = items[0]["digest"]
        self.assertTrue(mgr.forget(victim))
        self.assertFalse(mgr.forget(victim), "重复解绑同一个应返回 False")
        left = mgr.list_devices()
        self.assertEqual(len(left), 1)
        self.assertNotEqual(left[0]["digest"], victim)
        self.assertFalse(mgr.forget(None))
        self.assertFalse(mgr.forget(""))

    def test_forget_all(self):
        mgr = pairing.PairingManager(persist=False)
        token = mgr.issue_token()
        self.assertEqual(mgr.forget_all(), 1)
        self.assertFalse(mgr.token_valid(token))

    def test_tokens_are_not_stored_in_plaintext(self):
        tmp = tempfile.mkdtemp()
        try:
            path = os.path.join(tmp, "pairing.json")
            mgr = pairing.PairingManager(store_path=path)
            token = mgr.issue_token("browser")
            with open(path, "r", encoding="utf-8") as fh:
                raw = fh.read()
            self.assertNotIn(token, raw, "落盘的必须是哈希，不能是令牌明文")
            reloaded = pairing.PairingManager(store_path=path)
            self.assertTrue(reloaded.token_valid(token), "重启后仍应认这个令牌")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
