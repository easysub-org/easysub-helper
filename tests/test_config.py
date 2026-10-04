# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 hcz1017
"""配置：音频参数常量 + 用户偏好（界面语言）的读写。

助手不碰识别模型、不托管页面，所以这里**不该**再有任何 web_root / models_dir / 模型下载
的发现逻辑——那条线由 tests/test_scope.py 机械把关。
"""

import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path

from easysub_helper import config


class SettingsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self._data_dir = config.data_dir
        config.data_dir = lambda: Path(self.tmp)
        self.addCleanup(lambda: setattr(config, "data_dir", self._data_dir))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_missing_file_means_no_preferences(self):
        self.assertEqual(config.load_settings(), {})

    def test_round_trip(self):
        self.assertTrue(config.save_settings({"lang": "en"}))
        self.assertEqual(config.load_settings(), {"lang": "en"})
        # 覆盖写：旧键不会残留
        self.assertTrue(config.save_settings({"lang": "zh_CN", "extra": 1}))
        values = config.load_settings()
        self.assertEqual(values["lang"], "zh_CN")
        self.assertEqual(values["extra"], 1)

    def test_written_file_is_utf8_json(self):
        config.save_settings({"lang": "en"})
        with open(str(config.settings_path()), "r", encoding="utf-8") as fh:
            self.assertEqual(json.load(fh)["lang"], "en")

    def test_corrupt_file_is_ignored(self):
        path = config.settings_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{not json", encoding="utf-8")
        self.assertEqual(config.load_settings(), {})
        # 坏文件不该拦住"再存一次"
        self.assertTrue(config.save_settings({"lang": "en"}))

    def test_non_dict_json_is_ignored(self):
        path = config.settings_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("[1, 2]", encoding="utf-8")
        self.assertEqual(config.load_settings(), {})

    def test_unwritable_location_is_not_fatal(self):
        """只读目录/沙箱里存不下偏好也必须能继续用（下次再记）。"""
        blocker = Path(self.tmp) / "blocker"
        blocker.write_text("x", encoding="utf-8")
        config.data_dir = lambda: blocker / "sub"        # 父路径是个文件 → mkdir 必失败
        self.assertFalse(config.save_settings({"lang": "en"}))
        self.assertEqual(config.load_settings(), {})


class DefaultsTest(unittest.TestCase):
    def test_audio_contract_constants(self):
        # 识别引擎的契约：16 kHz 单声道、20 ms 一帧。前端 HELPER_PORT_SCAN 与扫描范围对齐
        self.assertEqual(config.TARGET_RATE, 16000)
        self.assertEqual(config.FRAME_MS, 20)
        self.assertEqual(config.DEFAULT_PORT, 8790)
        self.assertEqual(config.PORT_SCAN_RANGE, 20)
        self.assertEqual(config.DEFAULT_HOST, "127.0.0.1")

    def test_data_dir_is_per_platform_and_not_the_cwd(self):
        path = str(config.data_dir())
        self.assertTrue(path)
        self.assertIn(config.APP_NAME, path)
        self.assertNotEqual(path, os.getcwd())

    def test_no_model_or_web_hosting_helpers_left(self):
        for gone in ("find_web_root", "web_root_candidates", "find_models_dir",
                     "models_dir_candidates", "default_models_dir", "model_archive_url",
                     "ASR_DATA_REL"):
            self.assertFalse(hasattr(config, gone), "{} 应该已经删掉（助手不托管页面/模型）".format(gone))


if __name__ == "__main__":
    unittest.main()
