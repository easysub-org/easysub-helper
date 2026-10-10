# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 hcz1017
"""配对码与设备令牌（取代"URL 里塞 token"）。

流程：
  1. 助手启动时生成一次性配对码（6 位、默认 10 分钟有效），打印在控制台；
  2. 页面**探测** `/api/pair/info`：用来判断「有没有助手、这个浏览器配过没有、助手是否暂停」；
     音源是**常驻显示**的（早期「探测到才显示」已废弃），但**配对成功才允许开始识别**；
  3. 用户在页面输入配对码 → `POST /api/pair` → 校验通过后下发**长期设备令牌**；
  4. 令牌由页面保存，之后 WS 与 `/api/*` 都用它，不再需要配对码。

为什么必须是"码换令牌"而不是"令牌直接放 URL"：
  - 助手是**用户手动启动**的控制台程序，配对码是"用户在场"的证明；URL 里的令牌一旦被
    本机别的页面/历史记录/截图拿到就等于永久授权。
  - 配对码很短，天然可被暴力枚举，所以配了失败限速与过期时间（见下），并且**校验不通过时
    绝不透露任何关于正确码的信息**（常数时间比较 + 统一错误）。

安全参数：
  - `MAX_ATTEMPTS=5`：60 秒窗口内失败 5 次 → 锁 300 秒（期间一律拒绝，含正确码）；
  - 配对码可在**助手窗口点「换一个」**随时轮换（无窗口模式每 15 秒自动轮换），重新生成会立刻作废旧码；
  - 令牌只在配对成功时下发；本地只存 **sha256 哈希**（文件泄漏也不等于令牌泄漏）。
"""

import hashlib
import json
import os
import secrets
import threading
import time

from .config import data_dir
from .security import safe_compare
from .i18n import t

#: 去掉容易看错的 0/O/1/I/L
CODE_ALPHABET = "23456789ABCDEFGHJKMNPQRSTUVWXYZ"
CODE_LEN = 6
CODE_TTL = 600.0
MAX_ATTEMPTS = 5
ATTEMPT_WINDOW = 60.0
LOCKOUT = 300.0
TOKEN_BYTES = 32
STORE_VERSION = 1

ERR_BAD_CODE = "bad_code"
# 与 protocol.ERR_CODE_EXPIRED / 前端 helperErrorKey 一致（历史上发过 "expired"，
# 前端现在两个都认，但线上以这个为准）
ERR_EXPIRED = "code_expired"
ERR_LOCKED = "locked"
ERR_NO_CODE = "no_code"

ERROR_KEYS = {
    ERR_BAD_CODE: "server.err.badCode",
    ERR_EXPIRED: "server.err.codeExpired",
    ERR_LOCKED: "server.err.locked",
    ERR_NO_CODE: "server.err.noCode",
}


def _now():
    return time.time()


def new_code_value():
    return "".join(secrets.choice(CODE_ALPHABET) for _ in range(CODE_LEN))


def format_code(code):
    """显示用：ABC123 → ABC-123（控制台/页面上更好读）。"""
    if code and len(code) == CODE_LEN:
        return "{}-{}".format(code[:3], code[3:])
    return code or ""


def normalize_code(text):
    """用户输入清洗：去空格/连字符、转大写，兼容全角字符。"""
    if not text:
        return ""
    out = []
    for ch in str(text):
        if ch in " \t\r\n-_\u2013\u2014":
            continue
        out.append(ch)
    cleaned = "".join(out).upper()
    # 全角 A-Z/0-9 归一（中文输入法下很常见）
    trans = {}
    for i in range(10):
        trans[0xFF10 + i] = chr(ord("0") + i)
    for i in range(26):
        trans[0xFF21 + i] = chr(ord("A") + i)
    return cleaned.translate(trans)


def _hash_token(token):
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class PairingManager(object):
    def __init__(self, store_path=None, code_ttl=CODE_TTL, max_attempts=MAX_ATTEMPTS,
                 attempt_window=ATTEMPT_WINDOW, lockout=LOCKOUT, persist=True):
        self._lock = threading.Lock()
        #: 落盘单独一把锁：`_save` 是"读全表→写 tmp→replace"，两个线程同时进来会互踩
        #: （`_save` 只吞 IOError/OSError，RuntimeError 会冒进 Tk 回调）。独立审查 m3。
        self._save_lock = threading.Lock()
        self._code = None
        self._code_expires = 0.0
        self._attempts = []
        self._locked_until = 0.0
        self._tokens = {}   # hash -> {"label":..., "created":...}
        self.code_ttl = float(code_ttl)
        self.max_attempts = int(max_attempts)
        self.attempt_window = float(attempt_window)
        self.lockout = float(lockout)
        self.persist = bool(persist)
        self._store = store_path or os.path.join(str(data_dir()), "pairing.json")
        if self.persist:
            self._load()

    # ---------------- 配对码 ----------------
    def new_code(self):
        """生成新码并立刻作废旧码；同时解除锁定（用户重新看码是明确的人工动作）。"""
        with self._lock:
            self._code = new_code_value()
            self._code_expires = _now() + self.code_ttl
            self._attempts = []
            self._locked_until = 0.0
            return self._code

    def ensure_code(self):
        with self._lock:
            if self._code and _now() < self._code_expires:
                return self._code
        return self.new_code()

    def has_valid_code(self):
        with self._lock:
            return bool(self._code) and _now() < self._code_expires

    def state(self):
        """给 CLI/配对页看的状态（不含任何令牌）。"""
        with self._lock:
            now = _now()
            remaining = max(0.0, self._code_expires - now) if self._code else 0.0
            locked = max(0.0, self._locked_until - now)
            return {
                "hasCode": bool(self._code),
                "valid": bool(self._code) and remaining > 0,
                "code": self._code if (self._code and remaining > 0) else None,
                "codeFormatted": format_code(self._code) if (self._code and remaining > 0) else None,
                "remainingSec": int(remaining),
                "lockedForSec": int(locked),
                "codeTtlSec": int(self.code_ttl),
                "devices": len(self._tokens),
            }

    def verify(self, code):
        """返回 (ok, error_code_or_None)。失败一律只回一个笼统原因，且先查锁定。"""
        normalized = normalize_code(code)
        with self._lock:
            now = _now()
            if self._locked_until > now:
                return False, ERR_LOCKED
            if not self._code:
                return False, ERR_NO_CODE
            if now >= self._code_expires:
                return False, ERR_EXPIRED
            if normalized and safe_compare(normalized, self._code):
                self._attempts = []
                return True, None
            # 失败：记一次并可能触发锁定
            self._attempts = [ts for ts in self._attempts if now - ts < self.attempt_window]
            self._attempts.append(now)
            if len(self._attempts) >= self.max_attempts:
                self._locked_until = now + self.lockout
                self._attempts = []
            return False, ERR_BAD_CODE

    def error_message(self, reason):
        """把错误码翻成用户可读文案（桌面端语言）。"""
        key = ERROR_KEYS.get(reason)
        if key is None:
            return reason or ""
        if reason == ERR_LOCKED:
            with self._lock:
                seconds = int(max(0.0, self._locked_until - _now()))
            return t(key, seconds=seconds)
        return t(key)

    # ---------------- 设备令牌 ----------------
    def issue_token(self, label=None):
        token = secrets.token_urlsafe(TOKEN_BYTES)
        entry = {"label": label or "browser", "created": int(_now())}
        with self._lock:
            self._tokens[_hash_token(token)] = entry
        if self.persist:
            self._save()
        return token

    def token_valid(self, token):
        if not token or not isinstance(token, str):
            return False
        digest = _hash_token(token)
        with self._lock:
            for stored in self._tokens:
                if safe_compare(digest, stored):
                    return True
        return False

    def list_devices(self):
        """已配对设备列表（**含 digest**，供窗口里的「解绑」用）。

        坑（用户可用性审查）：这两个方法以前是**死代码** —— 窗口/CLI 都不调，
        于是用户想撤销一个已配对设备（送修、换机、怀疑令牌泄露）只能自己去猜
        `pairing.json` 的路径删文件（三平台路径还各不相同）。现在窗口里有了
        「已配对设备 → 管理…」入口。
        """
        with self._lock:
            items = [dict(entry, digest=digest) for digest, entry in self._tokens.items()]
        items.sort(key=lambda x: x.get("created", 0))
        return items

    def forget(self, digest):
        """解绑单个设备（按 list_devices() 里的 digest）。返回是否真的删掉了。"""
        if not digest:
            return False
        with self._lock:
            removed = self._tokens.pop(digest, None) is not None
        if removed and self.persist:
            self._save()
        return removed

    def forget_all(self):
        with self._lock:
            count = len(self._tokens)
            self._tokens = {}
        if self.persist:
            self._save()
        return count

    # ---------------- 落盘 ----------------
    def _load(self):
        try:
            with open(self._store, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except (IOError, OSError, ValueError):
            return
        if not isinstance(data, dict) or data.get("version") != STORE_VERSION:
            return
        for item in data.get("tokens") or []:
            if isinstance(item, dict) and item.get("hash"):
                self._tokens[item["hash"]] = {
                    "label": item.get("label") or "browser",
                    "created": int(item.get("created") or 0),
                }

    def _save(self):
        with self._save_lock:                       # 见 __init__: _save_lock 的说明
            return self._save_unlocked()

    def _save_unlocked(self):
            data = {
                "version": STORE_VERSION,
                "tokens": [
                    {"hash": h, "label": v.get("label"), "created": v.get("created")}
                    for h, v in self._tokens.items()
                ],
            }
            tmp = self._store + ".tmp"
            try:
                directory = os.path.dirname(self._store)
                if directory and not os.path.isdir(directory):
                    os.makedirs(directory)
                with open(tmp, "w", encoding="utf-8") as fh:
                    json.dump(data, fh, ensure_ascii=False, indent=2)
                os.replace(tmp, self._store)
            except (IOError, OSError):
                # 落盘失败不影响本次运行（下次启动只是需要重新配对，不该让服务起不来）
                try:
                    if os.path.isfile(tmp):
                        os.remove(tmp)
                except OSError:
                    pass

    def reset(self):
        """清空码、锁定状态与令牌（测试用）。"""
        with self._lock:
            self._code = None
            self._code_expires = 0.0
            self._attempts = []
            self._locked_until = 0.0
            self._tokens = {}
