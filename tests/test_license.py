# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 hcz1017
"""许可证一致性闸门：改了 LICENSE 却忘了 pyproject / 源码头 / 界面入口，这里会红。

背景：2026-10-05 把 MIT 换成 AGPL-3.0-or-later。AGPL 的主张要站得住，至少得做到
"仓库里有全文、元数据声明一致、每个源文件带 SPDX、界面里给得出对应源码地址"——
这四件事全靠人记是不可靠的，所以钉成测试。
"""

import io
import os
import unittest

from easysub_helper import config

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class LicenseTest(unittest.TestCase):
    def test_license_file_is_agpl3(self):
        text = io.open(os.path.join(ROOT, "LICENSE"), encoding="utf-8").read()
        self.assertIn("GNU AFFERO GENERAL PUBLIC LICENSE", text)
        self.assertIn("Version 3, 19 November 2007", text)

    def test_pyproject_declares_the_same_license(self):
        text = io.open(os.path.join(ROOT, "pyproject.toml"), encoding="utf-8").read()
        self.assertIn('license = { text = "%s" }' % config.LICENSE_ID, text)
        self.assertIn("AGPLv3+", text, "缺少 OSI 分类器，PyPI 上会显示成未知许可证")

    def test_config_license_id(self):
        self.assertEqual(config.LICENSE_ID, "AGPL-3.0-or-later")

    def test_every_source_file_has_an_spdx_header(self):
        """**整仓**的 .py 都要有 SPDX 头（含 tools/ 打包脚本、tests/、根目录入口 launcher.py）。

        为什么扩到整仓：打包入口、构建脚本也是"分发出去的一部分"，漏了它们就等于漏了署名。
        """
        missing = []
        needle = "SPDX-License-Identifier: %s" % config.LICENSE_ID
        skip_dirs = {".venv", "__pycache__", "build", "dist", "release-assets"}
        for base, dirs, files in os.walk(ROOT):
            dirs[:] = [d for d in dirs if d not in skip_dirs and not d.startswith(".")]
            for name in sorted(files):
                if not name.endswith(".py"):
                    continue
                path = os.path.join(base, name)
                head = io.open(path, encoding="utf-8").read(400)
                if needle not in head:
                    missing.append(os.path.relpath(path, ROOT))
        self.assertEqual(missing, [], "这些文件没有 SPDX 头：%s" % missing)

    def test_project_url_is_a_public_repo(self):
        """AGPL 第 13 条的"对应源码"入口必须是个真地址（窗口右下角就点它）。"""
        self.assertTrue(config.PROJECT_URL.startswith("https://github.com/"),
                        config.PROJECT_URL)
        self.assertIn("easysub", config.PROJECT_URL)


if __name__ == "__main__":
    unittest.main()
