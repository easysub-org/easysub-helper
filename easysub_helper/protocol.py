# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 hcz1017
"""线路协议（页面 ⇄ 助手）。

设计目标：让前端接线**尽可能薄** —— 收到二进制帧就是一段 16k 单声道 float32 PCM，
直接 `new Float32Array(buf)` 交给 `AsrEngine.feedMicChunk(samples, 16000)` 即可，
不需要前端再做重采样/重排（主项目自研 resample 在 AUDIT 里已记录跨块状态与丢样本缺陷）。

## 配对（HTTP，先于 WS）

页面不能直接连 WS——先要拿到**设备令牌**：

  GET  /api/pair/info                 探测：助手在不在？我配过没有？（无鉴权，只回最小信息）
  POST /api/pair  {"code":"ABC-123"}  配对：配对码换长期令牌 → {"ok":true,"token":"..."}

页面拿到令牌后持久化保存（建议 localStorage），之后 `?token=` 或 `X-Easysub-Token` 头带上它。
**音源常驻显示**：`/api/pair/info` 只用来告诉页面「有没有助手、这个浏览器配过没有、助手是不是暂停了」，不再决定音源是否出现（早期「探测到才显示」已废弃：用户实测在 Web 版里因此根本找不到这个音源，连怎么配对都无从下手）。**配对成功才允许开始识别。**

## 传输

单个 WebSocket：`ws://127.0.0.1:<port>/ws?token=<设备令牌>`

## 服务端 → 客户端

- **文本帧**：JSON 控制消息（见下方 type）
- **二进制帧**：**裸 PCM**，固定 `sampleRate` / `channels` / `frameMs`（在 hello 里声明）。
  当前恒为 `f32le`、单声道、16 kHz、20 ms（= 320 样本 = 1280 字节）。
  不额外加包头：帧长固定，前端按 sampleRate/1000*frameMs 算样本数即可；
  万一将来变量长，再升级 api 版本加头。

  hello:
    {"type":"hello","api":1,"version":"0.1.0","sampleRate":16000,"channels":1,
     "format":"f32le","frameMs":20,"sources":["system","mic"],"backend":"parec",
     "device":"Monitor of ...","nativeRate":16000,"capturing":false,"paused":true,
     "lang":"zh_CN"}
  state:
    {"type":"state","capturing":true,"paused":false,"source":"system","backend":"parec",
     "device":"..."}
  level（~10Hz，给电平条；与主项目面板的 LEVEL 同义）：
    {"type":"level","rms":0.0123,"peak":0.05}
  error:
    {"type":"error","code":"capture_failed","message":"...","fatal":true}
  pong: {"type":"pong","t":<客户端原样回传的 t>}

## 客户端 → 服务端

  {"type":"start","source":"system"|"mic","device":"<可选，子串匹配>"}
  {"type":"stop"}
  {"type":"ping","t":123}

## 错误码（i18n 契约）

**前端应当按 `code` 用自己的 i18n 渲染文案**，`message` 只是桌面端语言下的兜底
（桌面端所有文案见 easysub_helper/i18n.py）。已定义的码：

  bad_message / capture_failed / forbidden / **paused**
  / bad_code / code_expired / locked / no_code        （配对相关）

采集 / 设备 / 重采样故障统一表现为 `capture_failed`，具体原因在 `message` 里（不另设码）。

`paused` 的含义：助手窗口的「启动 / 暂停」总开关处于暂停（**默认就是暂停**）。此时客户端发
`start` 不会打开采集设备，页面应当提示用户去助手窗口点「启动」；而连接与静音帧不受影响。

注意：**鉴权在握手时做**（`/ws` 只认设备令牌；`/api/pair` 的 Origin 白名单是**真闸门**
（不允许的来源直接 403），再加配对码与失败限速），
握手通过后不再逐帧校验，因为本机回环连接建立后没有第三方能插进来。
"""


import json

from .i18n import t

API_VERSION = 1
FORMAT_F32LE = "f32le"

# 控制消息类型
MSG_HELLO = "hello"
MSG_STATE = "state"
MSG_LEVEL = "level"
MSG_ERROR = "error"
MSG_PONG = "pong"

CLIENT_START = "start"
CLIENT_STOP = "stop"
CLIENT_PING = "ping"

CLIENT_MESSAGE_TYPES = (CLIENT_START, CLIENT_STOP, CLIENT_PING)

# 错误码
ERR_BAD_MESSAGE = "bad_message"
ERR_CAPTURE_FAILED = "capture_failed"
#: 助手窗口的总开关处于暂停（默认）：页面请求开始音频会被拒，等用户点「启动」
ERR_PAUSED = "paused"
ERR_FORBIDDEN = "forbidden"
# 配对相关（与 pairing.py 的 ERR_* 保持一致；前端按 code 渲染自己的文案）
#: 预留（服务端目前不发送；前端已认识它，将来要用就直接发）
ERR_NOT_PAIRED = "not_paired"
ERR_BAD_CODE = "bad_code"
ERR_CODE_EXPIRED = "code_expired"
ERR_LOCKED = "locked"
ERR_NO_CODE = "no_code"


def dumps(obj) -> str:
    """紧凑 JSON（控制消息很小，不必要美化；保证 UTF-8 安全）。"""
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))


def hello(version, sample_rate, channels, frame_ms, sources, backend, device, native_rate, capturing,
          lang=None, paused=False):
    return {
        "type": MSG_HELLO,
        "api": API_VERSION,
        "version": version,
        "sampleRate": int(sample_rate),
        "channels": int(channels),
        "format": FORMAT_F32LE,
        "frameMs": int(frame_ms),
        "sources": list(sources),
        "backend": backend,
        "device": device,
        "nativeRate": int(native_rate) if native_rate else None,
        "capturing": bool(capturing),
        "paused": bool(paused),   # 窗口总开关是否暂停（默认暂停，见文件头）
        "lang": lang,       # 桌面端当前语言：前端可用它决定"错误文案兜底"用哪种语言
    }


def state(capturing, source, backend, device, paused=False):
    return {
        "type": MSG_STATE,
        "capturing": bool(capturing),
        "paused": bool(paused),
        "source": source,
        "backend": backend,
        "device": device,
    }


def level(rms, peak):
    return {"type": MSG_LEVEL, "rms": round(float(rms), 6), "peak": round(float(peak), 6)}


def error(code, message, fatal=False):
    return {"type": MSG_ERROR, "code": code, "message": message, "fatal": bool(fatal)}


def pong(t):
    return {"type": MSG_PONG, "t": t}


class BadMessage(ValueError):
    """客户端消息不合法（缺字段/类型不对/未知 type）。"""


def parse_client_message(raw):
    """解析并校验一条客户端文本消息。返回 dict；不合法抛 BadMessage。

    校验从严是刻意的：这是本机回环上的**唯一**入口，静默忽略非法消息会让
    「前端写错了但看不出来」变成常态。
    """
    if isinstance(raw, (bytes, bytearray)):
        try:
            raw = bytes(raw).decode("utf-8")
        except UnicodeDecodeError:
            raise BadMessage(t("protocol.bad.notUtf8"))
    try:
        msg = json.loads(raw)
    except (ValueError, TypeError):
        raise BadMessage(t("protocol.bad.notJson"))
    if not isinstance(msg, dict):
        raise BadMessage(t("protocol.bad.notObject"))
    mtype = msg.get("type")
    if mtype not in CLIENT_MESSAGE_TYPES:
        raise BadMessage(t("protocol.bad.unknownType", type=mtype))
    if mtype == CLIENT_START:
        source = msg.get("source", "system")
        if source not in ("system", "mic"):
            raise BadMessage(t("protocol.bad.badSource", source=source))
        device = msg.get("device")
        if device is not None and not isinstance(device, str):
            raise BadMessage(t("protocol.bad.badDevice"))
    if mtype == CLIENT_PING and "t" in msg and not isinstance(msg["t"], (int, float)):
        raise BadMessage(t("protocol.bad.badPing"))
    return msg
