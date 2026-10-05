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

    def tearDown(self):
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
    def test_window_icon_is_loaded_from_assets(self):
        """窗口/任务栏图标必须来自 assets 里的 easysub 图标。

        钉两件事：① icon128.png 被加载成 PhotoImage；② 那个引用被留在 self._window_icon 上
        —— Tk 不持有 Python 对象的引用，被 GC 回收后图标会悄悄变回默认羽毛。
        当前 Tk 不支持 PNG 图标（老版本）时跳过，不误报。
        """
        path = os.path.join(gui.assets_dir(), "icon128.png")
        if not os.path.exists(path):
            self.skipTest("缺少 icon128.png")
        icon = getattr(self.window, "_window_icon", None)
        if icon is None:
            self.skipTest("当前 Tk 不支持 iconphoto(PNG)")
        self.assertGreater(int(icon.width()), 0)
        # 注意：`wm iconphoto` 没有"回读"形式（只接受 window + image…），不能拿它断言。
        # 这里钉的是"_window_icon 由 _set_window_icon 建出来并留在窗口对象上"——
        # 少掉这次调用或丢掉引用，属性就不存在/被 GC，这条立刻红。

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
        self.assertEqual(self.window.status_label.cget("text"),
                         i18n.t("gui.statusPaused", port=8799))
        self.assertEqual(self.window.toggle_button.cget("text"), i18n.t("gui.start"))

        # 注意：一定不能写成 lambda 里再调 self.server.snapshot()（那就是调用自己）
        self._stub_snapshot(running=True, paused=False, userOn=True, capturing=True,
                            source="system", backend="parec")
        self.window._refresh()
        self.assertEqual(self.window.status_label.cget("text"),
                         i18n.t("gui.captureOn", source=i18n.source_label("system")))
        self.assertEqual(self.window.toggle_button.cget("text"), i18n.t("gui.pause"))
        self.assertIn(i18n.t("gui.backend", backend="parec", rate=16000),
                      self.window.detail_label.cget("text"))

    def test_error_is_shown_instead_of_capture_state(self):
        self._stub_snapshot(running=True, paused=True, capturing=False, error="no device")
        self.window._refresh()
        self.assertEqual(self.window.status_label.cget("text"), i18n.t("gui.captureFailed"))
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
