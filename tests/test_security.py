# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 hcz1017
"""安全边界测试：token 与 Origin。

威胁模型见 easysub_helper/security.py 的模块注释——本机任意网页都能访问 127.0.0.1，
所以这两道锁是"能不能上线"的分界线，必须有测试盯着。
"""

import unittest

from easysub_helper import security


class TokenTest(unittest.TestCase):
    def test_roundtrip(self):
        token = security.new_token()
        self.assertTrue(security.token_ok(token, token))

    def test_tokens_are_random(self):
        self.assertNotEqual(security.new_token(), security.new_token())
        self.assertGreaterEqual(len(security.new_token()), 20)

    def test_rejects(self):
        token = security.new_token()
        self.assertFalse(security.token_ok(None, token))
        self.assertFalse(security.token_ok("", token))
        self.assertFalse(security.token_ok(token + "x", token))
        self.assertFalse(security.token_ok(token, ""))
        self.assertFalse(security.token_ok(token, None))
        self.assertFalse(security.token_ok(12345, token))


class LoopbackTest(unittest.TestCase):
    def test_loopback(self):
        for host in ("127.0.0.1", "localhost", "::1", "127.5.5.5"):
            self.assertTrue(security.is_loopback_host(host), host)

    def test_non_loopback(self):
        for host in ("0.0.0.0", "192.168.1.10", "example.com", "", None):
            self.assertFalse(security.is_loopback_host(host), host)


class OriginTest(unittest.TestCase):
    def test_same_port_only(self):
        self.assertTrue(security.origin_ok("http://127.0.0.1:8790", 8790))
        self.assertTrue(security.origin_ok("http://localhost:8790", 8790))
        self.assertTrue(security.origin_ok("http://127.0.0.1:8790/", 8790), "尾斜杠应被容忍")
        # 别的本地端口上的页面不该能连进来
        # 端口**刻意不校验**：Web 版可能跑在任意开发端口（vite 5173、serve-web 3000…）。
        # 真正的凭据是配对码；这条白名单只用在还没有令牌的 /api/pair 上。
        self.assertTrue(security.origin_ok("http://127.0.0.1:8791", 8790))
        self.assertTrue(security.origin_ok("http://127.0.0.1:5173", 8790))
        self.assertTrue(security.origin_ok("http://localhost:3000", 8790))
        self.assertTrue(security.origin_ok("https://[::1]:8443", 8790))

    def test_remote_and_missing_rejected(self):
        self.assertFalse(security.origin_ok("https://evil.example.com", 8790))
        self.assertFalse(security.origin_ok("http://192.168.1.10:8790", 8790), "局域网地址不算本机")
        self.assertFalse(security.origin_ok("file:///tmp/x.html", 8790))
        self.assertFalse(security.origin_ok(None, 8790))
        self.assertFalse(security.origin_ok("", 8790))
        self.assertFalse(security.origin_ok("null", 8790))

    def test_extra_origins(self):
        # 非扩展来源必须显式放行（例如自建的 Web 版部署在别的端口/域名上）
        web = "http://192.168.1.9:8322"
        self.assertFalse(security.origin_ok(web, 8790))
        self.assertTrue(security.origin_ok(web, 8790, extra=[web]))
        self.assertFalse(security.origin_ok("https://evil.example.com", 8790, extra=[web]))

    def test_missing_origin_opt_in(self):
        self.assertFalse(security.origin_ok_or_missing(None, 8790))
        self.assertTrue(security.origin_ok_or_missing(None, 8790, allow_no_origin=True))
        self.assertTrue(security.origin_ok_or_missing("http://127.0.0.1:8790", 8790))




class ExtensionOriginTest(unittest.TestCase):
    """扩展来源默认放行（配对码与令牌仍是硬门槛，见 security.py 模块注释）。"""

    def test_extension_origins_allowed(self):
        for origin in ("chrome-extension://abcdefghijklmnop",
                       "moz-extension://1234-5678",
                       "safari-web-extension://ABC"):
            self.assertTrue(security.origin_ok(origin, 8790), origin)
            self.assertTrue(security.is_extension_origin(origin), origin)

    def test_extension_scheme_only_is_not_enough(self):
        # 只有 scheme、没有 ID 的字符串不算扩展来源；非扩展 scheme 一律走白名单
        self.assertFalse(security.is_extension_origin("chrome-extension://"))
        self.assertFalse(security.origin_ok("file:///tmp/x.html", 8790))
        self.assertFalse(security.origin_ok("https://chrome-extension://evil", 8790))

    def test_can_be_turned_off(self):
        self.assertFalse(security.origin_ok("chrome-extension://abc", 8790, allow_extensions=False))

if __name__ == "__main__":
    unittest.main()

    def test_origin_ok_wildcard_extra(self):
        """`--allow-origin *`：extra 里的字面量 `*` 放行任意 Origin。

        助手只监听 127.0.0.1、配对码是唯一凭据，所以外部网页能做的最多是烧配对尝试
        （触发全局锁定）。这是 Web 版部署在别的域名/https 预览站时的省事开关。
        """
        origin = "https://easysub-preview.example.com"
        # 默认必须关：非回环、非扩展、不在白名单 → 拒绝
        self.assertFalse(security.origin_ok(origin, 8790))
        self.assertFalse(security.origin_ok(origin, 8790, extra=()))
        self.assertFalse(security.origin_ok(origin, 8790, extra=["https://other.example"]))
        # 显式 '*' → 放行
        self.assertTrue(security.origin_ok(origin, 8790, extra=["*"]))
        # '*' 不掩盖回环/扩展的既有判定
        self.assertTrue(security.origin_ok("http://127.0.0.1:5173", 8790, extra=["*"]))
        self.assertTrue(security.origin_ok("chrome-extension://abc", 8790, extra=["*"]))
        # 空 Origin 仍然拒绝（缺 Origin 走 allow_no_origin 那条独立开关）
        self.assertFalse(security.origin_ok("", 8790, extra=["*"]))
