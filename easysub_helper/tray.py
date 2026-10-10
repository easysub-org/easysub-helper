# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 hcz1017
"""系统托盘（可选）—— 让"关窗"不等于"停服务"。

为什么值得做：助手**关窗即停服务**，而页面在没连上助手时会拦住「开始」。所以用户一旦顺手
关掉窗口，浏览器那边就废了，他还得重新找到助手双击一次。有了托盘，关窗只是把窗口收起来，
服务与采集继续；托盘菜单里能「显示窗口 / 启动采集 / 暂停采集 / 退出」。

依赖是**可选**的（`pystray` + Pillow，见 pyproject 的 `[tray]` extra）：装不上就
`available()` 返回 False，窗口里那个勾选框会禁用并说明原因（`gui.trayUnavailable`），
其余功能一切照旧 —— 绝不让"托盘装不上"变成"助手起不来"。
"""

import logging
import threading

from .i18n import t

LOG = logging.getLogger("easysub-helper")

_INSTALL_HINT = "pystray"


def available():
    """托盘能不能用（pystray + Pillow + 平台后端都就绪才算）。"""
    try:
        import pystray                              # noqa: F401
        from PIL import Image                       # noqa: F401
    except Exception:                               # noqa: BLE001
        return False
    return True


def install_hint():
    """给用户看的"怎么让托盘可用"。"""
    return _INSTALL_HINT


class Tray(object):
    """托盘图标 + 菜单。所有动作都通过回调交回 GUI 线程执行。

    坑（tkinter 铁律）：pystray 的菜单回调跑在**它自己的线程**里，绝不能在回调里直接碰
    tkinter 控件 —— 所以这里的每个动作都只调用 `on_*` 回调，由 GUI 侧用 `root.after`
    切回主线程（见 gui.py 的 `_tray_action`）。
    """

    def __init__(self, icon_path, on_show, on_toggle, on_quit, is_capturing=None):
        self._icon = None
        self._thread = None
        self._on_show = on_show
        self._on_toggle = on_toggle
        self._on_quit = on_quit
        self._is_capturing = is_capturing or (lambda: False)
        self._icon_path = icon_path

    def start(self):
        if not available() or self._icon is not None:
            return False
        try:
            import pystray
            from PIL import Image

            image = Image.open(self._icon_path)
            menu = pystray.Menu(
                pystray.MenuItem(lambda _i: t("gui.trayShow"),
                                 lambda *_: self._on_show(), default=True),
                pystray.MenuItem(lambda _i: t("gui.trayPause") if self._is_capturing()
                                 else t("gui.trayStart"),
                                 lambda *_: self._on_toggle()),
                pystray.MenuItem(lambda _i: t("gui.trayQuit"), lambda *_: self._on_quit()),
            )
            self._icon = pystray.Icon("easysub-helper", image, t("gui.title"), menu)
        except Exception as exc:                    # noqa: BLE001 - 托盘失败不该影响主功能
            LOG.warning("tray unavailable: %s", exc)
            self._icon = None
            return False
        self._thread = threading.Thread(target=self._icon.run, name="easysub-helper-tray")
        self._thread.daemon = True
        self._thread.start()
        return True

    def refresh(self):
        """让菜单文案跟着状态/语言刷新。"""
        if self._icon is not None:
            try:
                self._icon.update_menu()
            except Exception:                       # noqa: BLE001
                pass

    def stop(self):
        icon, self._icon = self._icon, None
        if icon is not None:
            try:
                icon.stop()
            except Exception:                       # noqa: BLE001
                pass
