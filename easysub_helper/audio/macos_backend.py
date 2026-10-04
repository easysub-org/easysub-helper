# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 hcz1017
"""macOS 系统音频后端：**免驱动优先**。

背景（这是 Python 生态里 2025 年才补齐的能力）：
  - `soundcard` 在 macOS 上只能看到**真实输入设备**，系统音频必须先把声音路由到 BlackHole
    这类虚拟声卡，再当普通输入设备录——用户要装驱动、配聚合设备，门槛高；
  - `pysysaudio`（PyPI，0.1.3+）用 ScreenCaptureKit / Core Audio process tap，macOS 14.2+
    **不需要任何虚拟声卡**，只要一次「屏幕与系统音频录制」授权；
  - 更底层的 `catap`（py3.11+，纯 Python wheel）可按进程抓，但 API 更原始，这里不用。

所以策略是：system 音源优先 pysysaudio，失败（版本太老/未安装/授权被拒）才回落到 soundcard
（此时需要用户自己装了 BlackHole 之类的 loopback 输入设备）。mac 之外的平台不走这个后端。

注意：本文件在 Linux/Windows 上无法真机验证，代码刻意保持"探测失败就给清晰错误 + 回落"，
不留静默路径。
"""

import logging

import numpy as np

from ..i18n import t
from .base import AudioBackend, BackendError
from .soundcard_backend import SoundcardBackend

LOG = logging.getLogger("easysub-helper")


class PysysaudioCapture(object):
    """把 pysysaudio 的"超时生成器"适配成我们的 read(frames) 契约。"""

    name = "pysysaudio"

    def __init__(self, rate, frame_ms):
        self.rate = int(rate)
        self.frame_ms = int(frame_ms)
        self.device = "system audio (pysysaudio)"
        self._rec = None
        self._gen = None

    @property
    def native_rate(self):
        # pysysaudio 自己会重采样/混音到我们要求的采样率与声道数
        return self.rate

    def open(self):
        try:
            import pysysaudio  # noqa: WPS433 - 只在这条路上 import
        except Exception as exc:  # noqa: BLE001
            raise BackendError(t("audio.err.pysysaudioOpen", error=exc))
        checker = getattr(pysysaudio, "SystemAudioRecorder", None)
        if checker is None:
            raise BackendError(t("audio.err.pysysaudioNoRecorder"))
        if hasattr(checker, "check_permission"):
            try:
                if not checker.check_permission():
                    raise BackendError(t("audio.err.pysysaudioDenied"))
            except BackendError:
                raise
            except Exception as exc:  # noqa: BLE001 - 老版本没有该方法时忽略
                LOG.debug(t("log.pysysaudioPermCheck"), exc)
        try:
            self._rec = checker(sample_rate=self.rate, channels=1, format="numpy", dtype="float32")
            self._rec.start_recording()          # 只要流，不落文件
            self._gen = self._rec.stream(timeout=0.1)
        except Exception as exc:  # noqa: BLE001
            raise BackendError(t("audio.err.pysysaudioOpen", error=exc))

    def read(self, frames):
        if self._gen is None:
            raise BackendError(t("audio.err.notOpened"))
        try:
            chunk = next(self._gen)
        except StopIteration:
            # 超时窗口内没有新块：**不是** EOF，交回 None 让采集线程小睡后再读
            return None
        except Exception as exc:  # noqa: BLE001
            raise BackendError(t("audio.err.pysysaudioOpen", error=exc))
        if chunk is None:
            return None
        arr = np.asarray(chunk, dtype=np.float32)
        if arr.ndim == 2 and arr.shape[1] > 1:
            arr = arr.mean(axis=1)
        return np.ascontiguousarray(arr.reshape(-1), dtype=np.float32)

    def close(self):
        rec, self._rec = self._rec, None
        self._gen = None
        if rec is not None:
            try:
                rec.stop_recording()
            except Exception:  # noqa: BLE001
                pass


class MacSystemBackend(AudioBackend):
    """macOS：system 音源先试 pysysaudio（免驱动），再回落 soundcard（需虚拟声卡）。"""

    name = "mac-system"

    def __init__(self, source="system", device=None, rate=16000, frame_ms=20):
        AudioBackend.__init__(self, source=source, device=device, rate=rate, frame_ms=frame_ms)
        self._inner = None
        self._fallback_error = None

    def open(self):
        if self.source == "system" and not self.device:
            try:
                inner = PysysaudioCapture(self.rate, self.frame_ms)
                inner.open()
                self._adopt(inner)
                return
            except Exception as exc:  # noqa: BLE001 - 回落是设计的一部分
                self._fallback_error = exc
                LOG.info(t("log.pysysaudioFallback"), exc)
        inner = SoundcardBackend(source=self.source, device=self.device, rate=self.rate,
                                 frame_ms=self.frame_ms)
        try:
            inner.open()
        except BackendError as exc:
            if self._fallback_error is not None and self.source == "system":
                # 两层原因都带上：用户要能分清"没装 pysysaudio/没授权"还是"没装 BlackHole"
                raise BackendError(t("audio.err.macNoSystemAudioDetail",
                                     detail=self._fallback_error, detail2=exc))
            raise exc
        self._adopt(inner)

    def _adopt(self, inner):
        self._inner = inner
        self._native_rate = inner.native_rate
        self.device = inner.device
        self.name = inner.name
        self._opened = True

    def describe(self):
        info = AudioBackend.describe(self)
        info["backend"] = self.name
        if self._fallback_error is not None:
            info["fallbackReason"] = str(self._fallback_error)
        return info

    def read(self, frames):
        if self._inner is None:
            raise BackendError(t("audio.err.notOpened"))
        return self._inner.read(frames)

    def close(self):
        inner, self._inner = self._inner, None
        if inner is not None:
            try:
                inner.close()
            except Exception:  # noqa: BLE001
                pass
        self._opened = False
