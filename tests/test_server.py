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
import threading
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

    def test_pair_rejects_disallowed_origin(self):
        """**显式收紧**（`--restrict-origin`）时，白名单是真闸门：不允许的来源连正确码也不给令牌。

        （默认是全放行 —— 产品要求，见 test_pairs_from_any_origin_by_default。
         独立审查发现过：以前只回不回 CORS 头，请求照样被处理并下发 token。）
        """
        self.server.restrict_origin = True

        async def body(session):
            async with session.post(self.base + "/api/pair", json={"code": self.code},
                                    headers={"Origin": "https://evil.example.com"}) as resp:
                self.assertEqual(resp.status, 403)
                data = await resp.json()
                self.assertEqual(data["code"], "forbidden")
                self.assertNotIn("token", data)
            self.assertEqual(self.pairing.state()["devices"], 0, "不该因为被拒的请求发出令牌")

        self.with_server(body)

    def test_pair_rejects_simple_request_from_evil_origin(self):
        """收紧模式下，不触发 CORS 预检的"简单请求"（text/plain）同样要被拒。"""
        self.server.restrict_origin = True

        async def body(session):
            async with session.post(self.base + "/api/pair", data=json.dumps({"code": self.code}),
                                    headers={"Origin": "https://evil.example.com",
                                             "Content-Type": "text/plain"}) as resp:
                self.assertEqual(resp.status, 403)

        self.with_server(body)

    def test_pairs_from_any_origin_by_default(self):
        """**默认**（不加任何开关）任意 Origin 都能配对：这是产品要求 —— CORS 全放行，
        部署在任何域名/预览站的 Web 版都要开箱能用，不许让用户去找 `--allow-cors-all`。

        安全前提不变：令牌仍然是配对码换来的 —— 放行的是 Origin，不是鉴权。
        """
        origin = "https://easysub-preview.example.com"

        async def body(session):
            async with session.post(self.base + "/api/pair", json={"code": self.code},
                                    headers={"Origin": origin}) as resp:
                self.assertEqual(resp.status, 200)
                data = await resp.json()
                self.assertIn("token", data)
                self.assertEqual(resp.headers.get("Access-Control-Allow-Origin"), origin)
            self.assertEqual(self.pairing.state()["devices"], 1)

        self.with_server(body)

    def test_pair_info_omits_cors_unless_origin_allowed(self):
        """收紧模式下未放行的来源拿不到 CORS 头（浏览器会拦掉响应）—— 白名单才是防线。"""
        self.server.restrict_origin = True

        async def body(session):
            async with session.get(self.base + "/api/pair/info",
                                   headers={"Origin": "https://easysub-preview.example.com"}) as resp:
                self.assertEqual(resp.status, 200)
                self.assertIsNone(resp.headers.get("Access-Control-Allow-Origin"))
            async with session.get(self.base + "/api/pair/info",
                                   headers={"Origin": self.base}) as resp:
                self.assertEqual(resp.headers.get("Access-Control-Allow-Origin"), self.base)

        self.with_server(body)

    def test_pair_rejects_missing_origin_unless_allowed(self):
        """没有 Origin（curl/脚本）默认拒绝；显式 allow_no_origin 才放行。"""
        async def body(session):
            async with session.post(self.base + "/api/pair", json={"code": self.code}) as resp:
                self.assertEqual(resp.status, 403)

        self.with_server(body)

        async def allowed(session):
            async with session.post(self.base + "/api/pair", json={"code": self.code}) as resp:
                self.assertEqual(resp.status, 200)
                self.assertTrue((await resp.json())["ok"])

        self.with_server(allowed, allow_no_origin=True)

    def test_pair_label_must_be_a_string(self):
        """label 传非字符串不能 500（无令牌端点，任何人都能发）；list 也不许原样回显。

        （独立审查发现：`[:80]` 对 int/bool 会 TypeError → 500；对 list 会原样回显。）
        """
        async def body(session):
            for label in (123, True, ["a"], {"a": 1}, None):
                async with session.post(self.base + "/api/pair",
                                        json={"code": self.code, "label": label},
                                        headers={"Origin": self.origin}) as resp:
                    self.assertEqual(resp.status, 200, "label=%r" % (label,))
                    data = await resp.json()
                    self.assertIsInstance(data["label"], str)
            # 字符串要清洗：压成一行 + 截断到 80
            async with session.post(self.base + "/api/pair",
                                    json={"code": self.code, "label": "  a\n b  " + "x" * 200},
                                    headers={"Origin": self.origin}) as resp:
                expected = ("a b " + "x" * 200)[:80]      # 压一行(已去首尾空白)后截断到 80
                self.assertEqual((await resp.json())["label"], expected)

        self.with_server(body)

    def test_pair_label_must_be_a_string_on_fixed_token_path(self):
        """--token 调试路径同样不许被 label 类型打挂（以前是 500 全灭）。"""
        async def body(session):
            async with session.post(self.base + "/api/pair", json={"label": 123},
                                    headers={"Origin": self.origin}) as resp:
                self.assertEqual(resp.status, 200)
                self.assertEqual((await resp.json())["label"], "debug")

        self.with_server(body, fixed_token="debug-token")

    def test_port_zero_reports_the_bound_port(self):
        """--port 0 时横幅/快照不许谎报 0：要回读真实绑定端口。"""
        async def body(session):
            self.assertGreater(self.server.port, 0, "应回读真实绑定端口")
            async with session.get(self.server.base_url + "/api/pair/info") as resp:
                self.assertEqual(resp.status, 200)

        self.with_server(body, port=0, scan_ports=False)

    def test_ws_connection_cap_returns_503(self):
        """WS 连接数上限：第 limit+1 条连接必须 503，不能让失控页面吃满内存。"""
        async def body(session):
            self.server.WS_MAX_CONNECTIONS = 1
            token = (await (await session.post(self.base + "/api/pair",
                                               json={"code": self.code},
                                               headers={"Origin": self.origin})).json())["token"]
            async with session.ws_connect(self.base + "/ws?token=" + token) as first:
                self.assertEqual((await first.receive_json())["type"], "hello")
                # 第二条连接应被 503 拒绝（握手阶段）
                with self.assertRaises(aiohttp.WSServerHandshakeError) as ctx:
                    async with session.ws_connect(self.base + "/ws?token=" + token):
                        pass
                self.assertEqual(ctx.exception.status, 503)

        self.with_server(body, user_on=True)

    def test_non_ascii_pair_code_is_bad_code_and_counts_toward_lockout(self):
        """非 ASCII 配对码以前会 500 且**不计入限速**（compare_digest 抛 TypeError）→ 现在必须是干净的 bad_code。"""
        async def body(session):
            async with session.post(self.base + "/api/pair", json={"code": "测试"},
                                    headers={"Origin": self.origin}) as resp:
                self.assertEqual(resp.status, 403)
                self.assertEqual((await resp.json())["code"], "bad_code")
            self.assertEqual(len(self.pairing._attempts), 1, "失败必须计入限速，别被异常绕过")

        self.with_server(body)

    def test_non_ascii_origin_does_not_500(self):
        async def body(session):
            async with session.get(self.base + "/api/pair/info",
                                   headers={"Origin": "http://exémple.com"}) as resp:
                self.assertEqual(resp.status, 200)
                self.assertTrue((await resp.json())["ok"])

        self.with_server(body)

    def test_cors_headers_for_any_origin_by_default(self):
        """默认对**任意** Origin 都回 CORS 头（产品要求：不做手动设置）。"""
        async def body(session):
            for origin in (self.origin, "https://evil.example.com",
                           "https://easysub-preview.example.com"):
                async with session.get(self.base + "/api/pair/info",
                                       headers={"Origin": origin}) as resp:
                    self.assertEqual(resp.headers.get("Access-Control-Allow-Origin"), origin)

        self.with_server(body)

    def test_cors_only_for_allowed_origin_when_restricted(self):
        """显式收紧后，未放行的来源不回 CORS 头（浏览器会拦掉响应）。"""
        self.server.restrict_origin = True

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

    def test_silence_ticker_does_not_stack_across_reconnects(self):
        """暂停时「断开 → 立刻重连」（= 暂停状态下刷新页面）不许叠出多个静音 ticker。

        坑（独立审查实测的竞态）：ticker 引用曾有两处写者（_sync_silence 与协程的 finally）。
        取消是异步的，于是「断开触发 cancel 摘引用」与「重连触发新建 ticker」可以交错，
        旧协程的 finally 会把**新**引用抹成 None → 新 ticker 成孤儿继续跑，下一次
        _sync_silence 又建一个 → 每个 ticker 每 20ms 推一帧，速率翻倍并随重连累积。
        复现必须打中这个交错：靠真实 TCP 断开/重连的时序是**打不中**的（实测旧实现在
        那种写法下也只有 27 帧/0.5s，纯粹的假绿），所以这里按服务端自己的调用顺序
        精确驱动 _queues + _sync_silence，再量队列里的到达速率。
        """
        async def body(_session):
            # ① 连上：want=True → ticker A
            q1 = asyncio.Queue()
            self.server._queues.add(q1)
            self.server._sync_silence()
            first = self.server._silence_task
            self.assertIsNotNone(first, "连上后应起静音 ticker")
            await asyncio.sleep(0)

            # ② 断开：want=False → 摘引用并 cancel（A 的 finally 还没跑）
            self.server._queues.discard(q1)
            self.server._sync_silence()
            self.assertIsNone(self.server._silence_task, "断开后应摘掉引用")

            # ③ 同一拍内立刻重连：want=True → 建 ticker B
            q2 = asyncio.Queue()
            self.server._queues.add(q2)
            self.server._sync_silence()
            second = self.server._silence_task
            self.assertIsNotNone(second, "重连后应起新静音 ticker")
            self.assertIsNot(second, first)

            await asyncio.sleep(0)     # 让 A 的 finally 跑：修复前它会把 B 的引用抹掉

            # ④ 再来一次"新连接"的 _sync_silence：引用若被抹过，这里会又建一个 ticker
            q3 = asyncio.Queue()
            self.server._queues.add(q3)
            self.server._sync_silence()

            # 量 0.3s 内 q3 的到达速率：额定 20ms/帧 → ~15 帧；叠加一个 ticker 会接近 30
            n = 0
            deadline = asyncio.get_event_loop().time() + 0.3
            while asyncio.get_event_loop().time() < deadline:
                while True:
                    try:
                        q3.get_nowait()
                        n += 1
                    except asyncio.QueueEmpty:
                        break
                await asyncio.sleep(0.005)

            self.server._queues.clear()
            self.server._sync_silence()
            self.assertGreater(n, 5, "静音帧太少（{} 帧/0.3s）——ticker 没在跑".format(n))
            self.assertLess(n, 24, "静音帧速率异常（{} 帧/0.3s，额定 ~15）：静音 ticker 叠加了".format(n))

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

    def test_runtime_capture_error_returns_switch_and_keeps_silence_flowing(self):
        """运行中采集失败：总开关退回暂停（窗口回到「启动」= 一键重试），静音帧不能断。

        坑（独立审查抓的 minor）：以前只清 _session、把 user_on 留在 True——状态成了
        paused=false + capturing=false：窗口按钮仍显示「暂停」（重试要点两次），
        而且 _sync_silence 认为"用户要采"，连静音帧都不推，页面彻底断流。
        """
        class BoomBackend(FakeBackend):
            name = "boom"

            def read(self, frames):
                raise RuntimeError("device unplugged")

        original = helper_server.create_backend
        helper_server.create_backend = lambda source="system", backend="auto", device=None, \
            rate=16000, frame_ms=20: BoomBackend(source=source, device=device, rate=rate,
                                                 frame_ms=frame_ms)
        self.addCleanup(lambda: setattr(helper_server, "create_backend", original))

        async def body(session):
            async with session.ws_connect(self.ws_url(self.token()),
                                          headers={"Origin": self.origin}) as ws:
                await asyncio.wait_for(ws.receive(), timeout=5)          # hello
                ok, err = await self.server.set_user_enabled(True)
                self.assertTrue(ok, err)
                payload = await recv_text_type(ws, "error", timeout=5)
                self.assertEqual(payload["code"], "capture_failed")
                self.assertTrue(payload["fatal"])
                snap = self.server.snapshot()
                self.assertFalse(snap["userOn"], "采集挂了就该退回暂停，让用户一键重试")
                self.assertFalse(snap["capturing"])
                self.assertTrue(snap["paused"], "状态不能自相矛盾（paused=false 且 capturing=false）")
                frames = await recv_binary(ws, 2, timeout=5)
                self.assertEqual(len(frames), 2, "采集出错后静音帧必须继续推，否则页面假装在跑")
                for frame in frames:
                    self.assertEqual(frame, SILENCE)

        self.with_server(body)

    def test_switch_source_without_capture_only_remembers(self):
        async def body(_session):
            ok, err = await self.server.switch_source("mic")
            self.assertTrue(ok)
            self.assertIsNone(err)
            self.assertEqual(self.server.default_source, "mic")
            self.assertFalse(self.server.snapshot()["capturing"])

        self.with_server(body)

    def test_switch_source_while_paused_then_start_uses_the_new_source(self):
        """暂停状态下切音源，再点「启动」：必须用**新**音源。

        坑（全盲审查实测复现的 major）：`_capture_source` 一旦采集过就永久残留，而
        `set_user_enabled(True)` 重开时 `_open_locked()` 不带 source → 残留值优先于窗口新选的
        `default_source`。实测：采 system → 暂停 → 切麦克风 → 再启动，实际建的还是 system
        （backends_built=['system','system']）——"暂停换音源再启动"被无声吞掉。
        （旧用例只断言 default_source 变了，没覆盖"之后再启动用哪个"，正是盲区所在。）
        """
        built = []
        original = helper_server.create_backend

        def recording(source="system", backend="auto", device=None, rate=16000, frame_ms=20):
            built.append(source)
            return FakeBackend(source=source, device=device, rate=rate,
                               frame_ms=frame_ms, value=0.5)

        helper_server.create_backend = recording
        self.addCleanup(lambda: setattr(helper_server, "create_backend", original))

        async def body(_session):
            # 采 system → 暂停（_capture_source 从此残留 "system"）
            self.assertTrue((await self.server.set_user_enabled(True))[0])
            self.assertTrue((await self.server.set_user_enabled(False))[0])
            # 暂停状态下切到 mic：窗口下拉显示已切换（此前只改 default_source）
            ok, err = await self.server.switch_source("mic")
            self.assertTrue(ok, err)
            # 再点「启动」：必须用 mic，而不是残留的 system
            self.assertTrue((await self.server.set_user_enabled(True))[0])
            self.assertEqual(built, ["system", "mic"],
                             "暂停后切音源没生效：实际建的后端 = %r" % built)
            self.assertEqual(self.server.snapshot()["source"], "mic")

        self.with_server(body)

    def test_switch_source_failure_recovers_switch_and_keeps_silence_flowing(self):
        """采集中切音源、重启失败：总开关退回暂停 + 静音帧续推（不能无声冻结）。

        坑（独立审查实测的 major）：`_restart` 失败以前只把错误返回，服务器停在
        `user_on=True + capturing=False` 的自相矛盾状态——页面 1 秒内只收到 1 帧后彻底断流，
        窗口按钮仍显示「暂停」，重试要点两次。现在失败后走 `_recover_after_failed_restart`：
        与"运行时采集挂掉"同一套契约（退回暂停、广播状态、静音续推）。
        """
        def mic_fails(source="system", backend="auto", device=None, rate=16000, frame_ms=20):
            from easysub_helper.audio import BackendError

            if source == "mic":
                raise BackendError("no microphone here")
            return FakeBackend(source=source, device=device, rate=rate,
                               frame_ms=frame_ms, value=0.5)

        original = helper_server.create_backend
        helper_server.create_backend = mic_fails
        self.addCleanup(lambda: setattr(helper_server, "create_backend", original))

        async def body(session):
            async with session.ws_connect(self.ws_url(self.token()),
                                          headers={"Origin": self.origin}) as ws:
                await asyncio.wait_for(ws.receive(), timeout=5)          # hello
                ok, err = await self.server.set_user_enabled(True)
                self.assertTrue(ok, err)
                # 等真实帧：刚取消的静音 ticker 可能还压了几帧全零进队列，别被它骗了
                saw_real = False
                for _ in range(50):
                    frame = (await recv_binary(ws, 1, timeout=5))[0]
                    if frame != SILENCE:
                        saw_real = True
                        break
                self.assertTrue(saw_real, "启动后应出现真实帧")

                ok, err = await self.server.switch_source("mic")        # ← 重启失败
                self.assertFalse(ok)
                self.assertIn("no microphone", err)

                snap = self.server.snapshot()
                self.assertFalse(snap["userOn"], "重启失败必须把总开关退回暂停（一键重试）")
                self.assertTrue(snap["paused"], "状态不能自相矛盾（paused=False 且 capturing=False）")
                self.assertFalse(snap["capturing"])
                # 页面那条流不能断：接下来必须开始出现静音帧
                saw_silence = False
                for _ in range(50):
                    frame = (await recv_binary(ws, 1, timeout=5))[0]
                    if frame == SILENCE:
                        saw_silence = True
                        break
                self.assertTrue(saw_silence, "切换失败后必须续推静音帧，否则页面无声冻结")

        self.with_server(body)

    def _slow_close_backend(self, opened, built=None):
        """造一个"关闭很慢"的后端：`_join` 会被拖住 ~0.3s，好让并发操作落进那个锁间隙。

        `built` 传入列表时会记录每次建后端用的 `source`（用于断言"最后谁生效"）。
        """
        class SlowCloseBackend(FakeBackend):
            def open(self):
                super().open()
                opened.add(id(self))

            def close(self):
                time.sleep(0.3)
                super().close()
                opened.discard(id(self))

        def factory(source="system", backend="auto", device=None, rate=16000, frame_ms=20):
            if built is not None:
                built.append(source)
            return SlowCloseBackend(source=source, device=device, rate=rate,
                                    frame_ms=frame_ms, value=0.5)

        original = helper_server.create_backend
        helper_server.create_backend = factory
        self.addCleanup(lambda: setattr(helper_server, "create_backend", original))

    def test_source_clicked_during_restart_gap_wins(self):
        """重启间隙里又点了一次音源：**最后一次选择必须生效**（第九轮审查实测的 major）。

        根因：`_restart` 以前用调用时快照的 `source` 开设备，而 `_open_locked(source)` 还会把它
        **写回** `_capture_source`。间隙里用户再点一次别的音源只落库（非采集态分支不开设备），
        随后在飞的重启用**旧** source 开设备并覆盖回来 —— GUI 下拉显示最后一次选择、实际采的
        却是旧音源，暂停再启动仍是旧的（选择永久失效，head 与基线同样 2/2 复现）。
        修法：重开时**无参**调用 `_open_locked()`，取每次切换都已同步好的"当前意图"。
        """
        opened, built = set(), []
        self._slow_close_backend(opened, built)

        async def body(_session):
            ok, err = await self.server.set_user_enabled(True)      # 默认 source = system
            self.assertTrue(ok, err)
            # 点「麦克风」→ 采集态 → 走重启（慢 join，锁在这个窗口里放开）
            restart = asyncio.ensure_future(self.server.switch_source("mic"))
            await asyncio.sleep(0.1)                                # 让重启进到 join 间隙
            # 间隙里又点回「系统音频」：这是用户**最后一次**选择
            ok, err = await self.server.switch_source("system")
            self.assertTrue(ok, err)
            await restart

            snap = self.server.snapshot()
            self.assertEqual(snap["source"], "system",
                             "★ 最后一次点击必须生效（旧实现会让 'mic' 覆盖回来）")
            self.assertEqual(built[-1], "system",
                             "实际建的后端也必须是最后一次选择：built=%r" % built)
            self.assertTrue(snap["capturing"], "设备必须真的被打开（不能变成没人开的静音态）")

        self.with_server(body)

    def test_pause_during_restart_gap_does_not_reopen_capture(self):
        """重启的 join 间隙里点「暂停」：不得再开出会话（第八轮审查 S1，实测 3/3 复现）。

        根因：`_restart` 在 `await self._join(session)` 处放开锁，间隙里用户点了「暂停」——
        那次暂停把 user_on 置 False 并接管清理，而重启回来后**照常开设备**，于是得到
        `userOn=False + capturing=True` 的自相矛盾态：真实帧与静音帧交错、窗口显示"已暂停"
        却在采音频。修法：join 之后复查开关/代次，有更新的意图就作废本次重启。

        哪一条在承重（第九轮审查的变异结论）：**`if not self.user_on` 复查**。把代次机制
        整个删掉这条测试仍是绿的；只有同时去掉 user_on 复查，它才会红。
        """
        opened = set()
        self._slow_close_backend(opened)

        async def body(_session):
            ok, err = await self.server.set_user_enabled(True)
            self.assertTrue(ok, err)
            # 发起重启（切音源）→ 它会先 detach + 慢 join，锁在这个窗口里是放开的
            restart = asyncio.ensure_future(self.server.switch_source("mic"))
            await asyncio.sleep(0.1)                       # 让重启进到 join 间隙
            self.assertTrue((await self.server.set_user_enabled(False))[0])   # 间隙里点暂停
            await restart

            snap = self.server.snapshot()
            self.assertFalse(snap["userOn"], "暂停态")
            self.assertTrue(snap["paused"], "状态必须自洽（paused=True）")
            self.assertFalse(snap["capturing"], "★ S1：暂停后重启不得再开出会话来")
            self.assertEqual(len(opened), 0, "不得留下打开的采集设备")

        self.with_server(body)

    def test_page_start_during_restart_gap_does_not_orphan_a_session(self):
        """页面 start 落进重启间隙：不得把它的会话覆盖成孤儿（第八轮审查 S2，实测 2/2）。

        根因：页面 WS onopen 会自动发 start，若它落在 `_restart` 的锁间隙里，
        `start_capture` 见 `_session is None` 就开出一个会话，随后重启的 `_open_locked`
        **无条件覆盖** `_session` —— 前一个会话没人 join/stop，变成并行推帧的孤儿
        （实测双会话各 ~50fps、暂停后仍剩 1 个 running + backend 未 close）。
        修法：页面 start 也推进代次（让在飞的重启作废）+ `_open_locked` 不覆盖存活会话。

        哪一条在承重（第九轮审查的变异结论）：**`_open_locked` 开头的"不覆盖存活会话"**。
        只删代次机制时这条测试仍是绿的；两道都去掉才会红（打开 backend 数 2≠1）。
        """
        opened = set()
        self._slow_close_backend(opened)

        async def body(_session):
            ok, err = await self.server.set_user_enabled(True)
            self.assertTrue(ok, err)
            restart = asyncio.ensure_future(self.server.switch_source("mic"))
            await asyncio.sleep(0.1)                       # 让重启进到 join 间隙
            # 页面（或任何客户端）在这个间隙里请求开始
            self.assertTrue((await self.server.start_capture("mic"))[0])
            await restart

            snap = self.server.snapshot()
            self.assertTrue(snap["capturing"], "应该有一个会话在采")
            self.assertEqual(len(opened), 1,
                             "★ S2：只允许一个会话在采（双会话=孤儿，backend 会泄漏）")

        self.with_server(body)

    def test_queued_page_start_must_not_open_device_while_paused(self):
        """自己那次「启动」失败后，排队的页面 start 不得在暂停态把设备开出来（并发审计 F1）。

        契约：**页面 start 只是意图，绝不自己开设备**。触发链：用户点「启动」→ `_open_locked`
        持锁卡在设备解析（pactl，数十~数百毫秒）→ 此窗口里页面 WS 发来 start（它看到的
        `paused=false`，因为 user_on 已被置 True）→ 用户那次启动失败、把 user_on 退回 False
        → 锁释放后**排队的 start 拿到锁**：不复查就会在暂停态把设备开出来，终态
        `userOn=False + capturing=True` 粘住，页面真实帧与静音帧各 ~50fps 并存（实测）。

        哪一条在承重（我自己的变异结论，别再说反了）：**`_open_locked` 顶部那道兜底**
        （`if self._shutting_down or not self.user_on`）。只删 `start_capture` 里的复查这条
        测试**仍是绿的**（被兜底接住）；把两道都删掉它才会红。`start_capture` 那道的作用是
        让页面拿到正确的 `ERR_PAUSED` 契约、而不是让它去尝试开设备。
        """
        from easysub_helper.audio import BackendError

        opened = []

        def slow_resolve(source, device):
            time.sleep(0.3)                       # 拖住锁，让页面 start 排队
            return device or "default-dev", None

        def failing_open(source="system", backend="auto", device=None, rate=16000, frame_ms=20):
            opened.append(source)
            raise BackendError("device busy")     # 用户这次启动失败

        orig_resolve = helper_server.audio_devices.resolve
        orig_create = helper_server.create_backend
        helper_server.audio_devices.resolve = slow_resolve
        helper_server.create_backend = failing_open

        def restore():
            helper_server.audio_devices.resolve = orig_resolve
            helper_server.create_backend = orig_create

        self.addCleanup(restore)

        async def body(_session):
            # 让本次启动走"带设备名"的解析路径（device 为空时 _resolve_device 直接返回、不 await）
            self.server.device = "USB Mic"
            enable = asyncio.ensure_future(self.server.set_user_enabled(True))
            await asyncio.sleep(0.1)              # 让它进到解析 await（此时锁被持有）
            page = asyncio.ensure_future(self.server.start_capture("system"))   # 页面 start 排队
            ok, err = await enable
            self.assertFalse(ok, "用户这次启动应当失败：%r" % (err,))
            pok, perr = await page
            self.assertFalse(pok, "★ F1：暂停态下页面 start 不得成功（%r）" % (perr,))
            snap = self.server.snapshot()
            self.assertFalse(snap["userOn"], "总开关已退回暂停")
            self.assertFalse(snap["capturing"], "★ F1：暂停态不得有会话在采")
            self.assertIsNone(self.server._session, "不得留下没人管的会话")
            self.assertEqual(opened, ["system"], "只允许那一次失败的尝试：opened=%r" % opened)

        self.with_server(body)

    def test_capture_error_is_attributed_to_the_bound_session(self):
        """旧会话的迟到错误不得记到新会话头上（并发审计 F2：否则新会话成孤儿、backend 泄漏）。

        以前 `_on_capture_error` 跑在采集线程里**现读 `self._session`** 判断"谁出错了"：
        旧会话在 `session.stop` 排队（默认线程池被慢解析占住）时抛错，就会被认成新会话 →
        新会话被清掉且**没人 join** → 孤儿继续推帧、backend 泄漏（实测甚至跨过 server.stop()
        还活着）。修法：错误回调在构造后绑定到自己的会话实例。

        另：陈旧错误**不得外溢**（第十一轮审查的 major 新缝）—— 不写 `last_error`、不广播
        `fatal=true` 的 `capture_failed`，否则窗口红字「采集故障」压住「采集中」，页面侧
        （该码不在静音清单里）会当成 ERROR 把整场会话连模型一起拆掉，而音频其实一直在流。
        """
        async def body(_session):
            # 坑（第十一轮审查抓的 CI blocker）：**必须装假后端**。漏了这一句就去开真设备，
            # Linux 上碰巧能过、Windows runner 必红（CI run 37468672278 就是这么红的）。
            self.use_fake_backend()
            self.assertTrue((await self.server.set_user_enabled(True))[0])
            old = self.server._session
            self.assertIsNotNone(old)
            self.assertTrue((await self.server.switch_source("mic"))[0])
            new = self.server._session
            self.assertIsNot(old, new, "换音源后应当是新会话")
            error_before = self.server.snapshot()["error"]
            # 旧会话"迟到"的错误回调（用它的**绑定**回调，模拟采集线程在排队窗口后才报错）
            old.on_error(RuntimeError("stale session failed"))
            await asyncio.sleep(0.05)             # 等 call_soon_threadsafe 的回调跑完
            self.assertIs(self.server._session, new, "★ F2：新会话不得被旧会话的错误清掉")
            self.assertTrue(new.running, "★ F2：新会话必须还在跑（不能变成没人 join 的孤儿）")
            self.assertTrue(self.server.snapshot()["capturing"], "新会话仍应在采")
            self.assertEqual(self.server.snapshot()["error"], error_before,
                             "★ F2 新缝：陈旧会话的错误不得写进 last_error（窗口会挂红字）")
            self.assertTrue(self.server.snapshot()["userOn"], "陈旧错误不得把总开关拉回暂停")

        self.with_server(body)

    def test_capture_failure_during_device_switch_keeps_the_error_visible(self):
        """切设备期间采集失败：窗口必须仍能显示失败原因（既存 minor，第十三轮审查实测 3/3）。

        场景：采集中点「设备」下拉（慢解析）→ 解析期间采集挂了（失败回调把开关退回暂停、
        致命 capture_failed 也广播出去了）→ 解析回来走"非采集态成功"分支，把**刚写的**
        `last_error` 清成 None。结果：页面拿到了 fatal 错误，窗口却不再显示原因。
        修法：清之前比对"错误代次"，只在等待期间没发生采集失败时才清。
        """
        self.use_fake_backend()
        started, release = self._patch_resolve_by_device("设备A")

        async def body(_session):
            self.assertTrue((await self.server.set_user_enabled(True))[0])
            current = self.server._session
            loop = asyncio.get_event_loop()
            slow = asyncio.ensure_future(self.server.switch_device("设备A"))
            await loop.run_in_executor(None, started.wait, 5)   # 慢解析已挂住
            # 解析期间采集挂了（当前会话的真错误）
            self.server._on_capture_error(RuntimeError("device unplugged"), current)
            await asyncio.sleep(0.05)                           # 让 loop 侧的错误处理跑完
            self.assertFalse(self.server.snapshot()["userOn"], "错误应已把开关退回暂停")
            release.set()                                       # 放行慢解析
            await slow
            snap = self.server.snapshot()
            self.assertFalse(snap["capturing"])
            self.assertEqual(snap["error"], "device unplugged",
                             "★ 刚发生的采集失败不得被非采集态成功分支清掉（窗口要显示原因）")

        self.with_server(body)

    def test_failed_start_error_survives_a_concurrent_device_switch(self):
        """「启动」失败刚写下的错误，不得被在飞的（更早发起的）设备切换清掉（R14-1）。

        根因（第十四轮审查实测）：`_error_seq` 以前只在"采集运行中挂掉"那一条路径 +1，
        `_open_locked` 的解析/后端失败、切换失败等 4 个写入点不推进代次 → 一个更早发起、
        仍在慢解析里的切换动作回来后按"代次相等"把它当成陈旧错误清掉。而 GUI 只认
        `snapshot()["error"]`（gui.py 的 error 分支还排在 paused 之前），于是用户点「启动」
        失败后窗口回到「已暂停」且**不显示任何原因**。
        修法：所有非 None 写入统一走 `_set_error()`（内部推进代次）。
        """
        from easysub_helper.audio import BackendError

        started, release = self._patch_resolve_by_device("设备A", slow_error=None)
        calls = {"n": 0}

        def fail_on_second_open(source="system", backend="auto", device=None,
                                rate=16000, frame_ms=20):
            calls["n"] += 1
            if calls["n"] >= 2:                     # 用户那次「启动」失败
                raise BackendError("device busy")
            return FakeBackend(source=source, device=device, rate=rate,
                               frame_ms=frame_ms, value=0.5)

        orig = helper_server.create_backend
        helper_server.create_backend = fail_on_second_open
        self.addCleanup(lambda: setattr(helper_server, "create_backend", orig))

        async def body(_session):
            loop = asyncio.get_event_loop()
            self.server.device = "USB Mic"
            self.assertTrue((await self.server.set_user_enabled(True))[0])
            # 更早发起的设备切换：慢解析（解析期间事件循环是空的，下面那次启动能挤进来）
            slow = asyncio.ensure_future(self.server.switch_device("设备A"))
            await loop.run_in_executor(None, started.wait, 5)
            # 解析期间：用户点「暂停」再点「启动」，这次启动失败 → 写下新错误
            self.assertTrue((await self.server.set_user_enabled(False))[0])
            ok, err = await self.server.set_user_enabled(True)
            self.assertFalse(ok, "这次启动应当失败：%r" % (err,))
            self.assertEqual(self.server.snapshot()["error"], "device busy")
            release.set()                                 # 放行那个慢解析
            await slow
            self.assertEqual(self.server.snapshot()["error"], "device busy",
                             "★ R14-1：刚发生的启动失败不得被在飞的切换清掉（窗口要显示原因）")

        self.with_server(body)

    def test_pause_noop_does_not_erase_a_fresh_error(self):
        """「启动」失败后紧跟的「暂停」（no-op 分支）不得擦掉刚写下的失败原因（第十七轮 F17-1）。

        路径：用户点「启动」（慢解析、注定失败）→ 解析窗口里按钮已变「暂停」→ 用户点「暂停」，
        这个协程排在启动的锁后面；启动失败时 `_set_error()` 写入原因并把开关退回暂停 →
        暂停拿到锁后命中 no-op 分支（`enabled == user_on`）→ 以前在锁内**无条件**清错误，
        于是窗口只剩「已暂停」、不显示任何原因（GUI 只认 `snapshot()["error"]`）。
        修法：代次快照挪到**加锁之前**，no-op 分支也走 `_clear_error_if_unchanged()`。
        """
        from easysub_helper.audio import BackendError

        started, release = self._patch_resolve_by_device("USB")

        def failing_open(source="system", backend="auto", device=None, rate=16000, frame_ms=20):
            raise BackendError("device busy")

        orig = helper_server.create_backend
        helper_server.create_backend = failing_open
        self.addCleanup(lambda: setattr(helper_server, "create_backend", orig))

        async def body(_session):
            loop = asyncio.get_event_loop()
            self.server.device = "USB"                    # 让启动走慢解析（持锁）
            start = asyncio.ensure_future(self.server.set_user_enabled(True))
            await loop.run_in_executor(None, started.wait, 5)
            pause = asyncio.ensure_future(self.server.set_user_enabled(False))   # 排在启动的锁后
            await self._wait_for_lock_waiters(1)          # 确定暂停已排上（不靠墙钟）
            release.set()                                 # 放行解析 → 启动失败、写错误、退回暂停
            ok, err = await start
            self.assertFalse(ok, "这次启动应当失败：%r" % (err,))
            await pause
            snap = self.server.snapshot()
            self.assertEqual(snap["error"], "device busy",
                             "★ F17-1：紧跟其后的「暂停」（no-op）不得擦掉刚写下的失败原因")
            self.assertFalse(snap["userOn"])

        self.with_server(body)

    def test_error_written_during_pause_join_is_not_erased(self):
        """暂停的 join 期间刚写下的错误，不得被这次"成功"擦掉（第十六轮 M-1）。

        根因：`set_user_enabled` 在 `await self._join(...)` **之后**无条件清 `last_error`；
        锁在那之前就释放了 —— 期间并发派发的动作（设备解析失败、另一次启动失败）刚写下的
        **真实新错误**会被擦掉，而 GUI 状态行只认 `snapshot()["error"]`，于是窗口只剩
        「已暂停」、不显示任何原因。修法：清之前比对 `_error_seq`。
        """
        opened = set()
        self._slow_close_backend(opened)      # close 慢 → 拉长 join 窗口
        orig = helper_server.audio_devices.resolve
        helper_server.audio_devices.resolve = lambda source, device: (None, "找不到设备 ghost")
        self.addCleanup(lambda: setattr(helper_server.audio_devices, "resolve", orig))

        async def body(_session):
            self.assertTrue((await self.server.set_user_enabled(True))[0])
            pause = asyncio.ensure_future(self.server.set_user_enabled(False))
            # 等暂停真的摘掉会话、进入慢 join（轮询可观测状态，不靠墙钟）
            for _ in range(2000):
                if self.server._session is None:
                    break
                await asyncio.sleep(0.002)
            self.assertIsNone(self.server._session, "暂停应已摘掉会话（正在 join）")
            # join 还没结束时，并发派发的动作失败并写下错误
            ok, err = await self.server.switch_device("ghost")
            self.assertFalse(ok)
            self.assertEqual(self.server.snapshot()["error"], "找不到设备 ghost")
            await pause
            self.assertEqual(self.server.snapshot()["error"], "找不到设备 ghost",
                             "★ M-1：join 期间刚写下的错误不得被擦掉（窗口要显示原因）")

        self.with_server(body)

    def test_stale_device_switch_cannot_overwrite_a_newer_source_reset(self):
        """作废判定与 `device` 写入必须在**同一个锁块**里（第十五轮 N-1 实测的 TOCTOU）。

        坑：R14-2 的修法把序号检查与 `self.device = …` 拆成两个锁块后不再原子 ——
        `asyncio.Lock.acquire()` 在"有已被唤醒但还没跑的等待者"时会 await 让出，于是更晚的
        `switch_source("mic")` 能插在"检查"与"写 device"之间推进序号并清掉 `device`，
        随后旧解析又把 `device` 覆盖回旧设备名 → 破坏"换音源必须重置设备名"的不变量
        （实测终态 `device='A'` + `source='mic'`；合并锁块后是 `device=None`）。
        编排：A=切设备（慢解析）→ X=启动（**持锁**慢解析）→ 放行 A（A 排在 X 后）→
        Z=切音源（排在 A 后）→ 放行 X。
        """
        gate_a, gate_x = threading.Event(), threading.Event()
        started_a, started_x = threading.Event(), threading.Event()
        orig = helper_server.audio_devices.resolve

        def resolve(source, device):
            if device == "A":
                started_a.set()
                gate_a.wait(timeout=5)
            elif device == "USB":
                started_x.set()
                gate_x.wait(timeout=5)
            return device, None

        helper_server.audio_devices.resolve = resolve
        self.addCleanup(lambda: setattr(helper_server.audio_devices, "resolve", orig))
        self.use_fake_backend()

        async def body(_session):
            loop = asyncio.get_event_loop()
            self.server.device = "USB"
            a = asyncio.ensure_future(self.server.switch_device("A"))      # 慢解析
            await loop.run_in_executor(None, started_a.wait, 5)
            x = asyncio.ensure_future(self.server.set_user_enabled(True))  # 持锁慢解析
            await loop.run_in_executor(None, started_x.wait, 5)
            gate_a.set()                       # A 的解析回来 → A 去等锁（排在 X 后）
            await self._wait_for_lock_waiters(1)          # 确定 A 已排上（不靠墙钟）
            z = asyncio.ensure_future(self.server.switch_source("mic"))    # 排在 A 后
            await self._wait_for_lock_waiters(2)          # 确定 Z 也排上了（顺序：A → Z）
            gate_x.set()                       # X 完成 → 依次放行 A、Z
            await asyncio.gather(a, x, z)
            snap = self.server.snapshot()
            self.assertEqual(snap["source"], "mic")
            self.assertIsNone(snap["device"],
                              "★ N-1：换音源必须重置设备名，旧解析不得再覆盖回 device")

        self.with_server(body)

    def test_superseded_device_switch_does_not_write_a_stale_error(self):
        """已被更新点击作废的设备切换解析失败时：不得写错误、不得返回失败（R14-2）。

        根因（第十四轮审查实测）：`switch_device` 的解析失败分支排在 `seq != _intent_seq`
        检查**之前**：用户最后一次点击（可用设备）已生效、采集在跑，窗口却挂红色
        「采集故障：慢设备找不到」—— 正是 R11-2 要避免的"陈旧错误盖住采集中"外溢；
        而且作废的调用仍返回 (False, err)，会让 GUI 去重载下拉。
        修法：序号检查提到错误写入之前，作废的调用直接 `return True, None`。
        """
        started, release = self._patch_resolve_by_device("慢设备", slow_error="找不到设备 慢设备")
        self.use_fake_backend()

        async def body(_session):
            loop = asyncio.get_event_loop()
            self.assertTrue((await self.server.set_user_enabled(True))[0])
            slow = asyncio.ensure_future(self.server.switch_device("慢设备"))
            await loop.run_in_executor(None, started.wait, 5)     # 慢设备解析挂住
            # 用户紧接着点了可用设备：这次生效
            ok, err = await self.server.switch_device("USB Mic")
            self.assertTrue(ok, err)
            release.set()                                        # 放行慢设备的解析（它会失败）
            ok_stale, err_stale = await slow
            snap = self.server.snapshot()
            self.assertEqual(snap["device"], "USB Mic", "最后一次点击必须生效")
            self.assertIsNone(snap["error"],
                              "★ R14-2：作废的切换不得写下陈旧错误（窗口会红字盖住「采集中」）")
            self.assertTrue(ok_stale, "作废的调用应静默成功，不该让 GUI 去重载下拉：%r" % (err_stale,))

        self.with_server(body)

    def test_two_failing_sessions_do_not_swallow_the_current_error(self):
        """两条会话在同一 loop 排空窗口内报错：**当前会话**的致命错误必须仍被报出来。

        坑（第十二轮审查实测的新缝 DOUBLE）：以前身份靠**单槽** `self._error_session` 传递。
        当前会话先报错、陈旧会话后报错（把槽覆盖成旧的）时，排空里当前会话的 finish 回调
        读到的却是陈旧会话 → 判成"不是当前会话"直接返回 → 真错误被**静默吞掉**：
        `_session` 留着死会话、`user_on=True`、`error=None`、页面收不到 `capture_failed`、
        静音也不接（`_session is not None`）→ 页面彻底断流且窗口什么都不显示。
        修法：会话随 `call_soon_threadsafe` 传参，删掉单槽。
        """
        self.use_fake_backend()

        async def body(_session):
            self.assertTrue((await self.server.set_user_enabled(True))[0])
            stale = self.server._session
            self.assertTrue((await self.server.switch_source("mic"))[0])
            current = self.server._session
            self.assertIsNot(stale, current)
            # 同一次排空窗口：当前会话先报错、陈旧会话后报错（后者会覆盖"单槽"）
            self.server._on_capture_error(RuntimeError("current died"), current)
            self.server._on_capture_error(RuntimeError("stale died"), stale)
            await asyncio.sleep(0.05)                     # 让两个回调跑完
            snap = self.server.snapshot()
            self.assertEqual(snap["error"], "current died",
                             "★ DOUBLE：当前会话的致命错误被吞了（单槽互相清空）")
            self.assertFalse(snap["userOn"], "★ DOUBLE：必须退回暂停，否则页面断流且窗口无提示")
            self.assertIsNone(self.server._session, "死会话必须被摘掉")

        self.with_server(body)

    async def _wait_for_lock_waiters(self, count):
        """等到 `HelperServer` 的锁上出现至少 `count` 个等待者为止（确定性排序，不靠墙钟）。

        坑（写这条测试时踩的）：只 `await asyncio.sleep(0)` 不能保证"A 先排队、Z 后排队" ——
        A 的解析是在 executor 线程里返回的，回到事件循环要晚几拍；顺序错了就复现不出 N-1
        （Z 先跑反而会让旧调用正确地判成过期，测试变成假绿）。
        """
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            lock = self.server._get_lock()
            if len(getattr(lock, "_waiters", None) or ()) >= count:
                return
            # 注意：这里必须**给真实时间**，不能只 `sleep(0)` —— A 的解析是在 executor 线程里
            # 返回的，结果送回事件循环要等线程被唤醒（实测 `sleep(0)` 空转 2000 次仍可能不够，
            # 会让编排偶发不成立 = flaky）。等锁队列填满是"带超时的等待"，不是靠紧凑竞态窗口。
            await asyncio.sleep(0.002)
        raise AssertionError("等不到 %d 个锁等待者（编排没成立）" % count)

    def _patch_resolve_by_device(self, slow_device, fast_s=0.05, slow_error=None):
        """按设备名给 `audio_devices.resolve` 不同行为：慢设备**挂在 Event 上**由测试放行。

        坑（第十二轮审查）：原来用 `time.sleep(0.4)` + `await asyncio.sleep(0.1)` 的墙钟余量，
        在慢机器上（0.1s 实耗 >0.4s）第二次点击会落在慢解析**之后** → 这两条回归会**丧失杀伤力**
        （变异后仍绿 = 假阴性）。改成 Event 握手：慢解析挂住 → 测试确定看到 `started` 再点第二次
        → 再 `release.set()` 放行。返回 `(started, release)`。
        """
        orig = helper_server.audio_devices.resolve
        started = threading.Event()
        release = threading.Event()

        def resolve(source, device):
            if device == slow_device:
                started.set()
                release.wait(timeout=5)
                if slow_error is not None:
                    return None, slow_error      # 慢，且最终解析失败
            else:
                time.sleep(fast_s)
            return device, None

        helper_server.audio_devices.resolve = resolve
        self.addCleanup(lambda: setattr(helper_server.audio_devices, "resolve", orig))
        return started, release

    def test_last_device_click_wins_when_resolution_speeds_differ(self):
        """连点两次设备下拉：**最后一次点击必须生效**（并发审计 F3b 实测的 major）。

        以前解析在锁外、写完 `self.device` 也不复查：先点设备A（慢解析 0.5s）、后点设备B
        （快解析 0.05s）→ 快解析的 B 先写、慢解析的 A 后写覆盖 → 用户最后点 B 却开到 A；
        而调用返回成功、GUI 不会重载下拉 → 窗口显示 B、声音是 A（项目最忌讳的"切了但没变"）。
        修法：`_intent_seq` 意图序号 —— 解析回来发现已过期就丢弃本次结果。
        """
        self.use_fake_backend()
        started, release = self._patch_resolve_by_device("设备A")

        async def body(_session):
            self.assertTrue((await self.server.set_user_enabled(True))[0])
            loop = asyncio.get_event_loop()
            slow = asyncio.ensure_future(self.server.switch_device("设备A"))
            # 确定慢解析已经挂住（Event 握手，不靠墙钟）
            await loop.run_in_executor(None, started.wait, 5)
            self.assertTrue((await self.server.switch_device("设备B"))[0])   # 后点、解析快
            release.set()                                 # 放行慢解析
            self.assertTrue((await slow)[0])
            self.assertEqual(self.server.device, "设备B",
                             "★ 最后一次点击必须生效（旧实现会被慢解析的 A 覆盖）")

        self.with_server(body)

    def test_source_click_during_device_switch_keeps_capture_alive(self):
        """设备切换在飞时点音源：不得把用户最后一次切换静默收敛成"暂停 + 报错"（F3①）。

        旧实现里在飞的 `switch_device` 会把**旧音源解析出的设备名**写回 `self.device`，
        再用"新音源 + 旧设备名"重开 → 打不开 → `_recover` 把总开关退回暂停：用户最后那次
        "切到麦克风"变成一场暂停+红字，得重新点启动。修法：意图序号让过期的设备切换作废。
        """
        from easysub_helper.audio import BackendError

        started, release = self._patch_resolve_by_device("设备A", fast_s=0.02)

        def strict_backend(source="system", backend="auto", device=None, rate=16000, frame_ms=20):
            # 真机上的"打不开"就长这样：新音源配旧设备名 → 后端直接拒
            if source == "mic" and device:
                raise BackendError("mic backend rejected monitor device")
            return FakeBackend(source=source, device=device, rate=rate,
                               frame_ms=frame_ms, value=0.5)

        orig = helper_server.create_backend
        helper_server.create_backend = strict_backend
        self.addCleanup(lambda: setattr(helper_server, "create_backend", orig))

        async def body(_session):
            self.assertTrue((await self.server.set_user_enabled(True))[0])
            loop = asyncio.get_event_loop()
            slow = asyncio.ensure_future(self.server.switch_device("设备A"))
            await loop.run_in_executor(None, started.wait, 5)   # 确定设备切换在慢解析里
            self.assertTrue((await self.server.switch_source("mic"))[0])   # 用户改点音源
            release.set()
            await slow
            snap = self.server.snapshot()
            self.assertTrue(snap["userOn"], "★ 不得把最后一次切换收敛成暂停（F3①）")
            self.assertTrue(snap["capturing"], "★ 采集必须还在跑")
            self.assertEqual(snap["source"], "mic")
            self.assertIsNone(snap["error"], "不得留下打不开设备的红字：%r" % snap["error"])

        self.with_server(body)

    def test_switch_device_failure_must_not_reopen_in_paused_state(self):
        """切设备失败、且已被恢复逻辑收敛（user_on=False）时：**不得**再"回退旧设备重开一次"。

        坑（第七轮审查实测的 major）：恢复逻辑把 user_on 退回 False 后，回退重试照样执行 →
        **暂停态重开出会话**（user_on=False + capturing=True 自相矛盾）：页面收到真实帧与
        静音帧**交错**，用户再点「启动」叠出第二个会话（1 秒 129 帧、应约 50），
        stop 后 3 秒仍有 backend 未 close。规则：已收敛就直接报错——反正已暂停，
        下次点「启动」自然用回退后的设备。另核对 `set_user_enabled(True)` 的 detach 兜底。
        """
        from easysub_helper.audio import BackendError

        attempts = {"n": 0}

        def fail_only_on_second_open(source="system", backend="auto", device=None,
                                     rate=16000, frame_ms=20):
            attempts["n"] += 1
            # 第 1 次：set_user_enabled(True) 成功；第 2 次：switch_device 重启失败
            # （恢复逻辑收敛，回退重试必须被跳过）；第 3 次：用户再点「启动」——
            # 必须能正常成功，且只建**一个**会话（若旧实现回退重开，这里会叠出第二个）。
            if attempts["n"] == 2:
                raise BackendError("device gone")
            return FakeBackend(source=source, device=device, rate=rate,
                               frame_ms=frame_ms, value=0.5)

        original = helper_server.create_backend
        helper_server.create_backend = fail_only_on_second_open
        self.addCleanup(lambda: setattr(helper_server, "create_backend", original))

        async def body(_session):
            ok, err = await self.server.set_user_enabled(True)
            self.assertTrue(ok, err)
            self.assertTrue(self.server.snapshot()["capturing"])

            # 切到"系统默认设备"（device=None）：解析被跳过，直接落到 create_backend 失败
            ok, err = await self.server.switch_device(None)
            self.assertFalse(ok)
            snap = self.server.snapshot()
            self.assertFalse(snap["userOn"], "已被恢复逻辑收敛：总开关应保持在暂停")
            # 关键断言：**暂停态不得重开出会话**（旧实现回退重开 → capturing=True）
            self.assertFalse(snap["capturing"], "回退重试不得在暂停态重开出会话")
            # 兜底验证：再点「启动」不会叠出第二个会话（detach 兜底 + 正常 open）
            ok, err = await self.server.set_user_enabled(True)
            self.assertTrue(ok, err)
            snap = self.server.snapshot()
            self.assertTrue(snap["capturing"])
            self.assertFalse(snap["userOn"] is None)

        self.with_server(body)

    def test_capture_thread_hooks_wrap_open_and_close(self):
        """采集线程：prepare_thread() 必须在 open() 之前，cleanup_thread() 在 close() 之后。

        Windows 就靠这两个钩子在**正确的线程**上 CoInitializeEx/CoUninitialize
        （否则报 0x800401f0，用户实测）。
        """
        order = []
        original = helper_server.create_backend

        class LifecycleBackend(FakeBackend):
            def prepare_thread(self):
                order.append("prepare")

            def open(self):
                order.append("open")
                FakeBackend.open(self)

            def close(self):
                order.append("close")
                FakeBackend.close(self)

            def cleanup_thread(self):
                order.append("cleanup")

        def factory(source="system", backend="auto", device=None, rate=16000, frame_ms=20):
            return LifecycleBackend(source=source, device=device, rate=rate, frame_ms=frame_ms)

        helper_server.create_backend = factory
        self.addCleanup(lambda: setattr(helper_server, "create_backend", original))

        async def body(_session):
            self.assertTrue((await self.server.set_user_enabled(True))[0])
            await asyncio.sleep(0.15)
            self.assertTrue((await self.server.set_user_enabled(False))[0])
            for _ in range(60):                     # close/cleanup 在采集线程的 finally 里
                if "cleanup" in order:
                    break
                await asyncio.sleep(0.05)
            self.assertEqual(order[:2], ["prepare", "open"], order)
            self.assertIn("close", order, order)
            self.assertIn("cleanup", order, order)
            self.assertLess(order.index("close"), order.index("cleanup"), order)

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
