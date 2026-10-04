# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 hcz1017
"""soundcard 后端：Windows(WASAPI loopback) / macOS(CoreAudio) / Linux(PulseAudio monitor)。

为什么选 soundcard 作为跨平台首选：
  - 纯 CFFI 实现，**不需要编译器**（pyaudiowpatch 之类要 CPython 扩展/预编译 wheel，
    Win7 + py3.8 上常常没有可用 wheel）；
  - Windows 侧 `all_microphones(include_loopback=True)` 直接给出扬声器环回设备，
    WASAPI loopback 自 Vista 就在（不依赖声卡带不带 loopback 硬件）。
    见 https://learn.microsoft.com/en-us/windows/win32/coreaudio/loopback-recording

Linux 上它是**可选**后端：parec 子进程零依赖且更稳（见 subprocess_backend.py）。
"""


import sys

import numpy as np

from .base import AudioBackend, BackendError, com_initialize, com_uninitialize
from ..i18n import t

# 目标采样率打不开时的回落序列（WASAPI 共享模式受混音格式限制，硬要 16k 可能被拒）
FALLBACK_RATES = (48000, 44100, 16000)


def import_soundcard():
    """导入 soundcard（带一个必需的 argv 兜底，见下）。

    坑：soundcard 在 **import 期** 就建立 PulseAudio 上下文，并在 `_infer_program_name()`
    里读 `sys.argv[1][:30]`——没有第二个参数时直接 `IndexError`。
    `python -m easysub_helper`（无子命令）、将来 PyInstaller 打包的 GUI 启动、以及
    被别的程序 import 时都会踩。所以必须在 import 之前把 argv 补齐。
    """
    if len(sys.argv) < 2:
        sys.argv.append("easysub-helper")
    import soundcard  # noqa: WPS433 - 故意延迟到函数内，缺依赖时能干净降级

    return soundcard


def prepare_com_for_soundcard():
    """在本线程准备 COM —— **顺序很关键：先 import soundcard，再补我们自己的初始化**。

    为什么不能反：soundcard 的 `_COMLibrary` 是**模块级单例**，在 import 期就调用
    `CoInitializeEx(NULL, COINIT_MULTITHREADED)`，而它的 `check_error` **只把 S_OK(0) 当成功**。
    如果我们抢先初始化了 MTA，它拿到的就是 S_FALSE(1) → 直接抛
    `RuntimeError: Error 0x100000001`（本机实测复现，见 tests/test_windows_com.py）。
    所以顺序必须是：

      ① ``import soundcard``：首次 import 的那个线程由它自己完成 CoInitializeEx，返回 S_OK；
      ② 我们再 `com_initialize()`：同线程同模式会拿到 S_FALSE（**不是**我们初始化的 → 不配对释放）；
         "模块已经在别的线程 import 过"的线程（采集线程、GUI 枚举线程）这里会拿到 S_OK → 由我们释放。

    返回 True = 本线程这次由**我们**初始化了 COM，收尾时须调用 `com_uninitialize()`。
    """
    _module, com_taken = import_and_prepare_com()
    return com_taken


def import_and_prepare_com():
    """import soundcard 并按正确顺序准备本线程 COM；返回 ``(模块或 None, com_taken)``。

    顺序的理由见 `prepare_com_for_soundcard`。返回模块是为了让调用方**不要二次 import**
    （虽然只是 sys.modules 命中，但把"先 import 再初始化"这条纪律收在一处更不容易写错）。
    """
    module = None
    try:
        module = import_soundcard()
    except Exception:  # noqa: BLE001 - 没装/没有音频服务：留给调用方报明确的错
        pass
    return module, com_initialize()


class SoundcardBackend(AudioBackend):
    name = "soundcard"

    def __init__(self, source="system", device=None, rate=16000, frame_ms=20, exclusive=False):
        super().__init__(source=source, device=device, rate=rate, frame_ms=frame_ms)
        self.exclusive = bool(exclusive)
        self._mic = None
        self._rec = None
        #: 本线程的 COM 是不是**我们**初始化的（决定收尾时要不要 CoUninitialize）
        self._com_initialized = False

    def prepare_thread(self):
        """WASAPI 的 COM 必须在**要用它的线程**里初始化（顺序见 prepare_com_for_soundcard）。

        否则一去读设备就报 0x800401f0（CO_E_NOTINITIALIZED）—— 用户实测的
        `采集故障：Error 0x800401f0` 就是这个（soundcard 的 _com 只覆盖它被 import 的线程）。
        """
        self._com_initialized = prepare_com_for_soundcard()

    def cleanup_thread(self):
        if self._com_initialized:
            com_uninitialize()
            self._com_initialized = False

    def open(self):
        sc = import_soundcard()
        self._mic = self._pick(sc)
        last_err = None
        for rate in self._rates():
            try:
                rec = self._mic.recorder(samplerate=int(rate), channels=None, blocksize=None)
                rec.__enter__()
            except Exception as exc:  # noqa: BLE001 - 换下一个采样率继续试
                last_err = exc
                continue
            self._rec = rec
            self._native_rate = int(rate)
            self._opened = True
            self.device = self._mic.name
            return
        raise BackendError(t(
            "audio.err.openDevice",
            device=getattr(self._mic, "name", self.device),
            rates=list(self._rates()),
            error=last_err,
        ))

    def _rates(self):
        out = [int(self.rate)]
        for r in FALLBACK_RATES:
            if r not in out:
                out.append(r)
        return out

    def _pick(self, sc):
        if self.device:
            needle = self.device.lower()
            for mic in sc.all_microphones(include_loopback=True):
                # 同时匹配 name（描述）与 id（PulseAudio 源名）：Linux 上窗口给的是 pactl 源名
                if needle in (mic.name or "").lower() or needle in str(getattr(mic, "id", "")).lower():
                    return mic
            raise BackendError(t("audio.err.deviceNotFound", device=self.device))
        if self.source == "system":
            spk = sc.default_speaker()
            if spk is None:
                raise BackendError(t("audio.err.noSpeaker"))
            try:
                return sc.get_microphone(str(spk.name), include_loopback=True)
            except Exception as exc:  # noqa: BLE001
                raise BackendError(t("audio.err.loopbackFailed", device=spk.name, error=exc))
        mic = sc.default_microphone()
        if mic is None:
            raise BackendError(t("audio.err.noMic"))
        return mic

    def read(self, frames):
        if self._rec is None:
            raise BackendError(t("audio.err.notOpened"))
        data = self._rec.record(numframes=int(frames))
        if data is None:
            return np.zeros(0, dtype=np.float32)
        arr = np.asarray(data, dtype=np.float32)
        if arr.ndim == 2 and arr.shape[1] > 1:
            arr = arr.mean(axis=1)          # 下混到单声道（识别只要单声道）
        else:
            arr = arr.reshape(-1)
        return np.ascontiguousarray(arr, dtype=np.float32)

    def close(self):
        # close() 可能被**另一个线程**调用（CaptureSession.stop() 由服务线程执行、read 阻塞时
        # 顶不开就由调用方收尾），所以这里也确保当前线程有 COM 公寓；只释放本次自己拿到的。
        taken = com_initialize()
        try:
            rec, self._rec = self._rec, None
            if rec is not None:
                try:
                    rec.__exit__(None, None, None)
                except Exception:  # noqa: BLE001 - 释放设备失败不阻塞停止流程
                    pass
            self._opened = False
        finally:
            if taken:
                com_uninitialize()
