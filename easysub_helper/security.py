# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 hcz1017
"""本机服务的安全边界。

威胁模型（必须当真）：服务监听在 127.0.0.1，**本机上任何网页**都能访问它。
如果没有鉴权，任意网页都能：
  ① 连上 WS 往识别管道里灌音频（伪造字幕）；② 反向监听把用户的实时字幕读走。
所以两道锁都要有：

  1. **设备令牌**：由配对码换取，只存 sha256 并落盘；`/ws` 只认它（用 `hmac.compare_digest`
     做常数时间比较）。令牌有效就等价于"这个页面被用户亲手放行过"——所以握手不再看 Origin，
     扩展的 offscreen 文档不带 Origin 也不会被误伤。
  2. **Origin 白名单**（只用于 `/api/pair`，那一步还没有令牌）：接受**任意端口**的本机页面
     （`http://127.0.0.1:*` / `http://localhost:*` / `http://[::1]:*` —— Web 版可能跑在任意开发端口）、
     **扩展来源**（`chrome-extension://` / `moz-extension://` / `safari-web-extension://`），
     以及 `--allow-origin` 显式放行的非回环来源。

     扩展来源为什么默认放行：主项目的扩展形态（面板 + offscreen）本来就是一等公民用户，
     要求用户为每个扩展 ID 手加 `--allow-origin` 等于把功能藏起来。放行它的实际风险有限——
     探测接口只回最小信息，**配对仍需要助手窗口里的配对码**（且有失败限速），
     连 WS 还必须有配对成功后下发的设备令牌。介意的话 `PairingManager`/`--allow-origin`
     两侧都能收紧（`allow_extensions=False`）。

另外：**只允许绑回环地址**。--host 传非回环时要显式 --allow-lan（见 cli），
那时服务就暴露在局域网里了，属于用户自担风险的高危操作。
"""


import hmac
import ipaddress
import secrets
from urllib.parse import urlsplit

TOKEN_BYTES = 24


def new_token() -> str:
    return secrets.token_urlsafe(TOKEN_BYTES)


def safe_compare(a, b) -> bool:
    """常数时间比较，且**对非 ASCII 安全**。

    坑（真机复现）：`hmac.compare_digest` 的 str 重载要求两侧都是 ASCII，否则抛
    `TypeError: comparing strings with non-ASCII characters is not supported`。配对码或
    Origin 里只要有一个非 ASCII 字符，`/api/pair` / `/api/pair/info` 就会 500，而且抛异常
    发生在"记一次失败"之前 → **限速被绕过**。所以统一先 encode 成 bytes 再比。
    """
    if not isinstance(a, str) or not isinstance(b, str):
        return False
    return hmac.compare_digest(a.encode("utf-8"), b.encode("utf-8"))


def token_ok(provided, expected) -> bool:
    """常数时间比较。空 expected（未启用）或空 provided 一律拒绝。"""
    if not expected or not provided:
        return False
    return safe_compare(provided, expected)


def is_loopback_host(host: str) -> bool:
    if not host:
        return False
    if host in ("localhost", "localhost.localdomain"):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


#: 浏览器扩展页面的 Origin 前缀（扩展 ID 各不相同，只能按 scheme 放行）
EXTENSION_SCHEMES = ("chrome-extension://", "moz-extension://", "safari-web-extension://")


def is_extension_origin(origin):
    if not origin:
        return False
    text = str(origin).strip().rstrip("/").lower()
    for scheme in EXTENSION_SCHEMES:
        if text.startswith(scheme) and len(text) > len(scheme):
            return True
    return False


def is_loopback_origin(origin) -> bool:
    """任意端口的**本机页面**：`http://127.0.0.1:5173` / `http://localhost:3000` / `http://[::1]:8443`。

    端口刻意不校验：Web 版可能跑在任意开发端口（vite 5173、serve-web 3000…），
    "必须与助手同端口"会把正常用户挡在门外（用户实测：dist-web 起来后页面里根本没有这个音源）。
    真正的凭据是**配对码**——Origin 白名单只用在没有令牌的 `/api/pair` 上，且失败有限速。
    非回环来源（部署在别的域名）仍然要 `--allow-origin` 显式放行。
    """
    text = (origin or "").strip().rstrip("/")
    if not text:
        return False
    try:
        parts = urlsplit(text)
    except ValueError:
        return False
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return False
    host = parts.hostname.lower()
    if host in ("localhost", "localhost.localdomain"):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def allowed_origins(port: int, extra=None):
    """本机页面 + 显式放行的其它来源。"""
    items = [
        "http://127.0.0.1:{}".format(port),
        "http://localhost:{}".format(port),
    ]
    if extra:
        for o in extra:
            o = (o or "").strip().rstrip("/")
            if o:
                items.append(o)
    return tuple(items)


def origin_ok(origin, port: int, extra=None, allow_extensions=True) -> bool:
    """校验 Origin（只用于 `/api/pair`：那一步还没有令牌，白名单是唯一防线）。

    放行三类：本机任意端口的页面（`is_loopback_origin`）、浏览器扩展协议、
    以及 `--allow-origin` 显式列出的来源。缺失 Origin 默认拒绝，需要时走
    allow_no_origin（见 origin_ok_or_missing）。
    """
    if not origin:
        return False
    origin = origin.strip().rstrip("/")
    if allow_extensions and is_extension_origin(origin):
        return True
    if is_loopback_origin(origin):
        return True
    for allowed in allowed_origins(port, extra):
        if safe_compare(origin, allowed):
            return True
    return False


def origin_ok_or_missing(origin, port: int, extra=None, allow_no_origin=False,
                         allow_extensions=True) -> bool:
    if not origin:
        return bool(allow_no_origin)
    return origin_ok(origin, port, extra, allow_extensions=allow_extensions)
