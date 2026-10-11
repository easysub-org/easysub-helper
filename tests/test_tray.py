# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 hcz1017
"""托盘的就绪/可见语义。

为什么要专门测这两条（独立审查抓到的 blocker）：pystray 规定"若自定义 setup 函数，
**必须自己**把 `icon.visible` 置 True"（默认 setup 才会自动置）。少了那一行，图标**永远不出现**，
而 `ready()` 已经为 True → 关窗就把窗口收走 → 用户得到一个"没有窗口、没有图标、服务还在跑"
的隐形进程。CI 的测试作业不装 pystray，所以这里直接驱动 `_on_ready`，不依赖真实后端。
"""

import os
import sys
import tempfile
import unittest
from unittest import mock

from easysub_helper import tray
from easysub_helper.i18n import t


class FakeIcon(object):
    def __init__(self, boom=False):
        self._boom = boom
        self._visible = False

    def _set_visible(self, value):
        if self._boom:
            raise RuntimeError("no indicator host")
        self._visible = value

    def _get_visible(self):
        return getattr(self, "_visible", False)

    visible = property(_get_visible, _set_visible)


def make_tray():
    return tray.Tray(icon_path="unused.png", on_show=lambda: None,
                     on_toggle=lambda: None, on_quit=lambda: None)


class TrayReadinessTest(unittest.TestCase):
    def test_on_ready_makes_the_icon_visible(self):
        t = make_tray()
        icon = FakeIcon()
        t._on_ready(icon)
        self.assertTrue(icon.visible, "自定义 setup 必须自己置 visible，否则图标永不出现")
        t._icon = icon
        self.assertTrue(t.ready())

    def test_ready_follows_visible_not_just_setup(self):
        """托盘运行中途死掉（桌面/explorer 重启）→ visible 变 False → 不许再"关窗只收窗口"。"""
        t = make_tray()
        icon = FakeIcon()
        t._on_ready(icon)
        t._icon = icon
        self.assertTrue(t.ready())
        icon._visible = False
        self.assertFalse(t.ready(), "图标没了还收窗口 = 隐形进程")

    def test_ready_is_false_before_setup(self):
        t = make_tray()
        self.assertFalse(t.ready())

    def test_failure_to_show_is_reported(self):
        t = make_tray()
        icon = FakeIcon(boom=True)
        t._on_ready(icon)
        self.assertFalse(t.ready())
        self.assertTrue(t.failed(), "置 visible 失败要留下原因，供窗口显示（复审 m2）")

    def test_stop_resets_readiness(self):
        t = make_tray()
        icon = FakeIcon()
        t._on_ready(icon)
        t._icon = icon
        t.stop()
        self.assertFalse(t.ready())

    def test_post_enqueues_for_the_main_thread(self):
        """菜单回调跑在 pystray 线程：只许往队列里投函数，绝不直接碰 tkinter。"""
        import queue

        actions = queue.Queue()
        t = tray.Tray(icon_path="unused.png", on_show=lambda: None, on_toggle=lambda: None,
                      on_quit=lambda: None, actions=actions)
        calls = []
        t.post(lambda: calls.append(1))
        self.assertEqual(calls, [], "post 不该在 pystray 线程里直接执行")
        actions.get_nowait()()
        self.assertEqual(calls, [1])

    def test_macos_is_reported_unavailable(self):
        with mock.patch.object(tray.sys, "platform", "darwin"):
            self.assertFalse(tray.available())
            self.assertIn("macOS", tray.unavailable_reason())

    def test_install_hint_used_in_the_reason(self):
        with mock.patch.object(tray.sys, "platform", "linux"):
            with mock.patch.object(tray, "available", return_value=False):
                self.assertIn(tray.install_hint(), tray.unavailable_reason())


class FakeMenuItem(object):
    def __init__(self, text, action, default=False):
        self.text = text
        self.action = action
        self.default = default


class FakeMenu(object):
    def __init__(self, *items):
        self.items = items


class FakePystrayIcon(object):
    def __init__(self, name, image, title, menu):
        # pystray 的 X11 后端会在这里（或渲染时）用 latin-1 编码标题；中文直接抛
        title.encode("latin-1")
        self.name = name
        self.title = title
        self.menu = menu
        self.visible = False

    def run(self, setup=None):
        if setup is not None:
            setup(self)

    def stop(self):
        self.visible = False

    def update_menu(self):
        pass


class FakeImage(object):
    @staticmethod
    def open(path):
        return object()


class FakePystray(object):
    MenuItem = FakeMenuItem
    Menu = FakeMenu
    Icon = FakePystrayIcon


def _fake_modules():
    import types
    pystray_mod = types.ModuleType("pystray")
    for attr in ("MenuItem", "Menu", "Icon"):
        setattr(pystray_mod, attr, getattr(FakePystray, attr))
    pil_mod = types.ModuleType("PIL")
    image_mod = types.ModuleType("PIL.Image")
    image_mod.open = FakeImage.open
    return {"pystray": pystray_mod, "PIL": pil_mod, "PIL.Image": image_mod}


class TrayModuleSmokeTest(unittest.TestCase):
    """把 tray 模块的公开函数都真的调一遍。

    坑（用户实测）：`compatibility_note()` 里忘了 `import os` —— 只在"托盘就绪后"那条路径上
    被调用，于是启动看起来一切正常，随后 Tk 回调里抛 `NameError`。纯读码/只测别的函数都发现不了，
    所以这里逐个调用。
    """

    def test_public_helpers_are_callable(self):
        self.assertIsInstance(tray.install_hint(), str)
        self.assertTrue(tray.install_hint())
        self.assertIsInstance(tray.available(), bool)
        # 后端名可能为 None（没装 pystray），但调用不能抛
        backend = tray.backend_name()
        self.assertTrue(backend is None or isinstance(backend, str))

    def test_compatibility_note_variants(self):
        with mock.patch.dict(os.environ, {"XDG_CURRENT_DESKTOP": "UKUI"}, clear=False):
            tray.compatibility_note()            # 不能抛
        with mock.patch.dict(os.environ, {"XDG_CURRENT_DESKTOP": "XFCE"}, clear=False):
            tray.compatibility_note()
        with mock.patch.object(tray.sys, "platform", "win32"):
            self.assertIsNone(tray.compatibility_note())

    def test_compatibility_note_flags_xorg_on_sni_desktops(self):
        with mock.patch.object(tray, "backend_name", return_value="pystray._xorg"):
            with mock.patch.dict(os.environ, {"XDG_CURRENT_DESKTOP": "UKUI"}, clear=False):
                note = tray.compatibility_note()
                self.assertIsNotNone(note)
                self.assertIn("UKUI", note)
            with mock.patch.dict(os.environ, {"XDG_CURRENT_DESKTOP": "XFCE"}, clear=False):
                self.assertIsNone(tray.compatibility_note(), "XFCE 用 XEmbed，不需要提示")

    def test_unavailable_reason_variants(self):
        with mock.patch.object(tray.sys, "platform", "darwin"):
            self.assertIn("macOS", tray.unavailable_reason())
        with mock.patch.object(tray.sys, "platform", "linux"):
            with mock.patch.object(tray, "available", return_value=False):
                self.assertIn(tray.install_hint(), tray.unavailable_reason())


class TrayEncodingTest(unittest.TestCase):
    """用户实测的 bug：中文标题在 X11 后端抛 latin-1 编码错误 → 托盘根本起不来。

    现在：本地化文案过不了编码就退回 ASCII（能起来比好看重要），并且说清楚原因。
    """

    def test_latin1_safe_helper(self):

        self.assertEqual(tray.latin1_safe("OK", "fallback"), "OK")
        self.assertEqual(tray.latin1_safe(t("gui.title"), tray.ASCII_TITLE), tray.ASCII_TITLE)
        self.assertTrue(tray.latin1_safe(t("gui.title"), tray.ASCII_TITLE).isascii())

    def test_start_passes_a_latin1_encodable_title(self):
        import sys as _sys

        with mock.patch.dict(_sys.modules, _fake_modules()):
            with mock.patch.object(tray, "available", return_value=True):
                t = make_tray()
                try:
                    self.assertTrue(t.start(), "托盘应当能起来（标题已降级为 ASCII）")
                    self.assertEqual(t._icon.title, tray.ASCII_TITLE)
                    # 菜单标签也必须能在 latin-1 后端渲染
                    for item in t._icon.menu.items:
                        self.assertIsInstance(item.text(None), str)
                        self.assertTrue(item.text(None).isascii(), item.text(None))
                finally:
                    t.stop()

    def test_localized_labels_are_kept_when_encodable(self):
        """能编码就保留本地化文案（不要为了兜底把所有界面都变英文）。"""
        tray_obj = make_tray()
        with mock.patch.object(tray, "latin1_safe", side_effect=lambda text, fallback: text):
            self.assertEqual(tray_obj._label("gui.trayShow"), t("gui.trayShow"))

    def test_ascii_fallback_is_used_on_a_latin1_backend(self):
        tray_obj = make_tray()
        self.assertEqual(tray_obj._label("gui.trayQuit"), tray.ASCII_LABELS["gui.trayQuit"])
        self.assertEqual(tray_obj._label("gui.trayShow"), tray.ASCII_LABELS["gui.trayShow"])


if __name__ == "__main__":
    unittest.main()
