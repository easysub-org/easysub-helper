# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 hcz1017
"""图形界面（tkinter / ttk）：用户唯一该看见的入口。

**为什么必须有这个文件**：命令行是给开发者排障用的，普通用户不会敲命令。所以助手的常态是：
双击图标 → 弹出一个窗口 → 窗口里按「启动」开始采音频、看到配对码、看到电平图。
窗口只需要回答三件事：**服务活着吗**、**配对码是多少**、**真的在采吗（电平图）**。

窗口只做两件事对应助手只做两件事：**采集开关**（启动/暂停）与**配对码**。
识别、模型、页面托管都与助手无关——那些是浏览器那一侧的事。

设计约束：
  - **只用标准库**：tkinter/ttk 在 Windows/macOS 自带，Linux 上由 python3-tk 提供；
    打包（PyInstaller --windowed）也只需带上它，不引入 Qt 这种几十兆的重依赖。
  - **服务跑在后台线程**（自带 asyncio 事件循环），tkinter 只在主线程跑——tkinter 不是线程安全的，
    界面↔服务的所有交互都走两条安全通道：
      1. 读：每 100ms 轮询 `server.snapshot()`（无锁只读标量，见 net/server.py）；
      2. 写：`asyncio.run_coroutine_threadsafe(coro, loop)` 把协程丢回事件循环。
    这样永远不需要从采集线程里碰 tkinter 控件。
  - **没有 tkinter / 没有显示环境就老实回落命令行**（`available()` 返回 False）。
  - 文案全部走 i18n（`gui.*` / `lang.*` / `log.*`）：窗口里**每一个**控件、以及日志区里显示的
    日志正文都能切中英；语言按钮点击后**就地重译**（不重启），选择写进 settings.json。
"""

import asyncio
import collections
import logging
import math
import os
import queue
import sys
import threading
import time
import webbrowser

# 允许直接跑这个文件（`python easysub_helper/gui.py`）：那时没有包上下文，
# 下面的相对 import 会抛 ImportError。补一个包上下文，语义等价于 `python -m easysub_helper`
# （见文件末尾的 __main__：参数直接转发给命令行入口）。
if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    __package__ = "easysub_helper"

from . import config
from . import tray
from .audio import devices
from .i18n import LANGUAGES, get_language, set_language, source_label, t
from .pairing import format_code

#: 轮询间隔：100ms 内刷新状态/电平，肉眼流畅，又不至于让 tkinter 空转
POLL_MS = 100
#: 日志区保留的行数（只给人看，不做归档）
LOG_LINES = 400
#: 采集线程写电平的"新鲜度"：超过这个时间没有新电平，就当静音（避免卡住的柱子）
LEVEL_STALE_SEC = 0.6

SOURCE_ORDER = ("system", "mic")

#: 配色（浅色主题下都够清晰；深色系统主题下也只是不跟随，不会看不清）
COLOR_OK = "#16a34a"
COLOR_WARN = "#d97706"
COLOR_ERR = "#dc2626"
COLOR_IDLE = "#9ca3af"
COLOR_CODE_BG = "#111827"
COLOR_CODE_FG = "#f8fafc"
COLOR_WAVE = "#22c55e"       # 波形柱
COLOR_PEAK = "#f59e0b"       # 峰值保持线
COLOR_GRID = "#1e293b"       # 画布网格/基线
COLOR_TROUGH = "#334155"     # 未点亮的电平段
COLOR_DIM = "#64748b"        # 刻度/占位文字
ACCENT_STYLE = "EasysubAccent.TButton"
COLOR_LINK = "#2563eb"       # 链接（含页脚的源码地址）

#: 电平图窗口：助手每 100ms 报一次电平 → 120 格 ≈ 12 秒的滚动波形
LEVEL_BARS = 120
#: 电平图画布高度（上半波形、下半分段电平表 + dB 刻度）
LEVEL_CANVAS_H = 84


def tkinter_module():
    """返回可用的 tkinter 模块；不可用（缺包 / 无显示 / 被显式关掉）返回 None。"""
    if os.environ.get("EASYSUB_HELPER_NO_GUI") == "1":
        return None
    try:
        import tkinter
    except Exception:  # noqa: BLE001 - ImportError，或某些 Linux 上缺 libtk 的 OSError
        return None
    if not config.is_windows() and not config.is_macos():
        # Linux/BSD：没有 DISPLAY/WAYLAND_DISPLAY 就是纯 SSH/CI 环境，起窗口只会报错
        if not os.environ.get("DISPLAY") and not os.environ.get("WAYLAND_DISPLAY"):
            return None
    return tkinter


def available():
    return tkinter_module() is not None


def assets_dir():
    """资源目录：源码运行用包内 assets/，PyInstaller 单文件用解包目录。"""
    base = getattr(sys, "_MEIPASS", None)
    if base:
        for rel in (os.path.join("easysub_helper", "assets"), "assets"):
            cand = os.path.join(base, rel)
            if os.path.isdir(cand):
                return cand
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets")


def other_language(current=None):
    """另一种语言（语言按钮显示的名字用它）。"""
    current = current or get_language()
    for code in LANGUAGES:
        if code != current:
            return code
    return current


def language_label(code):
    """语言的**自称**（中文界面显示 English，英文界面显示 中文）。

    刻意不用"拼 key"的写法（把 code 拼到 lang. 后面）：i18n 的完整性测试是
    **扫源码里的字面量 key**，拼出来的 key 它扫不到，漏翻译也就没人拦。
    这里两个分支都是字面量，扫描器盯得住。
    """
    return t("lang.zh_CN") if code == "zh_CN" else t("lang.en")


class LogBufferHandler(logging.Handler):
    """把日志尾部留给窗口里的日志区：不落盘、不阻塞、线程安全。"""

    def __init__(self, maxlen=LOG_LINES):
        logging.Handler.__init__(self)
        self._mutex = threading.Lock()
        self._lines = collections.deque(maxlen=maxlen)
        self._total = 0

    def emit(self, record):
        try:
            text = self.format(record)
        except Exception:  # noqa: BLE001 - 格式化失败不能反过来打断采集
            return
        with self._mutex:
            self._lines.append(text)
            self._total += 1

    def snapshot(self):
        """返回 (行列表, 累计条数)。累计条数让界面知道有没有被 deque 挤掉老行。"""
        with self._mutex:
            return list(self._lines), self._total


def install_log_buffer(level=logging.INFO):
    """装一个日志缓冲处理器并返回它（供界面读取）。

    幂等：入口（cli）为了不漏掉"服务启动失败"那几条，会先装一次；窗口起来后又会要一份。
    重复安装会让日志区里每行出现两遍，所以这里先找现成的。
    """
    root = logging.getLogger()
    for handler in root.handlers:
        if isinstance(handler, LogBufferHandler):
            return handler
    handler = LogBufferHandler()
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s", "%H:%M:%S"))
    handler.setLevel(level)
    if not root.level:
        root.setLevel(level)
    root.addHandler(handler)
    return handler


def _level_percent(rms, peak):
    """把 rms/peak 映射成 0..100 的柱子高度：按 dBFS 线性铺开 -60..0 dB。"""
    value = max(float(rms or 0.0), float(peak or 0.0) * 0.5)
    if value <= 0.0:
        return 0.0
    db = 20.0 * math.log10(value)
    pct = (db + 60.0) / 60.0 * 100.0
    return max(0.0, min(100.0, pct))


class HelperWindow(object):
    """主窗口。构造完调 `mainloop()`，返回进程退出码。"""

    def __init__(self, server, pairing):
        self.tk = tkinter_module()
        if self.tk is None:
            raise RuntimeError("tkinter unavailable")
        self.server = server
        self.pairing = pairing

        self._loop = None
        self._thread = None
        self._closing = False
        self._tray = None
        #: 托盘菜单回调（跑在 pystray 线程）只往这里投函数，由主线程 _poll 取出来执行
        self._tray_actions = queue.Queue()
        self._boot_error = None
        self._shown_code = None
        self._shown_ttl = None
        self._toggle_label = None
        self._log_rendered = 0
        self._poll_job = None
        self._copy_job = None
        #: 电平图的滚动历史 / 峰值保持 / 三态标志
        self.level_history = collections.deque(maxlen=LEVEL_BARS)
        self._last_level_at = None
        self._level_pct = 0.0
        self._peak_hold = 0.0
        self._peak_hold_at = 0.0
        self._paused = True
        self._capturing = False
        self._drawn_state = None
        #: 设备下拉（只在窗口里用：页面不需要选设备，见 audio/devices.py）
        self._device_items = []
        self._device_generation = 0
        self._device_reload_pending = False
        #: 枚举结果走队列回主线程：**tkinter 不是线程安全的**，连 `after()` 都不能从
        #: 别的线程调（会抛异常或被静默忽略），所以工作线程只投递数据，主线程轮询取走。
        self._device_results = queue.Queue()

        self.log_handler = install_log_buffer()

        self.root = self.tk.Tk()
        self.root.title(t("gui.title"))
        self._set_window_icon()
        self.root.minsize(430, 560)
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        self._setup_style()
        self._build()
        self._sync_device_picker()
        self._reload_devices()
        self._start_server_thread()

        self._schedule_poll()

    # ---------------- 外观 ----------------
    def _setup_style(self):
        from tkinter import font as tkfont
        from tkinter import ttk

        self.ttk = ttk
        self.style = ttk.Style(self.root)
        # Linux 的默认主题很朴素；clam 允许我们调色，观感立刻正常。Windows/macOS 保持原生主题
        # （vista/aqua 比 clam 好看得多，但几乎不允许改背景色，所以只改字体/内边距）。
        self._clam = False
        if config.is_linux() and "clam" in self.style.theme_names():
            try:
                self.style.theme_use("clam")
                self._clam = True
            except Exception:  # noqa: BLE001
                pass

        mono_candidates = ["DejaVu Sans Mono", "Consolas", "Menlo", "Monaco", "Courier New", "TkFixedFont"]
        families = set(tkfont.families(self.root))
        mono = next((f for f in mono_candidates if f in families), "TkFixedFont")
        self.font_mono = tkfont.Font(root=self.root, family=mono, size=30, weight="bold")
        self.font_mono_small = tkfont.Font(root=self.root, family=mono, size=10)
        self.font_title = tkfont.Font(root=self.root, size=15, weight="bold")
        self.font_body = tkfont.Font(root=self.root, size=10)
        self.font_body_bold = tkfont.Font(root=self.root, size=10, weight="bold")
        self.font_small = tkfont.Font(root=self.root, size=9)

        self.style.configure("Easysub.TLabel", font=self.font_body)
        self.style.configure("Title.TLabel", font=self.font_title)
        self.style.configure("Sub.TLabel", font=self.font_small, foreground="#6b7280")
        self.style.configure("Hint.TLabel", font=self.font_small, foreground="#6b7280")
        self.style.configure("Status.TLabel", font=self.font_body_bold)
        self.style.configure("Field.TLabel", font=self.font_small, foreground="#6b7280")
        self.style.configure("Link.TLabel", font=self.font_small, foreground=COLOR_LINK)
        if self._clam:
            self.style.configure(ACCENT_STYLE, font=self.font_body_bold, padding=(16, 7),
                                 background="#2563eb", foreground="#ffffff", borderwidth=0)
            self.style.map(ACCENT_STYLE, background=[("active", "#1d4ed8"), ("disabled", "#93c5fd")])
            self.style.configure("TButton", padding=(10, 5))
        else:
            # aqua/vista 不接受自定义配色，只统一字体与内边距，交给系统画
            self.style.configure(ACCENT_STYLE, font=self.font_body_bold, padding=(16, 7))
            self.style.configure("TButton", padding=(10, 5))

    def _set_window_icon(self):
        """把窗口/任务栏图标换成 easysub 的（默认是 Tk 的羽毛，用户反馈过）。

        跨平台差异都试一遍、失败一律吞掉——图标再好看也不值得让窗口起不来：
          * Windows：`iconbitmap(default=…)` 认 `.ico`，任务栏与 Alt-Tab 都用它；
          * Linux/macOS：`iconphoto` + PNG（Tk 8.6 起支持 PNG；Dock 图标由打包时的
            `--icon easysub.icns` 决定，这里补的是窗口/任务栏那一份）。
        坑：PhotoImage 必须**留一个引用**——Tk 不持有 Python 对象的引用，被 GC 回收后图标会
        悄悄变回默认羽毛（与下面的 _logo() 同一套约定）。
        """
        base = assets_dir()
        ico = os.path.join(base, "easysub.ico")
        png = os.path.join(base, "icon128.png")
        #: 记下实际走了哪个分支（"ico" / "png" / None）——测试要按平台断言，而不是无条件
        #: 要求 `_window_icon` 存在：Windows 上 ico 成功时**刻意**不设 PNG（见下）。
        self._window_icon_kind = None
        ico_done = False
        if config.is_windows() and os.path.exists(ico):
            for call in (lambda: self.root.iconbitmap(default=ico), lambda: self.root.iconbitmap(ico)):
                try:
                    call()
                    ico_done = True
                    self._window_icon_kind = "ico"
                    break
                except Exception:  # noqa: BLE001 - 个别 Tcl/Tk 版本不吃 default 关键字
                    continue
        # 坑（独立审查提醒）：Windows 上 iconphoto 会再发一次 WM_SETICON，可能把刚设好的 .ico
        # 盖掉（.ico 才是 Windows 任务栏/Alt-Tab 最认的那份）。所以 Windows 上 ico 成功就不再
        # 叠加 iconphoto；其它平台（Linux 的 _NET_WM_ICON、macOS 的窗口图标）走 PNG。
        if os.path.exists(png) and not (config.is_windows() and ico_done):
            try:
                from tkinter import PhotoImage

                image = PhotoImage(file=png)
                self.root.iconphoto(True, image)     # True = 之后所有 Toplevel 都继承
                # 坑（第四/五轮审查）：`_window_icon` 与 `_window_icon_kind` 必须在
                # **iconphoto 真的调过之后**才记 —— 先记再调的话，iconphoto 抛异常时会把状态
                # 记成"设好了"（假绿）；另外"调用被删"这一种由
                # test_window_icon_actually_calls_iconphoto 的 spy 独立盯住。
                self._window_icon = image            # 留引用，防 GC（见 docstring）
                self._window_icon_kind = "png"
            except Exception:  # noqa: BLE001 - 没有 PNG 支持/图坏了：退回默认图标
                pass

    def _logo(self):
        """插件 logo（与主项目图标同源，见 tools/make_icons.py）。取不到就退回纯文字标题。"""
        from tkinter import PhotoImage

        path = os.path.join(assets_dir(), "icon128.png")
        try:
            image = PhotoImage(file=path)
            factor = max(1, int(image.width() // 48))
            self._logo_image = image.subsample(factor, factor)
            return self._logo_image
        except Exception:  # noqa: BLE001 - 图缺了不影响功能
            return None

    def _build(self):
        ttk = self.ttk
        self.root.columnconfigure(0, weight=1)
        outer = ttk.Frame(self.root, padding=(16, 14, 16, 12))
        outer.grid(row=0, column=0, sticky="nsew")
        outer.columnconfigure(1, weight=1)

        # 头部：logo + 标题 + 一句话说明 + 语言切换按钮
        header = ttk.Frame(outer)
        header.grid(row=0, column=0, columnspan=3, sticky="ew")
        header.columnconfigure(1, weight=1)
        logo = self._logo()
        if logo is not None:
            ttk.Label(header, image=logo).grid(row=0, column=0, rowspan=2, padx=(0, 10))
        self.title_label = ttk.Label(header, text=t("gui.title"), style="Title.TLabel")
        self.title_label.grid(row=0, column=1, sticky="w")
        # macOS 上不能说"不需要装虚拟声卡"：14.2+ 免驱，更早的系统要 BlackHole
        # （用户可用性审查抓到：窗口副标题与打包事实相反，直接摧毁用户对文档的信任）
        self.subtitle_label = ttk.Label(
            header,
            text=t("gui.subtitleMac") if sys.platform == "darwin" else t("gui.subtitle"),
            style="Sub.TLabel",
                                        justify="left", wraplength=330)
        self.subtitle_label.grid(row=1, column=1, sticky="w", pady=(2, 0))
        # 语言切换：按钮上写"另一种语言"的名字，点一下就地重译并记住
        self.lang_button = ttk.Button(header, text=language_label(other_language()),
                                      command=self.switch_language, width=8)
        self.lang_button.grid(row=0, column=2, rowspan=2, sticky="ne", padx=(8, 0))

        ttk.Separator(outer, orient="horizontal").grid(row=1, column=0, columnspan=3,
                                                      sticky="ew", pady=(12, 10))

        # 状态行：彩色圆点 + 文字；右侧显示已连接页面数
        status = ttk.Frame(outer)
        status.grid(row=2, column=0, columnspan=3, sticky="ew")
        status.columnconfigure(2, weight=1)
        self.dot = self.tk.Canvas(status, width=14, height=14, highlightthickness=0)
        self.dot.grid(row=0, column=0, padx=(0, 6))
        self._dot_id = self.dot.create_oval(2, 2, 12, 12, fill=COLOR_IDLE, outline="")
        self.status_label = ttk.Label(status, text=t("gui.statusStarting"), style="Status.TLabel")
        self.status_label.grid(row=0, column=1, sticky="w")
        self.clients_label = ttk.Label(status, text="", style="Hint.TLabel")
        self.clients_label.grid(row=0, column=2, sticky="e")
        self.detail_label = ttk.Label(outer, text="", style="Hint.TLabel", justify="left",
                                      wraplength=380)
        self.detail_label.grid(row=3, column=0, columnspan=3, sticky="w", pady=(3, 0))

        # 音源 + 电平图（电平图是"真的在采"的唯一证据，没配对时也能看）
        controls = ttk.Frame(outer)
        controls.grid(row=4, column=0, columnspan=3, sticky="ew", pady=(10, 0))
        controls.columnconfigure(3, weight=1)
        # 音源（系统音频 / 麦克风）+ 设备（具体用哪一个）：两个下拉都只影响"下次打开设备"，
        # 正在采集时会立刻重启采集，所见即所得。
        self.source_field_label = ttk.Label(controls, text=t("gui.sourceLabel"), style="Field.TLabel")
        self.source_field_label.grid(row=0, column=0, sticky="w", padx=(0, 8))
        self.source_box = ttk.Combobox(controls, state="readonly", width=12, font=self.font_body)
        self.source_box["values"] = [source_label(s) for s in SOURCE_ORDER]
        initial = self.server.default_source if self.server.default_source in SOURCE_ORDER else "system"
        self.source_box.current(SOURCE_ORDER.index(initial))
        self.source_box.grid(row=0, column=1, sticky="w")
        self.source_box.bind("<<ComboboxSelected>>", self._on_source_change)
        self.device_field_label = ttk.Label(controls, text=t("gui.deviceLabel"), style="Field.TLabel")
        self.device_field_label.grid(row=0, column=2, sticky="w", padx=(12, 8))
        self.device_box = ttk.Combobox(controls, state="readonly", width=26, font=self.font_small)
        self.device_box["values"] = [t("device.default")]
        self.device_box.current(0)
        self.device_box.grid(row=0, column=3, sticky="w")
        self.device_box.bind("<<ComboboxSelected>>", self._on_device_change)
        self.level_text = ttk.Label(controls, text="", style="Hint.TLabel")
        self.level_text.grid(row=0, column=4, sticky="e")
        self.level_field_label = ttk.Label(controls, text=t("gui.levelLabel"), style="Field.TLabel")
        self.level_field_label.grid(row=1, column=0, sticky="nw", padx=(0, 8), pady=(8, 0))
        # 电平图用 Canvas 自己画：上半是 12 秒滚动波形（和面板里的波形一个观感），
        # 下半是绿/黄/红分段电平表 + 峰值保持线。ttk.Progressbar 只能给一根空槽，
        # 静音和"没在采集"长得一模一样，用户会以为坏了。
        self.level_canvas = self.tk.Canvas(controls, height=LEVEL_CANVAS_H, highlightthickness=0,
                                           background="#0b1220", bd=0)
        self.level_canvas.grid(row=1, column=1, columnspan=4, sticky="ew", pady=(8, 0))
        self.level_canvas.bind("<Configure>", lambda _e: self._draw_level())

        # 主操作：启动/暂停（唯一控制音频的开关）+ 退出
        actions = ttk.Frame(outer)
        actions.grid(row=5, column=0, columnspan=3, sticky="ew", pady=(12, 0))
        self.toggle_button = ttk.Button(actions, text=t("gui.start"), style=ACCENT_STYLE,
                                        command=self.toggle_capture)
        self.toggle_button.grid(row=0, column=0, sticky="w")
        self.quit_button = ttk.Button(actions, text=t("gui.quit"), command=self.quit)
        self.quit_button.grid(row=0, column=1, padx=(8, 0))

        # 配对码：整个窗口里最该被一眼看到的东西
        self.pair_frame = ttk.LabelFrame(outer, text=t("gui.pairTitle"), padding=(12, 8, 12, 10))
        self.pair_frame.grid(row=6, column=0, columnspan=3, sticky="ew", pady=(12, 0))
        self.pair_frame.columnconfigure(0, weight=1)
        self.code_label = self.tk.Label(self.pair_frame, text="···", font=self.font_mono,
                                        background=COLOR_CODE_BG, foreground=COLOR_CODE_FG,
                                        padx=14, pady=8, cursor="hand2")
        self.code_label.grid(row=0, column=0, sticky="w")
        self.code_label.bind("<Button-1>", lambda _e: self.copy_code())
        pair_buttons = ttk.Frame(self.pair_frame)
        pair_buttons.grid(row=0, column=1, sticky="e", padx=(10, 0))
        self.copy_button = ttk.Button(pair_buttons, text=t("gui.pairCopy"), command=self.copy_code)
        self.copy_button.grid(row=0, column=0)
        self.new_button = ttk.Button(pair_buttons, text=t("gui.pairNew"), command=self.new_code)
        self.new_button.grid(row=0, column=1, padx=(6, 0))
        self.pair_hint = ttk.Label(self.pair_frame, text=t("gui.pairHint"), style="Hint.TLabel",
                                   justify="left", wraplength=380)
        self.pair_hint.grid(row=1, column=0, columnspan=2, sticky="w", pady=(8, 0))
        # 已配对设备 + 解绑入口（用户可用性审查：以前撤销一个已配对设备只能自己去删
        # pairing.json —— 三平台路径还各不相同，普通用户根本做不到）
        self.devices_label = ttk.Label(self.pair_frame, text=t("gui.devicesCount", count=0),
                                       style="Hint.TLabel")
        self.devices_label.grid(row=2, column=0, sticky="w", pady=(8, 0))
        self.devices_button = ttk.Button(self.pair_frame, text=t("gui.devicesManage"),
                                         command=self.manage_devices)
        self.devices_button.grid(row=2, column=1, sticky="e", pady=(8, 0))
        if getattr(self.server, "fixed_token", None):
            # 调试模式（--token 固定令牌）根本没有"配对设备"这回事：禁用而不是给个点了没反应的按钮
            self.devices_label.configure(text=t("gui.pairDebug"))
            self.devices_button.state(["disabled"])

        # 设置行（用户可用性审查：助手"关窗即停服务"，用户手滑关掉窗口浏览器那边就废了）
        self.settings = ttk.Frame(outer)
        self.settings.grid(row=7, column=0, columnspan=3, sticky="ew", pady=(8, 0))
        # 默认**开**（用户要的就是"关窗别杀服务"），并记住用户的选择
        tray_ok = tray.available()
        self._tray_wanted = bool(config.get_setting("minimize_to_tray", True)) and tray_ok
        self.tray_var = self.tk.BooleanVar(value=self._tray_wanted)
        self.tray_box = ttk.Checkbutton(
            self.settings, text=t("gui.minimizeToTray"), variable=self.tray_var,
            command=self._on_tray_toggle)
        self.tray_box.grid(row=0, column=0, sticky="w")
        self.tray_hint = ttk.Label(self.settings, text="", style="Hint.TLabel",
                                   wraplength=380, justify="left")
        if not tray_ok:
            # 装不上就说清楚（含安装命令），而不是给一个点了没反应的勾选框
            self.tray_box.state(["disabled"])
            self.tray_var.set(False)
            self.tray_hint.configure(text=tray.unavailable_reason())
            self.tray_hint.grid(row=1, column=0, sticky="w")

        footer = ttk.Frame(outer)
        footer.grid(row=8, column=0, columnspan=3, sticky="ew", pady=(8, 0))
        footer.columnconfigure(0, weight=1)
        self.keep_open_label = ttk.Label(footer, text=t("gui.keepOpen"), style="Hint.TLabel",
                                         justify="left", wraplength=360)
        self.keep_open_label.grid(row=0, column=0, sticky="w")
        # AGPL 第 13 条：本助手是个"通过网络提供服务"的程序，界面里给出对应源码地址最省事
        self.license_label = ttk.Label(footer, text=t("gui.licenseSource"), style="Link.TLabel",
                                       cursor="hand2")
        self.license_label.grid(row=0, column=1, sticky="e", padx=(10, 0))
        self.license_label.bind("<Button-1>", self.open_source)

        # 日志（排障时才看，所以放最下面且不抢眼）
        self.log_frame = ttk.LabelFrame(outer, text=t("gui.logLabel"), padding=(8, 4, 8, 6))
        self.log_frame.grid(row=9, column=0, columnspan=3, sticky="nsew", pady=(10, 0))
        outer.rowconfigure(9, weight=1)
        self.log_frame.columnconfigure(0, weight=1)
        self.log_frame.rowconfigure(0, weight=1)
        self.log_text = self.tk.Text(self.log_frame, height=7, wrap="none", font=self.font_mono_small,
                                     background="#f9fafb", foreground="#374151",
                                     relief="flat", highlightthickness=1,
                                     highlightbackground="#e5e7eb")
        self.log_text.grid(row=0, column=0, sticky="nsew")
        self.log_text.configure(state="disabled")
        scroll = ttk.Scrollbar(self.log_frame, orient="vertical", command=self.log_text.yview)
        scroll.grid(row=0, column=1, sticky="ns")
        self.log_text.configure(yscrollcommand=scroll.set)

    # ---------------- 服务线程 ----------------
    def _start_server_thread(self):
        self._thread = threading.Thread(target=self._server_main, name="easysub-helper-server")
        self._thread.daemon = True
        self._thread.start()

    def _server_main(self):
        # 服务自带事件循环：tkinter 占着主线程，谁也别想共用
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self.server.start())
            self._loop.run_forever()
        except Exception as exc:  # noqa: BLE001 - 端口占用等要显示在窗口里
            self._boot_error = str(exc)
            logging.getLogger("easysub-helper").error(t("log.serviceStartFailed", error=exc))
        finally:
            try:
                self._loop.run_until_complete(self.server.stop())
            except Exception:  # noqa: BLE001
                pass
            try:
                self._loop.close()
            except Exception:  # noqa: BLE001
                pass
            self._loop = None

    def _submit(self, coro):
        """把协程丢回服务线程的事件循环（界面线程唯一允许的"写"通道）。"""
        loop = self._loop
        if loop is None or self._closing:
            coro.close()
            return None

        def _log_outcome(future):
            try:
                future.result()
            except Exception as exc:  # noqa: BLE001
                logging.getLogger("easysub-helper").debug(t("log.guiActionFailed", error=exc))

        try:
            future = asyncio.run_coroutine_threadsafe(coro, loop)
        except RuntimeError:
            coro.close()
            return None
        future.add_done_callback(_log_outcome)
        return future

    # ---------------- 轮询刷新 ----------------
    def _schedule_poll(self):
        if self._closing:
            return
        self._poll_job = self.root.after(POLL_MS, self._poll)

    def _poll(self):
        if self._closing:
            return
        try:
            self._refresh()
        except Exception as exc:  # noqa: BLE001 - 刷新出错也不能把界面弄死
            logging.getLogger("easysub-helper").debug(t("log.guiRefreshFailed", error=exc))
        self._refresh_log()
        self._drain_device_results()
        self._drain_tray_actions()
        if self._device_reload_pending:
            self._device_reload_pending = False
            self._reload_devices()
        self._schedule_poll()

    def _refresh(self):
        snap = self.server.snapshot()
        self._paused = bool(snap["paused"])
        self._capturing = bool(snap["capturing"])

        if self._boot_error:
            self._set_status(t("gui.statusFailed"), COLOR_ERR)
            self.detail_label.configure(text=self._boot_error)
        elif not snap["running"]:
            self._set_status(t("gui.statusStarting"), COLOR_WARN)
            self.detail_label.configure(text="")
        elif snap["error"]:
            # 采集中断时把"下一步"写在这里：用户在助手窗口看的就是这一行
            self._set_status(self._status_with_port(
                "{} —— {}".format(t("gui.captureFailed"), t("gui.captureFailedHint"))), COLOR_ERR)
            self.detail_label.configure(text=snap["error"])
        elif snap["paused"]:
            self._set_status(self._status_with_port(
                t("gui.statusPaused", port=snap["port"])), COLOR_WARN)
            self.detail_label.configure(text="")
        elif snap["capturing"]:
            self._set_status(self._status_with_port(
                t("gui.captureOn", source=source_label(snap["source"]))), COLOR_OK)
            self.detail_label.configure(text=self._backend_text(snap))
        else:
            self._set_status(self._status_with_port(t("gui.waitingFrames")), COLOR_WARN)
            self.detail_label.configure(text=self._backend_text(snap))

        self.clients_label.configure(text=t("gui.clients", count=snap["clients"]))

        # 启动/暂停按钮：文案与可用性都跟着服务端状态走（按钮只是"意图"，状态以服务端为准）
        label = t("gui.pause") if snap["userOn"] else t("gui.start")
        if label != self._toggle_label:
            self._toggle_label = label
            self.toggle_button.configure(text=label)
        if snap["running"] and not self._boot_error:
            self.toggle_button.state(["!disabled"])
        else:
            self.toggle_button.state(["disabled"])

        self._refresh_level(snap)
        self._drain_tray_actions()
        self._refresh_pair_code()

    def _backend_text(self, snap):
        """状态行副标题：后端 + 采样率 +（采集时）后端实际打开的设备名。"""
        text = t("gui.backend", backend=snap["backend"] or "-", rate=snap["rate"])
        opened = snap.get("openedDevice")
        if snap["capturing"] and opened:
            text += "  ·  " + t("gui.openedDevice", device=devices.label_for(opened, limit=36))
        return text

    def open_source(self, _event=None):
        """打开源码地址（AGPL 第 13 条的"提供对应源码"入口）。"""
        try:
            webbrowser.open(config.PROJECT_URL)
        except Exception as exc:  # noqa: BLE001 - 没浏览器/无桌面环境不该让窗口崩
            logging.getLogger("easysub-helper").warning(
                t("log.openSourceFailed", error=exc))

    def _status_with_port(self, text):
        """状态行永远带上端口：页面离线框里那句"助手状态行里那个「端口 N」"要靠它。

        坑（独立审查）：以前只有"已暂停"那一态显示端口，用户点了「启动」之后照着页面文案
        去状态行找端口就找不到了。
        """
        port = getattr(self.server, "port", None)
        if not port:
            return text
        suffix = t("gui.statusPort", port=port)
        if suffix in text:          # 有的文案（gui.statusPaused）本来就带端口，别写两遍
            return text
        return "{}  ·  {}".format(text, suffix)

    def _set_status(self, text, color):
        self.status_label.configure(text=text)
        self.dot.itemconfigure(self._dot_id, fill=color)

    def _refresh_level(self, snap):
        """电平图：暂停 / 采集中 / 已启动等帧 三态分明。

        助手每 100ms 报一次电平，这里把它推进 12 秒滚动窗并重画。**暂停与"没在采集"要长得
        完全不一样**——否则用户分不清"我按了暂停"和"助手坏了"。
        """
        now = time.monotonic()
        if snap["capturing"] and not snap["paused"]:
            if snap["levelAt"] and snap["levelAt"] != self._last_level_at:
                self._last_level_at = snap["levelAt"]
                self.level_history.append((snap["levelRms"], snap["levelPeak"]))
            fresh = (now - snap["levelAt"]) <= LEVEL_STALE_SEC
            self._level_pct = _level_percent(snap["levelRms"], snap["levelPeak"]) if fresh else 0.0
            if self._level_pct >= self._peak_hold:
                self._peak_hold = self._level_pct
                self._peak_hold_at = now
            elif now - self._peak_hold_at > 0.9:
                self._peak_hold = max(0.0, self._peak_hold - 4.0)
            if self._level_pct >= 3.0:
                self.level_text.configure(text="{} · {:.0f}%".format(t("gui.levelSignal"),
                                                                    self._level_pct))
            else:
                self.level_text.configure(text=t("gui.levelSilent"))
        else:
            # 暂停（或刚启动还没出帧）：清空历史、峰值归零，图里写清当前状态
            if self.level_history:
                self.level_history.clear()
            self._last_level_at = None
            self._level_pct = 0.0
            self._peak_hold = 0.0
            self.level_text.configure(text="")

        state = (bool(snap["paused"]), bool(snap["capturing"]))
        if state != self._drawn_state:          # 状态切换时立刻重画（不等下一帧）
            self._drawn_state = state
            self._draw_level()
        elif snap["capturing"] and not snap["paused"]:
            self._draw_level()

    def _draw_level(self):
        """画滚动波形 + 分段电平表 + 状态占位。纯 Canvas 图元，100ms 重画一次的开销可忽略。"""
        canvas = self.level_canvas
        width = int(canvas.winfo_width())
        height = int(canvas.winfo_height())
        if width <= 1 or height <= 1:
            # 还没布局出来（窗口刚建好、尚未映射）：退回**请求尺寸**，别把首帧画成 1px 宽，
            # 也别干脆不画 —— 100ms 后会用真实尺寸重画，用户看不到这一帧。
            width = max(int(canvas.winfo_reqwidth()), 1)
            height = max(int(canvas.winfo_reqheight()), 1)
        canvas.delete("all")
        capturing = bool(self._capturing) and not self._paused
        # 从下往上排版：留出 dB 刻度文字的高度，波形区吃剩下的空间（不让任何图元被裁掉）
        meter_h = 12
        legend_h = 16
        pad = 4
        meter_bottom = height - legend_h - pad
        meter_top = meter_bottom - meter_h
        wave_top = pad
        wave_bottom = max(wave_top + 12, meter_top - 6)
        wave_h = wave_bottom - wave_top
        # 时间网格：一格 100ms，每 20 格（2 秒）一条竖线
        for i in range(LEVEL_BARS // 6, LEVEL_BARS, LEVEL_BARS // 6):
            x = width * i / float(LEVEL_BARS)
            canvas.create_line(x, wave_top, x, wave_bottom, fill=COLOR_GRID)
        canvas.create_line(0, wave_bottom, width, wave_bottom, fill=COLOR_GRID)

        bars = list(self.level_history)
        count = len(bars)
        bar_w = width / float(LEVEL_BARS)
        for index, (rms, peak) in enumerate(bars):
            pct = _level_percent(rms, peak)
            if pct <= 0.0:
                continue
            bar_h = max(1.0, wave_h * min(pct, 100.0) / 100.0)
            x1 = width - (count - index - 1) * bar_w - 1.0      # 最新的贴右边
            canvas.create_rectangle(x1 - max(1.0, bar_w - 1.0), wave_bottom - bar_h,
                                    x1, wave_bottom, fill=COLOR_WAVE, outline="")

        # 分段电平表：绿 → 黄 → 红，未点亮部分留暗槽；峰值保持线单独一根
        segments = 60
        y0 = meter_top
        y1 = meter_bottom
        seg_w = max(1.0, (width - (segments - 1)) / float(segments))
        lit = int(round(self._level_pct / 100.0 * segments)) if capturing else 0
        for seg in range(segments):
            x = seg * (seg_w + 1.0)
            if seg < segments * 0.7:
                color = COLOR_OK
            elif seg < segments * 0.88:
                color = COLOR_WARN
            else:
                color = COLOR_ERR
            canvas.create_rectangle(x, y0, x + seg_w, y1,
                                    fill=(color if seg < lit else COLOR_TROUGH), outline="")
        if capturing and self._peak_hold > 0.0:
            x = self._peak_hold / 100.0 * width
            canvas.create_line(x, y0 - 1, x, y1 + 1, fill=COLOR_PEAK, width=2)

        canvas.create_text(2, y1 + 3, anchor="nw", text="-60 dB", fill=COLOR_DIM,
                           font=self.font_mono_small)
        canvas.create_text(width - 2, y1 + 3, anchor="ne", text="0 dB", fill=COLOR_DIM,
                           font=self.font_mono_small)
        if not capturing:
            canvas.create_text(width / 2.0, (wave_top + wave_bottom) / 2.0,
                               text=t("gui.pausedHint") if self._paused else t("gui.waitingFrames"),
                               fill=COLOR_DIM, font=self.font_small)

    def _refresh_pair_code(self):
        if self.server.fixed_token:
            self.code_label.configure(text="--token--", font=self.font_mono_small)
            self.pair_hint.configure(text=t("gui.pairDebug"))
            self.copy_button.state(["disabled"])
            self.new_button.state(["disabled"])
            self._shown_code = None
            return
        if self.pairing is None:
            self.code_label.configure(text="——", font=self.font_mono)
            self.pair_hint.configure(text=t("gui.pairNone"))
            self.copy_button.state(["disabled"])
            self.new_button.state(["disabled"])
            self._shown_code = None
            return
        # 过期自动换码这件事**本来就已经成立**：ensure_code() 在码过期时会自己 new_code()
        # （pairing.py 的 ensure_code → new_code），所以窗口不会长期摆着一个死码。
        # 更正（独立审查核对 `git show 69d5241`）：我先前以为这里是"显示死码"的死路并加了一个
        # `new_code()` 分支 —— 那是**不可达的死代码**，已删除。这条路径真正缺的只是下面那句
        # "被锁了怎么办"的提示（已补）。
        self.pairing.ensure_code()
        state = self.pairing.state()
        locked = int(state.get("lockedForSec") or 0)
        if locked:
            hint = t("gui.pairLocked", sec=locked)
            if self.pair_hint.cget("text") != hint:
                self.pair_hint.configure(text=hint)
        elif self.pair_hint.cget("text") != t("gui.pairHint"):
            self.pair_hint.configure(text=t("gui.pairHint"))
        devices = int(state.get("devices") or 0)
        if devices != getattr(self, "_shown_devices", None):
            self._shown_devices = devices
            self.devices_label.configure(text=t("gui.devicesCount", count=devices))
        if not state.get("valid"):
            return
        code = state["code"]
        if code != self._shown_code:
            self._shown_code = code
            self.code_label.configure(text=format_code(code), font=self.font_mono)
            self.copy_button.state(["!disabled"])
            self.new_button.state(["!disabled"])
        if state["remainingSec"] != self._shown_ttl:
            self._shown_ttl = state["remainingSec"]
            self.pair_frame.configure(
                text="{}  ·  {}".format(t("gui.pairTitle"),
                                        t("gui.pairTtl", sec=state["remainingSec"])))

    def manage_devices(self):
        """「已配对设备」窗口：看列表 + 解绑（单个或全部）。

        坑（用户可用性审查）：`PairingManager.list_devices()/forget_all()` 早已存在却是**死代码**
        —— 界面里没有入口，用户想撤销一个已配对设备（送修、换机、怀疑令牌泄露）只能自己去猜
        `pairing.json` 的路径删文件。这里把它接上。
        """
        if self.pairing is None:
            return
        items = self.pairing.list_devices()
        win = self.tk.Toplevel(self.root)
        win.title(t("gui.devicesTitle"))
        win.transient(self.root)
        win.resizable(False, False)
        frame = self.ttk.Frame(win, padding=(12, 10, 12, 10))
        frame.grid(row=0, column=0, sticky="nsew")
        self.ttk.Label(frame, text=t("gui.devicesHint"), style="Hint.TLabel",
                       wraplength=420, justify="left").grid(row=0, column=0, columnspan=2,
                                                            sticky="w")
        box = self.tk.Listbox(frame, height=max(3, min(8, len(items) or 3)), width=50,
                              activestyle="none", font=self.font_mono_small, relief="flat",
                              highlightthickness=1, highlightbackground="#e5e7eb",
                              selectmode="extended")
        box.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(8, 6))
        digests = []
        for entry in items:
            created = int(entry.get("created") or 0)
            when = (time.strftime("%Y-%m-%d %H:%M", time.localtime(created))
                    if created else t("gui.deviceWhenUnknown"))
            box.insert("end", "{}  ·  {}".format(entry.get("label") or "browser",
                                                 t("gui.deviceWhen", when=when)))
            digests.append(entry.get("digest"))
        if not items:
            box.insert("end", t("gui.devicesEmpty"))

        #: 「全部解绑」的二次确认状态（闭包要用，必须先定义）
        confirm_all = {"armed": False}
        all_button = self.ttk.Button(frame, text=t("gui.deviceUnbindAll"), command=None)

        def report(count):
            if count:
                logging.getLogger("easysub-helper").info(t("gui.devicesUnbound", count=count))
            self._shown_devices = None
            self._refresh()

        def unbind_selected():
            selected = sorted(box.curselection(), reverse=True)
            if not selected:
                log = logging.getLogger("easysub-helper")
                log.info(t("gui.deviceSelectFirst"))     # 静默 return 看起来像按钮坏了
                return
            removed = 0
            for index in selected:
                digest = digests[index] if index < len(digests) else None
                if digest and self.pairing.forget(digest):
                    removed += 1
                box.delete(index)
                if index < len(digests):
                    del digests[index]
            if not digests:
                box.delete(0, "end")
                box.insert("end", t("gui.devicesEmpty"))
            report(removed)

        def unbind_all():
            # 二次确认：这个按钮一按就废掉所有页面（独立审查：缺护栏）
            if not confirm_all["armed"]:
                confirm_all["armed"] = True
                all_button.configure(text=t("gui.deviceUnbindAllConfirm"))
                return
            removed = self.pairing.forget_all()
            box.delete(0, "end")
            del digests[:]
            box.insert("end", t("gui.devicesEmpty"))
            report(removed)

        self.ttk.Button(frame, text=t("gui.deviceUnbind"), command=unbind_selected).grid(
            row=2, column=0, sticky="w")
        all_button.configure(command=unbind_all)     # 见上面 confirm_all 的说明
        all_button.grid(row=2, column=1, sticky="e")
        self.ttk.Button(frame, text=t("gui.close"), command=win.destroy).grid(
            row=3, column=0, columnspan=2, sticky="e", pady=(8, 0))
        win.bind("<Escape>", lambda _e: win.destroy())

    def _refresh_log(self):
        lines, total = self.log_handler.snapshot()
        pending = total - self._log_rendered
        if pending <= 0:
            return
        if pending > len(lines):       # 老行被 deque 挤掉了：整体重画
            self.log_text.configure(state="normal")
            self.log_text.delete("1.0", "end")
            self._log_rendered = total - len(lines)
            pending = len(lines)
        chunk = lines[len(lines) - pending:]
        self.log_text.configure(state="normal")
        self.log_text.insert("end", "\n".join(chunk) + "\n")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")
        self._log_rendered = total

    # ---------------- 语言 ----------------
    def switch_language(self):
        """切换界面语言：就地重译 + 记住选择（下一次启动仍用它）。"""
        target = other_language()
        set_language(target)
        settings = config.load_settings()
        settings["lang"] = target
        config.save_settings(settings)
        self.retranslate()
        logging.getLogger("easysub-helper").info(
            t("log.langSwitched", lang=language_label(target)))

    def retranslate(self):
        """把窗口里**每一个**控件文案重新取一遍（i18n 的机械保证：这里不许硬编码）。"""
        self.root.title(t("gui.title"))
        self.title_label.configure(text=t("gui.title"))
        self.subtitle_label.configure(
            text=t("gui.subtitleMac") if sys.platform == "darwin" else t("gui.subtitle"))
        self.lang_button.configure(text=language_label(other_language()))
        self.keep_open_label.configure(text=t("gui.keepOpen"))
        self.license_label.configure(text=t("gui.licenseSource"))
        self.source_field_label.configure(text=t("gui.sourceLabel"))
        self.device_field_label.configure(text=t("gui.deviceLabel"))
        self.level_field_label.configure(text=t("gui.levelLabel"))
        self.quit_button.configure(text=t("gui.quit"))
        self.tray_box.configure(text=t("gui.minimizeToTray"))
        if getattr(self, "tray_hint", None) is not None:
            self.tray_hint.configure(text=t("gui.trayUnavailable"))
        if self._tray is not None:
            self._tray.refresh()
        self.pair_frame.configure(text=t("gui.pairTitle"))
        self.pair_hint.configure(text=t("gui.pairHint"))
        self.copy_button.configure(text=t("gui.pairCopy"))
        self.new_button.configure(text=t("gui.pairNew"))
        self.log_frame.configure(text=t("gui.logLabel"))
        self.devices_label.configure(text=t("gui.devicesCount",
                                           count=getattr(self, "_shown_devices", 0)))
        self.devices_button.configure(text=t("gui.devicesManage"))
        self._shown_ttl = None          # 强制下一帧重建配对框标题（锁定提示也要跟着换语言）
        # 音源下拉的选项名也要跟着翻译（保持当前选择）
        index = self.source_box.current()
        self.source_box["values"] = [source_label(s) for s in SOURCE_ORDER]
        self.source_box.current(index if index >= 0 else 0)
        self._sync_device_picker()
        self._reload_devices()          # 「系统默认」这一项的名字也要跟着语言变
        # 强制下一帧重建：配对框标题（含 TTL）、启动/暂停按钮、电平图占位文字
        self._shown_ttl = None
        self._toggle_label = None
        self._drawn_state = None
        self._refresh()

    # ---------------- 用户操作 ----------------
    def _current_source(self):
        index = self.source_box.current()
        if index < 0 or index >= len(SOURCE_ORDER):
            return SOURCE_ORDER[0]
        return SOURCE_ORDER[index]

    def _on_source_change(self, _event=None):
        source = self._current_source()
        logging.getLogger("easysub-helper").info(t("log.switchSource", source=source))
        self._submit(self.server.switch_source(source))
        self._sync_device_picker(source)
        # 设备名与音源绑定（麦克风名 ≠ monitor 名）：换音源要把设备列表跟着换掉
        self._reload_devices(source)

    def _sync_device_picker(self, source=None):
        """设备下拉只在**麦克风**音源下出现。

        系统音频刻意不给选择：各平台实现不同（Windows WASAPI loopback / Linux PulseAudio
        monitor / macOS CoreAudio tap），用户能选的"来源"其实只有"默认输出的环回"一个概念，
        列出多个 monitor 只会让人困惑（用户明确要求过）。
        """
        source = source or self._current_source()
        if source == "mic":
            self.device_field_label.grid()
            self.device_box.grid()
        else:
            self.device_field_label.grid_remove()
            self.device_box.grid_remove()

    def _reload_devices(self, source=None):
        """枚举设备可能耗时（soundcard 要问音频服务），所以放线程里；结果回主线程填下拉。"""
        source = source or self._current_source()
        self._device_generation += 1
        generation = self._device_generation

        def worker():
            try:
                items = devices.list_devices(source)
            except Exception:  # noqa: BLE001 - 枚举失败就只留「系统默认」
                items = [devices.default_entry(source)]
            self._device_results.put((generation, items))

        threading.Thread(target=worker, name="easysub-helper-devices", daemon=True).start()

    def _drain_device_results(self):
        """主线程取走枚举结果（见 _device_results 的说明）。"""
        while True:
            try:
                generation, items = self._device_results.get_nowait()
            except queue.Empty:
                return
            self._apply_devices(generation, items)

    def _apply_devices(self, generation, items):
        if self._closing or generation != self._device_generation:
            return          # 已经有更新的一次枚举在跑，旧结果丢掉
        self._device_items = items
        self.device_box["values"] = [item["label"] for item in items]
        current = self.server.device or ""
        index = 0
        for i, item in enumerate(items):
            if item["id"] == current:
                index = i
                break
        self.device_box.current(index)

    def _on_device_change(self, _event=None):
        index = self.device_box.current()
        if index < 0 or index >= len(self._device_items):
            return
        item = self._device_items[index]
        logging.getLogger("easysub-helper").info(t("log.switchDevice", device=item["label"]))
        future = self._submit(self.server.switch_device(item["id"] or None))
        if future is not None:
            future.add_done_callback(self._device_switch_done)

    def _device_switch_done(self, future):
        """换设备失败（服务端已退回原设备）→ 让下拉也退回服务端的真实状态。

        这个回调跑在**服务线程的事件循环**里，绝不能碰 tkinter（连 `after` 都不行），
        所以只置标记，由主线程的轮询去重填。
        """
        try:
            ok, error = future.result()
        except Exception:  # noqa: BLE001
            return
        if not ok:
            if error:
                logging.getLogger("easysub-helper").warning(error)
            self._device_reload_pending = True

    def toggle_capture(self):
        """启动/暂停：只提交意图，按钮文案与状态以服务端 snapshot 为准。"""
        if self._closing:
            return
        self.toggle_button.state(["disabled"])
        self._submit(self.server.set_user_enabled(not self.server.user_on))

    def copy_code(self):
        if not self._shown_code:
            return
        code = format_code(self._shown_code)
        try:
            self.root.clipboard_clear()
            self.root.clipboard_append(code)
        except Exception:  # noqa: BLE001 - 某些 Linux 无剪贴板管理进程
            return
        self.copy_button.configure(text=t("gui.pairCopied"))
        self._copy_job = self.root.after(1600, self._restore_copy_label)

    def _restore_copy_label(self):
        if not self._closing:
            self.copy_button.configure(text=t("gui.pairCopy"))

    def new_code(self):
        if self.pairing is None:
            return
        code = self.pairing.new_code()
        self._shown_code = None
        self._shown_ttl = None
        self.pair_frame.configure(text=t("gui.pairTitle"))
        logging.getLogger("easysub-helper").info(t("log.newCode", code=format_code(code)))
        self._refresh_pair_code()

    # ---------------- 托盘（可选：装了 pystray 才有） ----------------
    def _on_tray_toggle(self):
        want = bool(self.tray_var.get())
        config.set_setting("minimize_to_tray", want)     # 记住选择：别每次重启都回到默认
        if want and not self._tray:
            self._tray = tray.Tray(
                icon_path=os.path.join(assets_dir(), "icon128.png"),
                on_show=self.show_window,
                on_toggle=self.toggle_capture,
                on_quit=self.quit,
                is_capturing=lambda: bool(self.server and self.server.user_on),
                actions=self._tray_actions,
            )
            if not self._tray.start():
                self._tray = None
                self.tray_var.set(False)
                self._show_tray_hint(tray.unavailable_reason())
                return
            # 坑（独立审查 B1）：start() 返回 True **不代表图标真的起来了**（run() 里的
            # 异常没人接）。所以要等就绪回调；没就绪就把勾去掉并说明，绝不能"勾上了但没图标"
            # —— 那样 on_close 会把窗口收走，用户找不回窗口也退不出。
            self.root.after(1500, self._verify_tray)
        elif not want and self._tray:
            self._tray.stop()
            self._tray = None

    def _verify_tray(self):
        """托盘真的就绪了吗？没就绪就退回去（宁可没有托盘，也不能有隐形进程）。"""
        if self._tray is None:
            return
        if self._tray.ready():
            logging.getLogger("easysub-helper").info(t("log.trayReady"))
            return
        reason = self._tray.failed() or "not ready"
        self._tray.stop()
        self._tray = None
        self.tray_var.set(False)
        config.set_setting("minimize_to_tray", False)
        self._show_tray_hint(tray.unavailable_reason())
        logging.getLogger("easysub-helper").warning(t("log.trayFailed", error=reason))

    def _show_tray_hint(self, text):
        if getattr(self, "tray_hint", None) is not None:
            self.tray_hint.configure(text=text)
            self.tray_hint.grid()
        else:
            logging.getLogger("easysub-helper").warning(text)

    def _drain_tray_actions(self):
        """托盘菜单回调排在这里执行（pystray 线程绝不碰 tkinter）。"""
        while True:
            try:
                func = self._tray_actions.get_nowait()
            except queue.Empty:
                return
            try:
                func()
            except Exception as exc:                 # noqa: BLE001
                logging.getLogger("easysub-helper").warning("tray action failed: %s", exc)

    def on_close(self):
        """点窗口的 × —— 勾了「最小化到托盘」**且托盘确实就绪**时，只收窗口、服务继续。

        坑（独立审查 B1）：如果托盘没就绪就把窗口收走，用户会得到一个"没有窗口、没有图标、
        服务还在跑"的隐形进程 —— 既叫不回窗口也退不出。所以这里必须同时要求 `ready()`。
        """
        if self._tray is not None and self._tray.ready() and bool(self.tray_var.get()):
            self.root.withdraw()
            logging.getLogger("easysub-helper").info(t("log.minimizedToTray"))
            return
        self.quit()

    def show_window(self):
        try:
            self.root.deiconify()
            self.root.lift()
            self.root.focus_force()
        except Exception:                            # noqa: BLE001
            pass

    def quit(self):
        if self._closing:
            return
        self._closing = True
        for attr in ("_poll_job", "_copy_job"):
            job = getattr(self, attr, None)
            if job is None:
                continue
            try:
                self.root.after_cancel(job)
            except Exception:  # noqa: BLE001
                pass
            setattr(self, attr, None)
        loop = self._loop
        if loop is not None:
            try:
                loop.call_soon_threadsafe(loop.stop)
            except RuntimeError:
                pass
        if self._tray is not None:
            self._tray.stop()
            self._tray = None
        try:
            logging.getLogger().removeHandler(self.log_handler)
        except Exception:  # noqa: BLE001
            pass
        try:
            self.root.destroy()
        except Exception:  # noqa: BLE001
            pass

    def _start_tray_if_wanted(self):
        """按用户上次的选择把托盘拉起来（勾过一次就该自动生效）。"""
        if getattr(self, "_tray_wanted", False) and self._tray is None:
            self._on_tray_toggle()

    def mainloop(self):
        self.root.mainloop()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=5.0)   # 守护线程：万一设备卡住也不拦着进程退出
        return 0 if not self._boot_error else 1


def run(server, pairing):
    """起窗口并阻塞到用户退出；返回 None 表示"没有可用图形界面，请回落命令行"。"""
    if tkinter_module() is None:
        return None
    window = HelperWindow(server, pairing)
    window._start_tray_if_wanted()      # 上次勾过"关窗最小化到托盘"就自动拉起
    return window.mainloop()


if __name__ == "__main__":
    # 直接跑本文件 = 走同一条入口（默认就是弹窗口，参数原样转发）
    from .cli import main

    sys.exit(main(sys.argv[1:]))
