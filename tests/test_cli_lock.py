# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 hcz1017
"""启动护栏：端口提醒 + 单实例探测的**耗时上界**。

（锁文件本身的语义见 tests/test_instance.py。）
为什么要有"耗时上界"这一条：CI 的打包冒烟是"后台起服务 → `sleep 3` → curl"，而 onefile 产物
在 Windows 上本身就要约 3 秒才监听；护栏哪怕只多花 0.5 秒，也会把 curl 推到 listening 之前
10 毫秒 → 假红（helper-ci 38066370590 实测）。
"""

import os
import shutil
import tempfile
import unittest
from unittest import mock

from easysub_helper import cli, config, instance


class PortWarningTest(unittest.TestCase):
    def test_default_port_needs_no_warning(self):
        self.assertFalse(cli._port_warning_needed(config.DEFAULT_PORT))

    def test_any_non_default_base_port_warns(self):
        # 关键：8800 本身"在范围内"，但助手是从它往后顺延的 → 可能漂出 8810（复审 M1）
        self.assertTrue(cli._port_warning_needed(8800))
        self.assertTrue(cli._port_warning_needed(8791))
        self.assertTrue(cli._port_warning_needed(9000))
        self.assertTrue(cli._port_warning_needed(1024))

    def test_random_port_warns(self):
        # --port 0 是受支持的开发用法，但页面永远探不到它
        self.assertTrue(cli._port_warning_needed(0))


class FindRunningHelperTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))
        patcher = mock.patch.object(config, "data_dir", lambda: self.tmp)
        patcher.start()
        self.addCleanup(patcher.stop)
        env = mock.patch.dict(os.environ, {"EASYSUB_NO_LOCK": ""})
        env.start()
        self.addCleanup(env.stop)

    def test_lock_file_wins_over_network(self):
        """锁文件优先：它瞬时、精确，也不受 http_proxy 影响。

        但（盲审 S2）**端口要活着才算数**：pid 会被复用、也可能是僵尸，只信 pid 会把用户
        永久拦在"已经有一个助手在运行"外面。所以这里 mock 探活为真（锁有效），
        另加一条"探不通即视为陈旧锁"的用例。
        """
        path = instance.lock_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write('{"pid": %d, "port": 9000}' % (os.getpid() + 1))
        with mock.patch.object(instance, "pid_alive", return_value=True), \
             mock.patch.object(cli, "_probe_helper_port", return_value=True):
            self.assertEqual(cli._find_running_helper(), 9000)
        # 盲审第 7 轮：**探不到 ≠ 不存在**。锁是刚写的（新鲜）→ 仍当活实例，
        # 否则"第 1 个实例已 claim 但还没 bind"的窗口里会放行第二个实例。
        with mock.patch.object(instance, "pid_alive", return_value=True), \
             mock.patch.object(cli, "_probe_helper_port", return_value=False):
            self.assertEqual(cli._find_running_helper(), 9000)
        # 只有"锁已经够旧"（不像是在启动中）才允许按陈旧锁放行
        old = os.stat(path).st_mtime - cli._STALE_LOCK_GRACE_SEC - 60
        os.utime(path, (old, old))
        with mock.patch.object(instance, "pid_alive", return_value=True), \
             mock.patch.object(cli, "_probe_helper_port", return_value=False):
            self.assertIsNone(cli._find_running_helper())


    def test_stale_lock_with_reused_pid_does_not_block_startup(self):
        """pid 复用/僵尸导致的假"已有实例"必须被识破（否则 --no-gui 直接 return 2）。

        判据是"探不到 **且锁够旧**"：新鲜锁代表可能在启动中，那种情况下必须拦住（否则会起第二个
        实例共写 pairing.json）；只有旧锁才允许按陈旧放行。
        """
        path = instance.lock_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write('{"pid": %d, "port": 9001}' % (os.getpid() + 1))
        old = os.stat(path).st_mtime - cli._STALE_LOCK_GRACE_SEC - 60
        os.utime(path, (old, old))
        with mock.patch.object(instance, "pid_alive", return_value=True), \
             mock.patch.object(cli, "_probe_helper_port", return_value=False):
            self.assertIsNone(cli._find_running_helper())

    def test_fast_path_never_touches_the_network(self):
        """命令行/无窗口路径必须**零延迟**：CI 的打包冒烟只给 3 秒（见文件头说明）。"""
        import urllib.request as _url

        calls = []

        class Opener(object):
            def open(self, url, timeout=None):
                calls.append(url)
                raise OSError("no listener")

        with mock.patch.object(_url, "build_opener", return_value=Opener()):
            self.assertIsNone(cli._find_running_helper(9100, fast=True))
        self.assertEqual(calls, [], "fast 路径不允许发任何 HTTP 请求")

    def test_fallback_probe_is_bounded(self):
        """兜底探测也要有上界：Windows 上"连不通"会走超时而不是秒拒。"""
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


if __name__ == "__main__":
    unittest.main()
