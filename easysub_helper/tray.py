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
import sys
import threading

from .i18n import t

LOG = logging.getLogger("easysub-helper")


def available():
    """托盘能不能用（pystray + Pillow + **本平台允许**才算）。"""
    if sys.platform == "darwin":
        # 见模块开头第 1 条：macOS 要求 run() 在主线程，而我们主线程是 tkinter
        return False
    try:
        import pystray                              # noqa: F401
        from PIL import Image                       # noqa: F401
    except Exception:                               # noqa: BLE001
        return False
    return True


def unavailable_reason():
    """给用户看的"为什么没有托盘"（窗口里那一行说明）。"""
    if sys.platform == "darwin":
        return t("gui.trayMacUnsupported")
    return t("gui.trayUnavailable", install=install_hint())


def install_hint():
    """怎么让托盘可用（写进提示里，别让用户去猜）。"""
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

    # ---------------- 状态 ----------------
    def ready(self):
        """图标**真的挂上去了**吗（`_ready` 只说明 setup 跑过，还要看 visible）。

        托盘运行中途死掉（桌面/explorer 重启）时 visible 会变 False → 这时不许再"关窗只收窗口"，
        否则用户既没窗口也没图标（复审 m12）。
        """
        with self._lock:
            if not self._ready:
                return False
        try:
            return bool(self._icon is not None and self._icon.visible)
        except Exception:                            # noqa: BLE001
            return False

    def failed(self):
        with self._lock:
            return self._failed

    def post(self, func):
        """菜单回调 → 只投队列，由主线程执行。"""
        if self._actions is not None:
            self._actions.put(func)

    # ---------------- 生命周期 ----------------
    def start(self):
        if not available() or self._icon is not None:
            return False
        try:
            import pystray
            from PIL import Image

            image = Image.open(self._icon_path)
            menu = pystray.Menu(
                pystray.MenuItem(lambda _i: t("gui.trayShow"),
                                 lambda *_: self.post(self._on_show), default=True),
                pystray.MenuItem(lambda _i: t("gui.trayPause") if self._is_capturing()
                                 else t("gui.trayStart"),
                                 lambda *_: self.post(self._on_toggle)),
                pystray.MenuItem(lambda _i: t("gui.trayQuit"),
                                 lambda *_: self.post(self._on_quit)),
            )
            self._icon = pystray.Icon("easysub-helper", image, t("gui.title"), menu)
        except Exception as exc:                    # noqa: BLE001 - 托盘失败不该影响主功能
            LOG.warning("tray unavailable: %s", exc)
            self._icon = None
            return False
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
        if self._icon is not None:
            try:
                self._icon.update_menu()
            except Exception:                       # noqa: BLE001
                pass

    def stop(self):
        icon, self._icon = self._icon, None
        with self._lock:
            self._ready = False
        if icon is not None:
            try:
                icon.stop()
            except Exception:                       # noqa: BLE001
                pass
