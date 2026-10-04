# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 hcz1017
"""采集层：后端工厂 + 会话 + 重采样。

后端选择规则（`auto`）：
  - **Linux**：parec / pw-record 子进程优先（零依赖、服务端重采样、PipeWire 与 PulseAudio 通用），
    找不到工具才用 soundcard（PulseAudio monitor 源）。
  - **macOS**：`mac-system` —— system 音源先试 pysysaudio（macOS 14.2+，**免驱动**），
    失败回落 soundcard（需要 BlackHole 之类的 loopback 输入设备）；麦克风直接 soundcard。
  - **Windows**：soundcard（WASAPI loopback，Win7 起可用，纯 CFFI 无需编译器）。

显式 `backend=` 可覆盖以上规则：`soundcard` / `parec` / `pw-record` / `mac-system`（别名 pysysaudio）。
"""

from ..i18n import t
from .base import AudioBackend, BackendError, CaptureSession
from .resample import ResampleUnavailable, create_resampler

__all__ = [
    "AudioBackend",
    "BackendError",
    "CaptureSession",
    "ResampleUnavailable",
    "create_resampler",
    "create_backend",
    "available_backends",
]

#: 走 macOS 专用后端的显式名字
MAC_BACKEND_NAMES = ("mac-system", "macsystem", "mac", "pysysaudio")


def available_backends():
    """当前机器上可用的后端名（不打开设备，只探测）。"""
    from ..config import is_linux, is_macos
    from .subprocess_backend import find_tool

    names = []
    if is_linux():
        tool = find_tool()
        if tool:
            names.append(tool)
    if is_macos():
        names.append("mac-system")
    names.append("soundcard")
    return names


def create_backend(source="system", backend="auto", device=None, rate=16000, frame_ms=20):
    """构造（不打开）采集后端。真正的打开发生在采集线程里，见 CaptureSession._run。"""
    from ..config import is_linux, is_macos
    from .macos_backend import MacSystemBackend
    from .soundcard_backend import SoundcardBackend
    from .subprocess_backend import SubprocessBackend, find_tool

    name = (backend or "auto").lower()
    if name in MAC_BACKEND_NAMES:
        if not is_macos():
            raise BackendError(t("audio.err.backendNotMac", backend=name))
        return MacSystemBackend(source=source, device=device, rate=rate, frame_ms=frame_ms)
    if name in ("auto", ""):
        if is_linux():
            tool = find_tool()
            if tool:
                return SubprocessBackend(tool=tool, source=source, device=device, rate=rate,
                                         frame_ms=frame_ms)
            name = "soundcard"
        elif is_macos():
            return MacSystemBackend(source=source, device=device, rate=rate, frame_ms=frame_ms)
        else:
            name = "soundcard"
    if name in ("parec", "pw-record"):
        if not is_linux():
            raise BackendError(t("audio.err.backendNotLinux", backend=name))
        return SubprocessBackend(tool=name, source=source, device=device, rate=rate, frame_ms=frame_ms)
    if name == "soundcard":
        return SoundcardBackend(source=source, device=device, rate=rate, frame_ms=frame_ms)
    raise BackendError(t("audio.err.unknownBackend", backend=backend))
