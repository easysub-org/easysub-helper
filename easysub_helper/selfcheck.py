# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 hcz1017
"""冻结产物自检（`--selftest`）：把"只有打出来的包才看得见"的问题在 CI 里变成红灯。

为什么需要它（独立审查指出的 CI 盲区）：三平台的冒烟以前只跑
`--version` / `--help` / `--no-gui` 起服务，**从不构造 GUI、也不走 WS / 采集 / 重采样**。
于是：

  * 打包漏了 tkinter → `--windowed` 产物的 `gui.available()` 静默变 False，进程回落成
    "没有窗口也没有控制台"的常驻服务，CI 依然全绿，而用户双击什么都没发生；
  * 重采样器坏掉（历史上多相位索引写反、频谱里全是镜像杂散）产线一次都不报。

本模块逐项检查，任何一项失败都让进程以非 0 退出，打包作业因此变红。
**不要在这里构造 tkinter 窗口**：CI 的 Linux 作业没有显示环境，`import tkinter` 足以
覆盖"打包漏了 tkinter/tcl"这一失败模式。

细节文案刻意保持英文：这是开发者/CI 面向的诊断接口，不进用户界面（窗口与 CLI 帮助
仍是本地化的）。
"""

import asyncio
import os
import sys

from . import gui

#: 打包时必须带上的资源（缺了窗口图标就会在运行期才报错）
ASSET_FILES = ("icon128.png", "easysub.ico")
#: 一帧 20ms@16k 单声道 f32le 的字节数（协议契约，页面按它切帧）
FRAME_BYTES = 16000 * 20 // 1000 * 4


def check_tkinter():
    """tkinter 必须在（--windowed 产物漏了它 → 静默变无窗口常驻）。"""
    if gui.tkinter_module() is None:
        raise RuntimeError("tkinter is missing from this build (a --windowed binary would "
                           "silently fall back to a windowless process)")
    import tkinter.ttk   # noqa: F401  （只要包壳没有 ttk，窗口一样起不来）


def check_assets():
    base = gui.assets_dir()
    if not base:
        raise RuntimeError("assets directory not found (the bundle dropped easysub_helper/assets)")
    missing = [name for name in ASSET_FILES if not os.path.exists(os.path.join(base, name))]
    if missing:
        raise RuntimeError("assets directory is missing: " + ", ".join(missing))


def check_resampler():
    """重采样要能建、能跑、增益正确（这条曾经整体坏掉而产线看不见）。"""
    import numpy as np

    from .audio.resample import create_resampler

    res = create_resampler(44100, 16000, prefer="polyphase")   # 历史 blocker 正在这个比率上
    x = np.sin(np.arange(4410, dtype=np.float32) * 0.05).astype(np.float32)
    y = res.process(x)
    if y.size < 1000 or not bool(np.isfinite(y).all()):
        raise RuntimeError("resampler produced %d samples (finite=%s)" % (y.size, bool(np.isfinite(y).all())))
    # 直流输入经低通后应仍≈1.0：多相归一化写错时这里立刻露馅
    flat = create_resampler(48000, 16000, prefer="polyphase").process(
        np.ones(4800, dtype=np.float32))
    mid = flat[300:1200]
    worst = float(np.abs(mid - 1.0).max())
    if worst > 0.05:
        raise RuntimeError("resampler gain error %.3f (DC input should stay at 1.0)" % worst)


async def _roundtrip():
    """本机 HTTP + WS 环路：起服务 → /api/pair/info → WS 连上 → 暂停态收到静音帧。"""
    import aiohttp

    from . import __version__
    from .net.server import HelperServer
    from .pairing import PairingManager

    pairing = PairingManager(persist=False)
    pairing.new_code()
    server = HelperServer(pairing=pairing, port=0, scan_ports=False,
                          version=__version__, user_on=False)      # 暂停：不需要任何音频设备
    await server.start()
    try:
        base = server.base_url
        async with aiohttp.ClientSession() as session:
            async with session.get(base + "/api/pair/info") as resp:
                if resp.status != 200:
                    raise RuntimeError("/api/pair/info returned %d" % resp.status)
                info = await resp.json()
                if info.get("app") != "easysub-helper":
                    raise RuntimeError("/api/pair/info app=%r" % (info.get("app"),))
            token = pairing.issue_token("selftest")
            url = base.replace("http://", "ws://") + "/ws?token=" + token
            async with session.ws_connect(url) as ws:
                frame = None
                for _ in range(20):                       # hello/state 是文本帧，跳过
                    msg = await asyncio.wait_for(ws.receive(), timeout=5)
                    if msg.type == aiohttp.WSMsgType.BINARY:
                        frame = msg.data
                        break
                    if msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.CLOSING):
                        break
                if frame is None:
                    raise RuntimeError("no audio frame after connecting (paused mode must keep "
                                       "pushing silence frames)")
                if len(frame) != FRAME_BYTES:
                    raise RuntimeError("frame is %d bytes, expected %d" % (len(frame), FRAME_BYTES))
                if frame != b"\x00" * len(frame):
                    raise RuntimeError("paused mode pushed a non-silent frame")
    finally:
        await server.stop()


def check_server_roundtrip():
    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(asyncio.wait_for(_roundtrip(), timeout=30))
    finally:
        loop.close()


def default_checks():
    return (
        ("tkinter", check_tkinter),
        ("assets", check_assets),
        ("resampler", check_resampler),
        ("http+ws round trip", check_server_roundtrip),
    )


def run(checks=None, out=None):
    """跑全部检查，返回 [(name, ok, detail)]。checks 可注入（测试用）。"""
    out = out if out is not None else getattr(sys, "stdout", None)
    results = []
    for name, fn in (checks if checks is not None else default_checks()):
        try:
            fn()
            results.append((name, True, ""))
            _emit(out, "  [ok]   %s" % name)
        except Exception as exc:                          # noqa: BLE001 - 任何异常都算该项失败
            detail = "%s: %s" % (type(exc).__name__, exc)
            results.append((name, False, detail))
            _emit(out, "  [FAIL] %s - %s" % (name, detail))
    return results


def main(checks=None, out=None):
    """返回进程退出码：全部通过 0，否则 1。"""
    results = run(checks=checks, out=out)
    failed = [name for name, ok, _ in results if not ok]
    if failed:
        _emit(out, "selftest FAILED (%d/%d): %s" % (len(failed), len(results), ", ".join(failed)))
        return 1
    _emit(out, "selftest OK (%d checks)" % len(results))
    return 0


def _emit(out, line):
    # --windowed / --noconsole 产物根本没有 stdout：自检只看退出码，
    # 打印失败（None、编码不匹配、管道已关）都不能影响结论。
    if out is None:
        return
    try:
        print(line, file=out)
    except Exception:                                     # noqa: BLE001
        pass
