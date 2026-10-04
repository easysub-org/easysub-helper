# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 hcz1017
"""路径、默认值与平台判定。

纪律：本文件（以及整个包）不得 import 主项目的任何东西——它要能独立成库/独立打包。
与主项目的耦合只发生在**运行时**：通过本机 WS 把 PCM 交给页面。

助手**不碰识别模型**：模型是页面里那套 wasm 引擎（sherpa-onnx）在读，助手连 onnxruntime
都不 import。这里只关心音频参数、端口，和一点点用户偏好（界面语言）。
"""

import json
import os
import sys
import tempfile
from pathlib import Path

APP_NAME = "easysub-helper"

#: 源码地址：AGPL 第 13 条要求向"通过网络使用本程序"的人提供对应源码，
#: 助手是本机 WS 服务 + 窗口里给出这个入口，最省事的合规做法。
PROJECT_URL = "https://github.com/easysub-org/easysub-helper"

#: 许可证标识（与 LICENSE 文件、pyproject.toml 保持一致）
LICENSE_ID = "AGPL-3.0-or-later"

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8790
#: 端口被占时向后尝试的范围。**前端探测范围与它对齐**（src/helper.ts 的 HELPER_PORT_SCAN）
PORT_SCAN_RANGE = 20

#: 识别引擎的契约：16 kHz 单声道。PCM 在线路上的格式固定为 f32le（见 protocol.py）。
TARGET_RATE = 16000
FRAME_MS = 20
LEVEL_INTERVAL_MS = 100


def is_windows() -> bool:
    return sys.platform.startswith("win")


def is_macos() -> bool:
    return sys.platform == "darwin"


def is_linux() -> bool:
    return sys.platform.startswith("linux")


def data_dir() -> Path:
    """用户数据目录（配对库、界面偏好）。"""
    if is_windows():
        base = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA") or str(Path.home())
        return Path(base) / APP_NAME
    if is_macos():
        return Path.home() / "Library" / "Application Support" / APP_NAME
    base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(base) / APP_NAME


def settings_path() -> Path:
    return data_dir() / "settings.json"


def load_settings() -> dict:
    """读用户偏好（目前只有界面语言）。

    坏文件、无权限、目录不存在一律当"没有偏好"——偏好不该拦住启动。
    """
    try:
        with open(str(settings_path()), "r", encoding="utf-8") as fh:
            values = json.load(fh)
    except Exception:  # noqa: BLE001 - IOError/ValueError/权限/文件不存在
        return {}
    return values if isinstance(values, dict) else {}


def save_settings(values) -> bool:
    """原子写（临时文件 + os.replace），失败返回 False。

    只读目录/沙箱里写不进去时也必须能继续用——语言选择下次再记。
    """
    path = settings_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        handle, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".settings-", suffix=".json")
        try:
            with os.fdopen(handle, "w", encoding="utf-8") as fh:
                json.dump(values, fh, ensure_ascii=False, indent=2, sort_keys=True)
            os.replace(tmp, str(path))
        except Exception:  # noqa: BLE001 - 写失败要清掉临时文件再往外抛给下面兜住
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
        return True
    except Exception:  # noqa: BLE001
        return False
