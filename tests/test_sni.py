# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 hcz1017
"""SNI 托盘（`easysub_helper/sni.py`）的测试。

为什么要有这一份：
  * 用户实测在 UKUI（KDE 系）上 pystray 的 XEmbed 后端"有槽位、没图标、点了没反应"，
    所以 Linux 的托盘改走原生 StatusNotifierItem；
  * 协议层最容易出的错是**签名/参数不匹配**（真机上表现为"右键点击后宿主报错、菜单不弹"），
    所以这里用**真实的 dbus-next 装饰器**把菜单接口跑一遍（没装 dbus-next 就跳过）。
"""

import os
import unittest
from unittest import mock

from easysub_helper import sni

try:                                                    # pragma: no cover - 环境相关
    from PIL import Image                               # noqa: F401

    HAVE_PIL = True
except Exception:                                       # noqa: BLE001
    HAVE_PIL = False

try:                                                    # pragma: no cover - 环境相关
    from dbus_next import PropertyAccess, Variant
    from dbus_next.service import ServiceInterface, dbus_property, method, signal

    HAVE_DBUS = True
except Exception:                                       # noqa: BLE001
    HAVE_DBUS = False


@unittest.skipUnless(HAVE_PIL, "需要 Pillow（在 [tray] extra 里；CI 的测试作业没装）")
class PixmapTest(unittest.TestCase):
    def test_argb32_layout(self):
        """SNI 的 IconPixmap 是 ARGB32 **大端**逐像素；这里用 2×2 图钉住字节顺序。"""
        from PIL import Image

        image = Image.new("RGBA", (2, 2))
        image.putdata([(1, 2, 3, 255), (4, 5, 6, 128), (7, 8, 9, 0), (10, 11, 12, 64)])
        width, height, payload = sni.argb32_pixmap(image, size=2)
        self.assertEqual((width, height), (2, 2))
        self.assertEqual(len(payload), 2 * 2 * 4)
        self.assertEqual(payload[0:4], bytes((255, 1, 2, 3)), "第一个像素应当是 A,R,G,B")
        self.assertEqual(payload[4:8], bytes((128, 4, 5, 6)))

    def test_argb32_resizes_to_requested_size(self):
        from PIL import Image

        image = Image.new("RGBA", (128, 128), (255, 0, 0, 255))
        width, height, payload = sni.argb32_pixmap(image, size=22)
        self.assertEqual((width, height), (22, 22))
        self.assertEqual(len(payload), 22 * 22 * 4)


class AvailabilityTest(unittest.TestCase):
    def test_dbus_available_needs_bus_and_library(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("DBUS_SESSION_BUS_ADDRESS", None)
            self.assertFalse(sni.dbus_available())
        with mock.patch.dict(os.environ, {"DBUS_SESSION_BUS_ADDRESS": "unix:path=/tmp/x"}):
            self.assertEqual(sni.dbus_available(), HAVE_DBUS)


@unittest.skipUnless(HAVE_DBUS, "需要 dbus-next（打包产物与 Linux 上都有；CI 的测试作业没装）")
class MenuProtocolTest(unittest.TestCase):
    """用真实装饰器跑菜单协议 —— 参数/签名不匹配会在这里直接炸（用户实测踩过）。"""

    @staticmethod
    def _call(menu, name, *args):
        """调用被 dbus-next `@method()` 包起来的**原始函数**。

        坑：装饰后直接 `menu.GetLayout(...)` 会返回 None（装饰器负责通过总线回包），
        所以测试必须走 `__wrapped__`，否则拿不到返回值、也测不到参数不匹配。
        """
        return getattr(type(menu), name).__wrapped__(menu, *args)

    def _menu(self, on_event, labels=None):
        labels = labels or {sni.MENU_SHOW: "显示窗口", sni.MENU_TOGGLE: "启动采集",
                            sni.MENU_QUIT: "退出"}
        cls = sni.build_menu(ServiceInterface, dbus_property, method, signal, PropertyAccess,
                             Variant, labels, on_event)
        return cls()

    def test_get_layout_returns_three_items_with_labels(self):
        menu = self._menu(lambda _id: None)
        revision, root = self._call(menu, 'GetLayout', 0, -1, [])
        self.assertIsInstance(revision, int)
        item_id, props, children = root
        self.assertEqual(item_id, 0)
        self.assertEqual(len(children), 3, "应当有 显示/启动暂停/退出 三项")
        labels = []
        for child in children:
            value = child.value if hasattr(child, "value") else child
            labels.append(value[1]["label"].value)
        self.assertIn("退出", labels)

    def test_event_dispatches_the_clicked_item(self):
        seen = []
        menu = self._menu(seen.append)
        self._call(menu, 'Event', sni.MENU_QUIT, "clicked", Variant("s", ""), 0)
        self.assertEqual(seen, [sni.MENU_QUIT])
        self._call(menu, 'Event', sni.MENU_SHOW, "hovered", Variant("s", ""), 0)
        self.assertEqual(seen, [sni.MENU_QUIT], "非 clicked 事件不该触发动作")

    def test_set_labels_refreshes_the_layout(self):
        """切语言/采集状态后，宿主重拉布局必须拿到新文案（否则菜单一直是旧语言）。"""
        menu = self._menu(lambda _id: None, labels={sni.MENU_QUIT: "退出"})
        menu.set_labels({sni.MENU_QUIT: "Quit"})
        _revision, root = self._call(menu, 'GetLayout', 0, -1, [])
        labels = []
        for child in root[2]:
            value = child.value if hasattr(child, "value") else child
            labels.append(value[1]["label"].value)
        self.assertIn("Quit", labels)

    def test_group_and_about_methods(self):
        menu = self._menu(lambda _id: None)
        props = self._call(menu, 'GetGroupProperties', [], [])
        self.assertEqual(len(props), 3)
        self.assertFalse(self._call(menu, 'AboutToShow', 0))
        self.assertEqual(self._call(menu, 'AboutToShowGroup', []), [[], []])


if __name__ == "__main__":
    unittest.main()
