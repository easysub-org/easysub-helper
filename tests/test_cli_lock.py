# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 hcz1017
"""单实例护栏与端口提醒的测试。

为什么要这两条护栏（可用性审查抓到的真实危害）：两个助手实例**各有各的配对码**，却写同一份
`pairing.json`，于是互相覆盖对方发出的设备令牌 —— 用户的表现是"刚配对好、过一会儿又要重新
配对"。而 `--port` 一旦不是默认值，助手顺延后可能落到字幕页面**探测不到**的端口。
"""

import json
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

from easysub_helper import cli, config


class PortWarningTest(unittest.TestCase):
    def test_default_port_needs_no_warning(self):
        self.assertFalse(cli._port_warning_needed(config.DEFAULT_PORT))

    def test_any_non_default_base_port_warms(self):
        # 关键：8800 本身"在范围内"，但助手是从它往后顺延的 → 可能漂出 8810
        self.assertTrue(cli._port_warning_needed(8800))
        self.assertTrue(cli._port_warning_needed(8791))
        self.assertTrue(cli._port_warning_needed(9000))
        self.assertTrue(cli._port_warning_needed(1024))

    def test_random_port_warns(self):
        # --port 0 是受支持的开发用法，但页面永远探不到它
        self.assertTrue(cli._port_warning_needed(0))


class LockTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))
        self._patches = [mock.patch.object(config, "data_dir", lambda: self.tmp)]
        for item in self._patches:
            item.start()
            self.addCleanup(item.stop)

    def test_write_then_read_lock(self):
        cli._write_lock(8790)
        path = os.path.join(self.tmp, "helper.lock")
        self.assertTrue(os.path.exists(path))
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
        self.assertEqual(data["port"], 8790)
        self.assertEqual(data["pid"], os.getpid())
        # 自己的锁不算"另一个实例"
        self.assertIsNone(cli._read_lock())

    def test_stale_lock_is_cleaned_up(self):
        cli._write_lock(8790)
        path = os.path.join(self.tmp, "helper.lock")
        with open(path, "w", encoding="utf-8") as handle:
            json.dump({"pid": 999999999, "port": 8790}, handle)   # 几乎不可能存在的 pid
        self.assertIsNone(cli._read_lock())
        self.assertFalse(os.path.exists(path), "僵尸锁要顺手清掉，别让下次启动误判")

    def test_live_other_instance_is_detected(self):
        cli._write_lock(8790)
        path = os.path.join(self.tmp, "helper.lock")
        with open(path, "w", encoding="utf-8") as handle:
            json.dump({"pid": os.getpid() + 1, "port": 9000}, handle)
        with mock.patch.object(cli, "_pid_alive", return_value=True):
            self.assertEqual(cli._read_lock(), 9000)
            # 锁文件优先：它不受 http_proxy 影响，也不管实例监听哪个端口/网卡
            self.assertEqual(cli._find_running_helper(), 9000)

    def test_fallback_probe_is_bounded_and_never_slow(self):
        """坑：CI 的打包冒烟是"后台起服务 → sleep 3 → curl"。第一版护栏把 22 个端口逐个
        0.2s 超时探测（Windows 上防火墙会丢 SYN → 走超时），启动被拖 ~4.4s，冒烟直接红
        （helper-ci 38064062965 / 38065887519）。所以 HTTP 兜底必须是**有界**的。
        """
        import time as _time
        import urllib.request as _url

        calls = []

        class SlowOpener(object):
            def open(self, url, timeout=None):
                calls.append(url)
                _time.sleep(0.5)          # 模拟"连不通但要等超时"的最坏情况
                raise OSError("no listener")

        with mock.patch.object(_url, "build_opener", return_value=SlowOpener()):
            started = _time.monotonic()
            self.assertIsNone(cli._find_running_helper(9100))
            elapsed = _time.monotonic() - started
        self.assertLess(elapsed, 1.5, "探测总耗时必须有界（否则会把启动拖过冒烟的 sleep）")
        self.assertLessEqual(len(calls), 4, "兜底探测只该探少数几个端口")

    def test_fast_path_never_touches_the_network(self):
        """命令行/无窗口路径必须**零延迟**：CI 的打包冒烟只给 3 秒，而 onefile 产物自己就要
        约 3 秒才监听 —— 多 0.5s 就会假红（helper-ci 38066370590 的 `after 0 ms` 对比
        `listening` 同一秒即可看出是这种"差 10ms"的假红）。"""
        import urllib.request as _url

        calls = []

        class Opener(object):
            def open(self, url, timeout=None):
                calls.append(url)
                raise OSError("no listener")

        with mock.patch.object(_url, "build_opener", return_value=Opener()):
            self.assertIsNone(cli._find_running_helper(9100, fast=True))
        self.assertEqual(calls, [], "fast 路径不允许发任何 HTTP 请求")

    def test_fallback_probe_finds_helper_on_preferred_port(self):
        import urllib.request as _url

        class FakeResponse(object):
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self):
                return b'{"app": "easysub-helper"}'

        class Opener(object):
            def open(self, url, timeout=None):
                return FakeResponse()

        with mock.patch.object(_url, "build_opener", return_value=Opener()):
            self.assertEqual(cli._find_running_helper(9100), 9100)

    def test_clear_lock(self):
        cli._write_lock(8790)
        cli._clear_lock()
        self.assertIsNone(cli._read_lock())

    def test_pid_alive(self):
        self.assertTrue(cli._pid_alive(os.getpid()))
        self.assertFalse(cli._pid_alive(0))
        self.assertFalse(cli._pid_alive(999999999))


if __name__ == "__main__":
    unittest.main()
