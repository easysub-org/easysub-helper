# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 hcz1017
"""Windows/COM：必须在**真正使用 COM 的那个线程**里 CoInitializeEx。

背景（真机故障）：Windows 版一按「启动」就报 `采集故障：Error 0x800401f0`
（= CO_E_NOTINITIALIZED）。soundcard 的 `_com` 是模块级单例，只在 import 它的那个线程上初始化过
COM；而我们的**读设备**在独立采集线程里、**设备枚举**在 GUI 的工作线程里 —— 这两个线程都没有
COM 公寓，WASAPI 调用一律失败。

这些测试在 Linux 上跑，验证的是"编排正确 + 非 Windows 上绝不出事"；真正的 CoInitializeEx
只能靠 Windows 真机（用户会测）。
"""

import os
import sys
import types
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from easysub_helper import config  # noqa: E402
from easysub_helper.audio import base, devices, soundcard_backend  # noqa: E402


class ComHelperTest(unittest.TestCase):
    def test_no_op_off_windows(self):
        if config.is_windows():
            self.skipTest("这条是给非 Windows 用的")
        self.assertFalse(base.com_initialize(), "非 Windows 上不该声称初始化了 COM")
        base.com_uninitialize()          # 空操作，不该抛

    def test_missing_windll_is_not_fatal(self):
        """拿不到 ctypes.windll 时（交叉运行/异常环境）必须安静返回 False，不能崩。

        注意：真 Windows 上 `ctypes.windll` **是存在的**，所以这里必须**显式构造"缺失"**
        （把属性删掉）—— 否则本用例会在真 Windows 上翻车（真机上 CoInitializeEx 会返回 S_OK）。
        """
        import ctypes as real_ctypes

        had_windll = hasattr(real_ctypes, "windll")
        original_windll = getattr(real_ctypes, "windll", None)
        if had_windll:
            del real_ctypes.windll
        original_windows = config.is_windows
        config.is_windows = lambda: True

        def restore():
            if had_windll:
                real_ctypes.windll = original_windll
            config.is_windows = original_windows
        self.addCleanup(restore)

        try:
            result = base.com_initialize()
        finally:
            restore()
        self.assertIsInstance(result, bool)
        self.assertFalse(result, "没有 windll 时应安静地返回 False")
        base.com_uninitialize()

    def test_backend_hooks_initialize_and_release_on_this_thread(self):
        calls = []
        original_init, original_uninit = soundcard_backend.com_initialize, soundcard_backend.com_uninitialize
        original_import = soundcard_backend.import_soundcard
        soundcard_backend.com_initialize = lambda: (calls.append("init") or True)
        soundcard_backend.com_uninitialize = lambda: calls.append("uninit")
        soundcard_backend.import_soundcard = lambda: (calls.append("import") or object())
        self.addCleanup(lambda: (setattr(soundcard_backend, "com_initialize", original_init),
                                 setattr(soundcard_backend, "com_uninitialize", original_uninit),
                                 setattr(soundcard_backend, "import_soundcard", original_import)))

        backend = soundcard_backend.SoundcardBackend(source="mic")
        backend.prepare_thread()
        backend.cleanup_thread()
        self.assertEqual(calls, ["import", "init", "uninit"])

        # 线程本来就有公寓（CoInitializeEx 返回 S_FALSE/CHANGED_MODE）→ 不该由我们 Uninitialize
        calls[:] = []
        soundcard_backend.com_initialize = lambda: False
        other = soundcard_backend.SoundcardBackend(source="mic")
        other.prepare_thread()
        other.cleanup_thread()
        self.assertEqual(calls, ["import"], "不是我们初始化的，收尾时不能去动它")

    def test_device_enumeration_initializes_com_too(self):
        """设备枚举跑在 GUI 的工作线程里 → 同样要（按正确顺序）准备 COM，否则下拉是空的。"""
        calls = []
        original_init, original_uninit = soundcard_backend.com_initialize, soundcard_backend.com_uninitialize
        original_import = soundcard_backend.import_soundcard
        soundcard_backend.com_initialize = lambda: (calls.append("init") or True)
        soundcard_backend.com_uninitialize = lambda: calls.append("uninit")
        mic = types.SimpleNamespace(name="USB Mic", isloopback=False)
        soundcard_backend.import_soundcard = lambda: (calls.append("import") or types.SimpleNamespace(
            all_microphones=lambda include_loopback=False: [mic]))
        self.addCleanup(lambda: (setattr(soundcard_backend, "com_initialize", original_init),
                                 setattr(soundcard_backend, "com_uninitialize", original_uninit),
                                 setattr(soundcard_backend, "import_soundcard", original_import)))

        self.assertEqual(devices._soundcard_names("mic"), ["USB Mic"])
        self.assertEqual(calls, ["import", "init", "uninit"], "枚举前后必须成对准备/释放 COM")

    def test_soundcard_import_runs_before_our_com_initialize(self):
        """**顺序**回归：先 import soundcard，再补我们自己的 CoInitializeEx。

        顺序反了会怎样（已实测复现）：soundcard 的 `_COMLibrary` 在 import 期调
        `CoInitializeEx(MTA)`，而同线程同模式重复初始化返回 **S_FALSE(1)**，它的 check_error
        **只认 S_OK**，于是 import 直接抛 `RuntimeError: Error 0x100000001` —— 用户会从
        0x800401f0 变成 0x100000001（还是不能用）。
        """
        events = []
        original_import = soundcard_backend.import_soundcard
        original_init = soundcard_backend.com_initialize
        soundcard_backend.import_soundcard = lambda: (events.append("import") or object())
        soundcard_backend.com_initialize = lambda: (events.append("com_init") or False)
        self.addCleanup(lambda: (setattr(soundcard_backend, "import_soundcard", original_import),
                                 setattr(soundcard_backend, "com_initialize", original_init)))

        soundcard_backend.prepare_com_for_soundcard()
        self.assertEqual(events, ["import", "com_init"], "必须先 import soundcard 再初始化 COM")

    def test_enumeration_keeps_com_alive_while_reading_names(self):
        """`mic.name` / `isloopback` 也要公寓（内部会 CoCreateInstance）→ COM 不能提前释放。"""
        state = {"com": False}
        original_init, original_uninit = soundcard_backend.com_initialize, soundcard_backend.com_uninitialize
        original_import = soundcard_backend.import_soundcard
        soundcard_backend.com_initialize = lambda: (state.__setitem__("com", True) or True)
        soundcard_backend.com_uninitialize = lambda: state.__setitem__("com", False)

        class Mic(object):
            @property
            def name(self):
                if not state["com"]:
                    raise RuntimeError("Error 0x800401f0")   # 没公寓就是它
                return "USB Mic"

            @property
            def isloopback(self):
                if not state["com"]:
                    raise RuntimeError("Error 0x800401f0")
                return False

        soundcard_backend.import_soundcard = lambda: types.SimpleNamespace(
            all_microphones=lambda include_loopback=False: [Mic()])
        self.addCleanup(lambda: (setattr(soundcard_backend, "com_initialize", original_init),
                                 setattr(soundcard_backend, "com_uninitialize", original_uninit),
                                 setattr(soundcard_backend, "import_soundcard", original_import)))

        self.assertEqual(devices._soundcard_names("mic"), ["USB Mic"],
                         "读名字时 COM 必须还活着")
        self.assertFalse(state["com"], "读完要释放（这次是我们初始化的）")

    def test_windows_path_calls_coinitialize_with_mta(self):
        """在 Linux 上把 ctypes.windll 换成假的，跑一遍 Windows 分支：参数与调用序列都要对。"""
        import ctypes as real_ctypes

        calls = []

        class FakeOle32(object):
            def CoInitializeEx(self, _reserved, coinit):
                calls.append(("CoInitializeEx", coinit))
                return 0                     # S_OK

            def CoUninitialize(self):
                calls.append(("CoUninitialize", None))

        fake = types.SimpleNamespace(ole32=FakeOle32())
        had_windll = hasattr(real_ctypes, "windll")
        original_windll = getattr(real_ctypes, "windll", None)
        real_ctypes.windll = fake
        original_windows = config.is_windows
        config.is_windows = lambda: True
        self.addCleanup(self._restore(real_ctypes, had_windll, original_windll, config, original_windows))

        backend = soundcard_backend.SoundcardBackend(source="system")
        backend.prepare_thread()
        backend.cleanup_thread()

        self.assertEqual(calls, [("CoInitializeEx", 0), ("CoUninitialize", None)],
                         "必须用 COINIT_MULTITHREADED(0) 初始化，并在收尾配对释放")
        self.assertFalse(backend._com_initialized)

    def test_windows_path_tolerates_already_initialized(self):
        """线程已有公寓（返回 S_FALSE/CHANGED_MODE）→ 视为可用，但**不能**由我们 Uninitialize。"""
        import ctypes as real_ctypes

        calls = []

        class FakeOle32(object):
            def __init__(self, hr):
                self.hr = hr

            def CoInitializeEx(self, _reserved, coinit):
                calls.append("CoInitializeEx")
                return self.hr

            def CoUninitialize(self):
                calls.append("CoUninitialize")

        for hr in (1, -2147417850):          # S_FALSE / RPC_E_CHANGED_MODE
            calls[:] = []
            fake = types.SimpleNamespace(ole32=FakeOle32(hr))
            had_windll = hasattr(real_ctypes, "windll")
            original_windll = getattr(real_ctypes, "windll", None)
            real_ctypes.windll = fake
            original_windows = config.is_windows
            config.is_windows = lambda: True
            try:
                backend = soundcard_backend.SoundcardBackend(source="system")
                backend.prepare_thread()
                backend.cleanup_thread()
            finally:
                self._restore(real_ctypes, had_windll, original_windll, config, original_windows)()
            self.assertEqual(calls, ["CoInitializeEx"], "hr={} 时不该调用 CoUninitialize".format(hr))

    @staticmethod
    def _restore(ctypes_module, had_windll, original_windll, config_module, original_windows):
        def undo():
            if had_windll:
                ctypes_module.windll = original_windll
            else:
                try:
                    del ctypes_module.windll
                except AttributeError:
                    pass
            config_module.is_windows = original_windows
        return undo

    def test_default_hooks_are_no_ops(self):
        backend = base.AudioBackend()
        backend.prepare_thread()
        backend.cleanup_thread()          # 默认实现什么都不做，也不该抛


if __name__ == "__main__":
    unittest.main()
