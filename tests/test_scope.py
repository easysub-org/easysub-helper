# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 hcz1017
"""范围闸门：助手只做两件事 —— **采本机音频** + **和浏览器配对**。

这些断言是机械的"防回潮"措施：托管主项目页面（静态文件、COOP/COEP）、模型目录与
模型下载、浏览器拉起、设备列举、HTML 配对页 —— 全部已经删掉，任何一条被写回来这里就会红。

为什么不用"扫源码里有没有某几个词"那种做法：注释和 docstring 里**故意**会提到
"助手连 onnxruntime 都不 import"这类说明，按词扫会误伤。这里改成查**结构**（文件、导入、
参数、路由），既准确又不受排版影响。
"""

import ast
import inspect
import io
import os
import unittest

from easysub_helper import config, gui, protocol
from easysub_helper.net import HelperServer

PACKAGE_DIR = os.path.dirname(os.path.abspath(HelperServer.__module__.replace(".", os.sep)))
ROOT = os.path.dirname(PACKAGE_DIR)

#: 这些能力已经删掉，文件不该再出现（注意 `audio/devices.py` 是**保留**的：
#: 设备枚举只服务窗口里的「设备」下拉，不进 HTTP 面、也不进协议）
REMOVED_MODULES = (
    "models.py",
    os.path.join("net", "static.py"),
    os.path.join("net", "pair_page.py"),
    "browser.py",
)

#: 导入名里出现这些片段的，就是把删掉的能力引回来了
FORBIDDEN_IMPORTS = ("onnx", "models", "static", "pair_page", "browser", "wasm")


def iter_sources():
    for base, _dirs, files in os.walk(PACKAGE_DIR):
        if "__pycache__" in base:
            continue
        for name in sorted(files):
            if name.endswith(".py"):
                yield os.path.join(base, name)


def imports_of(path):
    """该文件导入的所有模块名（相对导入带上点号前缀）。"""
    with io.open(path, encoding="utf-8") as fh:
        tree = ast.parse(fh.read())
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            names.add("." * node.level + (node.module or ""))
    return names


class RemovedCapabilitiesTest(unittest.TestCase):
    def test_hosting_and_model_modules_are_gone(self):
        for relative in REMOVED_MODULES:
            path = os.path.join(PACKAGE_DIR, relative)
            self.assertFalse(os.path.exists(path), "{} 应该已经删掉".format(relative))

    def test_no_import_brings_them_back(self):
        for path in iter_sources():
            for name in imports_of(path):
                for bad in FORBIDDEN_IMPORTS:
                    self.assertNotIn(bad, name,
                                     "{} 导入了 {}（助手不托管页面、也不碰模型）".format(path, name))

    def test_gui_has_no_page_opening_or_model_status(self):
        for gone in ("page_url", "open_subtitle_page", "error_box"):
            self.assertFalse(hasattr(gui, gone), "gui.{} 应该已经删掉".format(gone))

    def test_server_constructor_takes_no_hosting_paths(self):
        params = set(inspect.signature(HelperServer.__init__).parameters)
        for gone in ("web_root", "models_dir"):
            self.assertNotIn(gone, params)
        # 采集与配对该有的还在
        for kept in ("pairing", "port", "host", "default_source", "backend", "allow_origins",
                     "fixed_token", "user_on"):
            self.assertIn(kept, params)

    def test_config_has_no_model_or_web_helpers(self):
        for gone in ("find_web_root", "web_root_candidates", "find_models_dir",
                     "models_dir_candidates", "default_models_dir", "model_archive_url",
                     "ASR_DATA_REL", "WASM_VERSION", "WASM_MODEL"):
            self.assertFalse(hasattr(config, gone), "config.{} 应该已经删掉".format(gone))

    def test_protocol_has_no_devices_message(self):
        for gone in ("devices_msg", "CLIENT_DEVICES", "MSG_DEVICES"):
            self.assertFalse(hasattr(protocol, gone), "protocol.{} 应该已经删掉".format(gone))
        self.assertEqual(set(protocol.CLIENT_MESSAGE_TYPES), {"start", "stop", "ping"})


class HttpSurfaceTest(unittest.TestCase):
    """HTTP 面只有配对与 WS：多一条路由就说明又有人开始"顺手托管点东西"了。"""

    def setUp(self):
        self.app = HelperServer(pairing=None, port=8799, scan_ports=False).build_app()

    def test_resources_are_exactly_pair_info_pair_and_ws(self):
        self.assertEqual(sorted(r.canonical for r in self.app.router.resources()),
                         ["/api/pair", "/api/pair/info", "/ws"])

    def test_methods_are_exactly_the_expected_four(self):
        methods = set()
        for route in self.app.router.routes():
            resource = getattr(route, "resource", None)
            if resource is None:        # aiohttp 自带的 404/405 兜底不算我们的面
                continue
            methods.add(route.method)
        self.assertEqual(methods, {"GET", "HEAD", "OPTIONS", "POST"})


class PairingStillThereTest(unittest.TestCase):
    """瘦身不能把该有的东西删掉：配对、采集开关、快照都必须在。"""

    def test_core_api_survives(self):
        for kept in ("set_user_enabled", "start_capture", "stop_capture", "switch_source",
                     "snapshot", "handle_pair", "handle_pair_info", "handle_ws"):
            self.assertTrue(hasattr(HelperServer, kept), "HelperServer.{} 不该消失".format(kept))

    def test_gui_exposes_start_pause_and_language_switch(self):
        for kept in ("toggle_capture", "switch_language", "retranslate", "copy_code", "new_code"):
            self.assertTrue(hasattr(gui.HelperWindow, kept), "HelperWindow.{} 不该消失".format(kept))


if __name__ == "__main__":
    unittest.main()
