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


if __name__ == "__main__":
    unittest.main()
