# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 hcz1017
"""单实例标记（`data_dir/helper.lock`）：pid + 实际监听端口。

为什么要拦（用户可用性审查）：两个助手实例**各有各的配对码**（各自内存里的），却写**同一份**
`pairing.json`，于是互相覆盖对方发出的设备令牌 —— 用户的表现是"刚配对好、过一会儿又要重新
配对"，照另一个窗口的码输入还会连错五次被锁 300 秒。

为什么用锁文件而不是 HTTP 探测（独立审查 M1/M2）：
  * HTTP 只能探"默认段 + 指定端口"，`--port 9000` 或 `--host <网卡>` 的实例根本探不到；
  * 探测要等超时，而 CI 的打包冒烟只给 3 秒（onefile 产物本身就要约 3 秒才监听）；
  * 有 `http_proxy` 的机器上探测还会被代理带偏。
锁文件是瞬时的、精确的，与端口/网卡/代理都无关。

纪律（复审要求）：
  * **认主人**：`release()` 只删"pid 是自己的"那份锁 —— 否则第二个实例先退出时会把还活着的
    那个实例的锁删掉，第三个实例就畅通无阻了；
  * **提前占位**：`claim()` 在服务真正绑定**之前**调用，堵住"双击没反应→再双击一次"撞进
    `检查 → 绑定` 那段宽窗口（打包产物约 3 秒）的 TOCTOU；绑定后再 `update(port)` 更正端口；
  * **坏锁不拦启动**：读不出来/pid 非数字/权限不足，一律当"没有别的实例"，绝不因为锁文件
    把启动搞崩（这是护栏，不是业务）。
"""

import json
import logging
import os

from . import config

LOG = logging.getLogger("easysub-helper")


def lock_path():
    return os.path.join(str(config.data_dir()), "helper.lock")


def pid_alive(pid):
    """进程还在吗（跨平台，只用标准库）。

    坑（复审 m5）：权限不足**不等于**进程不存在 —— Windows 上对管理员进程
    `OpenProcess` 会 ACCESS_DENIED，POSIX 上 `os.kill(pid, 0)` 会 PermissionError。
    这两种情况都要当"还活着"（宁可误报"已有实例"，也别误判"已死"而放行第二个实例）。
    """
    if not isinstance(pid, int) or pid <= 0:
        return False
    if os.name == "nt":
        try:
            import ctypes

            SYNCHRONIZE = 0x00100000
            kernel32 = ctypes.windll.kernel32
            kernel32.OpenProcess.restype = ctypes.c_void_p      # 64 位句柄不能被截断
            handle = kernel32.OpenProcess(SYNCHRONIZE, False, pid)
            if not handle:
                error = kernel32.GetLastError()
                # 5 = ACCESS_DENIED：进程存在但没权限打开 → 当"活着"
                return error == 5
            kernel32.CloseHandle(ctypes.c_void_p(handle))
            return True
        except Exception:                            # noqa: BLE001 - 探测失败一律当"不知道"
            return False
    try:
        os.kill(pid, 0)
    except PermissionError:
        return True                                  # 存在但没权限发信号
    except OSError:
        return False
    except Exception:                                # noqa: BLE001
        return False
    return True


def read():
    """锁里那个实例的端口（没有/是自己/进程已死 → None）。

    支持两种返回值语义：端口未知时返回 True（"有人在跑但端口不明"）。
    """
    try:
        with open(lock_path(), "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except Exception:                                # noqa: BLE001 - 没有文件/坏文件都算没有
        return None
    if not isinstance(data, dict):
        return None
    try:
        pid = int(data.get("pid") or 0)
    except (TypeError, ValueError):
        return None                                  # pid 非数字：当坏锁，别崩启动
    if pid == os.getpid():
        return None
    if not pid_alive(pid):
        release(force=True)                          # 上次没退干净留下的僵尸锁：顺手清掉
        return None
    try:
        port = int(data.get("port") or 0)
    except (TypeError, ValueError):
        return True
    return port if port > 0 else True


def _disabled():
    """测试期间关掉（见 tests/__init__.py）：绝不碰用户真实的锁文件。"""
    return bool(os.environ.get("EASYSUB_NO_LOCK"))


def claim(port=None):
    """在绑定**之前**占位（见模块开头的 TOCTOU 说明）。"""
    if _disabled():
        return
    _write(port)


def update(port):
    """绑定成功后把真实端口写进去（`--port 0` 时端口是系统给的）。"""
    if _disabled():
        return
    _write(port)


def release(force=False):
    """退出时清锁 —— **只清自己的**（复审 M3）。"""
    if _disabled():
        return
    if not force and not _is_ours():
        return
    try:
        os.remove(lock_path())
    except OSError:
        pass


def _is_ours():
    try:
        with open(lock_path(), "r", encoding="utf-8") as handle:
            data = json.load(handle)
        return int(data.get("pid") or 0) == os.getpid()
    except Exception:                                # noqa: BLE001
        return False


def _write(port):
    try:
        path = lock_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        # 原子写：临时文件 + replace（别让另一个实例读到写了一半的 JSON）
        tmp = "{}.{}.tmp".format(path, os.getpid())
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump({"pid": os.getpid(), "port": port}, handle)
        os.replace(tmp, path)
    except Exception as exc:                         # noqa: BLE001 - 写不了锁不该拦住启动
        LOG.debug("cannot write instance lock: %s", exc)
