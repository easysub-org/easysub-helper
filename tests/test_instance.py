# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 hcz1017
"""单实例标记（`easysub_helper/instance.py`）的测试 —— 全在临时目录里，绝不碰用户真实锁。

为什么值得这么细（审查交叉确认的三条）：
  * 两个助手实例会互相覆盖 `pairing.json` 里的设备令牌（"刚配对好、过一会儿又要重新配对"）；
  * 锁必须**认主人**：第二个实例先退出时不能删掉还活着的那个实例的锁；
  * 锁文件坏了/pid 不是数字/权限不足，都**不能**让启动崩掉（护栏不是业务）。
"""

import json
import os
import shutil
import tempfile
import unittest
from unittest import mock

from easysub_helper import config, instance


class InstanceTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))
        patcher = mock.patch.object(config, "data_dir", lambda: self.tmp)
        patcher.start()
        self.addCleanup(patcher.stop)
        # 这些用例就是要验锁文件的读写，所以显式打开（tests/__init__.py 默认关掉它防污染）
        env = mock.patch.dict(os.environ, {"EASYSUB_NO_LOCK": ""})
        env.start()
        self.addCleanup(env.stop)

    def _write_raw(self, payload):
        path = instance.lock_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(payload if isinstance(payload, str) else json.dumps(payload))

    def test_claim_then_read_is_ours(self):
        instance.claim(8790)
        self.assertTrue(os.path.exists(instance.lock_path()))
        # 自己写的锁不算"另一个实例"
        self.assertIsNone(instance.read())

    def test_update_overwrites_port(self):
        instance.claim(8790)
        instance.update(9001)
        with open(instance.lock_path(), encoding="utf-8") as handle:
            self.assertEqual(json.load(handle)["port"], 9001)

    def test_other_live_instance_is_read(self):
        self._write_raw({"pid": os.getpid() + 1, "port": 9000})
        with mock.patch.object(instance, "pid_alive", return_value=True):
            self.assertEqual(instance.read(), 9000)

    def test_unknown_port_reports_true(self):
        self._write_raw({"pid": os.getpid() + 1})
        with mock.patch.object(instance, "pid_alive", return_value=True):
            self.assertIs(instance.read(), True)

    def test_stale_lock_is_removed(self):
        self._write_raw({"pid": 999999999, "port": 8790})
        self.assertIsNone(instance.read())
        self.assertFalse(os.path.exists(instance.lock_path()), "僵尸锁要顺手清掉")

    def test_broken_lock_never_raises(self):
        for payload in ("not json at all", "[]", '{"pid": "abc"}', '{"pid": null, "port": "x"}'):
            self._write_raw(payload)
            self.assertIsNone(instance.read(), payload)

    def test_release_only_removes_our_own_lock(self):
        # 别人（活着的实例）的锁：绝不能删
        self._write_raw({"pid": os.getpid() + 1, "port": 9000})
        instance.release()
        self.assertTrue(os.path.exists(instance.lock_path()), "不能删别人的锁（复审 M3）")
        # 自己的锁：删
        instance.claim(8790)
        instance.release()
        self.assertFalse(os.path.exists(instance.lock_path()))

    def test_disabled_in_tests_never_touches_the_file(self):
        """tests/__init__.py 会设 EASYSUB_NO_LOCK：跑测试不能偷走正在运行的助手的锁。"""
        with mock.patch.dict(os.environ, {"EASYSUB_NO_LOCK": "1"}):
            instance.claim(8790)
            self.assertFalse(os.path.exists(instance.lock_path()))

    def test_pid_alive_basics(self):
        self.assertTrue(instance.pid_alive(os.getpid()))
        self.assertFalse(instance.pid_alive(0))
        self.assertFalse(instance.pid_alive(-5))
        self.assertFalse(instance.pid_alive(999999999))
        self.assertFalse(instance.pid_alive("abc"))

    @unittest.skipIf(os.name == "nt", "POSIX 分支：Windows 走 OpenProcess，见下一条")
    def test_pid_alive_treats_permission_denied_as_alive(self):
        """权限不足 ≠ 进程不存在：宁可误报"已有实例"，也别放行第二个实例去覆盖配对数据。"""
        with mock.patch("os.kill", side_effect=PermissionError("denied")):
            self.assertTrue(instance.pid_alive(1))

    @unittest.skipUnless(os.name == "nt", "Windows 用 OpenProcess 判断存在性")
    def test_pid_alive_treats_access_denied_as_alive_on_windows(self):
        """坑（CI 抓到的两轮）：① 只 patch `os.kill` 对 Windows 分支毫无作用；② 用普通类做假
        kernel32 也不行 —— 代码里有 `kernel32.OpenProcess.restype = c_void_p`（给 64 位句柄
        设返回类型），普通类的方法不能赋属性 → 直接走进 except 返回 False。所以要用 Mock。

        ACCESS_DENIED(5) = 进程**存在**但没权限打开（例如以管理员身份跑）→ 必须当"活着"。
        """
        import ctypes

        kernel32 = mock.Mock()
        kernel32.OpenProcess.return_value = 0        # 打不开
        kernel32.GetLastError.return_value = 5       # ERROR_ACCESS_DENIED
        with mock.patch.object(ctypes, "windll", mock.Mock(kernel32=kernel32)):
            self.assertTrue(instance.pid_alive(1))

    @unittest.skipUnless(os.name == "nt", "Windows 用 OpenProcess 判断存在性")
    def test_pid_alive_false_when_windows_says_gone(self):
        import ctypes

        kernel32 = mock.Mock()
        kernel32.OpenProcess.return_value = 0
        kernel32.GetLastError.return_value = 87      # ERROR_INVALID_PARAMETER：进程不存在
        with mock.patch.object(ctypes, "windll", mock.Mock(kernel32=kernel32)):
            self.assertFalse(instance.pid_alive(999999999))

    @unittest.skipUnless(os.name == "nt", "Windows 用 OpenProcess 判断存在性")
    def test_pid_alive_true_when_windows_opens_the_handle(self):
        import ctypes

        kernel32 = mock.Mock()
        kernel32.OpenProcess.return_value = 0x1234   # 打开成功
        with mock.patch.object(ctypes, "windll", mock.Mock(kernel32=kernel32)):
            self.assertTrue(instance.pid_alive(os.getpid()))


if __name__ == "__main__":
    unittest.main()
