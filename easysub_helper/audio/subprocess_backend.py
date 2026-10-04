# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 hcz1017
"""Linux 子进程后端：parec（PulseAudio/PipeWire-pulse）或 pw-record（PipeWire）。

为什么 Linux 上优先用它而不是 soundcard：
  - 零 Python 依赖（不需要 CFFI/声卡库），部署面小；
  - PulseAudio 自己会做服务端重采样，直接要 16k 就能拿到 16k（省掉一次重采样）；
  - PipeWire 与 PulseAudio 通用（pipewire-pulse 提供 parec）。
而 macOS/Windows 上没有等价的、稳定的命令行工具，所以那边走 soundcard。
"""


import shutil
import subprocess
import threading
import time

import numpy as np

from .base import AudioBackend, BackendError
from ..i18n import t

DEFAULT_MONITOR = "@DEFAULT_MONITOR@"
TOOLS = ("parec", "pw-record")


def find_tool(preferred=None):
    """返回可用的采集工具名（优先 preferred，其次 parec，最后 pw-record）。"""
    names = ([preferred] if preferred else []) + ["parec", "pw-record"]
    for name in names:
        if name and shutil.which(name):
            return name
    return None


class SubprocessBackend(AudioBackend):
    def __init__(self, tool=None, source="system", device=None, rate=16000, frame_ms=20, latency_ms=None):
        super().__init__(source=source, device=device, rate=rate, frame_ms=frame_ms)
        self.tool = tool or find_tool()
        if not self.tool:
            raise BackendError(t("audio.err.toolMissing"))
        self.exe = shutil.which(self.tool) or self.tool
        self.latency_ms = int(latency_ms if latency_ms is not None else frame_ms)
        self._proc = None
        self._stderr_lines = []
        self._stderr_thread = None

    @property
    def name(self):
        return self.tool

    def build_argv(self):
        """构造命令行；单独抽出来是为了能单测（不真的起进程）。"""
        if self.tool == "parec":
            argv = [
                self.exe,
                "--format=float32le",
                "--rate={}".format(self.rate),
                "--channels=1",
                "--latency-msec={}".format(self.latency_ms),
            ]
            dev = self._device_name()
            if dev:
                argv.append("--device={}".format(dev))
            return argv
        if self.tool == "pw-record":
            argv = [
                self.exe,
                "--format=f32",
                "--rate", str(self.rate),
                "--channels", "1",
                "--latency", "{}ms".format(self.latency_ms),
            ]
            dev = self._device_name()
            if dev:
                argv += ["--target", dev]
            argv.append("-")     # 输出到 stdout
            return argv
        raise BackendError(t("audio.err.unknownBackend", backend=self.tool))

    def _device_name(self):
        """system → 默认输出的 monitor；mic → None（即工具自己的默认输入源）。"""
        if self.device:
            return self.device
        if self.source == "system":
            return DEFAULT_MONITOR
        return None

    def _resolve_device(self):
        """把用户给的设备名解析成 PulseAudio 源名。

        必须在这里做：`parec`/`pw-record` 对未知设备名**静默回落到默认源**
        （实测 `parec --device=definitely-not-a-source` 照样一直采），
        不先解析的话用户会以为"换了麦克风但声音还是原来那个"。
        """
        if not self.device:
            return
        from . import devices as devices_mod

        resolved, error = devices_mod.resolve(self.source, self.device)
        if error:
            raise BackendError(error)
        if resolved:
            self.device = resolved

    def open(self):
        self._resolve_device()
        argv = self.build_argv()
        try:
            self._proc = subprocess.Popen(
                argv,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                stdin=subprocess.DEVNULL,
                bufsize=0,
            )
        except OSError as exc:
            raise BackendError(t("audio.err.toolStart", tool=self.tool, error=exc))
        self._stderr_thread = threading.Thread(target=self._drain_stderr, name="easysub-capture-err", daemon=True)
        self._stderr_thread.start()
        # 立刻检查是否秒退（设备名不对/没有 PulseAudio 时 parec 会马上报错退出）
        time.sleep(0.15)
        code = self._proc.poll()
        if code is not None:
            raise BackendError(t(
                "audio.err.toolExited",
                tool=self.tool,
                code=code,
                detail=self._stderr_tail() or t("audio.err.noStderr"),
            ))
        self._native_rate = int(self.rate)   # parec/pw-record 按我们要求的速率输出
        self._opened = True
        self.device = self._device_name() or "default source"

    def _drain_stderr(self):
        proc = self._proc
        if proc is None or proc.stderr is None:
            return
        try:
            for raw in iter(proc.stderr.readline, b""):
                line = raw.decode("utf-8", "replace").strip()
                if line:
                    self._stderr_lines.append(line)
                    del self._stderr_lines[:-20]   # 只留最近 20 行，避免长会话无限增长
        except Exception:  # noqa: BLE001 - 管道关闭时的异常无需上报
            pass

    def _stderr_tail(self):
        return " | ".join(self._stderr_lines[-3:])

    def read(self, frames):
        proc = self._proc
        if proc is None or proc.stdout is None:
            raise BackendError(t("audio.err.notOpened"))
        need = int(frames) * 4
        parts = []
        while need > 0:
            chunk = proc.stdout.read(need)
            if not chunk:
                code = proc.poll()
                raise BackendError(t(
                    "audio.err.toolEnded",
                    tool=self.tool,
                    code=code,
                    detail=self._stderr_tail() or t("audio.err.noStderr"),
                ))
            parts.append(chunk)
            need -= len(chunk)
        data = parts[0] if len(parts) == 1 else b"".join(parts)
        return np.frombuffer(data, dtype="<f4").astype(np.float32, copy=False)

    def close(self):
        proc, self._proc = self._proc, None
        if proc is None:
            self._opened = False
            return
        try:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=1.0)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=1.0)
        except Exception:  # noqa: BLE001
            pass
        for stream in (proc.stdout, proc.stderr):
            try:
                if stream is not None:
                    stream.close()
            except Exception:  # noqa: BLE001
                pass
        self._opened = False
