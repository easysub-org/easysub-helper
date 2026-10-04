# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 hcz1017
"""easysub-helper：绕过浏览器 API 的本机音频采集服务（只做采集 + 与页面配对）。

为什么要它（背景）：
  - Chrome 的 `getDisplayMedia` 在 macOS/Linux 拿不到整机系统音频，Windows 也要用户每次
    选一次共享面板；Firefox 的 `getDisplayMedia` 至今**完全不支持音频**（bug 1541425），
    而 `chrome.tabCapture` 是 Chrome 独有 API。
  - 于是唯一的通用解是：由本机进程用操作系统 API 采集（Windows WASAPI loopback /
    Linux PulseAudio monitor / macOS CoreAudio），再把 PCM 交给页面。
  - 助手**只做两件事**：采集本机音频、与页面配对（HTTP 面只有 /api/pair* 与 /ws）。
    它不托管 Web 产物、不碰模型 —— 页面由浏览器侧（扩展 / Web 版）自己提供，
    跨源隔离（COOP/COEP）也由页面自己负责。

协议与集成方式见 README「协议」一节；前端只需一个音源把 PCM 交给 `feedMicChunk`。
"""


__all__ = ["__version__"]

__version__ = "0.1.0"
