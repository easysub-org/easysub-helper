# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 hcz1017
"""系统托盘（Linux 原生路线）：直接实现 StatusNotifierItem。

为什么不用 pystray 的 Linux 后端（用户实测 + 官方文档）：
  * pystray 文档自己写着 `xorg` 后端是"其它后端都加载不了时的兜底，除默认动作外不支持菜单"，
    `gtk` 后端"在 gnome-shell 下需要第三方插件才能显示"；
  * 而 KDE / UKUI / GNOME(扩展) / LXQt 用的是 **StatusNotifierItem（SNI，走 D-Bus）**。
    两者不通用 —— 用户实测表现正是"托盘里有个槽位、但没有图标、左右键都没反应"
    （X 查询确认：pystray 建出来的是 1×1 的窗口，从未被嵌进托盘宿主）。

所以这里直接按规范实现：
  1. 在会话总线上导出 `org.kde.StatusNotifierItem`（属性 Category/Id/Title/Status/IconPixmap/Menu…，
     方法 Activate/SecondaryActivate/ContextMenu/Scroll）；
  2. 再导出 `com.canonical.dbusmenu`（右键菜单：显示窗口 / 启动·暂停采集 / 退出）；
  3. 调 `org.kde.StatusNotifierWatcher.RegisterStatusNotifierItem(我们的总线名)` 注册。
进程从总线断开时，宿主会自动移除图标。

依赖：`dbus-next`（纯 Python、零系统依赖、自带 asyncio），见 pyproject 的 `[tray]` extra。
拿不到会话总线 / 没有 watcher 时如实失败（GUI 会把勾选框取消并说明原因，绝不留隐形进程）。
"""

import asyncio
import logging
import os
import threading

from .i18n import t

LOG = logging.getLogger("easysub-helper")

ITEM_PATH = "/StatusNotifierItem"
MENU_PATH = "/MenuBar"
WATCHER_NAME = "org.kde.StatusNotifierWatcher"
WATCHER_PATH = "/StatusNotifierWatcher"
ITEM_IFACE = "org.kde.StatusNotifierItem"
MENU_IFACE = "com.canonical.dbusmenu"

#: 菜单项 id（dbusmenu 用整数 id 表示条目）
MENU_SHOW = 1
MENU_TOGGLE = 2
MENU_QUIT = 3


def dbus_available():
    """能不能走 SNI 路线（装没装 dbus-next + 有没有会话总线）。"""
    if not os.environ.get("DBUS_SESSION_BUS_ADDRESS"):
        return False
    try:
        import dbus_next                               # noqa: F401
    except Exception:                                  # noqa: BLE001
        return False
    return True


def argb32_pixmap(image, size=64):
    """把 PIL 图转成 SNI 的 IconPixmap 格式：`a(iiay)`，像素为 **ARGB32 大端**。

    走 pixmap 而不是 `IconName`（图标名 + 主题路径）：后者要求图标按 XDG 主题目录摆放，
    放错了会**静默不显示**（这是另一个项目的真实坑）。直接给像素最省事。
    """
    resized = image.convert("RGBA").resize((size, size))
    payload = bytearray()
    for red, green, blue, alpha in resized.getdata():
        payload += bytes((alpha, red, green, blue))
    return [size, size, bytes(payload)]


class _SNIItem(object):
    """`org.kde.StatusNotifierItem` 的实现（由 ServiceInterface 动态构造，见 build_item）。"""


def build_item(service_interface, dbus_property, method, PropertyAccess, pixmaps,
               title, on_activate, on_secondary, on_context, on_scroll):
    """构造 SNI 接口类。

    单独抽出来是为了能在没有 D-Bus 的环境里测（传入假的装饰器即可）—— 真机上我们再验一次注册。
    """

    class SNIItem(service_interface):
        def __init__(self):
            super().__init__(ITEM_IFACE)

        # ---- 只读属性（宿主靠它们画图标）----
        @dbus_property(access=PropertyAccess.READ)
        def Category(self) -> "s":
            return "ApplicationStatus"

        @dbus_property(access=PropertyAccess.READ)
        def Id(self) -> "s":
            return "easysub-helper"

        @dbus_property(access=PropertyAccess.READ)
        def Title(self) -> "s":
            return title

        @dbus_property(access=PropertyAccess.READ)
        def Status(self) -> "s":
            return "Active"

        @dbus_property(access=PropertyAccess.READ)
        def IconName(self) -> "s":
            return ""

        @dbus_property(access=PropertyAccess.READ)
        def IconPixmap(self) -> "a(iiay)":
            return pixmaps

        @dbus_property(access=PropertyAccess.READ)
        def Menu(self) -> "o":
            return MENU_PATH

        @dbus_property(access=PropertyAccess.READ)
        def ItemIsMenu(self) -> "b":
            return False

        @dbus_property(access=PropertyAccess.READ)
        def WindowId(self) -> "i":
            return 0

        # ---- 交互方法（宿主在用户点击时调用）----
        @method()
        def Activate(self, x: "i", y: "i"):
            on_activate()

        @method()
        def SecondaryActivate(self, x: "i", y: "i"):
            on_secondary()

        @method()
        def ContextMenu(self, x: "i", y: "i"):
            on_context()

        @method()
        def Scroll(self, delta: "i", orientation: "s"):
            on_scroll()

    return SNIItem


def build_menu(service_interface, dbus_property, method, signal, PropertyAccess, Variant,
               labels, on_event):
    """构造 `com.canonical.dbusmenu` 接口类（右键菜单）。

    labels 形如 {MENU_SHOW: "显示窗口", MENU_TOGGLE: "启动采集", MENU_QUIT: "退出"}，
    on_event(item_id) 由 GUI 侧执行（这里只负责协议）。
    """

    _current = {"instance": None}

    def variant_string(text):
        return Variant("s", text)

    def item_props(item_id, source=None):
        """菜单项属性。`source` 是**当前**标签表（支持 set_labels 后刷新；默认初始表）。"""
        table = labels if source is None else source
        return {
            "label": variant_string(table.get(item_id, "")),
            "enabled": Variant("b", True),
            "visible": Variant("b", True),
        }

    def item_layout_item(item_id):
        # (id, properties, children) —— 用**当前**标签（支持 set_labels 后刷新）
        menu = _current["instance"]
        table = menu._labels if menu is not None else labels
        return [item_id, item_props(item_id, table), []]

    class DBusMenu(service_interface):
        def __init__(self):
            super().__init__(MENU_IFACE)
            self._revision = 1
            self._labels = dict(labels)
            _current["instance"] = self

        def set_labels(self, new_labels):
            """刷新菜单文案（切语言 / 采集状态改变时）。"""
            self._labels = dict(new_labels)
            self._revision += 1

        @dbus_property(access=PropertyAccess.READ)
        def Version(self) -> "u":
            return 3

        @dbus_property(access=PropertyAccess.READ)
        def Status(self) -> "s":
            return "normal"

        @dbus_property(access=PropertyAccess.READ)
        def TextDirection(self) -> "s":
            return "ltr"

        @dbus_property(access=PropertyAccess.READ)
        def IconThemePath(self) -> "as":
            return []

        @method()
        def GetLayout(self, parent_id: "i", recursion_depth: "i",
                      property_names: "as") -> "u(ia{sv}av)":
            children = [Variant("(ia{sv}av)", item_layout_item(item)) for item in
                        (MENU_SHOW, MENU_TOGGLE, MENU_QUIT)]
            root = [0, {"children-display": Variant("s", "submenu")}, children]
            return [self._revision, root]

        @method()
        def GetGroupProperties(self, ids: "ai", property_names: "as") -> "a(ia{sv})":
            wanted = list(ids) or [MENU_SHOW, MENU_TOGGLE, MENU_QUIT]
            return [[item, item_props(item, self._labels)] for item in wanted]

        @method()
        def Event(self, item_id: "i", event_id: "s", data: "v", timestamp: "u"):
            if event_id == "clicked":
                on_event(int(item_id))

        @method()
        def AboutToShow(self, item_id: "i") -> "b":
            return False

        @method()
        def AboutToShowGroup(self, ids: "ai") -> "aiai":   # 注意：dbus 签名里不能有空格
            return [[], []]

        @signal()
        def LayoutUpdated(self, revision: "u", parent: "i"):
            return [revision, parent]

    return DBusMenu


class SNITray(object):
    """SNI 托盘的启动/停止（跑在自己的线程里，不碰 tkinter）。

    与 pystray 版 Tray 的接口保持一致：start/ready/failed/stop/refresh/post。
    """

    def __init__(self, icon_path, on_show, on_toggle, on_quit, is_capturing=None, actions=None):
        self._icon_path = icon_path
        self._on_show = on_show
        self._on_toggle = on_toggle
        self._on_quit = on_quit
        self._is_capturing = is_capturing or (lambda: False)
        self._actions = actions
        self._ready = False
        self._failed = None
        self._thread = None
        self._loop = None
        self._bus = None
        self._lock = threading.Lock()

    # ---------- 与 pystray 版一致的小接口 ----------
    def ready(self):
        with self._lock:
            return self._ready

    def failed(self):
        with self._lock:
            return self._failed

    def post(self, func):
        if self._actions is not None:
            self._actions.put(func)

    def _label(self, key, ascii_text):
        from .tray import latin1_safe            # SNI 走 D-Bus（UTF-8 没问题），这里只是复用降级逻辑

        return latin1_safe(t(key), ascii_text)

    def _labels(self):
        from .tray import ASCII_LABELS

        return {
            MENU_SHOW: self._label("gui.trayShow", ASCII_LABELS["gui.trayShow"]),
            MENU_TOGGLE: self._label("gui.trayPause" if self._is_capturing() else "gui.trayStart",
                                     ASCII_LABELS["gui.trayStart"]),
            MENU_QUIT: self._label("gui.trayQuit", ASCII_LABELS["gui.trayQuit"]),
        }

    # ---------- 生命周期 ----------
    def _dispatch(self, func):
        """D-Bus 线程 → 队列 → 主线程执行（绝不在 D-Bus 线程里碰 tkinter）。"""
        self.post(func)

    def start(self):
        if not dbus_available():
            return False
        self._thread = threading.Thread(target=self._run, name="easysub-helper-sni", daemon=True)
        self._thread.start()
        return True

    def _run(self):
        loop = asyncio.new_event_loop()
        self._loop = loop
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(self._setup())
            loop.run_forever()
        except Exception as exc:                       # noqa: BLE001 - 如实报告给 GUI
            with self._lock:
                self._failed = str(exc)
            LOG.warning("SNI tray failed: %s", exc)
        finally:
            try:
                loop.close()
            except Exception:                          # noqa: BLE001
                pass

    async def _setup(self):
        from dbus_next import PropertyAccess, Variant
        from dbus_next.aio import MessageBus
        from dbus_next.service import ServiceInterface, dbus_property, method, signal

        from PIL import Image

        image = Image.open(self._icon_path)
        pixmaps = [argb32_pixmap(image, 64)]
        title = self._label_title()

        item_cls = build_item(ServiceInterface, dbus_property, method, PropertyAccess, pixmaps,
                              title,
                              on_activate=lambda: self._dispatch(self._on_show),
                              on_secondary=lambda: self._dispatch(self._on_toggle),
                              on_context=lambda: None,
                              on_scroll=lambda: None)
        menu_cls = build_menu(ServiceInterface, dbus_property, method, signal, PropertyAccess,
                              Variant, self._labels(), self._on_menu_event)

        bus = await MessageBus().connect()
        self._bus = bus
        self._menu = menu_cls()
        bus.export(ITEM_PATH, item_cls())
        bus.export(MENU_PATH, self._menu)
        # 规范推荐的服务名（宿主看着这个名字找我们）
        await bus.request_name("org.kde.StatusNotifierItem-{}-1".format(os.getpid()))

        introspection = await bus.introspect(WATCHER_NAME, WATCHER_PATH)
        proxy = bus.get_proxy_object(WATCHER_NAME, WATCHER_PATH, introspection)
        watcher = proxy.get_interface(WATCHER_NAME)
        await watcher.call_register_status_notifier_item(bus.unique_name)
        with self._lock:
            self._ready = True
        LOG.info("SNI tray registered (%s)", bus.unique_name)

    def _label_title(self):
        from .tray import ASCII_TITLE, latin1_safe

        return latin1_safe(t("gui.title"), ASCII_TITLE)

    def _on_menu_event(self, item_id):
        if item_id == MENU_SHOW:
            self._dispatch(self._on_show)
        elif item_id == MENU_TOGGLE:
            self._dispatch(self._on_toggle)
        elif item_id == MENU_QUIT:
            self._dispatch(self._on_quit)

    def refresh(self):
        """菜单文案随状态/语言变化：换标签并通知宿主重拉布局。"""
        menu, bus = getattr(self, "_menu", None), self._bus
        if menu is None or bus is None:
            return
        try:
            menu.set_labels(self._labels())
            menu.LayoutUpdated(menu._revision, 0)
        except Exception as exc:                       # noqa: BLE001 - 刷新失败不该影响主功能
            LOG.debug("menu refresh failed: %s", exc)

    def stop(self):
        with self._lock:
            self._ready = False
        loop, bus = self._loop, self._bus
        if loop is not None and bus is not None:
            try:
                loop.call_soon_threadsafe(bus.disconnect)
            except Exception:                          # noqa: BLE001
                pass
        if loop is not None:
            try:
                loop.call_soon_threadsafe(loop.stop)
            except Exception:                          # noqa: BLE001
                pass
        self._bus = None
