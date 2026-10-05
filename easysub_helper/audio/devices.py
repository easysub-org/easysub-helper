# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 hcz1017
"""采集设备枚举与**解析**（服务助手窗口的「设备」下拉，以及 `--device` 参数）。

为什么只在本机用、不进 HTTP：页面**不需要**选设备——扩展/Web 的麦克风授权框自带设备选择。
只有坐在本机的人才会想"换一个麦克风"。所以设备列表不进协议、也不进 HTTP 面。

**为什么还要"解析"**：Linux 上默认走后端是 `parec`/`pw-record`，而这两个工具对**未知设备名
是静默回落到默认源**的（实测：`parec --device=definitely-not-a-source` 照样一直采）。
如果直接把界面上的名字丢过去，用户会看到"我换了麦克风但声音还是从原来那个来"——正是用户报的 bug。
所以这里先把用户给的名字解析成后端真正能用的 **PulseAudio 源名**（`pactl list sources` 的
`Name:`），解析不出来就**大声报错**，绝不静默。

同一份 `list_devices()` 会给不同后端两套 id：
  - Linux + parec/pw-record：id = `pactl` 的源名，label = 描述（"内置音频 Analog Stereo"）；
  - 其它平台（soundcard）：id = label = 设备名（WASAPI/CoreAudio 就用这个名字）。
两者的解析与截断都是**纯函数**，单测不需要真声卡。
"""

import os
import shutil
import subprocess

from .. import config
from ..i18n import t

#: 空 id = 用系统默认设备（后端不传 device，交给操作系统挑）
DEFAULT_ID = ""

#: 下拉里标签的最大长度（Linux 的 monitor 名经常 60+ 字符，太长会把下拉撑爆）
LABEL_LIMIT = 60

#: 那些**不是给人看**的内部设备串：显示前必须换成可读文案。
#: `@DEFAULT_MONITOR@` 是 PulseAudio「默认输出 monitor」的哨兵值（原样传给 parec/pw-record
#: 才有意义），`default source` 是 mic 未指定设备时的兜底串。独立审查抓到的现象：
#: 窗口状态行显示成「设备 @DEFAULT_MONITOR@」。测试盯住它俩与 subprocess_backend 同值。
INTERNAL_DEVICE_LABELS = {
    "@DEFAULT_MONITOR@": "audio.deviceDefaultSystem",
    "default source": "audio.deviceDefaultInput",
}


def default_entry(source="mic"):
    return {"id": DEFAULT_ID, "label": t("device.default"), "kind": source, "default": True}


def label_for(name, limit=LABEL_LIMIT):
    """把设备名压成适合下拉显示的一行（内部哨兵值换成可读文案）。"""
    text = " ".join(str(name or "").split())
    key = INTERNAL_DEVICE_LABELS.get(text)
    if key:
        return t(key)
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


# ---------------- pactl 解析（纯函数）----------------
def parse_pactl_sources(text, monitor=False):
    """解析 `pactl list short sources`：挑出（非）monitor 源的**名字**。

    行格式：`<index>\\t<name>\\t<module>\\t<sample spec>\\t<state>`。
    """
    out = []
    for line in (text or "").splitlines():
        parts = line.split("\t")
        if len(parts) < 2:
            # 个别版本/语言用空格分隔；此时**首列必须是索引数字**，
            # 否则随便一行文本都会被当成设备名（测试盯过这个坑）
            parts = line.split()
            if len(parts) < 2 or not parts[0].isdigit():
                continue
        name = parts[1].strip()
        if not name or name in out:
            continue
        if (".monitor" in name) != bool(monitor):
            continue
        out.append(name)
    return out


#: 字段名在 `pactl` 输出里是**本地化**的（实测 zh_CN 下是"名称：/描述："），所以两种都认；
#: 调用侧还会强制 LC_ALL=C（见 _run_pactl），这里是第二道保险。
_NAME_KEYS = ("name", "名称")
_DESCRIPTION_KEYS = ("description", "描述")


def _is_block_header(line):
    """`Source #52` / `信源 #52` / `Source Output #7` 都算块头（末段是 #数字）。"""
    tail = line.rsplit(" ", 1)[-1]
    return tail.startswith("#") and tail[1:].isdigit()


def parse_pactl_source_table(text, monitor=False):
    """解析 `pactl list sources`（完整版）：拿源名（后端能用）+ 描述（给人看）。

    返回 `[{"id": 源名, "label": 描述}]`；没有描述就用源名当标签。
    """
    blocks = []
    current = None
    for raw in (text or "").splitlines():
        line = raw.strip()
        if _is_block_header(line):
            if current is not None:
                blocks.append(current)
            current = {}
            continue
        if current is None or ":" not in line and "：" not in line:
            continue
        key, _, value = line.replace("：", ":").partition(":")
        key = key.strip().lower()
        if key in _NAME_KEYS:
            current["id"] = value.strip()
        elif key in _DESCRIPTION_KEYS:
            current["label"] = value.strip()
    if current is not None:
        blocks.append(current)

    out = []
    for block in blocks:
        name = (block.get("id") or "").strip()
        if not name:
            continue
        if (".monitor" in name) != bool(monitor):
            continue
        out.append({"id": name, "label": (block.get("label") or "").strip() or name})
    return out


# ---------------- 枚举 ----------------
def _tool_available():
    """Linux 上默认后端（parec/pw-record）在不在。"""
    try:
        from .subprocess_backend import find_tool

        return bool(find_tool())
    except Exception:  # noqa: BLE001
        return False


def prefer_pactl_names():
    """设备 id 要不要用 pactl 源名（Linux + 子进程后端才是）。"""
    return config.is_linux() and _tool_available()


def _run_pactl(args):
    """跑一条 pactl 命令并返回文本。

    刻意**强制 LC_ALL=C**：pactl 的字段名/块头会跟着 locale 变（实测 zh_CN 下是
    "信源 #52 / 名称：/ 描述："），按英文写死的解析在这种环境里会静默拿到空列表，
    而空列表的后果是"设备下拉里没有设备"或"用 soundcard 的友好名去喂 parec"。
    """
    tool = shutil.which("pactl")
    if not tool:
        return ""
    env = dict(os.environ)
    env["LC_ALL"] = "C"
    env["LANG"] = "C"
    try:
        raw = subprocess.check_output([tool] + list(args), stderr=subprocess.DEVNULL,
                                      timeout=3, env=env)
    except Exception:  # noqa: BLE001 - 没装/没服务/超时
        return ""
    return raw.decode("utf-8", "replace") if isinstance(raw, (bytes, bytearray)) else str(raw)


def _pactl_table(source):
    return parse_pactl_source_table(_run_pactl(["list", "sources"]),
                                    monitor=(source == "system"))


def _pactl_short_names(source):
    return parse_pactl_sources(_run_pactl(["list", "short", "sources"]),
                               monitor=(source == "system"))


def _soundcard_names(source):
    """用 soundcard 枚举设备名；任何异常（没装/没有音频服务）都当"枚举不到"。"""
    try:
        from .soundcard_backend import com_uninitialize, import_and_prepare_com
    except Exception:  # noqa: BLE001 - 没装 soundcard
        return []
    # Windows 上枚举设备同样走 COM，而这里是 GUI 的工作线程 → 也要（按正确顺序）准备 COM。
    # 注意：**读 `mic.name` / `mic.isloopback` 也要公寓**（它们会 CoCreateInstance 去问设备），
    # 所以 COM 必须活到整个循环结束，不能只包住 all_microphones()。
    soundcard, com_taken = import_and_prepare_com()
    try:
        if soundcard is None:
            return []
        mics = soundcard.all_microphones(include_loopback=(source == "system")) or []
        names = []
        for mic in mics:
            name = getattr(mic, "name", None) or str(mic)
            loopback = bool(getattr(mic, "isloopback", False))
            if (source == "system") != loopback:
                continue
            if name and name not in names:
                names.append(name)
        return names
    except Exception:  # noqa: BLE001 - 没声卡/没服务/PulseAudio 拒绝连接
        return []
    finally:
        if com_taken:
            com_uninitialize()


def list_devices(source="mic", prefer_pactl=None):
    """返回 `[系统默认, 设备…]`；枚举不到设备时就只有「系统默认」一项。"""
    if prefer_pactl is None:
        prefer_pactl = prefer_pactl_names()
    entries = []
    if prefer_pactl:
        entries = _pactl_table(source)
    if not entries:
        entries = [{"id": name, "label": name} for name in _soundcard_names(source)]
    if not entries:
        entries = [{"id": name, "label": name} for name in _pactl_short_names(source)]

    items = [default_entry(source)]
    seen = set()
    for entry in entries:
        name = (entry.get("id") or "").strip()
        if not name or name in seen:
            continue
        seen.add(name)
        items.append({"id": name, "label": label_for(entry.get("label") or name),
                      "kind": source, "default": False})
    return items


# ---------------- 解析用户给的设备名 ----------------
def resolve(source, device):
    """把界面/`--device` 给的字符串解析成**后端真正能用的设备 id**。

    返回 `(id, error)`；`device` 为空时返回 `(None, None)`。顺序：
    精确 id → 大小写不敏感 id → 精确标签 → 唯一子串。找不到或有歧义都返回 error
    ——**绝不静默回落到默认设备**（parec 会，用户看到的就是"切换没生效"）。
    设备枚举不出来时（没有 pactl / 没有声卡）原样返回，交给后端自己判。
    """
    if not device:
        return None, None
    text = str(device).strip()
    if not text:
        return None, None
    entries = list_devices(source, prefer_pactl=True)[1:]
    if not entries:
        return text, None
    for item in entries:
        if item["id"] == text:
            return item["id"], None
    lowered = text.lower()
    for item in entries:
        if item["id"].lower() == lowered:
            return item["id"], None
    for item in entries:
        if item["label"].lower() == lowered:
            return item["id"], None
    hits = [item for item in entries
            if lowered in item["id"].lower() or lowered in item["label"].lower()]
    if len(hits) == 1:
        return hits[0]["id"], None
    if len(hits) > 1:
        return None, t("audio.err.deviceAmbiguous", device=text, count=len(hits))
    return None, t("audio.err.deviceNotFound", device=text)
