# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 hcz1017
"""助手窗口的测试。

分两层（和采集测试同样的理由）：
  - 不需要显示器的部分（电平映射、日志缓冲、语言按钮、`--no-gui` 开关、
    三个入口文件能直接跑）**无条件**跑，CI 上也能过；
  - 真要建 tkinter 窗口的部分用 `EASYSUB_HELPER_GUI_TESTS=1` 打开（需要 DISPLAY/WAYLAND_DISPLAY）。

窗口用例刻意**不绑端口、不碰声卡**：
  - `_WindowWithoutServer` 连事件循环都不起，只验证控件树 + 刷新映射；
  - `_LoopOnlyWindow` 起事件循环但不监听端口，音频用假后端（见 test_server.FakeBackend），
    这样才能真的测"按「启动」→ 服务端开始采集"这条闭环。
"""

import asyncio
import logging
import gc
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from easysub_helper import config, gui, i18n
from easysub_helper.net import HelperServer
from easysub_helper.net import server as helper_server
from easysub_helper.pairing import PairingManager, format_code
from tests.test_server import accept_any_device, fake_backend_factory

GUI_TESTS = os.environ.get("EASYSUB_HELPER_GUI_TESTS") == "1"


def _make_server():
    return HelperServer(
        pairing=PairingManager(persist=False), port=8799, scan_ports=False, version="test",
    )


class LevelTest(unittest.TestCase):
    def test_silence_is_zero_and_full_scale_is_full(self):
        self.assertEqual(gui._level_percent(0.0, 0.0), 0.0)
        self.assertEqual(gui._level_percent(None, None), 0.0)
        self.assertEqual(gui._level_percent(1.0, 1.0), 100.0)
        self.assertEqual(gui._level_percent(2.0, 2.0), 100.0)   # 削波也不越界

    def test_minus_30_dbfs_lands_in_the_middle(self):
        value = gui._level_percent(10 ** (-30 / 20.0), 0.0)
        self.assertAlmostEqual(value, 50.0, places=3)

    def test_monotonic(self):
        values = [gui._level_percent(v, 0.0) for v in (0.0001, 0.001, 0.01, 0.1, 0.5)]
        self.assertEqual(values, sorted(values))
        self.assertTrue(all(0.0 <= v <= 100.0 for v in values))


class LogBufferTest(unittest.TestCase):
    def test_keeps_only_the_tail_but_counts_everything(self):
        handler = gui.LogBufferHandler(maxlen=3)
        handler.setFormatter(logging.Formatter("%(message)s"))
        log = logging.getLogger("easysub-helper-test")
        log.addHandler(handler)
        try:
            for i in range(5):
                log.warning("line-%d", i)
        finally:
            log.removeHandler(handler)
        lines, total = handler.snapshot()
        self.assertEqual(total, 5)
        self.assertEqual(lines, ["line-2", "line-3", "line-4"])

    def test_snapshot_is_a_copy(self):
        handler = gui.LogBufferHandler(maxlen=2)
        record = logging.LogRecord("x", logging.INFO, "f", 1, "hello", None, None)
        handler.emit(record)
        lines, _total = handler.snapshot()
        lines.append("mutated")
        self.assertEqual(handler.snapshot()[0], ["hello"])


class AvailabilityTest(unittest.TestCase):
    def test_available_is_a_bool(self):
        self.assertIsInstance(gui.available(), bool)

    def test_env_can_force_headless(self):
        old = os.environ.get("EASYSUB_HELPER_NO_GUI")
        os.environ["EASYSUB_HELPER_NO_GUI"] = "1"
        try:
            self.assertIsNone(gui.tkinter_module())
            self.assertFalse(gui.available())
        finally:
            if old is None:
                os.environ.pop("EASYSUB_HELPER_NO_GUI", None)
            else:
                os.environ["EASYSUB_HELPER_NO_GUI"] = old

    def test_install_log_buffer_is_idempotent(self):
        """入口和窗口都会装一次；装两次会让日志区里每行显示两遍。"""
        first = gui.install_log_buffer()
        try:
            self.assertIs(gui.install_log_buffer(), first)
            self.assertEqual(sum(isinstance(h, gui.LogBufferHandler)
                                 for h in logging.getLogger().handlers), 1)
        finally:
            logging.getLogger().removeHandler(first)


class OtherLanguageTest(unittest.TestCase):
    def test_other_language_toggles_between_the_two_catalogs(self):
        old = i18n.get_language()
        try:
            i18n.set_language("zh_CN")
            self.assertEqual(gui.other_language(), "en")
            i18n.set_language("en")
            self.assertEqual(gui.other_language(), "zh_CN")
        finally:
            i18n.set_language(old)


class AssetTest(unittest.TestCase):
    def test_logo_used_by_the_window_exists(self):
        for name in ("icon128.png", "icon48.png", "logo.jpg"):
            self.assertTrue(os.path.isfile(os.path.join(gui.assets_dir(), name)), name)


class ParserTest(unittest.TestCase):
    def test_flags_the_window_mode_cares_about(self):
        from easysub_helper.cli import build_parser

        parser = build_parser()
        self.assertFalse(parser.parse_args([]).no_gui)
        self.assertTrue(parser.parse_args(["--no-gui"]).no_gui)
        self.assertEqual(parser.parse_args(["--port", "9000"]).port, 9000)
        self.assertEqual(parser.parse_args(["--source", "mic"]).source, "mic")

    def test_running_entry_files_as_plain_scripts_works(self):
        """用户真的会 `python easysub_helper/cli.py` / `.../gui.py` 这么跑：没有包上下文时
        相对 import 必须能自己兜住（否则第一行就走不到 main）。"""
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        for relative in ("easysub_helper/cli.py", "easysub_helper/__main__.py", "easysub_helper/gui.py"):
            proc = subprocess.Popen([sys.executable, os.path.join(root, relative), "--version"],
                                    cwd=root, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            out, err = proc.communicate()
            self.assertEqual(proc.returncode, 0, err.decode("utf-8", "replace"))
            self.assertIn("easysub-helper", out.decode("utf-8", "replace"))


class _WindowWithoutServer(gui.HelperWindow):
    """不起服务线程：只验证控件树与刷新映射（不绑端口、不碰音频）。"""

    def _start_server_thread(self):
        self._thread = None


class _LoopOnlyWindow(gui.HelperWindow):
    """起事件循环但不监听端口：用来测"按按钮 → 服务端真的开/关采集"这条闭环。"""

    def _server_main(self):
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_forever()
        finally:
            # 退出时把还在飞的任务取消掉，否则 asyncio 会打印 "Task was destroyed but it is pending"
            getter = getattr(asyncio, "all_tasks", None)
            tasks = getter(self._loop) if getter is not None else asyncio.Task.all_tasks(self._loop)
            for task in tasks:
                task.cancel()
            try:
                self._loop.run_until_complete(self.server.stop())
            except Exception:  # noqa: BLE001
                pass
            self._loop.close()
            self._loop = None


#: 测试里被替换/摘掉的 Tk 图片必须**留住引用**。
#: 坑（CI 实测的 core dump，exit 134）：`PhotoImage` 一旦变成浮动对象，GC 可能在 **worker 线程**
#: 里回收它 → Tkinter 会在非主线程执行 `image delete` → `RuntimeError: main thread is not in
#: main loop`，紧接着 `Tcl_AsyncDelete: async handler deleted by the wrong thread` → **整个
#: GUI 作业 core dumped**（py3.8/ubuntu 上实测；py3.12 只是碰巧没触发）。真实代码里
#: `gui._set_window_icon` 早就"留引用来防 GC"，测试同样不能把引用丢掉。
_KEPT_TK_IMAGES = []


class WindowCase(unittest.TestCase):
    WINDOW_CLASS = _WindowWithoutServer
    FAKE_BACKEND = False

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        if self.FAKE_BACKEND:
            original = helper_server.create_backend
            helper_server.create_backend = fake_backend_factory(0.5)
            self.addCleanup(lambda: setattr(helper_server, "create_backend", original))
        self.server = _make_server()
        self.pairing = self.server.pairing
        self.code = self.pairing.new_code()
        self.window = self.WINDOW_CLASS(self.server, self.pairing)
        # **跑测试时不要在用户屏幕上弹真窗口**：在第一次 update（= 映射）之前 withdraw，
        # 窗口根本不会被映射出来。因此下面的断言一律不许依赖"已映射"（见 grid_info 那几处）。
        self.window.root.withdraw()
        self.window.root.update()

    def _stash_icon_reference(self):
        """把窗口当前持有的图标图片转移到模块级列表（见 `_KEPT_TK_IMAGES` 的注释）。

        测试需要"窗口手里没有旧图标"这个前提时用它，**不要**直接 `pop` 丢掉引用。
        """
        image = self.window.__dict__.pop("_window_icon", None)
        if image is not None:
            _KEPT_TK_IMAGES.append(image)

    def tearDown(self):
        # 兜底（两层）：先把窗口手里的图标转移到模块级列表（这样窗口对象被回收时也不会连带
        # 触发 Tk 图片的 __del__），再在**主线程**把其它浮动对象收干净 —— 都不能留到 worker
        # 线程里回收（见 _KEPT_TK_IMAGES 的注释：非主线程 `image delete` 会 core dump）。
        self._stash_icon_reference()
        gc.collect()
        try:
            self.window.quit()
        except Exception:  # noqa: BLE001
            pass

    def _stub_snapshot(self, **overrides):
        """把 snapshot 换成固定值：测"界面映射"时不需要真的起服务。"""
        real = self.server.snapshot

        def snapshot():
            snap = dict(real())
            snap.update(overrides)
            return snap

        self.server.snapshot = snapshot
        return real

    def _wait(self, predicate, timeout=3.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            self.window.root.update()
            if predicate():
                return True
            time.sleep(0.02)
        return False


@unittest.skipUnless(GUI_TESTS and gui.available(),
                     "需要 EASYSUB_HELPER_GUI_TESTS=1 且有显示环境")
class WindowTest(WindowCase):
    # ---- 窗口图标（用户反馈：默认是 Tk 的羽毛，要 easysub 的）----
    def _tk_supports_png(self):
        """Tk 8.6+ 才支持 PNG（iconphoto）。

        坑（CI 实测踩过）：**不要**用"创建一张 PhotoImage 试试"来探测 —— 临时图片对象被 GC 时
        Tkinter 会在非主线程调 `image delete`，直接 `Tcl_AsyncDelete: async handler deleted by
        the wrong thread` → core dumped（exit 134），整个 GUI 作业红。仓库里 `_logo()` 早就注明
        "PhotoImage 必须留引用"，探测也一样：查 patchlevel 就够了，不碰图片对象。
        """
        try:
            raw = str(self.window.root.tk.call("info", "patchlevel"))
            # 去掉补丁级里的非数字后缀（"8.7a1" / "8.6b1" 这类 beta 版本），否则 int() 会抛
            # 异常 → 被误判成"不支持 PNG"而 skip，反而掩盖回归（收口核验的 nit A）。
            digits = ["".join(ch for ch in part if ch.isdigit()) or "0"
                      for part in raw.split(".")[:2]]
            return tuple(int(d) for d in digits) >= (8, 6)
        except Exception:                                   # noqa: BLE001
            return False

    def test_windows_prefers_ico_and_does_not_stack_png(self):
        """Windows 上 ico 成功时刻意**不再**叠加 iconphoto（后者会再发一次 WM_SETICON 盖掉 .ico）。

        第四轮审查指出这个刻意行为此前没有测试守护：把叠加写回去不会让任何用例变红。
        这里用"假 is_windows + 假 iconbitmap"在真实 Tk root 上验分支（本机不是 Windows，
        只能验分支逻辑，验不了真实图标效果）。
        """
        from unittest import mock

        from easysub_helper import config as cfg

        calls = []
        self._stash_icon_reference()                             # 清掉构造时留下的 PNG 引用（留着别丢）
        with mock.patch.object(cfg, "is_windows", return_value=True), \
                mock.patch.object(self.window.root, "iconbitmap",
                                  side_effect=lambda *a, **k: calls.append((a, k))):
            self.window._set_window_icon()
        self.assertTrue(calls, "Windows 上应先试 iconbitmap(.ico)")
        # 断言真的把 easysub.ico 递过去了：调用形式可能是 iconbitmap(default=ico) 或 iconbitmap(ico)
        wanted = os.path.join(gui.assets_dir(), "easysub.ico")
        passed = [(a[0] if a else k.get("default")) for a, k in calls]
        self.assertIn(wanted, passed, "没把 easysub.ico 交给 iconbitmap")
        self.assertEqual(getattr(self.window, "_window_icon_kind", None), "ico")
        self.assertNotIn("_window_icon", self.window.__dict__,
                         "Windows 上 ico 成功后不该再叠加 iconphoto(PNG)")

    def test_detached_window_icon_keeps_a_python_reference(self):
        """从窗口摘下的 Tk 图片**必须留住引用**，不许裸 `pop` 丢掉。

        坑（CI 实测的 core dump，exit 134）：浮动 `PhotoImage` 被 GC 时若 GC 跑在 worker 线程里，
        Tkinter 会从非主线程执行 `image delete` → `Tcl_AsyncDelete: async handler deleted by the
        wrong thread` → 整个 GUI 作业 core dumped。py3.8/ubuntu 上真实发生过，就是被这条不变量
        的前身（裸 `pop`）触发的；py3.12 只是碰巧没触发。
        """
        before = len(_KEPT_TK_IMAGES)
        self._stash_icon_reference()
        self.assertEqual(len(_KEPT_TK_IMAGES), before + 1,
                         "★ 摘下的图标必须进 _KEPT_TK_IMAGES（留住引用），不能直接丢掉")
        self.assertNotIn("_window_icon", self.window.__dict__,
                         "摘下来之后窗口手里就不该再有这个引用（本用例的前提）")

    def test_window_icon_actually_calls_iconphoto(self):
        """必须**真的调用** iconphoto —— 只记状态不调用就是"图标没换上"。

        第五轮审查的残留假绿：`test_window_icon_is_loaded_from_assets` 只验证 Python 引用存在，
        把那行 `iconphoto` 调用删掉它照样绿。这条用 spy 把调用本身钉死。
        """
        from unittest import mock

        if config.is_windows():
            self.skipTest("Windows 走 iconbitmap(.ico)，不调用 iconphoto")
        if not self._tk_supports_png():
            self.skipTest("Tk < 8.6：不支持 PNG 图标（iconphoto 到不了）")
        self._stash_icon_reference()
        calls = []
        with mock.patch.object(self.window.root, "iconphoto",
                               side_effect=lambda *a, **k: calls.append((a, k))):
            self.window._set_window_icon()
        self.assertTrue(calls, "没有调用 iconphoto：窗口图标其实还是 Tk 默认羽毛")
        self.assertIs(calls[0][1].get("default") if calls[0][1] else calls[0][0][0], True,
                      "iconphoto 的第一个参数应为 default=True（让后续 Toplevel 也继承）")

    def test_window_icon_is_loaded_from_assets(self):
        """窗口/任务栏图标必须来自 assets 里的 easysub 图标。

        **按平台**断言实际走的分支（独立审查抓到：Windows 上 ico 成功时刻意不设 PNG，
        无条件要求 `_window_icon` 会让这条在 Windows 上假红）：
          * Windows → `_window_icon_kind == "ico"`（.ico 才是任务栏/Alt-Tab 认的那份）；
          * 其它平台 → `"png"` 且 PhotoImage 引用被留住（Tk 不持有 Python 引用，
            被 GC 回收后图标会悄悄变回默认羽毛）。
        只有**当前 Tk 真的不支持 PNG**（patchlevel < 8.6）时才跳过——不能因为"图标没设上"跳过，
        那正是这条要抓的回归。
        """
        path = os.path.join(gui.assets_dir(), "icon128.png")
        if not os.path.exists(path):
            self.skipTest("缺少 icon128.png")
        if not self._tk_supports_png():
            self.skipTest("Tk < 8.6：不支持 PNG 图标")
        # 注意：**不**在这里创建临时 PhotoImage 来探测 —— 见 _tk_supports_png 的坑注释。

        kind = getattr(self.window, "_window_icon_kind", None)
        if config.is_windows():
            # Windows 走 .ico；万一 iconbitmap 失败（老 Tcl/Tk）则回落 PNG，两者都算设上了
            self.assertIn(kind, ("ico", "png"),
                          "Windows 上必须设上窗口图标（.ico，或失败时回落 PNG）")
        else:
            self.assertEqual(kind, "png", "非 Windows 应走 iconphoto(PNG)")
            icon = getattr(self.window, "_window_icon", None)
            self.assertIsNotNone(icon, "_set_window_icon 没设上图标（窗口图标仍是 Tk 默认羽毛）")
            self.assertGreater(int(icon.width()), 0)

    # ---- 配对码（用户就是来抄这串字符的）----
    def test_pair_code_is_shown_in_the_window(self):
        self.window._refresh()
        self.assertEqual(self.window.code_label.cget("text"), format_code(self.code))

    def test_new_code_button_rotates_the_shown_code(self):
        self.window._refresh()
        self.window.new_code()
        rotated = self.window.code_label.cget("text")
        self.assertEqual(len(rotated.replace("-", "")), 6)
        self.assertEqual(rotated, format_code(self.pairing.state()["code"]))

    def test_copy_puts_the_code_on_the_clipboard(self):
        self.window._refresh()
        self.window.copy_code()
        self.window.root.update()
        self.assertEqual(self.window.root.clipboard_get(), format_code(self.code))

    # ---- 状态行与按钮文案 ----
    def test_status_line_maps_paused_and_capturing(self):
        self._stub_snapshot(running=True, paused=True, userOn=False, capturing=False, port=8799)
        self.window._refresh()
        paused_text = self.window.status_label.cget("text")
        self.assertEqual(paused_text, i18n.t("gui.statusPaused", port=8799))
        # 端口只能出现一次（statusPaused 自带端口，别再被 _status_with_port 追加一遍）
        self.assertEqual(paused_text.count(i18n.t("gui.statusPort", port=8799)), 1)
        self.assertEqual(self.window.toggle_button.cget("text"), i18n.t("gui.start"))

        # 注意：一定不能写成 lambda 里再调 self.server.snapshot()（那就是调用自己）
        self._stub_snapshot(running=True, paused=False, userOn=True, capturing=True,
                            source="system", backend="parec")
        self.window._refresh()
        # 采集态也带端口：页面离线框让用户"去助手状态行抄端口"，任何状态都得有
        self.assertEqual(self.window.status_label.cget("text"),
                         i18n.t("gui.captureOn", source=i18n.source_label("system"))
                         + "  ·  " + i18n.t("gui.statusPort", port=8799))
        self.assertEqual(self.window.toggle_button.cget("text"), i18n.t("gui.pause"))
        self.assertIn(i18n.t("gui.backend", backend="parec", rate=16000),
                      self.window.detail_label.cget("text"))

    def test_error_is_shown_instead_of_capture_state(self):
        self._stub_snapshot(running=True, paused=True, capturing=False, error="no device")
        self.window._refresh()
        # 采集中断时状态行还要带"下一步"（点「启动」重试）与端口（页面会指引用户来抄）
        shown = self.window.status_label.cget("text")
        self.assertTrue(shown.startswith(i18n.t("gui.captureFailed")), shown)
        self.assertIn(i18n.t("gui.captureFailedHint"), shown)
        self.assertIn(i18n.t("gui.statusPort", port=8799), shown)
        self.assertEqual(self.window.detail_label.cget("text"), "no device")

    def test_toggle_button_disabled_until_the_service_runs(self):
        self.window._refresh()
        self.assertIn("disabled", self.window.toggle_button.state())

    # ---- 电平图：暂停/采集/静音三态 ----
    def test_level_graph_starts_paused_with_a_placeholder(self):
        self.window._refresh()
        self.assertTrue(self.window._paused)
        self.assertEqual(len(self.window.level_history), 0)
        self.assertEqual(self.window._level_pct, 0.0)

    def test_level_graph_follows_server_levels(self):
        clock = [time.monotonic()]   # 用真实时钟基准：新鲜度判定是真的在跑
        scripted = [(0.02, 0.05), (0.2, 0.4), (0.5, 0.9)]
        state = {"i": 0}
        real = self.server.snapshot

        def fake():
            snap = dict(real())
            snap.update(capturing=True, paused=False, userOn=True,
                        levelRms=scripted[state["i"]][0], levelPeak=scripted[state["i"]][1],
                        levelAt=clock[0])
            return snap

        self.server.snapshot = fake
        for index in range(len(scripted)):
            state["i"] = index
            clock[0] += 0.1
            self.window._refresh()
        self.assertEqual(len(self.window.level_history), len(scripted))
        self.assertGreater(self.window._level_pct, 0.0)
        self.assertGreater(self.window._peak_hold, 0.0)
        # 同一个电平重复上报（levelAt 没变）不该多记一格
        self.window._refresh()
        self.assertEqual(len(self.window.level_history), len(scripted))

    def test_level_graph_clears_when_paused_again(self):
        real = self._stub_snapshot(capturing=True, paused=False, userOn=True, levelRms=0.3,
                                   levelPeak=0.6, levelAt=time.monotonic())
        self.window._refresh()
        self.assertEqual(len(self.window.level_history), 1)
        self.server.snapshot = real
        self.window._refresh()
        self.assertEqual(len(self.window.level_history), 0)
        self.assertEqual(self.window._level_pct, 0.0)
        self.assertEqual(self.window._peak_hold, 0.0)

    def test_level_graph_is_drawn_inside_the_canvas(self):
        """画布图元不许越界：坐标算错时柱子和电平表会画到控件外面（看不见=白画）。"""
        self._stub_snapshot(capturing=True, paused=False, userOn=True, levelRms=0.3,
                            levelPeak=0.6, levelAt=time.monotonic())
        canvas = self.window.level_canvas
        # 显式给尺寸，再用**请求尺寸**断言：测试跑在 withdraw 过的窗口上（不弹窗），
        # winfo_width() 恒为 1，不能用；而 _draw_level 在这种情形下正是按请求尺寸作画的。
        canvas.configure(width=400, height=120)
        self.window._refresh()
        self.window.root.update_idletasks()
        items = canvas.find_all()
        self.assertGreater(len(items), 60)          # 60 段电平表 + 波形/文字
        x0, y0, x1, y1 = canvas.bbox("all")
        # 横向允许 Tk 给线段算出的 ±2px 边界，纵向一格都不许溢出（溢出的刻度文字会被裁掉）
        self.assertGreaterEqual(x0, -3)
        self.assertLessEqual(x1, canvas.winfo_reqwidth() + 3)
        self.assertGreaterEqual(y0, 0)
        self.assertLessEqual(y1, canvas.winfo_reqheight())

    # ---- 设备下拉（只服务坐在本机的人；页面不需要选设备）----
    def test_pair_code_auto_rotates_after_expiry(self):
        """配对码过期后窗口必须**自己换一个**（不能让用户拿着死码反复输直到被锁）。"""
        import time as _time

        from easysub_helper.i18n import t

        mgr = self.window.pairing
        self.assertIsNotNone(mgr)
        old = mgr.new_code()
        mgr._code_expires = _time.time() - 1          # 人为过期
        self.window._refresh_pair_code()
        self.assertTrue(mgr.has_valid_code(), "过期后窗口应自动生成新码")
        self.assertNotEqual(mgr.state()["code"], old, "自动轮换必须是新码")
        self.assertEqual(self.window._shown_code, mgr.state()["code"])
        self.assertEqual(self.window.pair_hint.cget("text"), t("gui.pairHint"))

    def test_locked_hint_points_at_the_unlock_button(self):
        """被锁时必须提示"点「换一个」立即解锁"（真解就是它，但提示以前没说）。"""
        from easysub_helper.i18n import t

        mgr = self.window.pairing
        mgr.new_code()
        for _ in range(mgr.max_attempts):
            mgr.verify("WRONG1")
        self.assertGreater(mgr.state()["lockedForSec"], 0, "应当已被锁定")
        self.window._refresh_pair_code()
        text = self.window.pair_hint.cget("text")
        self.assertIn(t("gui.pairNew"), text, "锁定提示必须告诉用户点「换一个」")
        # 点「换一个」立即解锁（这是 pairing 的既有语义，界面现在把它说出来了）
        self.window.new_code()
        self.assertEqual(mgr.state()["lockedForSec"], 0)

    def test_devices_row_reflects_paired_count(self):
        from easysub_helper.i18n import t

        self.window.pairing.issue_token("chrome")
        self.window._shown_devices = None
        self.window._refresh_pair_code()
        self.assertEqual(self.window.devices_label.cget("text"), t("gui.devicesCount", count=1))

    def test_manage_devices_opens_and_unbinds(self):
        """「管理…」要能打开设备窗口；解绑后计数归零（以前只能手删 pairing.json）。"""
        mgr = self.window.pairing
        mgr.issue_token("chrome")
        self.window.manage_devices()
        self.window.root.update()
        toplevels = [w for w in self.window.root.winfo_children() if w.winfo_class() == "Toplevel"]
        self.assertTrue(toplevels, "应当弹出一个设备窗口")
        for win in toplevels:
            win.destroy()
        self.window.root.update()
        self.assertTrue(mgr.forget_all())          # 全部解绑走的是 pairing 的入口
        self.assertEqual(mgr.list_devices(), [])

    def test_close_does_not_hide_when_tray_is_not_ready(self):
        """坑（独立审查 B1）：托盘没就绪就把窗口收走 = 没有窗口、没有图标、服务还在跑的
        隐形进程 —— 用户既叫不回窗口也退不出。所以没就绪时必须**照旧退出**。"""

        class NotReadyTray(object):
            def ready(self):
                return False

            def stop(self):
                pass

        self.window._tray = NotReadyTray()
        self.window.tray_var.set(True)
        self.window.on_close()
        # 走的是真退出（quit → root.destroy），所以退出后再查窗口状态会抛 TclError
        self.assertTrue(self.window._closing, "托盘没就绪时绝不能把窗口藏起来")

    def test_tray_is_disabled_on_macos(self):
        """macOS 上 pystray 要求 run() 在主线程，而主线程被 tkinter 占着 → 宁可没有托盘，
        也不能给一个"勾上了、图标没出现、窗口却被收走"的隐形进程（独立审查 B1）。"""
        from unittest import mock

        from easysub_helper import tray as tray_mod

        with mock.patch.object(tray_mod.sys, "platform", "darwin"):
            self.assertFalse(tray_mod.available())
            self.assertIn("macOS", tray_mod.unavailable_reason())

    def test_tray_control_exists(self):
        """设置行要有「关窗最小化到托盘」勾选框；装不上 pystray 时禁用并说明原因。

        坑（复审 + 用户环境实测）：这条断言以前写死"必须 disabled"，等于依赖"本机没装 pystray"
        —— pystray 一装上（用户就是这样）测试就假红。现在按**实际可用性**断言契约：
        「不可用 ⇒ 禁用且有原因；可用 ⇒ 不禁用」。
        """
        from easysub_helper import tray as tray_mod
        from easysub_helper.i18n import t

        self.assertEqual(self.window.tray_box.cget("text"), t("gui.minimizeToTray"))
        state = str(self.window.tray_box.state())
        if tray_mod.available():
            self.assertNotIn("disabled", state)
        else:
            self.assertIn("disabled", state)
            self.assertTrue(self.window.tray_hint.cget("text"), "不可用时必须说明原因")

    def test_close_without_tray_quits(self):
        """没开托盘时，关窗仍然 = 退出（保持原有语义，不悄悄留一个后台进程）。"""
        self.window._tray = None
        self.window.tray_var.set(True)          # 勾了但托盘不可用 → 仍应退出
        self.window.on_close()
        self.assertTrue(self.window._closing, "关窗应当真的走退出")

    def test_close_with_tray_only_hides_the_window(self):
        """开了托盘时，关窗只收窗口：服务与采集继续（用户可用性审查的诉求）。"""
        class FakeTray(object):
            def __init__(self, ready=True):
                self._ready = ready

            def ready(self):
                return self._ready

            def stop(self):
                pass

            def refresh(self):
                pass

        self.window._tray = FakeTray()
        self.window.tray_var.set(True)
        self.window.on_close()
        self.assertFalse(self.window._closing, "收进托盘时不能退出服务")
        self.assertEqual(self.window.root.state(), "withdrawn")
        self.window.show_window()
        self.assertNotEqual(self.window.root.state(), "withdrawn")

    def test_device_list_is_filled_from_enumeration(self):
        original = gui.devices.list_devices
        gui.devices.list_devices = lambda source="mic": [
            {"id": "", "label": "系统默认", "kind": source, "default": True},
            {"id": "USB Mic", "label": "USB Mic", "kind": source, "default": False},
        ]
        self.addCleanup(lambda: setattr(gui.devices, "list_devices", original))
        self.window._reload_devices()
        self.assertTrue(self._wait(lambda: len(self.window.device_box["values"]) == 2),
                        "设备下拉没有被填上")
        self.assertEqual(list(self.window.device_box["values"]), ["系统默认", "USB Mic"])
        self.assertEqual(self.window.device_box.current(), 0)

    def test_device_list_falls_back_to_default_when_nothing_is_found(self):
        original = gui.devices.list_devices
        gui.devices.list_devices = lambda source="mic": [
            {"id": "", "label": "系统默认", "kind": source, "default": True},
        ]
        self.addCleanup(lambda: setattr(gui.devices, "list_devices", original))
        self.window._reload_devices()
        self.assertTrue(self._wait(lambda: len(self.window.device_box["values"]) == 1))
        self.assertEqual(self.window.device_box.current(), 0)

    def test_device_picker_is_hidden_for_system_audio(self):
        """系统音频不给设备选择：各平台实现不同，用户能选的只有"默认输出的环回"。"""
        self.window.source_box.current(0)          # 系统音频
        self.window._sync_device_picker("system")
        self.window.root.update_idletasks()
        # grid_remove() 之后 grid_info() 为空 —— 这比 winfo_ismapped() 更贴切：
        # 测试窗口是 withdraw 过的，"是否被布局管理"才是我们真正要断言的。
        self.assertEqual(self.window.device_box.grid_info(), {})
        self.assertEqual(self.window.device_field_label.grid_info(), {})

        self.window.source_box.current(1)          # 麦克风
        self.window._sync_device_picker("mic")
        self.window.root.update_idletasks()
        self.assertNotEqual(self.window.device_box.grid_info(), {})
        self.assertNotEqual(self.window.device_field_label.grid_info(), {})

    def test_backend_line_shows_the_device_the_backend_actually_opened(self):
        """状态行要显示后端**实际打开**的设备：切换到底生没生效，用户一眼能看出来。"""
        self._stub_snapshot(running=True, paused=False, userOn=True, capturing=True,
                            source="mic", backend="parec", openedDevice="alsa_input.usb.mic")
        self.window._refresh()
        self.assertIn("alsa_input.usb.mic", self.window.detail_label.cget("text"))

    # ---- 许可证（AGPL 第 13 条：界面里要能拿到对应源码）----
    def test_footer_shows_the_agpl_license_and_source_link(self):
        text = self.window.license_label.cget("text")
        self.assertIn("AGPL", text)
        self.assertIn("3.0", text)

    def test_source_link_opens_the_repository_url(self):
        original = gui.webbrowser.open
        opened = []
        gui.webbrowser.open = lambda url: opened.append(url) or True
        self.addCleanup(lambda: setattr(gui.webbrowser, "open", original))
        self.window.open_source()
        self.assertEqual(opened, [config.PROJECT_URL])

    # ---- 语言切换 ----
    def test_language_switch_retranslates_every_widget_and_persists(self):
        original_data_dir = config.data_dir
        config.data_dir = lambda: Path(self.tmp)
        self.addCleanup(lambda: setattr(config, "data_dir", original_data_dir))
        saved = i18n.get_language()
        self.addCleanup(lambda: i18n.set_language(saved))

        i18n.set_language("zh_CN")
        self.window.retranslate()
        before = self.window.title_label.cget("text")
        self.assertEqual(self.window.lang_button.cget("text"), "English")

        self.window.switch_language()
        self.assertEqual(i18n.get_language(), "en")
        self.assertNotEqual(self.window.title_label.cget("text"), before)
        self.assertEqual(self.window.title_label.cget("text"), "EasySub Helper")
        self.assertEqual(self.window.lang_button.cget("text"), "中文")
        # 按钮/提示/标题都得跟着变（不许漏控件）
        self.assertEqual(self.window.quit_button.cget("text"), "Quit")
        self.assertEqual(self.window.pair_frame.cget("text").split("  ·  ")[0], "Pair code")
        self.assertEqual(self.window.toggle_button.cget("text"), "Start")
        # 选择要落盘，下次启动仍用它
        self.assertEqual(config.load_settings().get("lang"), "en")


@unittest.skipUnless(GUI_TESTS and gui.available(),
                     "需要 EASYSUB_HELPER_GUI_TESTS=1 且有显示环境")
class ToggleTest(WindowCase):
    """按「启动」→ 服务端真的开始采集；再按 → 真的停（音频用假后端，不需要声卡）。"""

    WINDOW_CLASS = _LoopOnlyWindow
    FAKE_BACKEND = True

    def test_toggle_starts_and_pauses_capture(self):
        self.assertFalse(self.server.user_on)
        self.window._refresh()
        self.assertEqual(self.window.toggle_button.cget("text"), i18n.t("gui.start"))

        self.window.toggle_capture()
        self.assertTrue(self._wait(lambda: self.server.user_on), "「启动」没有传到服务端")
        self.assertTrue(self._wait(lambda: self.server.snapshot()["capturing"]))
        self.assertTrue(self._wait(lambda: len(self.window.level_history) > 0),
                        "启动后电平图应该开始动")
        self.window._refresh()
        self.assertEqual(self.window.toggle_button.cget("text"), i18n.t("gui.pause"))
        self.assertTrue(self.window._capturing)

        self.window.toggle_capture()
        self.assertTrue(self._wait(lambda: not self.server.user_on))
        self.assertTrue(self._wait(lambda: not self.server.snapshot()["capturing"]))
        self.window._refresh()
        self.assertEqual(self.window.toggle_button.cget("text"), i18n.t("gui.start"))
        self.assertEqual(len(self.window.level_history), 0)


    def test_device_picker_switches_the_capture_device(self):
        self.addCleanup(accept_any_device())
        self.assertTrue(self._wait(lambda: len(self.window.device_box["values"]) >= 1))
        self.window._apply_devices(
            self.window._device_generation,
            [{"id": "", "label": "系统默认", "kind": "system", "default": True},
             {"id": "Monitor of X", "label": "Monitor of X", "kind": "system", "default": False}],
        )
        self.window.device_box.current(1)
        self.window._on_device_change()
        self.assertTrue(self._wait(lambda: self.server.device == "Monitor of X"),
                        "选设备没有传到服务端")
        self.window.device_box.current(0)
        self.window._on_device_change()
        self.assertTrue(self._wait(lambda: self.server.device is None))


if __name__ == "__main__":
    unittest.main()
