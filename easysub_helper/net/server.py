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
                 allow_origins=(), allow_no_origin=False, allow_cors_all=False,
                 scan_ports=True,
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
        #: CORS 全放行：任意 Origin 都回 CORS 头（Web 版部署在别的域名/https 预览站用）。
        # 安全前提：助手只监听 127.0.0.1，配对码是唯一凭据——外部网页拿不到音频。
        self.allow_cors_all = bool(allow_cors_all)
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
        #: 「采集意图」的代次：启动/暂停/重启/页面 start 每次都会 +1。
        #: 用途：`_restart` 在 `await self._join(session)` 处**会放开锁**，间隙里可能落进
        #: 用户点击或页面自动 start —— 那两种都是更新的意图，本次重启必须作废。
        #:
        #: **诚实说明（第九轮审查的变异结论）**：这是**纵深防御，不是唯一承重**。
        #: 把它整个删掉，两条间隙回归测试仍然是绿的 —— 真正挡住 S1 的是 `_restart` 里
        #: `if not self.user_on` 复查，挡住 S2 的是 `_open_locked` 开头的"不覆盖存活会话"防线。
        #: 保留它是因为"意图变化"这个语义足够便宜、能让未来新增的间隙路径自动被覆盖；
        #: 但**别再声称哪条测试是靠它变红的**。
        self._capture_epoch = 0
        #: 「窗口意图」的序号：每次 switch_source / switch_device 在**锁内** +1。
        #: 用途（并发审计 F3）：设备解析是慢 await，连点两次下拉时用序号判断"我的结果是否已
        #: 过期"，过期就丢弃 —— 否则慢解析的先点会覆盖后点的选择（实测最后点 B 却开到 A）。
        self._intent_seq = 0
        #: 「错误」的代次：**每一处写非 None 的 `last_error` 都 +1**（统一走 `_set_error()`）。
        #: 用途（第十三/十四轮审查的既存 minor）：`switch_source`/`switch_device` 的"非采集态成功"
        #: 分支要清陈旧的 `last_error`，但若这次等待期间**刚写下了新错误**（采集运行中挂掉把开关
        #: 退回暂停，或页面/窗口那次启动失败），那条错误一点都不过时 —— 清了之后窗口就不显示原因
        #: （GUI 只认 `snapshot()["error"]`，且 gui.py 的 error 分支排在 paused 之前）。
        #: 比对进入函数时的代次即可区分"陈旧"与"刚发生"。
        #:
        #: 坑（第十四轮审查 R14-1）：这个 +1 以前只写在"采集运行中挂掉"那一条路径上，而
        #: `last_error` 另有 4 个可达写入点没 +1 —— 于是"启动失败写下的新错误"会被在飞的切换
        #: 动作按"代次相等"清掉（窗口什么都不显示）。现在所有非 None 写入都收敛到 `_set_error()`。
        self._error_seq = 0
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
        # 坑（第十一轮审查）：`_shutting_down` 以前只置位不复位，是个**永久闩锁** ——
        # `stop()` → `start()` 之后 `_open_locked` 会永远拒绝开设备（实测永远采不了）。
        # 当前 GUI/CLI 生命周期不会复用同一个 HelperServer，所以不可达；但复位一行更安全。
        self._shutting_down = False
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
        # 关闭也是一次意图变化（并发审计 F6）：让在飞的重启/开设备作废，
        # 免得关闭瞬间还去真开一次设备（`_open_locked` 也会用 `_shutting_down` 兜住）。
        #
        # 诚实说明（第十一轮审查的变异结论）：删掉这一行**零差异** —— `stop()` 已把
        # `user_on=False`，`_restart` 自己的复查就挡住了。属纵深防御，别声称它承重。
        self._capture_epoch += 1
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
        if self.allow_cors_all and origin:
            return True
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
        stale = None
        failure = None
        async with self._get_lock():
            if enabled == self.user_on:
                # no-op 成功：也把上一次失败的提示清掉（独立审查抓的陈旧展示）
                self.last_error = None
                return True, None
            error_seq = self._error_seq        # 快照：见函数尾部的清错误守卫（M-1）
            # 意图变化：让任何"在飞的重启"作废（第八轮审查 S1/S2 —— 重启间隙里点暂停/启动）
            self._capture_epoch += 1
            self.user_on = enabled
            if enabled:
                # 兜底（第七轮审查实测的 major）：开设备前先摘掉任何还活着的会话。
                # 场景：切换音源/设备失败已被收敛（user_on=False），但用户**立刻**点「启动」，
                # 与回退重开交错时曾叠出两个并行推帧的 CaptureSession（1 秒 129 帧、应约 50），
                # 且 stop 收不干净（backend 泄漏）。摘掉后由下面的 _join 统一收尸。
                stale = self._detach_locked()
                ok, err = await self._open_locked()
                if not ok:
                    # 打不开就退回暂停：窗口不能显示"正在采集"却没有设备
                    self.user_on = False
                    failure = err
            else:
                session = self._detach_locked()
        # 坑（第八轮审查）：**失败路径也必须 join** —— 以前 open 失败时提前 return，
        # 被 detach 的旧会话（stale）没人收，采集线程与设备句柄就这么泄漏了。
        await self._join(session)
        await self._join(stale)
        self._broadcast(("json", self._state_msg()))
        self._sync_silence()
        if failure is not None:
            return False, failure
        # 用户手动操作成功：上一次失败的提示就过时了（别再挂着）。
        # 坑（第十六轮审查实测的既存 minor M-1）：这里在 `await self._join(...)` **之后**，
        # 锁早已释放 —— 期间并发派发的动作（例如设备解析失败、另一次启动失败）可能刚写下
        # **真实的新错误**，无条件清会把窗口唯一的原因显示擦掉（GUI 只看 snapshot()["error"]）。
        # 与 R13/R14-1 同一类：只有"本次等待期间没发生新错误"时才清。
        if self._error_seq == error_seq:
            self.last_error = None
        return True, None

    async def start_capture(self, source=None, device=None):
        """页面请求开始：只在总开关打开时开设备。返回 (ok, error)。"""
        async with self._get_lock():
            if self._session is not None and self._session.running:
                return True, None          # 已在采：多客户端共享同一会话，不算错
            # 坑（并发审计实测的 major F1）：**锁内复查总开关**。页面消息里的暂停检查发生在
            # 拿锁之前（`_on_client_message`），若排队的这次 start 等到锁时"用户那次启动刚好
            # 失败、开关已退回暂停"，不复查就会在暂停态把设备开出来 —— 实测
            # `userOn=False + capturing=True`，真实帧与静音帧各 50fps 并存、窗口显示"已暂停"。
            # 契约是"页面 start 只是意图，绝不自己开设备"，这里就是那条契约的落点。
            if not self.user_on:
                return False, t("server.err.paused")
            # 页面 start 也是一次"意图变化"：让在飞的重启作废，否则它随后会把这里刚开的
            # 会话**覆盖**成没人 join 的孤儿（第八轮审查 S2 实测双会话 + backend 泄漏）。
            self._capture_epoch += 1
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
        # 防御（第八轮审查 S2）：绝不覆盖一个正在跑的会话 —— 被覆盖的那个再也没人 join/stop，
        # 会变成并行推帧的孤儿（实测双会话各 ~50fps、暂停也收不掉、backend 泄漏）。
        # 所有正常路径都会先 detach（set_user_enabled / _restart），所以这里只在竞态中生效。
        if self._session is not None and self._session.running:
            return True, None
        # 防御（并发审计 F1/F6）：**持锁后再复查一遍"现在能不能开设备"**。
        # 页面消息里的暂停检查发生在拿锁之前；若那次启动随后失败把 user_on 退回 False
        # （或服务正在关闭），排队等锁的这次调用就会在暂停态把设备开出来 —— 实测终态
        # `userOn=False + capturing=True` 粘住、真实帧与静音帧各 50fps 并存，窗口却显示
        # "已暂停"（并发审计 H5 确定性复现）。把不变量收在锁内，一处兜住所有入口。
        if self._shutting_down or not self.user_on:
            return False, t("server.err.paused")
        # 意图快照（并发审计 F4）：await 之后**不再重读 `self.*`**。以前是"await 前写
        # `_capture_source`、await 后读它"，于是设备可能按旧音源解析出来，却配给新音源用
        # （实测 `create_backend(source='mic', device='alsa_output…monitor')`：以为在采麦克风，
        #  实际拿到系统回环，Linux 上 parec 还会"静默成功"）。
        #
        # 诚实说明（第十一轮审查的变异结论）：把这里改回"await 后重读"**看不出任何差异** ——
        # 真正承重的是 F3 把窗口意图的读改写收进了锁（`_open_locked` 持锁跨过 await，锁内
        # 再没人能改 `_capture_source`）。这行局部快照是纵深防御：将来若有人把某段状态写入
        # 挪回锁外，这里还能兜住。
        capture_source = source or self._capture_source or self.default_source
        requested = device or self.device
        self._capture_source = capture_source
        resolved, error = await self._resolve_device(capture_source, requested)
        if error:
            self._set_error(error)
            return False, error
        # 坑（第十一轮审查）：解析是**慢 await**（pactl 数十~数百毫秒），期间可能刚开始关闭
        # 或用户点了暂停 —— 开头那次复查在 await 之前，不够。实测 `stop()` 恰好落在这段解析里时
        # 设备仍会被真开一次（终态自洽、无泄漏，但没必要真去碰一次麦克风）。这里再复查一次。
        if self._shutting_down or not self.user_on:
            return False, t("server.err.paused")
        try:
            backend = create_backend(
                source=capture_source,
                backend=self.backend,
                device=resolved or requested,
                rate=self.rate,
                frame_ms=self.frame_ms,
            )
        except BackendError as exc:
            self._set_error(str(exc))
            return False, str(exc)
        session = CaptureSession(
            backend,
            self.rate,
            self.frame_ms,
            on_frame=self._on_frame,
            on_level=self._on_level,
            on_error=None,          # 构造完立刻绑定到**这个实例**（见下），别用 self._session 现读
            level_interval_ms=config.LEVEL_INTERVAL_MS,
        )
        # 坑（并发审计 F2）：错误回调以前在采集线程里现读 `self._session` 判断"谁出错了"——
        # 旧会话在 `session.stop` 排队（默认线程池被慢解析占住）时抛错，会被认成新会话：
        # 新会话被当成出错的清掉、且没人 join → 孤儿继续推帧、backend 泄漏，实测甚至能跨过
        # `server.stop()` 活着。把会话实例绑进闭包，身份判断才可靠。
        session.on_error = (lambda exc, bound=session: self._on_capture_error(exc, bound))
        self._state_sent = False
        self.last_error = None
        self._session = session
        session.start()
        self._broadcast(("json", self._state_msg()))
        return True, None

    def _set_error(self, message):
        """写 `last_error` 的**唯一**入口：所有"真的出错了"的写入都必须走这里。

        它顺手推进 `_error_seq`，好让"非采集态成功"分支区分"陈旧的错误"与"本次等待期间
        刚发生的新错误"（见 `_error_seq` 的注释）。**清错误（写 None）不要用它**。
        """
        self.last_error = message
        self._error_seq += 1

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
            self._capture_epoch += 1        # 停止也是意图变化：让在飞的重启作废
            session = self._detach_locked()
        await self._join(session)
        if announce:
            self._broadcast(("json", self._state_msg()))
        self._sync_silence()
        return session is not None

    async def _restart(self):
        """关掉当前会话并按新参数重开（窗口换音源/换设备时用）。

        坑（独立审查实测的 major）：重开失败时不能只把错误返回 —— 调用方（switch_source /
        switch_device 的回退）未必会再试一次，服务器就会停在
        `user_on=True + capturing=False` 的自相矛盾状态：页面断流、窗口按钮仍显示「暂停」，
        恢复要点两次。所以失败后必须走 `_recover_after_failed_restart` 收敛。

        坑二（第八轮审查实测的 major S1/S2）：`await self._join(session)` 处**锁是放开的**
        （关设备可能要等采集线程退出，不能占着锁）。间隙里可能落进：
          * 用户点「暂停」（S1）→ 那次暂停会把 user_on 置 False 并接管清理，本次重启若照常
            开设备，就得到 `user_on=False + capturing=True` 的矛盾会话（实测真实帧与静音帧
            交错 3/3 复现）；
          * 页面 onopen 自动发 start（S2）→ 它已经开出一个会话，本次重启再开就会把它
            **覆盖**成没人 join 的孤儿（实测双会话各 ~50fps、暂停收不掉、backend 泄漏）。
        因此 join 之后必须复查代次：只要中间发生过任何更新的意图，本次重启就作废。

        坑三（第九轮审查实测的 major）：**开设备必须用"当前意图"，不能用调用时快照的参数**。
        间隙里用户可能又点了一次音源（`switch_source` 在非采集态只落库 `default_source` /
        `_capture_source`，不开设备），此时若拿旧快照 `source` 去开，`_open_locked(source)` 还会
        把它**写回** `_capture_source` —— 用户最后一次点击被覆盖并持久化：GUI 下拉显示 A、
        实际采集 B，暂停再启动仍是 B（选择永久失效，head 与基线同样 2/2 复现）。
        所以这里**无参**调用 `_open_locked()`：它取 `_capture_source`（每次切换都已同步），
        即"用户最后选的"。（注意：**不能**改成在 switch_source 的非采集态分支推进代次 ——
        那会让在飞的重启直接作废，而那次切换又不自己开设备，于是留下
        `userOn=true + capturing=false` 的静音态。）
        """
        async with self._get_lock():
            self._capture_epoch += 1        # 本次重启拥有当前代次
            epoch = self._capture_epoch
            session = self._detach_locked()
        await self._join(session)
        async with self._get_lock():
            if epoch != self._capture_epoch:
                return True, None           # 间隙里有更新的意图：别开设备，交给那次操作
            if not self.user_on:
                return True, None           # 用户已暂停（可能正是间隙里点的）
            ok, error = await self._open_locked()
        if not ok:
            await self._recover_after_failed_restart()
        return ok, error

    async def _recover_after_failed_restart(self):
        """重启采集失败后的收敛：与 `_finish_capture_error_on_loop` 同一套契约。

        没有存活的会话时把总开关退回「暂停」：状态恢复一致（paused=true）、窗口按钮回到
        「启动」（一键重试）、`_sync_silence()` 接上静音帧 —— **页面那条流不能断**。
        若调用方已经自己重试成功（`_session` 活着），这里只广播一次状态。
        """
        async with self._get_lock():
            if self._session is None and self.user_on:
                self.user_on = False
        self._broadcast(("json", self._state_msg()))
        self._sync_silence()

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
        # 坑（并发审计实测的 major F3）：窗口意图的**读改写必须收进锁**。以前这几行在锁外跑，
        # 与在飞的 `_open_locked`（持锁 await 解析设备）或另一次窗口动作互相覆盖：
        # 实测"设备切换在飞时点音源"会把用户最后一次切换静默收敛成暂停+报错。
        async with self._get_lock():
            self._intent_seq += 1
            if source != self.default_source:
                self.device = None
            self.default_source = source
            # 坑（全盲审查实测的 major）：**非采集态也要同步 `_capture_source`**。它一旦采集过就
            # 永久残留，而 `set_user_enabled(True)` 重开时 `_open_locked()` 不带 source → 残留值
            # 优先于窗口新选的 default_source。实测：采 system → 暂停 → 切麦克风 → 再点启动，
            # 实际建的后端还是 system——用户"暂停换音源再启动"这一最常见操作被无声吞掉。
            self._capture_source = source
            error_seq = self._error_seq   # 同上：只在等待期间没发生采集失败时才清陈旧错误
            holding = self._holding()
        if not holding:
            # 注（第十四轮审查）：本分支里 `error_seq` 快照与比对之间**没有 await**
            # （唯一 await 是 holding=True 才走的 `_restart()`，那条路径根本不清错误），
            # 所以这个守卫结构上不可达，纯纵深防御 —— 别以为它在承重。
            if self._error_seq == error_seq:
                self.last_error = None    # 切换成功且期间没出错：上一次失败的提示就过时了
            return True, None
        return await self._restart()

    async def switch_device(self, device):
        """切换采集设备（窗口里的「设备」下拉）。`None`/空串 = 用系统默认设备。

        打不开就**退回原来的设备**并如实报错：否则用户会以为"切了但还是老设备"
        （Linux 上 parec 对未知设备名是静默回落默认源的，是最坑的一种表现）。
        """
        requested = device or None
        # 坑（并发审计实测的 major F3）：以前解析（**慢**，pactl 数十~数百毫秒）在锁外、写完
        # `self.device` 也不复查 —— 连点两次设备下拉时，"解析快的后点"先写、慢解析的先点后写，
        # 结果**用户最后一次点击失效**（实测：最后点设备B，实际开到设备A，且因为返回成功、
        # GUI 不会重载下拉，窗口显示 B 而声音是 A —— 项目最忌讳的"切了但没变"）。
        # 这里用意图序号：每次窗口动作 +1，解析回来后若已过期就丢弃本次结果。
        async with self._get_lock():
            self._intent_seq += 1
            seq = self._intent_seq
            error_seq = self._error_seq          # 快照：见下面"非采集态成功"分支的注释
            previous = self.device
            current_source = self._capture_source or self.default_source
        resolved, error = await self._resolve_device(current_source, requested)
        # 坑（第十四轮审查 R14-2 + 第十五轮审查 N-1）：序号检查、错误写入、`device` 写入
        # **必须在同一个锁块里**：
        #   · 检查要排在错误分支**之前**（R14-2）—— 否则一次已被更新的点击作废的慢解析，
        #     仍会在解析失败时写 `last_error` 并返回失败：窗口红字「采集故障」盖住正在进行的
        #     「采集中」（正是 R11-2 要避免的外溢），还会触发 GUI 重载下拉。
        #   · 三者**不能拆成两次加锁**（N-1，实测新引入的 TOCTOU）—— `asyncio.Lock.acquire()`
        #     在有等待者时会 await 让出，更晚的 `switch_source` 就能插在两次加锁之间推进序号
        #     并清掉 `device`，随后旧解析又把 `device` 覆盖回旧设备名，破坏"换音源必须重置设备名"
        #     的不变量（实测终态 `device='A'` + `source='mic'`）。
        async with self._get_lock():
            if seq != self._intent_seq:
                # 期间有更新的一次窗口动作：丢弃本次结果（含错误），别覆盖用户最后的选择
                return True, None
            if error:
                self._set_error(error)
                return False, error
            self.device = resolved or requested
            holding = self._holding()
        if not holding:
            # F5（并发审计 minor）：非采集态成功也要清陈旧错误，否则窗口会一直挂着
            # 上一次失败的红色「采集故障」（gui.py 的 error 分支排在 paused 之前）。
            # 坑（第十三轮审查实测的既存 minor）：本次等待期间若**刚好发生采集失败**（解析慢，
            # 失败回调挤进来了：开关已被退回暂停、致命 capture_failed 也广播出去了），那条错误
            # 一点都不过时 —— 清了窗口就不再显示原因。用错误代次区分"陈旧"与"刚发生"。
            if self._error_seq == error_seq:
                self.last_error = None
            return True, None
        ok, error = await self._restart()
        if not ok:
            # 打不开就退回原设备，并且如实报错（否则用户以为"切了但声音没变"）
            async with self._get_lock():
                if seq == self._intent_seq:
                    self.device = previous
            self._set_error(error)
            # 坑（第七轮审查实测的 major）：`_restart` 失败时已由
            # `_recover_after_failed_restart` 把总开关退回「暂停」（user_on=False）。
            # 此时**不能**再无条件"回退旧设备再开一次"——那会在暂停态重开出会话：
            # user_on=False + capturing=True 自相矛盾，页面收到真实帧与静音帧交错，
            # 用户再点「启动」会叠出第二个会话（实测 stop 后 3 秒仍有 backend 未 close）。
            # 已被收敛时就直接报错：反正已暂停，下次点「启动」自然用回退后的设备。
            #
            # 下面这一句是**兜底**（第十轮审查用"解析慢失败 + 排队的页面 start 抢锁"确定性
            # 命中过一次，所以别再说它不可达）：`_open_locked` 失败通常意味着没装上会话，
            # 但若它持锁卡在解析 await 时被排队的 `start_capture` 抢先开会话，走到这里
            # `_session` 就**可能**非 None —— 此时把设备回退后的会话重开一次是正确的
            # （实测终态自洽：capturing=true / user_on=true / device=previous，无矛盾态）。
            if self._session is not None:
                await self._restart()
            return False, error
        return True, None

    # ---------------- 暂停时的"空音频" ----------------
    def _sync_silence(self):
        """暂停且有人连着 → 起静音 ticker（保持流不断）；否则停掉。只在事件循环里调。

        坑：`self._silence_task` 的引用**只在这里改**（ticker 自己不许动它，见 _silence_loop）。

        坑二（并发审计 F1）：`want` 里加了 `self._session is None` —— 让"静音 ticker 与活会话
        并存"在结构上不可能。以前一旦出现矛盾态（`user_on=False` 但会话还在采），页面会同时
        收到真实帧与静音帧（实测各 50fps，额定 50 → 实际 ~100 msg/s）。
        """
        want = ((not self.user_on) and self._session is None
                and bool(self._queues) and not self._shutting_down)
        if want:
            if self._silence_task is None or self._silence_task.done():
                self._silence_task = asyncio.ensure_future(self._silence_loop())
        elif self._silence_task is not None:
            task = self._silence_task
            self._silence_task = None        # 先摘引用再取消（cancel 是异步的，finally 稍后才跑）
            task.cancel()

    async def _silence_loop(self):
        """暂停期间每 frame_ms 推一帧全零：页面时间轴不断、WS 不超时、恢复无需重连。

        坑（独立审查实测的竞态，别在 finally 里清 self._silence_task）：本协程**不是**这个
        引用的管理者。以前 finally 里无条件 `self._silence_task = None`，与 _sync_silence 形成
        两个写者，于是"取消 → 立刻重连"（暂停状态下刷新页面）会这样错位：
          ① _sync_silence 摘引用 + cancel（旧 task 的 finally 还没跑）；
          ② 新连接触发 _sync_silence → want=True → 建**新** ticker 并赋给引用；
          ③ 旧 task 的 finally 此刻才跑，把②刚建的引用抹成 None；
          ④ 新 ticker 沦为孤儿但仍在跑；下一次 _sync_silence 又建一个 → ticker 叠加，
             静音帧速率翻倍并随每次重连累积（实测 60ms 收到 6 帧，应为 ~3）。
        现在引用只由 _sync_silence 改，叠加不可能发生。
        """
        try:
            while (not self.user_on) and self._queues and not self._shutting_down:
                self._broadcast(("bin", self.silence_frame))
                await asyncio.sleep(self.frame_ms / 1000.0)
        except asyncio.CancelledError:
            pass

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

    def _on_capture_error(self, exc, session=None):
        # 这个回调跑在**采集线程**里：直接改 self._session 会与事件循环侧的 _detach_locked 并发。
        # 先记日志，再把「上报错误 + 清会话」丢回事件循环执行（复用既有的跨线程桥）。
        LOG.warning(t("log.captureFailed", error=exc))
        # 坑（第十一轮审查的 major 新缝）：`last_error` **不在线程侧写**。以前无条件写，于是
        # "陈旧会话的迟到错误"会把窗口挂上红色「采集故障」（gui.py 的 error 分支排在 capturing
        # 之前），而当前会话其实好好在采。改为回到 loop 侧、判过会话身份再写。
        # 坑（并发审计实测的 major F2）：**用回调绑定的会话实例**，不要在这里现读 self._session。
        # 采集线程与事件循环并发，旧会话在 `session.stop` 排队（默认线程池被慢解析占住）时抛错，
        # 现读会把旧会话的错误认到新会话头上：新会话被清掉且没人 join → 孤儿推帧 + backend 泄漏。
        # `session=None` 只作兼容兜底。
        if session is None:
            session = self._session
        loop = self._loop
        if loop is None or loop.is_closed():
            self._finish_capture_error_now(exc, session)
            return
        try:
            # 坑（第十二轮审查实测的新缝 DOUBLE）：**会话必须随回调一起传过去**。
            # 以前用单槽 `self._error_session` 传递身份 —— 两条会话在同一 loop 排空窗口内先后
            # 报错、且陈旧者后写槽时，两个 finish 回调互相清槽：当前会话的**真致命错误**被静默
            # 吞掉（`_session` 留着死会话、`user_on=True`、`error=None`、页面收不到
            # capture_failed、静音也不接）→ 页面彻底断流且窗口什么都不显示。
            loop.call_soon_threadsafe(self._finish_capture_error_on_loop, exc, session)
        except RuntimeError:
            # loop 在 `is_closed()` 与 `call_soon_threadsafe` 之间被关掉（TOCTOU）：与
            # `_emit_from_thread` 一致地兜住 —— 否则异常从采集线程抛穿、被
            # `CaptureSession._fail` 的 except 吞掉 → 只剩日志、会话不清（仅关闭瞬间）。
            self._finish_capture_error_now(exc, session)

    def _finish_capture_error_now(self, exc, session):
        """loop 已不在/已关闭：就地收尾（进程多半在退出），只处理"还是出错的那个"。"""
        if session is not None and self._session is session:
            self._session = None
            self._set_error(str(exc))

    def _finish_capture_error_on_loop(self, exc, session):
        if session is None or self._session is not session:
            # 坑（第十一轮审查实测的 major 新缝）：**陈旧会话的迟到错误到此为止**。
            # 它已经不是当前会话了；若还写 last_error、还广播 fatal=true 的 capture_failed：
            #   ① 窗口显示红色「采集故障」压住「采集中」，而音频其实一直在流；
            #   ② 该码**不在** HELPER_SILENT_CODES 里，页面宿主会走 toPanel({type:'ERROR'})
            #      → background cleanupAll，把整场会话连模型一起拆掉。
            # 只留日志（线程侧已记过），什么都不改 —— 当前会话不受任何影响。
            return
        # 到这里说明**出错的正是当前会话**：这才是真的采集挂了，按原契约收敛。
        self._set_error(str(exc))            # 走统一入口（顺手推进 _error_seq）
        self._emit_from_thread(("json", protocol.error(
            protocol.ERR_CAPTURE_FAILED, t("server.err.captureAborted", detail=str(exc)), True)))
        self._session = None
        # 坑（独立审查抓的 minor）：采集挂了却把总开关留在「启动」上，会得到一个
        # 自相矛盾的状态：paused=false + capturing=false —— 窗口按钮仍显示「暂停」，
        # 用户必须先点暂停再点启动才能重试；而且 _sync_silence 认为"用户要采"，
        # 连静音帧都不推，页面那边彻底断流（时间轴停住、波形冻死）。
        # 把开关退回暂停态：按钮变回「启动」（一键重试），静音帧继续推（页面契约不变）。
        self.user_on = False
        self._emit_from_thread(("json", self._state_msg()))
        # 开关退回暂停后要立刻让静音 ticker 接上（页面那条流不能断）
        self._sync_silence()

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
