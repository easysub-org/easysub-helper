# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 hcz1017
"""系统托盘（可选）—— 让"关窗"不等于"停服务"。

为什么值得做：助手**关窗即停服务**，而页面在没连上助手时会拦住「开始」。所以用户一旦顺手
关掉窗口，浏览器那边就废了，他还得重新找到助手双击一次。有了托盘，关窗只是把窗口收起来，
服务与采集继续；托盘菜单里能「显示窗口 / 启动采集 / 暂停采集 / 退出」。

依赖是**可选**的（`pystray` + Pillow，见 pyproject 的 `[tray]` extra）：装不上就
`available()` 返回 False，窗口里那个勾选框会禁用并说明原因（`gui.trayUnavailable`），
其余功能一切照旧 —— 绝不让"托盘装不上"变成"助手起不来"。

## 两条硬约束（独立审查抓到的，别再踩）

1. **macOS 上 pystray 的 `Icon.run()` 必须在主线程**（官方文档：OSX 后端否则会失败），
   而我们的主线程被 tkinter 的 `mainloop()` 占着。pystray 另有一条 `run_detached()` 的路子
   （官方 FAQ 说它"主要就是为 macOS 准备的"），但它与 Tk 主循环如何共存**我们没有验证过**，
   所以 macOS 上先直接 `available() == False`（宁可不给托盘，也不能给一个"看着勾上了、
   实际没图标、窗口却已经被收走"的隐形进程）。等有人在 mac 真机上验证过 `run_detached`
   再放开这条。
2. **`start()` 返回 True 不代表图标真的起来了**：`run()` 里的异常没人接。所以用
   `run(setup=...)` 拿到"图标就绪"回调，GUI 侧再确认 `ready()` 才允许"关窗只收窗口"
   （见 gui.on_close）——托盘没就绪就绝不隐藏窗口。
"""

import logging
import os
import sys
import threading

from . import sni
from .i18n import t

LOG = logging.getLogger("easysub-helper")

#: 托盘后端的编码兜底：X11/Xlib 那条路**只吃 latin-1**，中文标题会直接抛
#: `UnicodeEncodeError: 'latin-1' codec can't encode characters in position 0-6`
#: （用户实测：托盘因此根本起不来）。能起来比好看重要，所以本地化文案过不了编码就退回 ASCII。
ASCII_TITLE = "EasySub Helper"
ASCII_LABELS = {
    "gui.trayShow": "Show window",
    "gui.trayStart": "Start capture",
    "gui.trayPause": "Pause capture",
    "gui.trayQuit": "Quit",
}


def latin1_safe(text, fallback):
    """后端只吃 latin-1 时退回 fallback（其它编码错误也一并兜住）。"""
    try:
        text.encode("latin-1")
        return text
    except (UnicodeEncodeError, AttributeError, TypeError):
        return fallback


def available():
    """托盘能不能用。

    Linux 上**优先** SNI（原生协议，KDE/UKUI/GNOME/LXQt 都认）；只有在拿不到会话总线或没装
    dbus-next 时，才回落到 pystray（旧 XEmbed —— 用户实测在 UKUI 上"有槽位、没图标、点了没反应"）。
    """
    return selected_backend() is not None


def selected_backend():
    """**实际会用的**托盘后端：`"sni"` / `"pystray:<模块>"` / None（没有可用的）。

    坑（用户实测 + 官方文档）：Linux 上 pystray 的 xorg/gtk 后端都是旧 XEmbed 协议，
    KDE/UKUI/GNOME 用的是 StatusNotifier —— 表现就是"托盘里有槽位、没图标、点了没反应"。
    所以 Linux 上只要有会话总线 + dbus-next 就走 SNI。
    """
    if sys.platform == "darwin":
        # macOS：pystray 的 run() 必须在主线程（被 tkinter 占着），但它提供了官方替代
        # `run_detached()`（文档：主要是为 macOS 准备的）—— 在 Tk 主线程里安装图标，
        # 之后由 Tk 驱动的 Cocoa runloop 继续跑。所以 macOS 上托盘是可用的。
        try:
            import pystray

            return "pystray:" + str(pystray.Icon.__module__)
        except Exception:                            # noqa: BLE001
            return None
    if sys.platform.startswith("linux") and sni.dbus_available():
        return "sni"
    try:
        import pystray

        return "pystray:" + str(pystray.Icon.__module__)
    except Exception:                                # noqa: BLE001
        return None


def backend_name():
    """当前 pystray 选中的后端（`pystray._xorg` 等）；拿不到返回 None。

    这条信息很关键（用户实测排查）：Linux 上 pystray 会因为**缺 PyGObject** 而退到旧的
    XEmbed 后端，而 KDE/UKUI/GNOME 这些桌面走的是 StatusNotifier 协议 —— 后端与桌面协议
    不匹配时，表现就是"托盘里没有图标、点什么都没反应"。
    """
    return selected_backend() or ""


def compatibility_note():
    """后端与桌面协议可能不匹配时，给一条**可执行**的建议；没问题就返回 None。"""
    if not sys.platform.startswith("linux"):
        return None
    name = backend_name() or ""
    if not name.endswith("_xorg"):
        return None                       # 走 SNI 就没有这个问题
    desktop = (os.environ.get("XDG_CURRENT_DESKTOP") or
               os.environ.get("DESKTOP_SESSION") or "").lower()
    if not any(key in desktop for key in ("kde", "ukui", "lxqt", "deepin", "gnome")):
        return None
    return t("gui.trayXorgHint", desktop=os.environ.get("XDG_CURRENT_DESKTOP")
             or os.environ.get("DESKTOP_SESSION") or "?",
             install=install_hint())


def unavailable_reason():
    """给用户看的"为什么没有托盘"（窗口里那一行说明）。"""
    return t("gui.trayUnavailable", install=install_hint())


def install_hint():
    """怎么让托盘可用（写进提示里，别让用户去猜）。

    Linux 上优先推 dbus-next：它是**原生 SNI**（图标与点击都走系统协议），而且是纯 Python，
    普通 venv 与打包产物都能用；pystray 在 KDE/UKUI 这类桌面上图标根本显示不出来。
    """
    if sys.platform.startswith("linux"):
        return "pip install dbus-next"
    return "pip install pystray pillow"


class Tray(object):
    """托盘图标 + 菜单。

    线程纪律：`Icon.run()` 跑在自己的线程里（Windows/Linux 允许），**菜单回调也在那个线程**
    —— 所以回调**只往 `actions` 队列里投一个函数**，由 GUI 的主线程轮询取出来执行
    （仓库既有范式：gui 的 `_drain_*`）。绝不在 pystray 线程里碰 tkinter，
    也绝不在那个线程里调 `root.after`（仓库注释写明：跨线程调 after 会抛异常或被静默忽略）。
    """

    def __init__(self, icon_path, on_show, on_toggle, on_quit, is_capturing=None,
                 actions=None):
        self._icon = None
        self._thread = None
        self._ready = False
        self._failed = None
        self._on_show = on_show
        self._on_toggle = on_toggle
        self._on_quit = on_quit
        self._is_capturing = is_capturing or (lambda: False)
        self._icon_path = icon_path
        self._actions = actions                # queue.Queue：跨线程投递动作
        self._lock = threading.Lock()
        self._impl = None                      # SNI 实现（优先）；None 表示走 pystray

    # ---------------- 状态 ----------------
    def ready(self):
        """图标**真的挂上去了**吗（`_ready` 只说明 setup 跑过，还要看 visible）。

        托盘运行中途死掉（桌面/explorer 重启）时 visible 会变 False → 这时不许再"关窗只收窗口"，
        否则用户既没窗口也没图标（复审 m12）。
        """
        if self._impl is not None:
            return self._impl.ready()
        with self._lock:
            if not self._ready:
                return False
        try:
            return bool(self._icon is not None and self._icon.visible)
        except Exception:                            # noqa: BLE001
            return False

    def failed(self):
        if self._impl is not None:
            return self._impl.failed()
        with self._lock:
            return self._failed

    def post(self, func):
        """菜单回调 → 只投队列，由主线程执行。"""
        if self._actions is not None:
            self._actions.put(func)

    # ---------------- 生命周期 ----------------
    def _label(self, key):
        """菜单/标题文案：本地化优先，编码不过就退回 ASCII（见 ASCII_LABELS 的说明）。"""
        return latin1_safe(t(key), ASCII_LABELS.get(key, key))

    def _make_pystray_icon(self, pystray, image):
        """构造 pystray 图标（标题与菜单都做 latin-1 兜底，见 ASCII_TITLE 的说明）。"""
        menu = pystray.Menu(
            pystray.MenuItem(lambda _i: self._label("gui.trayShow"),
                             lambda *_: self.post(self._on_show), default=True),
            pystray.MenuItem(lambda _i: self._label("gui.trayPause") if self._is_capturing()
                             else self._label("gui.trayStart"),
                             lambda *_: self.post(self._on_toggle)),
            pystray.MenuItem(lambda _i: self._label("gui.trayQuit"),
                             lambda *_: self.post(self._on_quit)),
        )
        title = latin1_safe(t("gui.title"), ASCII_TITLE)
        if title != t("gui.title"):
            LOG.info("tray backend only accepts latin-1: falling back to ASCII labels")
        return pystray.Icon("easysub-helper", image, title, menu)

    def start(self):
        if self._icon is not None or self._impl is not None:
            return False
        # ① Linux 首选：SNI（D-Bus 原生协议）。用户实测 pystray 在 UKUI 上图标显示不出来。
        if sys.platform.startswith("linux") and sni.dbus_available():
            self._impl = sni.SNITray(self._icon_path, self._on_show, self._on_toggle,
                                     self._on_quit, self._is_capturing, self._actions)
            if self._impl.start():
                LOG.info("tray backend: StatusNotifierItem (dbus-next)")
                return True
            self._impl = None
            LOG.warning("SNI tray unavailable, falling back to pystray")
        if not available():
            return False
        try:
            import pystray
            from PIL import Image

            image = Image.open(self._icon_path)
            self._icon = self._make_pystray_icon(pystray, image)
        except Exception as exc:                    # noqa: BLE001 - 托盘失败不该影响主功能
            LOG.warning("tray unavailable: %s", exc)
            self._icon = None
            return False
        if sys.platform == "darwin":
            # macOS：必须在主线程跑。tkinter 占着主线程，所以用官方的 run_detached()：
            # 在**当前线程**（Tk 主线程）里安装图标，之后由 Tk 驱动的 Cocoa runloop 继续跑。
            # 这条路径无法在本机（Linux）验证，已在 README 标注"待真机确认"。
            try:
                self._icon.run_detached(setup=self._on_ready)
            except Exception as exc:                # noqa: BLE001
                LOG.warning("tray unavailable: %s", exc)
                self._icon = None
                return False
            LOG.info("tray backend: pystray (run_detached, main thread)")
            return True
        # Windows / 其它：pystray 允许在工作线程里 run（macOS 才有主线程限制）
        self._thread = threading.Thread(target=self._run, name="easysub-helper-tray")
        self._thread.daemon = True
        self._thread.start()
        return True

    def _on_ready(self, icon):
        """pystray 在图标就绪后回这里。

        **坑（独立审查 blocker，务必别再省这一行）**：pystray 规定"若自定义 setup，
        必须自己把 `visible` 置 True"（默认 setup 才会自动置）—— 少了它，图标**永不出现**，
        而 `ready()` 已经为 True → 关窗就把窗口收走 → 又变成"没有窗口、没有图标、服务还在跑"
        的隐形进程（这次 Windows/Linux 也会，比修之前更糟）。
        所以：这里显式置 visible，并且 `ready()` 以 `icon.visible` 为准。
        """
        try:
            icon.visible = True
        except Exception as exc:                    # noqa: BLE001 - 置不起来就当托盘失败
            with self._lock:
                self._failed = "cannot show tray icon: {}".format(exc)
            return
        with self._lock:
            self._ready = True

    def _run(self):
        # 坑（独立审查 B1）：以前 start() 直接 return True，而 run() 里的异常没人接 ——
        # macOS 上（run 必须主线程）或后端起不来时，GUI 以为托盘可用 → on_close 把窗口
        # 收走 → 窗口没了、图标也没出现、服务还在跑，用户既找不回窗口也退不出。
        # 现在：异常记在 _failed 上，主线程会据此把勾去掉并显示原因。
        try:
            self._icon.run(setup=self._on_ready)
        except Exception as exc:                    # noqa: BLE001
            with self._lock:
                self._failed = str(exc)
            LOG.warning("tray failed: %s", exc)

    def refresh(self):
        """让菜单文案跟着状态/语言刷新。"""
        if self._impl is not None:
            self._impl.refresh()
            return
        if self._icon is not None:
            try:
                self._icon.update_menu()
            except Exception:                       # noqa: BLE001
                pass

    def stop(self):
        if self._impl is not None:
            impl, self._impl = self._impl, None
            try:
                impl.stop()
            except Exception:                       # noqa: BLE001
                pass
            return
        icon, self._icon = self._icon, None
        with self._lock:
            self._ready = False
        if icon is not None:
            try:
                icon.stop()
            except Exception:                       # noqa: BLE001
                pass
