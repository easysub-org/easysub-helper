# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 hcz1017
"""easysub-helper：绕过浏览器 API 的本机音频采集 + 本地 Web UI 托管服务。

为什么要它（背景）：
  - Chrome 的 `getDisplayMedia` 在 macOS/Linux 拿不到整机系统音频，Windows 也要用户每次
    选一次共享面板；Firefox 的 `getDisplayMedia` 至今**完全不支持音频**（bug 1541425），
    而 `chrome.tabCapture` 是 Chrome 独有 API。
  - 于是唯一的通用解是：由本机进程用操作系统 API 采集（Windows WASAPI loopback /
    Linux PulseAudio monitor / macOS CoreAudio），再把 PCM 交给页面。
  - 本模块同时把 Web 产物托管在 127.0.0.1（带 COOP/COEP 响应头），页面因此仍是
    `crossOriginIsolated`，识别引擎（sherpa-onnx wasm）与主项目**完全同一份代码**。

协议与集成方式见 README「协议」一节；前端只需一个音源把 PCM 交给 `feedMicChunk`。
"""


__all__ = ["__version__"]

__version__ = "0.1.0"
