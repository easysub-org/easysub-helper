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

    def test_default_allows_any_origin(self):
        """**默认全放行**（产品要求，用户反复强调）：部署在任何域名/端口的 Web 版开箱能用，
        不需要用户去找 `--allow-origin` / `--allow-cors-all` 这类开关。
        """
        for origin in ("https://evil.example.com", "http://192.168.1.10:8790",
                       "https://easysub.example.com", "http://127.0.0.1:5173",
                       "null"):
            self.assertTrue(security.origin_ok(origin, 8790), origin)
        # 缺失/空 Origin 仍拒绝：浏览器一定带 Origin，放了它等于给非浏览器客户端开口子
        self.assertFalse(security.origin_ok(None, 8790))
        self.assertFalse(security.origin_ok("", 8790))

    def test_restrict_mode_rejects_unlisted(self):
        """显式收紧（`--restrict-origin`）才退回白名单语义。"""
        self.assertFalse(security.origin_ok("https://evil.example.com", 8790, restrict=True))
        self.assertFalse(security.origin_ok("http://192.168.1.10:8790", 8790, restrict=True))
        self.assertFalse(security.origin_ok("file:///tmp/x.html", 8790, restrict=True))
        self.assertFalse(security.origin_ok("null", 8790, restrict=True))
        self.assertFalse(security.origin_ok(None, 8790, restrict=True))
        self.assertFalse(security.origin_ok("", 8790, restrict=True))
        # 收紧模式下本机页面与扩展照旧放行
        self.assertTrue(security.origin_ok("http://127.0.0.1:5173", 8790, restrict=True))
        self.assertTrue(security.origin_ok("chrome-extension://abc", 8790, restrict=True))

    def test_extra_origins_in_restrict_mode(self):
        # 收紧模式下要额外放行别处部署的 Web 版，用 extra 显式列出
        web = "http://192.168.1.9:8322"
        self.assertFalse(security.origin_ok(web, 8790, restrict=True))
        self.assertTrue(security.origin_ok(web, 8790, extra=[web], restrict=True))
        self.assertFalse(security.origin_ok("https://evil.example.com", 8790, extra=[web], restrict=True))

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
        # （默认全放行时这些都会被放行，所以要在 **收紧模式** 下验证匹配逻辑本身）
        self.assertFalse(security.is_extension_origin("chrome-extension://"))
        self.assertFalse(security.origin_ok("file:///tmp/x.html", 8790, restrict=True))
        self.assertFalse(security.origin_ok("https://chrome-extension://evil", 8790, restrict=True))

    def test_can_be_turned_off(self):
        self.assertFalse(security.origin_ok("chrome-extension://abc", 8790,
                                            allow_extensions=False, restrict=True))


class WildcardOriginTest(unittest.TestCase):
    """`--allow-origin *` / `--allow-cors-all`：extra 里的字面量 `*` 放行任意 Origin。

    坑（独立审查抓到的 major）：这条用例最初被追加在文件末尾，位置落在
    `if __name__ == "__main__": unittest.main()` **之后且缩进在里面** —— 作为模块导入时
    它根本不存在（`loadTestsFromModule` 收不到），于是这个安全开关零覆盖。测试文件末尾的
    这个结构是个陷阱：新增用例一律写在 `if __name__` 之前。
    """

    ORIGIN = "https://easysub-preview.example.com"

    def test_default_is_open(self):
        """默认全放行 —— 用户要求：不许把 CORS 变成需要手动设置的开关。"""
        self.assertTrue(security.origin_ok(self.ORIGIN, 8790))
        self.assertTrue(security.origin_ok(self.ORIGIN, 8790, extra=()))
        self.assertTrue(security.origin_ok(self.ORIGIN, 8790, extra=["https://other.example"]))

    def test_restrict_mode_is_closed(self):
        # 显式收紧后，只有白名单来源放行
        self.assertFalse(security.origin_ok(self.ORIGIN, 8790, restrict=True))
        self.assertFalse(security.origin_ok(self.ORIGIN, 8790, extra=(), restrict=True))
        self.assertFalse(security.origin_ok(self.ORIGIN, 8790,
                                            extra=["https://other.example"], restrict=True))

    def test_wildcard_allows_any_origin(self):
        # 收紧模式下 `"*"` 表示"回到全放行"
        self.assertTrue(security.origin_ok(self.ORIGIN, 8790, extra=["*"], restrict=True))

    def test_wildcard_keeps_existing_judgements(self):
        # '*' 不掩盖回环/扩展的既有判定；也不等于"缺 Origin 也放行"
        self.assertTrue(security.origin_ok("http://127.0.0.1:5173", 8790, extra=["*"], restrict=True))
        self.assertTrue(security.origin_ok("chrome-extension://abc", 8790, extra=["*"], restrict=True))
        self.assertFalse(security.origin_ok("", 8790, extra=["*"], restrict=True))


if __name__ == "__main__":
    unittest.main()
