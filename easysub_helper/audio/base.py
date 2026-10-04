# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 hcz1017
"""采集后端接口与采集会话（线程 + 定长出块）。

线程模型：采集后端都是**阻塞式读**（soundcard 的 record、parec 的管道读），所以放在
独立线程里跑；上层（aiohttp 事件循环）通过回调拿到 PCM，再由调用方用
`loop.call_soon_threadsafe` 送进 asyncio 队列（见 net/server.py 的 CaptureBridge）。

出块契约：恒定 `target_rate` / `frame_ms` / 单声道 f32 —— 与 protocol.py 的声明一致，
前端因此不需要做任何缓冲或重排。
"""


import logging
import threading
import time

import numpy as np

from .resample import ResampleUnavailable, create_resampler
from .. import config
from ..i18n import t

LOG = logging.getLogger("easysub-helper")


# ---------------- Windows / COM ----------------
#: COM HRESULT（0x80010106 = RPC_E_CHANGED_MODE）
_COM_S_OK = 0
_COM_S_FALSE = 1
_COM_RPC_E_CHANGED_MODE = -2147417850


def com_initialize():
    """在**当前线程**初始化 COM；返回 True 表示"这次真的初始化了"（须在同线程配对
    `com_uninitialize()`）。

    为什么必须自己做：Windows 上 soundcard 的 `_com` 是**模块级单例**，它只在 import 它的
    那个线程上调用过 `CoInitializeEx`。而我们的**读设备**跑在独立采集线程里、**设备枚举**跑在
    GUI 的工作线程里 —— 这两个线程都没有 COM 公寓，于是 WASAPI 调用一律失败。用户实测的
    报错就是这个：`采集故障：Error 0x800401f0`（CO_E_NOTINITIALIZED，"CoInitialize 没被调用"）。

    用 MTA（`COINIT_MULTITHREADED`，与 soundcard 在非 Win8 上的选择一致）。线程已有公寓时
    返回 S_FALSE(1)；已有的是 STA 则返回 RPC_E_CHANGED_MODE(0x80010106) —— 两者都表示
    "COM 可用"，但都不是本次初始化的，所以返回 False（不该配对 Uninitialize）。
    """
    if not config.is_windows():
        return False
    try:
        import ctypes

        hr = int(ctypes.windll.ole32.CoInitializeEx(None, 0x0))   # COINIT_MULTITHREADED
    except Exception:  # noqa: BLE001 - 非 Windows 上没有 windll；真失败就当没初始化
        return False
    return hr == _COM_S_OK


def com_uninitialize():
    """与 `com_initialize()` 配对（**必须在同一个线程**里调用，且只有它返回过 True 才该调）。"""
    if not config.is_windows():
        return
    try:
        import ctypes

        ctypes.windll.ole32.CoUninitialize()
    except Exception:  # noqa: BLE001
        pass


class BackendError(RuntimeError):
    """采集后端故障（设备消失、进程退出、参数不被支持）。"""


class AudioBackend(object):
    """采集后端。子类实现 open/read/close 与 native_rate。

    read(frames) 返回**单声道 float32 一维数组**（多声道由后端自己下混），返回值可以少于
    请求帧数（管道按到达量给），上层会累积到定长再出块。三种返回值语义（上层据此区分
    "还没到"与"结束了"）：
      - 数组（可短于 frames）：本次拿到的样本
      - **None**：此刻没有数据（如基于超时生成器的后端在窗口内没给块）——不是故障
      - 空数组：采集流已结束（EOF）——按故障上报
    """

    name = "base"

    def prepare_thread(self):
        """采集线程启动钩子（默认什么都不做）。

        在**即将 open() 的那个线程**上调用 —— Windows/WASAPI 必须在这里初始化 COM，
        因为 soundcard 的模块级单例只覆盖它自己被 import 时所在的那个线程。
        """

    def cleanup_thread(self):
        """采集线程收尾钩子（默认什么都不做），在 close() 之后、线程退出之前调用。"""

    def __init__(self, source="system", device=None, rate=16000, frame_ms=20):
        self.source = source
        self.device = device
        self.rate = int(rate)
        self.frame_ms = int(frame_ms)
        self._native_rate = int(rate)
        self._opened = False

    @property
    def native_rate(self):
        return self._native_rate

    @property
    def opened(self):
        return self._opened

    # --- 子类实现 ---
    def open(self):
        raise NotImplementedError

    def read(self, frames):
        raise NotImplementedError

    def close(self):
        raise NotImplementedError

    # --- 共用 ---
    def describe(self):
        return {
            "backend": self.name,
            "source": self.source,
            "device": self.device,
            "nativeRate": self.native_rate,
        }


class CaptureSession(object):
    """一次采集会话：打开后端 → 定长重采样出块 → 回调。

    on_frame(bytes)  —— 16k 单声道 f32le 裸 PCM，长度恒为 target_rate*frame_ms/1000*4 字节
    on_level(dict)   —— {"rms":..,"peak":..}，约每 level_interval_ms 一次
    on_error(exc)    —— 采集故障（停止过程中的正常退出不会回调）
    """

    def __init__(self, backend, target_rate, frame_ms, on_frame, on_level=None, on_error=None,
                 level_interval_ms=100):
        self.backend = backend
        self.target_rate = int(target_rate)
        self.frame_ms = int(frame_ms)
        self.on_frame = on_frame
        self.on_level = on_level
        self.on_error = on_error
        self.level_interval_ms = int(level_interval_ms)

        self._thread = None
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._running = False
        self._error = None
        self._resampler = None
        self.frames_sent = 0
        self.samples_sent = 0
        self.started_at = 0.0
        self._level_last = 0.0
        self._level_sum_sq = 0.0
        self._level_peak = 0.0
        self._level_count = 0

    # --- 生命周期 ---
    @property
    def running(self):
        return self._running

    @property
    def error(self):
        return self._error

    @property
    def resampler_name(self):
        return self._resampler.name if self._resampler is not None else None

    def start(self):
        with self._lock:
            if self._running:
                return False
            self._stop.clear()
            self._error = None
            self._thread = threading.Thread(target=self._run, name="easysub-capture", daemon=True)
            self._running = True
            self._thread.start()
            return True

    def stop(self, timeout=2.0):
        with self._lock:
            if not self._running and self._thread is None:
                return
            self._stop.set()
            thread = self._thread
        # 先给线程一个自然结束的机会（正常时它读到的下一块就退出循环）
        if thread is not None:
            thread.join(0.3)
            if thread.is_alive():
                # 阻塞在 read() 里：关掉后端把读操作顶开（parec 会被 kill，soundcard 会释放设备）
                try:
                    self.backend.close()
                except Exception:  # noqa: BLE001 - 停止路径上的异常不向外抛
                    pass
                thread.join(timeout)
                if thread.is_alive():
                    # 阻塞在 read() 里没被顶开：设备可能仍被占用，而快照马上会报 capturing=false ——
                    # 不留一条日志的话现场就"消失"了（独立审查指出）
                    LOG.warning(t("log.stopTimeout"))
        with self._lock:
            self._thread = None
            self._running = False

    # --- 线程主体 ---
    def _run(self):
        try:
            self.backend.prepare_thread()
            self.backend.open()
            in_rate = int(self.backend.native_rate)
            self._resampler = create_resampler(in_rate, self.target_rate)
            in_block = max(1, int(round(in_rate * self.frame_ms / 1000.0)))
            out_block = max(1, int(round(self.target_rate * self.frame_ms / 1000.0)))
            pending_in = np.zeros(0, dtype=np.float32)
            pending_out = np.zeros(0, dtype=np.float32)
            self.started_at = time.time()
            self._level_last = self.started_at
            while not self._stop.is_set():
                chunk = self.backend.read(in_block)
                if chunk is None:
                    # 后端此刻没数据（不是故障）：小睡再轮询，别把 CPU 打满
                    if self._stop.wait(0.005):
                        break
                    continue
                if len(chunk) == 0:
                    raise BackendError(t("audio.err.streamEnded"))
                chunk = np.asarray(chunk, dtype=np.float32).reshape(-1)
                pending_in = np.concatenate((pending_in, chunk)) if pending_in.size else chunk
                while pending_in.size >= in_block:
                    piece = pending_in[:in_block]
                    pending_in = pending_in[in_block:]
                    out = self._resampler.process(piece)
                    if out.size:
                        pending_out = np.concatenate((pending_out, out)) if pending_out.size else out
                while pending_out.size >= out_block:
                    frame = pending_out[:out_block]
                    pending_out = pending_out[out_block:]
                    self._emit(frame)
        except ResampleUnavailable as exc:
            self._fail(exc)
        except Exception as exc:  # noqa: BLE001 - 线程内的任何异常都必须上报，不能静默死掉
            self._fail(exc)
        finally:
            try:
                self.backend.close()
            except Exception:  # noqa: BLE001
                pass
            try:
                self.backend.cleanup_thread()
            except Exception:  # noqa: BLE001
                pass
            with self._lock:
                self._running = False

    def _emit(self, frame):
        self.frames_sent += 1
        self.samples_sent += int(frame.size)
        if self.on_frame is not None:
            try:
                self.on_frame(frame.tobytes())
            except Exception:  # noqa: BLE001 - 消费端异常不能拖死采集线程
                pass
        if self.on_level is not None:
            self._level_sum_sq += float(np.dot(frame, frame))
            self._level_count += int(frame.size)
            peak = float(np.max(np.abs(frame))) if frame.size else 0.0
            if peak > self._level_peak:
                self._level_peak = peak
            now = time.time()
            if (now - self._level_last) * 1000.0 >= self.level_interval_ms:
                count = self._level_count or 1
                rms = (self._level_sum_sq / count) ** 0.5
                try:
                    self.on_level({"rms": rms, "peak": self._level_peak})
                except Exception:  # noqa: BLE001
                    pass
                self._level_sum_sq = 0.0
                self._level_count = 0
                self._level_peak = 0.0
                self._level_last = now

    def _fail(self, exc):
        self._error = exc
        stopping = self._stop.is_set()
        if self.on_error is not None and not stopping:
            try:
                self.on_error(exc)
            except Exception:  # noqa: BLE001
                pass
