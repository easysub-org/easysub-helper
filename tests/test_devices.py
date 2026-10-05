# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 hcz1017
"""设备枚举：解析与截断都是纯函数，所以**不需要真声卡**就能测。

窗口里的「设备」下拉只服务坐在本机的人（页面不需要选设备），枚举结果错了用户就会选到打不开的
设备，所以这里盯三件事：pactl 解析对不对、标签会不会长得撑爆下拉、枚举不到设备时也要有
「系统默认」这一项。
"""

import unittest

from easysub_helper.audio import devices

PACTL = """\
41\talsa_output.pci-0000_00_1f.3.analog-stereo.monitor\tmodule-null-sink.c\tfloat32le 2ch 48000Hz\tIDLE
42\talsa_input.pci-0000_00_1f.3.analog-stereo\tmodule-alsa-card.c\ts16le 2ch 44100Hz\tSUSPENDED
43\talsa_output.pci-0000_00_1f.3.analog-stereo.monitor\tmodule-null-sink.c\tfloat32le 2ch 48000Hz\tRUNNING
44\talsa_input.usb-Plantronics.mic\tmodule-alsa-card.c\ts16le 1ch 16000Hz\tSUSPENDED
junk line
"""


class ParseTest(unittest.TestCase):
    def test_monitors_and_microphones_are_separated(self):
        self.assertEqual(devices.parse_pactl_sources(PACTL, monitor=False),
                         ["alsa_input.pci-0000_00_1f.3.analog-stereo",
                          "alsa_input.usb-Plantronics.mic"])
        self.assertEqual(devices.parse_pactl_sources(PACTL, monitor=True),
                         ["alsa_output.pci-0000_00_1f.3.analog-stereo.monitor"])

    def test_junk_and_short_lines_are_ignored(self):
        self.assertEqual(devices.parse_pactl_sources("junk line\n\n", monitor=False), [])
        self.assertEqual(devices.parse_pactl_sources("", monitor=True), [])
        self.assertEqual(devices.parse_pactl_sources(None, monitor=True), [])

    def test_space_separated_output_also_works(self):
        # `pactl list short sources` 在个别版本/语言下用空格而不是制表符
        text = "41 alsa_input.mic module-alsa-card.c s16le 1ch 16000Hz SUSPENDED\n"
        self.assertEqual(devices.parse_pactl_sources(text, monitor=False), ["alsa_input.mic"])


class LabelTest(unittest.TestCase):
    def test_short_names_are_left_alone(self):
        self.assertEqual(devices.label_for("Speakers (Realtek)"), "Speakers (Realtek)")

    def test_long_names_are_truncated(self):
        label = devices.label_for("a" * 200, limit=20)
        self.assertEqual(len(label), 20)
        self.assertTrue(label.endswith("…"))

    def test_whitespace_is_collapsed(self):
        self.assertEqual(devices.label_for("  A   B  "), "A B")
        self.assertEqual(devices.label_for(None), "")

    def test_internal_sentinels_get_readable_labels(self):
        """内部哨兵串不能原样显示（独立审查抓的：状态行出现「设备 @DEFAULT_MONITOR@」）。"""
        from easysub_helper.audio import subprocess_backend

        # 哨兵值必须与后端真正传给 parec/pw-record 的那个串一致（防两边漂移）
        self.assertEqual(devices.INTERNAL_DEVICE_LABELS.get(subprocess_backend.DEFAULT_MONITOR),
                         "audio.deviceDefaultSystem")
        self.assertNotEqual(devices.label_for(subprocess_backend.DEFAULT_MONITOR),
                            subprocess_backend.DEFAULT_MONITOR)
        for raw, key in devices.INTERNAL_DEVICE_LABELS.items():
            label = devices.label_for(raw)
            self.assertTrue(label, "空的显示标签: " + raw)
            self.assertNotIn("@", label, "哨兵没被换掉: " + raw)
            self.assertIsNotNone(key)

    def test_default_source_fallback_string_is_localized(self):
        self.assertNotEqual(devices.label_for("default source"), "default source")


PACTL_TABLE = """\
Source #52
\tState: SUSPENDED
\tName: alsa_output.pci-0000_00_1f.3.analog-stereo.monitor
\tDescription: Monitor of 内置音频 Analog Stereo

Source #53
\tState: RUNNING
\tName: alsa_input.pci-0000_00_1f.3.analog-stereo
\tDescription: 内置音频 Analog Stereo

Source #1269
\tState: RUNNING
\tName: bluez_input.5C_7D_35_C2_86_74.0
\tDescription: LE601
"""

#: 本地化输出（zh_CN 下 pactl 真的长这样：块头"信源 #52"、字段"名称：/描述："）
PACTL_TABLE_ZH = """\
信源 #52
\t状态：SUSPENDED
\t名称：alsa_output.pci-0000_00_1f.3.analog-stereo.monitor
\t描述：Monitor of 内置音频 Analog Stereo

信源 #53
\t状态：RUNNING
\t名称：alsa_input.pci-0000_00_1f.3.analog-stereo
\t描述：内置音频 Analog Stereo
"""


class SourceTableTest(unittest.TestCase):
    """`pactl list sources`：id 必须是**源名**（后端能用），label 用描述（给人看）。"""

    def test_names_and_descriptions_are_separated(self):
        items = devices.parse_pactl_source_table(PACTL_TABLE, monitor=False)
        self.assertEqual([item["id"] for item in items],
                         ["alsa_input.pci-0000_00_1f.3.analog-stereo",
                          "bluez_input.5C_7D_35_C2_86_74.0"])
        self.assertEqual([item["label"] for item in items], ["内置音频 Analog Stereo", "LE601"])

    def test_monitors_are_filtered(self):
        items = devices.parse_pactl_source_table(PACTL_TABLE, monitor=True)
        self.assertEqual([item["id"] for item in items],
                         ["alsa_output.pci-0000_00_1f.3.analog-stereo.monitor"])

    def test_localized_output_is_understood(self):
        # 实测 zh_CN 下字段名是"名称：/描述："、块头是"信源 #52"。
        # 调用侧虽然强制 LC_ALL=C，但解析本身也不该被 locale 搞死（第二道保险）。
        items = devices.parse_pactl_source_table(PACTL_TABLE_ZH, monitor=False)
        self.assertEqual([item["id"] for item in items],
                         ["alsa_input.pci-0000_00_1f.3.analog-stereo"])
        self.assertEqual(items[0]["label"], "内置音频 Analog Stereo")

    def test_missing_description_falls_back_to_the_name(self):
        text = "Source #9\n\tName: alsa_input.foo\n"
        items = devices.parse_pactl_source_table(text, monitor=False)
        self.assertEqual(items, [{"id": "alsa_input.foo", "label": "alsa_input.foo"}])

    def test_empty_input(self):
        self.assertEqual(devices.parse_pactl_source_table("", monitor=False), [])
        self.assertEqual(devices.parse_pactl_source_table(None, monitor=True), [])


class ResolveTest(unittest.TestCase):
    """解析用户给的设备名：**绝不静默回落到默认设备**（parec 会，用户就以为切换没生效）。"""

    TABLE = [
        {"id": "alsa_input.pci-0000_00_1f.3.analog-stereo", "label": "内置音频 Analog Stereo"},
        {"id": "bluez_input.5C_7D_35_C2_86_74.0", "label": "LE601"},
        {"id": "alsa_input.usb-Plantronics.mic", "label": "Plantronics USB"},
    ]

    def setUp(self):
        self._list = devices.list_devices
        items = [{"id": "", "label": "系统默认", "kind": "mic", "default": True}] + self.TABLE
        devices.list_devices = lambda source="mic", prefer_pactl=None: list(items)

    def tearDown(self):
        devices.list_devices = self._list

    def test_empty_device_means_default(self):
        self.assertEqual(devices.resolve("mic", None), (None, None))
        self.assertEqual(devices.resolve("mic", ""), (None, None))

    def test_exact_source_name(self):
        self.assertEqual(devices.resolve("mic", "bluez_input.5C_7D_35_C2_86_74.0"),
                         ("bluez_input.5C_7D_35_C2_86_74.0", None))

    def test_description_is_accepted(self):
        self.assertEqual(devices.resolve("mic", "LE601"),
                         ("bluez_input.5C_7D_35_C2_86_74.0", None))

    def test_unique_substring_still_works(self):
        # 老习惯：`--device Plantronics` 这种子串匹配要留着
        self.assertEqual(devices.resolve("mic", "plantronics"),
                         ("alsa_input.usb-Plantronics.mic", None))

    def test_ambiguous_substring_is_an_error(self):
        resolved, error = devices.resolve("mic", "alsa_input")
        self.assertIsNone(resolved)
        self.assertIn("alsa_input", error or "")

    def test_unknown_device_is_an_error_not_a_fallback(self):
        resolved, error = devices.resolve("mic", "不存在的麦克风")
        self.assertIsNone(resolved)
        self.assertTrue(error)
        self.assertIn("不存在的麦克风", error)

    def test_no_enumeration_lets_the_backend_decide(self):
        devices.list_devices = lambda source="mic", prefer_pactl=None: [
            {"id": "", "label": "系统默认", "kind": "mic", "default": True}]
        self.assertEqual(devices.resolve("mic", "whatever"), ("whatever", None))


class ListTest(unittest.TestCase):
    def setUp(self):
        self._soundcard = devices._soundcard_names
        self._short = devices._pactl_short_names
        self._table = devices._pactl_table

    def tearDown(self):
        devices._soundcard_names = self._soundcard
        devices._pactl_short_names = self._short
        devices._pactl_table = self._table

    def test_default_entry_always_comes_first(self):
        devices._soundcard_names = lambda source: []
        devices._pactl_short_names = lambda source: []
        devices._pactl_table = lambda source: []
        items = devices.list_devices("mic", prefer_pactl=False)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["id"], devices.DEFAULT_ID)
        self.assertTrue(items[0]["default"])
        self.assertEqual(items[0]["kind"], "mic")
        self.assertTrue(items[0]["label"])

    def test_enumerated_devices_get_id_label_and_kind(self):
        devices._soundcard_names = lambda source: ["USB Mic", "Built-in Mic"]
        items = devices.list_devices("mic", prefer_pactl=False)
        self.assertEqual([item["id"] for item in items],
                         [devices.DEFAULT_ID, "USB Mic", "Built-in Mic"])
        self.assertEqual(items[1]["label"], "USB Mic")
        self.assertEqual(items[1]["kind"], "mic")
        self.assertFalse(items[1]["default"])

    def test_pactl_short_list_is_used_as_fallback(self):
        devices._soundcard_names = lambda source: []
        devices._pactl_short_names = lambda source: ["alsa_output.speaker.monitor"]
        items = devices.list_devices("system", prefer_pactl=False)
        self.assertEqual([item["id"] for item in items],
                         [devices.DEFAULT_ID, "alsa_output.speaker.monitor"])

    def test_duplicates_are_dropped(self):
        devices._soundcard_names = lambda source: ["Mic", "Mic"]
        devices._pactl_short_names = lambda source: ["Other"]
        ids = [item["id"] for item in devices.list_devices("mic", prefer_pactl=False)]
        self.assertEqual(ids, [devices.DEFAULT_ID, "Mic"])

    def test_linux_prefers_pactl_source_names(self):
        """Linux 上默认后端是 parec：id 必须是**源名**，否则后端静默回落默认源。"""
        devices._pactl_table = lambda source: [
            {"id": "alsa_input.foo", "label": "内置音频"}]
        devices._soundcard_names = lambda source: ["内置音频"]
        items = devices.list_devices("mic", prefer_pactl=True)
        self.assertEqual([item["id"] for item in items], [devices.DEFAULT_ID, "alsa_input.foo"])
        self.assertEqual(items[1]["label"], "内置音频")


if __name__ == "__main__":
    unittest.main()
