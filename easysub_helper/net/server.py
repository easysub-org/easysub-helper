# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 hcz1017
"""本机服务：配对接线 + WebSocket 音频通道。

助手只做两件事：**采本机音频**、**把它推给配对过的页面**。所以 HTTP 面只有三条路由
（外加一个 OPTIONS 预检）：

    GET  /api/pair/info   无鉴权探测（只回最小信息，让页面决定要不要显示「桌面助手」音源）
    POST /api/pair        配对码换设备令牌
    GET  /ws?token=...    文本 JSON 控制 + 二进制 PCM

**没有静态文件服务**：页面由浏览器自己提供（扩展形态 / 已部署的 Web 版）。助手不托管任何产物，
也不碰识别模型——模型是页面里那套 wasm 引擎的事，助手连 onnxruntime 都不 import。

设计取舍：
  - **窗口总开关是设备的总闸**（`user_on`，默认关）：它唯一决定采集设备的开关。页面发来的
    start/stop 只是「我要 / 我不要音频」的意图，绝不会因为页面点了开始就去偷开用户的麦克风。
  - **暂停不断流**：暂停时关设备、释放硬件，但继续按 frame_ms 节奏给已连接客户端推**全零帧**，
    页面侧时间轴不断、WS 不超时、恢复瞬间不用重连（识别引擎只会看到静音）。
  - **没有客户端也能采**：用户按了「启动」就采——窗口电平图用来验证音频，不需要配对。
  - **一个采集会话，多客户端共享**：所有已连接的客户端收到同一份 PCM（面板 + 悬浮字幕窗、
    或页面刷新期间重连）。
  - **发送队列有界且丢旧**：页面卡住时宁可丢掉老音频，也不让延迟无限增长（识别是实时的）。
  - **停止采集不在事件循环里 join 线程**：`session.stop()` 会阻塞到采集线程退出，放 executor 里，
    否则一个慢设备能卡死整个服务。
  - **鉴权只在握手做**：`/ws` 只认设备令牌（配对换来的），`/api/pair` 还要 Origin 白名单 +
    配对码；`/api/pair/info` 公开但只回最小信息。
"""

import asyncio
import logging
import socket
import time

from aiohttp import web

from .. import config, protocol, security
from ..audio import BackendError, CaptureSession, create_backend
from ..audio import devices as audio_devices
from ..i18n import get_language, t
from ..pairing import ERR_LOCKED

LOG = logging.getLogger("easysub-helper")

MAX_WS_MSG = 64 * 1024
QUEUE_MAX = 300            # 20ms/帧 → 约 6 秒缓冲
STOP_JOIN_TIMEOUT = 2.0
TOKEN_HEADER = "X-Easysub-Token"

#: 回给浏览器的 CORS 之外的资源策略：跨源读取要显式允许（页面可能处于 COEP: require-corp）
CORP_HEADER = ("Cross-Origin-Resource-Policy", "cross-origin")


def _running_loop():
    """py3.6 没有 get_running_loop；协程里 get_event_loop 返回的就是运行中的循环。"""
    getter = getattr(asyncio, "get_running_loop", None)
    if getter is not None:
        return getter()
    return asyncio.get_event_loop()


def _clean_label(value, default):
    """把配对请求里的 `label` 收拾成一行短字符串。

    为什么要专门做（独立审查发现）：`(body.get("label") or "...")[:80]` 对 `123` / `true`
    这类 JSON 值会 TypeError → `/api/pair` 500（无令牌端点，任何人都能发）；对 list 还会
    原样回显。非字符串一律丢弃用默认值，字符串压成一行并截断。
    """
    if not isinstance(value, str):
        return default
    text = " ".join(value.split())
    return text[:80] or default


class HelperServer(object):
    def __init__(self, pairing, port, host=config.DEFAULT_HOST,
                 default_source="system", backend="auto", device=None,
                 frame_ms=config.FRAME_MS, rate=config.TARGET_RATE,
                 allow_origins=(), allow_no_origin=False, scan_ports=True,
                 version="0.0.0", fixed_token=None, user_on=False):
        self.pairing = pairing
        self.fixed_token = fixed_token       # 仅调试/测试：跳过配对码校验
        self.port = int(port)
        self.host = host
        self.default_source = default_source
        self.backend = backend
        self.device = device
        self.frame_ms = int(frame_ms)
        self.rate = int(rate)
        self.allow_origins = tuple(allow_origins or ())
        self.allow_no_origin = bool(allow_no_origin)
        self.scan_ports = bool(scan_ports)
        self.version = version
        #: 窗口「启动 / 暂停」总开关。默认关：不按启动，谁也不会被采集。
        self.user_on = bool(user_on)

        self.clients = 0
        self.started_at = 0.0
        #: 给 GUI 用的最近一次电平/错误（采集线程写、界面线程读；只存不可变标量，
        #: 不加锁也不会读到半个对象）。GUI 每 100ms 轮询一次，不需要跨线程回调 tkinter。
        self.last_level = {"rms": 0.0, "peak": 0.0, "at": 0.0}
        self.last_error = None
        #: 暂停期间推给页面的"空音频"：与真实帧完全同格式（f32le 单声道 16k 20ms = 1280 字节全 0）
        samples = int(round(self.rate * self.frame_ms / 1000.0))
        self.silence_frame = b"\x00" * (max(1, samples) * 4)

        self._queues = set()
        self._session = None
        self._capture_source = None
        self._state_sent = False
        self._lock = None
        self._loop = None
        self._runner = None
        self._site = None
        self._silence_task = None
        self._shutting_down = False

    # ---------------- 生命周期 ----------------
    @property
    def base_url(self):
        return "http://{}:{}".format(self.host, self.port)

    @property
    def url(self):
        return self.base_url

    @property
    def paused(self):
        return not self.user_on

    def _get_lock(self):
        if self._lock is None:
            self._lock = asyncio.Lock()
        return self._lock

    def build_app(self):
        """只注册这三条（+OPTIONS 预检）。其它路径由 aiohttp 直接回 404。"""
        app = web.Application()
        app.router.add_get("/ws", self.handle_ws)
        app.router.add_get("/api/pair/info", self.handle_pair_info)
        app.router.add_route("OPTIONS", "/api/pair", self.handle_preflight)
        app.router.add_post("/api/pair", self.handle_pair)
        return app

    async def start(self):
        self._loop = _running_loop()
        self._runner = web.AppRunner(self.build_app(), access_log=None)
        await self._runner.setup()
        ports = [self.port]
        if self.scan_ports:
            ports += [self.port + i for i in range(1, config.PORT_SCAN_RANGE + 1)]
        last_err = None
        for candidate in ports:
            site = web.TCPSite(self._runner, self.host, candidate)
            try:
                await site.start()
            except OSError as exc:
                last_err = exc
                continue
            self.port = candidate
            # --port 0：请求端口(0)与真实绑定端口不同，横幅/快照/base_url 不许谎报。
            # 不用 TCPSite 的私有属性（跨 aiohttp 版本不稳，py3.8 的 3.10 上实测拿不到），
            # 直接从 runner 的监听套接字回读——getsockname 是标准库语义，版本无关。
            if candidate == 0:
                for source in (getattr(site, "_server", None) and site._server.sockets,
                               getattr(self._runner, "addresses", None),
                               getattr(self._runner, "sockets", None)):
                    if not source:
                        continue
                    try:
                        items = list(source)
                    except TypeError:
                        continue
                    for item in items:
                        getsockname = getattr(item, "getsockname", None)
                        if getsockname is None:
                            continue            # addresses 里可能是字符串，跳过
                        try:
                            port = getsockname()[1]
                        except (OSError, IndexError):
                            continue
                        if port:
                            self.port = port
                            break
                    if self.port:
                        break
            self.started_at = time.monotonic()
            LOG.info(t("log.listening", url=self.base_url))
            return candidate
        await self._runner.cleanup()
        self._runner = None
        raise RuntimeError(t("run.err.portBusy", host=self.host, port=self.port,
                             count=len(ports), error=last_err))

    async def stop(self):
        self._shutting_down = True
        self.user_on = False
        try:
            await self.stop_capture(announce=False)
        except Exception:  # noqa: BLE001 - 收尾路径上不抛
            pass
        self._sync_silence()
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None
        self._site = None

    # ---------------- 给图形界面看的快照 ----------------
    def snapshot(self):
        """同步、无锁、不发网络请求的状态摘要（GUI 每 100ms 调一次）。

        故意只读不可变标量/短 dict：跨线程读永远不会拿到半成品，也不会阻塞事件循环。
        """
        session = self._session
        level = self.last_level
        return {
            "running": self._runner is not None,
            "port": self.port,
            "url": self.base_url,
            "clients": self.clients,
            "userOn": bool(self.user_on),
            "paused": self.paused,
            "capturing": bool(session is not None and session.running),
            "source": self._capture_source,
            "device": self.device,
            #: 后端**实际打开**的设备（可能是解析后的 PulseAudio 源名）：
            #: 窗口把它显示出来，"切换到底生没生效"就不用猜
            "openedDevice": session.backend.device if session is not None else None,
            "backend": session.backend.name if session is not None else None,
            "rate": self.rate,
            "frameMs": self.frame_ms,
            "frames": session.frames_sent if session is not None else 0,
            "levelRms": float(level.get("rms") or 0.0),
            "levelPeak": float(level.get("peak") or 0.0),
            "levelAt": float(level.get("at") or 0.0),
            "error": self.last_error,
        }

    # ---------------- 鉴权与响应头 ----------------
    def _token_from_request(self, request):
        token = request.query.get("token")
        if not token:
            token = request.headers.get(TOKEN_HEADER)
        return token

    def _valid_token(self, token):
        if self.fixed_token:
            return security.token_ok(token, self.fixed_token)   # 常数时间（--token 调试模式）
        if self.pairing is None:
            return False
        return self.pairing.token_valid(token)

    def _origin_allowed(self, origin, allow_missing=False):
        return security.origin_ok_or_missing(
            origin, self.port, self.allow_origins,
            self.allow_no_origin or allow_missing,
        )

    def _cors_headers(self, request):
        """只对**放行过的** Origin 回 CORS 头。

        配对接口要支持跨源（扩展面板的 Origin 是 chrome-extension://…，Web 版可能部署在
        别的端口/域名），但绝不能回 `*`——否则本机任意网页都能探测并尝试配对。
        未被放行的 Origin 不回 CORS 头，浏览器就会把响应拦掉。
        """
        origin = request.headers.get("Origin")
        if origin and self._origin_allowed(origin):
            return {
                "Access-Control-Allow-Origin": origin,
                "Access-Control-Allow-Headers": "Content-Type, " + TOKEN_HEADER,
                "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
                "Vary": "Origin",
            }
        return {}

    def _json(self, request, payload, status=200):
        """统一的 JSON 响应：跨源所需的头都在这里加，别用全局中间件。

        为什么不用中间件：未匹配的路径（404 兜底）aiohttp 会把 `None` 交给中间件链，
        在中间件里碰 `resp.headers` 就会抛 AttributeError、日志里刷一片 traceback。
        助手只有三个端点，挨个加头更简单也更可靠。
        """
        resp = web.json_response(payload, status=status)
        resp.headers.setdefault(*CORP_HEADER)
        for key, value in self._cors_headers(request).items():
            resp.headers.setdefault(key, value)
        resp.headers.setdefault("Cache-Control", "no-store")
        return resp

    # ---------------- 配对 ----------------
    async def handle_pair_info(self, request):
        """探测接口：页面据此判断「有没有助手、我配过没有、助手是否暂停」。
        （音源常驻显示，本接口不再决定"显示不显示"。）

        只回最小信息（无令牌、无设备名、无路径）。`paired` 表示**本次请求带的令牌**是否有效，
        因此页面一次调用就能判断"有没有助手 + 我配过没有"。
        """
        state = self.pairing.state() if self.pairing is not None else {"valid": False, "devices": 0}
        return self._json(request, {
            "ok": True,
            "app": "easysub-helper",
            "version": self.version,
            "api": protocol.API_VERSION,
            "lang": get_language(),
            "platform": "windows" if config.is_windows() else ("macos" if config.is_macos() else "linux"),
            "pairingRequired": not bool(self.fixed_token),
            "paired": self._valid_token(self._token_from_request(request)),
            "paused": self.paused,
            "sampleRate": self.rate,
            "frameMs": self.frame_ms,
        })

    async def handle_preflight(self, request):
        # 预检本身不带凭据；Origin 不在白名单时不回 CORS 头，浏览器会拦掉真正的 POST
        return self._json(request, {}, status=204)

    async def handle_pair(self, request):
        # **无令牌端点：Origin 白名单在这里是真闸门**（不只是加 CORS 响应头）。
        # 之前只回响应头、照样处理请求 —— 一个不触发预检的"简单请求"（text/plain、无 Content-Type）
        # 就能从任意网页驱动配对尝试，与本文件"白名单是唯一防线"的说法不符（独立审查发现）。
        # 没有 Origin 的请求（curl/脚本等非浏览器客户端）按 allow_no_origin 决定；
        # 浏览器一定带 Origin，所以这条不会放松对网页的约束。
        origin = request.headers.get("Origin")
        if not self._origin_allowed(origin, allow_missing=self.allow_no_origin):
            logging.getLogger("easysub-helper").warning(
                t("log.pairOriginDenied", origin=origin or t("log.noOrigin")))
            return self._json(request, {"ok": False, "code": protocol.ERR_FORBIDDEN,
                                        "message": t("server.err.originDenied")}, status=403)
        if self.pairing is None and not self.fixed_token:
            return self._json(request, {"ok": False, "code": protocol.ERR_FORBIDDEN,
                                        "message": t("server.err.forbidden")}, status=403)
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001 - aiohttp 各版本的 JSON 异常类型不同
            return self._json(request, {"ok": False, "code": protocol.ERR_BAD_MESSAGE,
                                        "message": t("server.err.pairBody")}, status=400)
        if not isinstance(body, dict):
            return self._json(request, {"ok": False, "code": protocol.ERR_BAD_MESSAGE,
                                        "message": t("server.err.pairBody")}, status=400)
        if self.fixed_token:
            # 调试模式：不校验配对码，直接回固定令牌（--token 的本意）
            return self._json(request, {"ok": True, "token": self.fixed_token,
                                        "label": _clean_label(body.get("label"), "debug")})
        ok, reason = self.pairing.verify(body.get("code"))
        if not ok:
            LOG.warning(t("log.pairFailed", reason=reason, origin=request.headers.get("Origin")))
            status = 429 if reason == ERR_LOCKED else 403
            return self._json(request, {
                "ok": False,
                "code": reason,
                "message": self.pairing.error_message(reason),
            }, status=status)
        label = _clean_label(body.get("label"), "browser")
        token = self.pairing.issue_token(label)
        LOG.info(t("log.pairOk", label=label, devices=self.pairing.state().get("devices", 0)))
        return self._json(request, {"ok": True, "token": token, "label": label})

    # ---------------- 采集：总开关 / 会话 ----------------
    async def set_user_enabled(self, enabled):
        """窗口「启动 / 暂停」总开关。返回 (ok, error)。

        启动：立刻开设备（**没有客户端也开**——窗口电平图要能在没配对时验证音频）。
        暂停：立刻关设备、释放硬件，但**不动任何连接**；静音帧由 _sync_silence 接着推。
        """
        enabled = bool(enabled)
        session = None
        async with self._get_lock():
            if enabled == self.user_on:
                return True, None
            self.user_on = enabled
            if enabled:
                ok, err = await self._open_locked()
                if not ok:
                    # 打不开就退回暂停：窗口不能显示"正在采集"却没有设备
                    self.user_on = False
                    self._broadcast(("json", self._state_msg()))
                    self._sync_silence()
                    return False, err
            else:
                session = self._detach_locked()
        await self._join(session)
        self._broadcast(("json", self._state_msg()))
        self._sync_silence()
        return True, None

    async def start_capture(self, source=None, device=None):
        """页面请求开始：只在总开关打开时开设备。返回 (ok, error)。"""
        async with self._get_lock():
            if self._session is not None and self._session.running:
                return True, None          # 已在采：多客户端共享同一会话，不算错
            return await self._open_locked(source, device)

    async def _resolve_device(self, source, device):
        """把设备名解析成后端能用的 id；解析不出来就返回错误（**同步**，别等采集线程报错）。

        `parec`/`pw-record` 对未知设备名是静默回落默认源的，所以这一步必须在这里做：
        用户要的是"换设备失败就告诉我"，而不是"换了但声音没变"。
        """
        if not device:
            return None, None
        loop = _running_loop()
        resolved, error = await loop.run_in_executor(None, audio_devices.resolve, source, device)
        if error:
            return None, error
        return resolved or device, None

    async def _open_locked(self, source=None, device=None):
        self._capture_source = source or self._capture_source or self.default_source
        requested = device or self.device
        resolved, error = await self._resolve_device(self._capture_source, requested)
        if error:
            self.last_error = error
            return False, error
        try:
            backend = create_backend(
                source=self._capture_source,
                backend=self.backend,
                device=resolved or requested,
                rate=self.rate,
                frame_ms=self.frame_ms,
            )
        except BackendError as exc:
            self.last_error = str(exc)
            return False, str(exc)
        session = CaptureSession(
            backend,
            self.rate,
            self.frame_ms,
            on_frame=self._on_frame,
            on_level=self._on_level,
            on_error=self._on_capture_error,
            level_interval_ms=config.LEVEL_INTERVAL_MS,
        )
        self._state_sent = False
        self.last_error = None
        self._session = session
        session.start()
        self._broadcast(("json", self._state_msg()))
        return True, None

    def _detach_locked(self):
        session, self._session = self._session, None
        return session

    async def _join(self, session):
        if session is None:
            return
        loop = _running_loop()
        try:
            await loop.run_in_executor(None, session.stop, STOP_JOIN_TIMEOUT)
        except Exception as exc:  # noqa: BLE001 - 停止失败也要继续广播状态
            LOG.warning(t("log.stopFailed", error=exc))

    async def stop_capture(self, announce=True):
        async with self._get_lock():
            session = self._detach_locked()
        await self._join(session)
        if announce:
            self._broadcast(("json", self._state_msg()))
        self._sync_silence()
        return session is not None

    async def _restart(self, source=None):
        """关掉当前会话并按新参数重开（窗口换音源/换设备时用）。"""
        async with self._get_lock():
            session = self._detach_locked()
        await self._join(session)
        async with self._get_lock():
            return await self._open_locked(source)

    def _holding(self):
        return bool(self.user_on and self._session is not None and self._session.running)

    async def switch_source(self, source):
        """切换默认音源（系统音频 / 麦克风）：正在采集就重启采集，否则只记住选择。

        返回 (ok, error)。GUI 的下拉框直接调它——用户改音源的期望就是"立刻生效"。
        **换音源会重置设备选择**：设备名与音源是绑定的（麦克风名 ≠ monitor 名），
        留着旧名字只会让新会话打不开设备。
        """
        source = source or self.default_source
        if source not in ("system", "mic"):
            return False, t("protocol.bad.badSource", source=source)
        if source != self.default_source:
            self.device = None
        self.default_source = source
        if not self._holding():
            return True, None
        return await self._restart(source)

    async def switch_device(self, device):
        """切换采集设备（窗口里的「设备」下拉）。`None`/空串 = 用系统默认设备。

        打不开就**退回原来的设备**并如实报错：否则用户会以为"切了但还是老设备"
        （Linux 上 parec 对未知设备名是静默回落默认源的，是最坑的一种表现）。
        """
        requested = device or None
        resolved, error = await self._resolve_device(
            self._capture_source or self.default_source, requested)
        if error:
            self.last_error = error
            return False, error
        previous = self.device
        self.device = resolved or requested
        if not self._holding():
            return True, None
        ok, error = await self._restart()
        if not ok:
            # 打不开就退回原设备，并且如实报错（否则用户以为"切了但声音没变"）
            self.device = previous
            self.last_error = error
            await self._restart()
            return False, error
        return True, None

    # ---------------- 暂停时的"空音频" ----------------
    def _sync_silence(self):
        """暂停且有人连着 → 起静音 ticker（保持流不断）；否则停掉。只在事件循环里调。"""
        want = (not self.user_on) and bool(self._queues) and not self._shutting_down
        if want and (self._silence_task is None or self._silence_task.done()):
            self._silence_task = asyncio.ensure_future(self._silence_loop())
        elif not want and self._silence_task is not None:
            self._silence_task.cancel()
            self._silence_task = None

    async def _silence_loop(self):
        """暂停期间每 frame_ms 推一帧全零：页面时间轴不断、WS 不超时、恢复无需重连。"""
        try:
            while (not self.user_on) and self._queues and not self._shutting_down:
                self._broadcast(("bin", self.silence_frame))
                await asyncio.sleep(self.frame_ms / 1000.0)
        except asyncio.CancelledError:
            pass
        finally:
            self._silence_task = None

    # ---------------- 采集线程 → 事件循环 ----------------
    def _offer(self, queue, item):
        """有界队列：满了丢最旧的（保证低延迟）。"""
        try:
            queue.put_nowait(item)
            return
        except asyncio.QueueFull:
            pass
        try:
            queue.get_nowait()
        except Exception:  # noqa: BLE001
            pass
        try:
            queue.put_nowait(item)
        except Exception:  # noqa: BLE001
            pass

    def _broadcast(self, item):
        for queue in list(self._queues):
            self._offer(queue, item)

    def _emit_from_thread(self, item):
        loop = self._loop
        if loop is None:
            return
        try:
            loop.call_soon_threadsafe(self._broadcast, item)
        except RuntimeError:
            pass  # 循环已关闭（正在退出）

    def _state_msg(self):
        session = self._session
        backend = session.backend if session is not None else None
        return protocol.state(
            bool(session is not None and session.running),
            self._capture_source,
            backend.name if backend is not None else None,
            backend.device if backend is not None else None,
            paused=self.paused,
        )

    def _on_frame(self, data):
        if not self._state_sent and self._session is not None:
            self._state_sent = True
            self._emit_from_thread(("json", self._state_msg()))
        self._emit_from_thread(("bin", data))

    def _on_level(self, info):
        self.last_level = {"rms": float(info["rms"]), "peak": float(info["peak"]),
                           "at": time.monotonic()}
        self._emit_from_thread(("json", protocol.level(info["rms"], info["peak"])))

    def _on_capture_error(self, exc):
        # 这个回调跑在**采集线程**里：直接改 self._session 会与事件循环侧的 _detach_locked 并发。
        # 先记日志，再把「上报错误 + 清会话」丢回事件循环执行（复用既有的跨线程桥）。
        LOG.warning(t("log.captureFailed", error=exc))
        self.last_error = str(exc)
        session = self._session
        self._error_session_id = id(session) if session is not None else None
        loop = self._loop
        if loop is None or loop.is_closed():
            self._session = None            # 循环没了：只能就地收尾（进程多半在退出）
            return
        loop.call_soon_threadsafe(self._finish_capture_error_on_loop, exc)

    def _finish_capture_error_on_loop(self, exc):
        self._emit_from_thread(("json", protocol.error(
            protocol.ERR_CAPTURE_FAILED, t("server.err.captureAborted", detail=str(exc)), True)))
        # 这里执行时用户可能已经重新 start_capture 出了**新会话**（排队期间点开始），
        # 只清"还是出错的那个"——拿 session 生成序号对比，别把新会话误杀
        session = self._session
        # 只清"出错的那个会话"：如果排队期间用户已经 start_capture 出了新会话
        # （对象不同 → id 不同），绝不能把新会话误杀
        if session is not None and id(session) == getattr(self, "_error_session_id", None):
            self._session = None
        self._emit_from_thread(("json", self._state_msg()))

    # ---------------- WebSocket ----------------
    #: 同一时刻允许的 WS 连接数上限（每个连接一条 6 秒发送队列；防单页面开几百条）
    WS_MAX_CONNECTIONS = 8

    async def handle_ws(self, request):
        token = self._token_from_request(request)
        origin = request.headers.get("Origin")
        # 只认令牌：令牌是配对的产物，比 Origin 更能说明"这个页面被用户放行过"。
        # 扩展的 offscreen 文档不一定带 Origin，硬拦 Origin 会误伤正常用户。
        if not self._valid_token(token):
            LOG.warning(t("log.wsBadToken", origin=origin))
            return web.Response(status=403, text=t("server.err.notPaired"), charset="utf-8")

        # 连接数上限：持令牌的页面理论上可以开任意多条连接（每条一条 6 秒发送队列）。
        # 本机场景风险低，但上限便宜——超了给 503，别让一个失控页面吃满内存（独立审查建议）。
        if len(self._queues) >= self.WS_MAX_CONNECTIONS:
            LOG.warning(t("log.wsTooManyConnections", limit=self.WS_MAX_CONNECTIONS))
            return web.Response(status=503, text=t("server.err.tooManyConnections"), charset="utf-8")
        ws = web.WebSocketResponse(heartbeat=30, max_msg_size=MAX_WS_MSG)
        await ws.prepare(request)
        queue = asyncio.Queue(maxsize=QUEUE_MAX)
        self._queues.add(queue)
        self.clients += 1
        LOG.info(t("log.wsOpen", origin=origin, clients=self.clients))
        pump = None
        try:
            session = self._session
            backend_desc = session.backend.describe() if session is not None else {
                "backend": None, "device": None, "nativeRate": None,
            }
            await ws.send_str(protocol.dumps(protocol.hello(
                version=self.version,
                sample_rate=self.rate,
                channels=1,
                frame_ms=self.frame_ms,
                sources=["system", "mic"],
                backend=backend_desc.get("backend"),
                device=backend_desc.get("device"),
                native_rate=backend_desc.get("nativeRate"),
                capturing=bool(session is not None and session.running),
                paused=self.paused,
                lang=get_language(),
            )))
            self._sync_silence()          # 暂停状态：新连上来的客户端立刻开始收静音帧
            pump = asyncio.ensure_future(self._pump(ws, queue))
            async for msg in ws:
                if msg.type == web.WSMsgType.TEXT:
                    await self._on_client_message(ws, msg.data)
                elif msg.type == web.WSMsgType.BINARY:
                    await ws.send_str(protocol.dumps(protocol.error(
                        protocol.ERR_BAD_MESSAGE, t("server.err.binaryFrame"), False)))
                elif msg.type == web.WSMsgType.ERROR:
                    LOG.warning(t("log.wsError", error=ws.exception()))
        finally:
            if pump is not None:
                pump.cancel()
                try:
                    await pump
                except asyncio.CancelledError:
                    pass
                except Exception:  # noqa: BLE001
                    pass
            self._queues.discard(queue)
            self.clients -= 1
            LOG.info(t("log.wsClosed", clients=self.clients))
            # 刻意**不**因为"最后一个客户端断开"就停采集：设备归窗口总开关管
            # （用户按了启动就是明确意图，而且没配对时也要能采来测试）。
            self._sync_silence()
        return ws

    async def _pump(self, ws, queue):
        while True:
            kind, payload = await queue.get()
            if ws.closed:
                return
            if kind == "bin":
                await ws.send_bytes(payload)
            else:
                await ws.send_str(protocol.dumps(payload))

    async def _on_client_message(self, ws, raw):
        try:
            msg = protocol.parse_client_message(raw)
        except protocol.BadMessage as exc:
            await ws.send_str(protocol.dumps(protocol.error(
                protocol.ERR_BAD_MESSAGE, t("server.err.badMessage", detail=str(exc)), False)))
            return
        mtype = msg.get("type")
        if mtype == protocol.CLIENT_PING:
            await ws.send_str(protocol.dumps(protocol.pong(msg.get("t"))))
            return
        if mtype == protocol.CLIENT_START:
            if self.paused:
                # 总开关关着：绝不因为页面点了开始就开设备；让页面去助手窗口点「启动」。
                # 静音帧继续推，所以页面侧的时间轴不断。
                await ws.send_str(protocol.dumps(protocol.error(
                    protocol.ERR_PAUSED, t("server.err.paused"), False)))
                return
            ok, err = await self.start_capture(msg.get("source"), msg.get("device"))
            if not ok:
                await ws.send_str(protocol.dumps(protocol.error(
                    protocol.ERR_CAPTURE_FAILED,
                    t("server.err.captureFailed", detail=err or ""), True)))
            return
        if mtype == protocol.CLIENT_STOP:
            # 取消订阅：设备继续由窗口总开关决定，这里只回一份当前状态
            await ws.send_str(protocol.dumps(self._state_msg()))
            return
