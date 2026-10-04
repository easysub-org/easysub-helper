# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 hcz1017
"""服务端 e2e：配对 → 令牌 → WS → PCM；以及「启动/暂停」总开关的语义。

两层（和采集测试同样的理由）：
  - 绝大多数用例用**假后端**（patch `create_backend`），不需要声卡，CI 上也能跑；
    暂停=静音帧、启动=真实帧、没配对也能采，这些开关语义全靠它覆盖；
  - 真实采集只有设 `EASYSUB_HELPER_AUDIO_TESTS=1` 才跑。
"""

import asyncio
import json
import os
import socket
import time
import unittest

import aiohttp
import numpy as np

from easysub_helper.audio import AudioBackend
from easysub_helper.audio import devices as audio_devices
from easysub_helper.net import HelperServer
from easysub_helper.net import server as helper_server
from easysub_helper.pairing import PairingManager

AUDIO_TESTS = os.environ.get("EASYSUB_HELPER_AUDIO_TESTS") == "1"
FRAME_BYTES = 320 * 4          # 16 kHz / 20 ms / f32le
SILENCE = b"\x00" * FRAME_BYTES


def free_port():
    sock = socket.socket()
    try:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]
    finally:
        sock.close()


class FakeBackend(AudioBackend):
    """恒定直流 0.5 的假后端：不需要任何音频设备，用来区分"真实帧"和"静音帧"。"""

    name = "fake"

    def __init__(self, value=0.5, **kwargs):
        AudioBackend.__init__(self, **kwargs)
        self.value = float(value)
        self.closed = False

    def open(self):
        self._opened = True

    def read(self, frames):
        if self.closed:
            return np.zeros(0, dtype="float32")     # 空数组 = EOF
        time.sleep(frames / float(self.rate))       # 按真实时长出块，别空转烧 CPU
        return np.full(int(frames), self.value, dtype="float32")

    def close(self):
        self.closed = True
        self._opened = False


def accept_any_device():
    """让设备解析对任何名字都点头。

    解析逻辑（pactl 源名 / 唯一子串 / 歧义 / 找不到）在 tests/test_devices.py 里单独测；
    服务端测试只关心"解析结果有没有如实传给后端、失败有没有退回"，所以这里把它替掉，
    免得测试依赖跑测试的那台机器上到底插了几个麦克风。
    """
    original = audio_devices.resolve
    audio_devices.resolve = lambda source, device: (device, None) if device else (None, None)
    return lambda: setattr(audio_devices, "resolve", original)


def reject_all_devices(message="找不到设备"):
    original = audio_devices.resolve
    audio_devices.resolve = lambda source, device: (None, message) if device else (None, None)
    return lambda: setattr(audio_devices, "resolve", original)


def fake_backend_factory(value=0.5):
    def factory(source="system", backend="auto", device=None, rate=16000, frame_ms=20):
        return FakeBackend(source=source, device=device, rate=rate, frame_ms=frame_ms, value=value)
    return factory


async def recv_binary(ws, count=2, timeout=3.0):
    """收 count 个二进制帧（文本消息直接跳过）。"""
    out = []
    while len(out) < count:
        msg = await asyncio.wait_for(ws.receive(), timeout=timeout)
        if msg.type == aiohttp.WSMsgType.BINARY:
            out.append(msg.data)
        elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.CLOSING):
            break
    return out


async def recv_text_type(ws, mtype, timeout=3.0, match=None):
    """收一条指定 type 的文本消息；match(payload) 为真才算数。"""
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        msg = await asyncio.wait_for(ws.receive(), timeout=timeout)
        if msg.type != aiohttp.WSMsgType.TEXT:
            continue
        payload = json.loads(msg.data)
        if payload.get("type") == mtype and (match is None or match(payload)):
            return payload
    raise AssertionError("没等到 type={} 的消息".format(mtype))


class ServerCase(unittest.TestCase):
    #: 默认关（窗口总开关的初值）——这也正是产品要求："默认关"
    USER_ON = False

    def setUp(self):
        self.pairing = PairingManager(persist=False)
        self.code = self.pairing.new_code()
        self.port = free_port()
        self.server = HelperServer(
            pairing=self.pairing, port=self.port, scan_ports=False,
            version="test", rate=16000, frame_ms=20, user_on=self.USER_ON,
        )

    def use_fake_backend(self, value=0.5):
        original = helper_server.create_backend
        helper_server.create_backend = fake_backend_factory(value)
        self.addCleanup(lambda: setattr(helper_server, "create_backend", original))

    def use_recording_backend(self, value=0.5):
        """假后端 + 记录每次建后端的参数（用来断言"换设备真的重开了设备"）。"""
        calls = []
        original = helper_server.create_backend

        def factory(source="system", backend="auto", device=None, rate=16000, frame_ms=20):
            calls.append({"source": source, "device": device})
            return FakeBackend(source=source, device=device, rate=rate, frame_ms=frame_ms,
                               value=value)

        helper_server.create_backend = factory
        self.addCleanup(lambda: setattr(helper_server, "create_backend", original))
        return calls

    @property
    def base(self):
        return "http://127.0.0.1:{}".format(self.server.port)

    @property
    def origin(self):
        return "http://127.0.0.1:{}".format(self.server.port)

    def run_case(self, factory, **kwargs):
        for key, value in kwargs.items():
            setattr(self.server, key, value)
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(factory())
        finally:
            loop.close()

    def with_server(self, body, **kwargs):
        async def runner():
            await self.server.start()
            try:
                async with aiohttp.ClientSession() as session:
                    return await body(session)
            finally:
                await self.server.stop()

        return self.run_case(runner, **kwargs)

    def token(self):
        return self.pairing.issue_token("test")

    def ws_url(self, token):
        return "ws://127.0.0.1:{}/ws?token={}".format(self.server.port, token)


class HttpTest(ServerCase):
    def test_pair_info_is_public_but_minimal(self):
        async def body(session):
            async with session.get(self.base + "/api/pair/info") as resp:
                self.assertEqual(resp.status, 200)
                data = await resp.json()
            self.assertEqual(data["app"], "easysub-helper")
            self.assertTrue(data["pairingRequired"])
            self.assertFalse(data["paired"])
            self.assertTrue(data["paused"])          # 默认关
            self.assertEqual(data["sampleRate"], 16000)
            # 托管/模型时代留下的字段必须消失（助手不托管任何东西、也不碰模型）
            for gone in ("modelReady", "devices", "pairCodeActive", "capturing", "sources"):
                self.assertNotIn(gone, data)

        self.with_server(body)

    def test_only_the_pairing_and_ws_routes_exist(self):
        async def body(session):
            for path in ("/", "/index.html", "/assets/icon48.png", "/wasm/x.data",
                         "/api/health", "/api/devices", "/pair"):
                async with session.get(self.base + path) as resp:
                    self.assertEqual(resp.status, 404, "{} 不该被服务".format(path))

        self.with_server(body)

    def test_pair_wrong_code_then_success(self):
        async def body(session):
            async with session.post(self.base + "/api/pair", json={"code": "ZZZZZZ"},
                                    headers={"Origin": self.origin}) as resp:
                self.assertEqual(resp.status, 403)
                data = await resp.json()
                self.assertFalse(data["ok"])
                self.assertEqual(data["code"], "bad_code")
            async with session.post(self.base + "/api/pair", json={"code": self.code, "label": "chrome"},
                                    headers={"Origin": self.origin}) as resp:
                self.assertEqual(resp.status, 200)
                data = await resp.json()
            self.assertTrue(data["ok"])
            self.assertTrue(self.pairing.token_valid(data["token"]))

        self.with_server(body)

    def test_pair_lockout_returns_429(self):
        async def body(session):
            status = 0
            for _ in range(self.pairing.max_attempts + 1):
                async with session.post(self.base + "/api/pair", json={"code": "ZZZZZZ"},
                                        headers={"Origin": self.origin}) as resp:
                    status = resp.status
            self.assertEqual(status, 429)

        self.with_server(body)

    def test_pair_rejects_broken_body(self):
        async def body(session):
            async with session.post(self.base + "/api/pair", data=b"{not json",
                                    headers={"Origin": self.origin}) as resp:
                self.assertEqual(resp.status, 400)

        self.with_server(body)

    def test_cors_only_for_allowed_origin(self):
        async def body(session):
            async with session.get(self.base + "/api/pair/info",
                                   headers={"Origin": self.origin}) as resp:
                self.assertEqual(resp.headers.get("Access-Control-Allow-Origin"), self.origin)
            async with session.get(self.base + "/api/pair/info",
                                   headers={"Origin": "https://evil.example.com"}) as resp:
                self.assertIsNone(resp.headers.get("Access-Control-Allow-Origin"))

        self.with_server(body)


class WebSocketTest(ServerCase):
    def test_ws_rejects_bad_token(self):
        async def body(session):
            try:
                await session.ws_connect(self.ws_url("bad"), headers={"Origin": self.origin})
                self.fail("坏令牌不应握手成功")
            except aiohttp.ClientError as exc:
                self.assertEqual(getattr(exc, "status", None), 403)

        self.with_server(body)

    def test_ws_token_is_the_only_credential(self):
        """令牌有效就接受：扩展的 offscreen 文档不一定带 Origin，硬拦 Origin 会误伤用户。"""
        async def body(session):
            async with session.ws_connect(self.ws_url(self.token())) as ws:      # 完全没有 Origin
                hello = json.loads((await asyncio.wait_for(ws.receive(), timeout=5)).data)
                self.assertEqual(hello["type"], "hello")

        self.with_server(body)

    def test_ws_hello_declares_paused_by_default(self):
        async def body(session):
            async with session.ws_connect(self.ws_url(self.token()),
                                          headers={"Origin": self.origin}) as ws:
                hello = json.loads((await asyncio.wait_for(ws.receive(), timeout=5)).data)
                self.assertEqual(hello["type"], "hello")
                self.assertEqual(hello["sampleRate"], 16000)
                self.assertEqual(hello["frameMs"], 20)
                self.assertEqual(hello["format"], "f32le")
                self.assertFalse(hello["capturing"])
                self.assertTrue(hello["paused"])
                self.assertIn(hello["lang"], ("zh_CN", "en"))

        self.with_server(body)

    def test_bad_client_message_gets_error(self):
        async def body(session):
            async with session.ws_connect(self.ws_url(self.token()),
                                          headers={"Origin": self.origin}) as ws:
                await asyncio.wait_for(ws.receive(), timeout=5)      # hello
                await ws.send_str("not json")
                payload = await recv_text_type(ws, "error")
                self.assertEqual(payload["code"], "bad_message")

        self.with_server(body)

    def test_start_while_paused_is_rejected_and_silence_keeps_flowing(self):
        """暂停（默认）时：页面点开始不会打开设备，但连接与静音帧不受影响。"""
        self.use_fake_backend()

        async def body(session):
            async with session.ws_connect(self.ws_url(self.token()),
                                          headers={"Origin": self.origin}) as ws:
                await asyncio.wait_for(ws.receive(), timeout=5)      # hello
                await ws.send_str(json.dumps({"type": "start", "source": "system"}))
                payload = await recv_text_type(ws, "error")
                self.assertEqual(payload["code"], "paused")
                self.assertFalse(payload["fatal"])
                frames = await recv_binary(ws, 2)
                self.assertEqual(len(frames), 2, "暂停时也必须持续推帧（否则页面会以为断了）")
                for frame in frames:
                    self.assertEqual(len(frame), FRAME_BYTES)
                    self.assertEqual(frame, SILENCE)
                self.assertIsNone(self.server._session, "暂停时不该打开采集设备")

        self.with_server(body)

    def test_start_after_switch_streams_real_frames_then_pause_silences(self):
        self.use_fake_backend(0.5)

        async def body(session):
            ok, err = await self.server.set_user_enabled(True)
            self.assertTrue(ok, err)
            async with session.ws_connect(self.ws_url(self.token()),
                                          headers={"Origin": self.origin}) as ws:
                hello = json.loads((await asyncio.wait_for(ws.receive(), timeout=5)).data)
                self.assertTrue(hello["capturing"])
                self.assertFalse(hello["paused"])
                await ws.send_str(json.dumps({"type": "start", "source": "system"}))
                frames = await recv_binary(ws, 2)
                self.assertTrue(all(frame != SILENCE for frame in frames), "启动后应是真实音频帧")

                # 暂停：设备关掉、连接不断、帧变成全零
                ok, err = await self.server.set_user_enabled(False)
                self.assertTrue(ok, err)
                state = await recv_text_type(ws, "state", match=lambda p: p["paused"] is True)
                self.assertFalse(state["capturing"])
                frames = await recv_binary(ws, 2)
                self.assertTrue(all(frame == SILENCE for frame in frames))

        self.with_server(body)

    def test_last_client_disconnect_does_not_stop_capture(self):
        """设备归窗口总开关管：页面走了、用户没按暂停，就继续采（没配对也要能测）。"""
        self.use_fake_backend()

        async def body(session):
            self.assertTrue((await self.server.set_user_enabled(True))[0])
            async with session.ws_connect(self.ws_url(self.token()),
                                          headers={"Origin": self.origin}) as ws:
                await asyncio.wait_for(ws.receive(), timeout=5)
                await ws.send_str(json.dumps({"type": "start"}))
                await recv_binary(ws, 1)
            await asyncio.sleep(0.1)
            snap = self.server.snapshot()
            self.assertTrue(snap["capturing"], "最后一个客户端断开不该关设备")
            self.assertEqual(snap["clients"], 0)
            self.assertTrue((await self.server.set_user_enabled(False))[0])
            self.assertFalse(self.server.snapshot()["capturing"])

        self.with_server(body)

    @unittest.skipUnless(AUDIO_TESTS, "需要真实音频设备：设 EASYSUB_HELPER_AUDIO_TESTS=1 打开")
    def test_real_capture_streams_pcm(self):
        async def body(session):
            self.assertTrue((await self.server.set_user_enabled(True))[0])
            async with session.ws_connect(self.ws_url(self.token()),
                                          headers={"Origin": self.origin}) as ws:
                await asyncio.wait_for(ws.receive(), timeout=5)      # hello
                await ws.send_str(json.dumps({"type": "start", "source": "system"}))
                frames = 0
                deadline = asyncio.get_event_loop().time() + 3.0
                while frames < 40 and asyncio.get_event_loop().time() < deadline:
                    try:
                        msg = await asyncio.wait_for(ws.receive(), timeout=2)
                    except asyncio.TimeoutError:
                        break
                    if msg.type == aiohttp.WSMsgType.BINARY:
                        self.assertEqual(len(msg.data), FRAME_BYTES, "每帧必须是 20ms/16k/f32le")
                        frames += 1
                    elif msg.type == aiohttp.WSMsgType.TEXT:
                        payload = json.loads(msg.data)
                        if payload["type"] == "error":
                            self.fail("采集报错: {}".format(payload))
                self.assertGreaterEqual(frames, 40, "3 秒内至少应有 40 帧（约 0.8 秒音频）")

        self.with_server(body)


class SnapshotTest(ServerCase):
    """GUI 依赖的只读快照与总开关（界面线程要能在不发请求的情况下拿到状态）。"""

    def test_snapshot_reflects_running_server(self):
        async def body(_session):
            snap = self.server.snapshot()
            self.assertTrue(snap["running"])
            self.assertEqual(snap["port"], self.port)
            self.assertEqual(snap["url"], self.base)
            self.assertFalse(snap["capturing"])
            self.assertTrue(snap["paused"])          # 默认关
            self.assertFalse(snap["userOn"])
            self.assertEqual(snap["clients"], 0)
            self.assertIsNone(snap["error"])
            for key in ("levelRms", "levelPeak", "levelAt"):
                self.assertIn(key, snap)

        self.with_server(body)

    def test_snapshot_before_start_is_not_running(self):
        snap = self.server.snapshot()
        self.assertFalse(snap["running"])
        self.assertFalse(snap["capturing"])
        self.assertEqual(snap["frames"], 0)

    def test_start_without_any_client_captures(self):
        """没配对、没页面、没人连：按了「启动」也要能采——窗口电平图就是靠它验证音频。"""
        self.use_fake_backend()

        async def body(_session):
            ok, err = await self.server.set_user_enabled(True)
            self.assertTrue(ok, err)
            self.assertEqual(self.server.clients, 0)
            snap = self.server.snapshot()
            self.assertTrue(snap["capturing"])
            self.assertFalse(snap["paused"])
            # 等电平/帧真的来（采集线程在跑；电平是每 100ms 报一次，所以要比帧多等一会）
            deadline = time.time() + 3.0
            while time.time() < deadline and self.server.last_level["rms"] <= 0.0:
                await asyncio.sleep(0.05)
            self.assertGreaterEqual(self.server.snapshot()["frames"], 3)
            self.assertGreater(self.server.last_level["rms"], 0.0)

        self.with_server(body)

    def test_set_user_enabled_is_idempotent(self):
        self.use_fake_backend()

        async def body(_session):
            self.assertTrue((await self.server.set_user_enabled(True))[0])
            self.assertTrue((await self.server.set_user_enabled(True))[0])
            self.assertTrue(self.server.snapshot()["capturing"])
            self.assertTrue((await self.server.set_user_enabled(False))[0])
            self.assertTrue((await self.server.set_user_enabled(False))[0])
            self.assertFalse(self.server.snapshot()["capturing"])

        self.with_server(body)

    def test_open_failure_keeps_the_switch_off(self):
        def boom(**kwargs):
            from easysub_helper.audio import BackendError

            raise BackendError("no device")

        original = helper_server.create_backend
        helper_server.create_backend = boom
        self.addCleanup(lambda: setattr(helper_server, "create_backend", original))

        async def body(_session):
            ok, err = await self.server.set_user_enabled(True)
            self.assertFalse(ok)
            self.assertIn("no device", err)
            snap = self.server.snapshot()
            self.assertFalse(snap["userOn"], "打不开设备就该退回暂停，窗口不能显示正在采集")
            self.assertFalse(snap["capturing"])
            self.assertTrue(snap["error"])

        self.with_server(body)

    def test_switch_source_without_capture_only_remembers(self):
        async def body(_session):
            ok, err = await self.server.switch_source("mic")
            self.assertTrue(ok)
            self.assertIsNone(err)
            self.assertEqual(self.server.default_source, "mic")
            self.assertFalse(self.server.snapshot()["capturing"])

        self.with_server(body)

    def test_switch_device_remembers_without_capture(self):
        self.addCleanup(accept_any_device())

        async def body(_session):
            ok, err = await self.server.switch_device("USB Mic")
            self.assertTrue(ok)
            self.assertIsNone(err)
            self.assertEqual(self.server.snapshot()["device"], "USB Mic")
            # 空串/None = 回到系统默认设备
            self.assertTrue((await self.server.switch_device(None))[0])
            self.assertIsNone(self.server.snapshot()["device"])

        self.with_server(body)

    def test_switch_source_resets_the_device(self):
        """设备名与音源绑定（麦克风名 ≠ monitor 名）：换音源必须丢掉旧设备名。"""
        self.addCleanup(accept_any_device())

        async def body(_session):
            await self.server.switch_device("USB Mic")
            await self.server.switch_source("mic")
            self.assertIsNone(self.server.snapshot()["device"])
            # 音源没变时不动设备选择
            await self.server.switch_device("USB Mic")
            await self.server.switch_source("mic")
            self.assertEqual(self.server.snapshot()["device"], "USB Mic")

        self.with_server(body)

    def test_switch_device_restarts_capture_with_the_new_device(self):
        self.addCleanup(accept_any_device())
        calls = self.use_recording_backend()

        async def body(_session):
            self.assertTrue((await self.server.set_user_enabled(True))[0])
            self.assertTrue(self.server.snapshot()["capturing"])
            ok, err = await self.server.switch_device("USB Mic")
            self.assertTrue(ok, err)
            self.assertTrue(self.server.snapshot()["capturing"], "换设备后应继续采集")
            self.assertEqual([call["device"] for call in calls], [None, "USB Mic"])

        self.with_server(body)

    def test_unknown_device_fails_loudly_and_keeps_the_old_one(self):
        """设备名解析不出来：同步报错 + 保持原设备，绝不静默换成默认源。

        背景（用户实测）：Linux 的 parec 对未知设备名**静默回落默认源**，所以
        "切换没生效"曾经表现为"声音还是从原来那个麦克风来"。
        """
        self.addCleanup(reject_all_devices("找不到设备 X"))
        calls = self.use_recording_backend()

        async def body(_session):
            self.assertTrue((await self.server.set_user_enabled(True))[0])
            self.assertIsNone(self.server.device)
            ok, error = await self.server.switch_device("X")
            self.assertFalse(ok)
            self.assertEqual(error, "找不到设备 X")
            self.assertIsNone(self.server.device, "解析失败不该改设备选择")
            self.assertTrue(self.server.snapshot()["capturing"], "原设备应继续工作")
            self.assertEqual([call["device"] for call in calls], [None], "不该用坏名字重开设备")

        self.with_server(body)

    def test_switch_source_rejects_unknown_source(self):
        async def body(_session):
            ok, err = await self.server.switch_source("speaker")
            self.assertFalse(ok)
            self.assertTrue(err)
            self.assertEqual(self.server.default_source, "system")

        self.with_server(body)

    def test_stop_when_idle_is_harmless(self):
        async def body(_session):
            self.assertFalse(await self.server.stop_capture(announce=False))
            self.assertTrue(self.server.snapshot()["running"])

        self.with_server(body)


if __name__ == "__main__":
    unittest.main()
