# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 hcz1017
"""i18n 完整性与一致性测试（"全程 i18n"的机械保证）。

1. 两份语言目录的 key 集合必须完全相同；
2. 源码里所有 `t("key")` 引用的 key 都必须存在于**每一份**目录里——这就是"没有硬编码、
   也没有漏翻"的抓手；
3. 语言判定与归一化（zh-CN/zh_CN.UTF-8/en-US…）。
"""

import io
import os
import re
import unittest

from easysub_helper import i18n

#: 源码里 t("key") 的引用
# 坑：必须排除 "set(" / "dict(" 这类以 t( 结尾的调用，否则会把一堆无关字符串当 key
T_CALL_RE = re.compile(r"""(?<![A-Za-z0-9_])t\(\s*["']([a-z][A-Za-z0-9_.]*)["']""")
#: 语言目录之外，还可能出现在这些地方（它们本身不调用 t）
SKIP_FILES = {"i18n.py"}


def _iter_sources():
    root = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "easysub_helper")
    for base, _dirs, files in os.walk(root):
        for name in files:
            if not name.endswith(".py") or name in SKIP_FILES:
                continue
            yield os.path.join(base, name)


class CatalogTest(unittest.TestCase):
    def test_catalogs_have_identical_keys(self):
        zh = set(i18n.catalog("zh_CN"))
        en = set(i18n.catalog("en"))
        self.assertTrue(zh)
        self.assertEqual(zh - en, set(), "en 目录缺这些 key")
        self.assertEqual(en - zh, set(), "zh_CN 目录缺这些 key")

    def test_every_referenced_key_exists(self):
        keys = set()
        for path in _iter_sources():
            with io.open(path, encoding="utf-8") as fh:
                keys.update(T_CALL_RE.findall(fh.read()))
        self.assertTrue(keys, "没扫到任何 t() 调用，正则或目录结构可能变了")
        for lang in i18n.available_languages():
            missing = sorted(k for k in keys if not i18n.has_key(k, lang))
            self.assertEqual(missing, [], "{} 目录缺少被引用的 key".format(lang))

    def test_all_languages_are_translated(self):
        # 缺翻译（值为空）也要拦：空串会渲染成空白 UI
        for lang in i18n.available_languages():
            for key, value in i18n.catalog(lang).items():
                self.assertTrue(str(value).strip(), "{} 的 {} 是空文案".format(lang, key))


class LanguageTest(unittest.TestCase):
    def test_normalize(self):
        for tag, expected in (
            ("zh_CN", "zh_CN"), ("zh-CN", "zh_CN"), ("zh_CN.UTF-8", "zh_CN"), ("zh", "zh_CN"),
            ("zh_TW", "zh_CN"), ("en", "en"), ("en-US", "en"), ("en_GB.utf8", "en"),
            ("fr_FR", None), ("", None), (None, None),
        ):
            self.assertEqual(i18n.normalize_lang(tag), expected, tag)

    def test_detect_precedence(self):
        saved = dict(os.environ)
        try:
            os.environ["EASYSUB_LANG"] = "en"
            os.environ["LANG"] = "zh_CN.UTF-8"
            self.assertEqual(i18n.detect_language(), "en")
            self.assertEqual(i18n.detect_language("zh-CN"), "zh_CN", "--lang 优先于环境变量")
            del os.environ["EASYSUB_LANG"]
            self.assertEqual(i18n.detect_language(), "zh_CN")
        finally:
            os.environ.clear()
            os.environ.update(saved)

    def test_switch_language(self):
        saved = i18n.get_language()
        try:
            i18n.set_language("en")
            self.assertEqual(i18n.t("app.title"), i18n.catalog("en")["app.title"])
            i18n.set_language("zh_CN")
            self.assertEqual(i18n.t("app.title"), i18n.catalog("zh_CN")["app.title"])
        finally:
            i18n.set_language(saved)

    def test_unknown_key_returns_key_without_raising(self):
        self.assertEqual(i18n.t("definitely.not.a.key"), "definitely.not.a.key")

    def test_missing_format_arg_does_not_raise(self):
        # 少传占位参数时宁可回退成原样文案，也不要抛异常把服务打断
        self.assertTrue(i18n.t("run.banner.ui"))

    def test_source_label(self):
        saved = i18n.get_language()
        try:
            i18n.set_language("zh_CN")
            self.assertEqual(i18n.source_label("system"), "系统音频")
            i18n.set_language("en")
            self.assertEqual(i18n.source_label("system"), "system audio")
        finally:
            i18n.set_language(saved)


if __name__ == "__main__":
    unittest.main()
