# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 hcz1017
"""入口：默认弹出图形界面；命令行只留给服务器与排障。

**用户路径不是命令行**：不给任何参数（或双击启动器）会弹出一个 tkinter 窗口——窗口里有
「启动 / 暂停」总开关、大字配对码、电平图、语言切换。助手只做两件事：
**采本机音频**、**和浏览器配对后把 16k PCM 推过去**。

没有图形界面（Linux 缺 python3-tk，或 SSH 无 DISPLAY）时自动回落命令行：
打印端口与配对码，然后等 Ctrl+C。无窗口模式下**默认就开始采集**——没有按钮可按，
音频由页面请求驱动（`--no-gui` 是服务器/排障用法，不是给用户用的）。

语言判定顺序：`--lang` > 窗口里保存的选择 > `EASYSUB_LANG`/`LANG` > `zh_CN`。
"""

import argparse
import asyncio
import io
import logging
import os
import signal
import sys

# 允许"直接跑文件"：`python easysub_helper/cli.py`、`./easysub_helper/cli.py`、双击。
# 那时模块没有包上下文，`from . import x` 会直接抛 ImportError（用户实际踩到过）。
# 把包的上一级目录塞进 sys.path 并补上 __package__，让这几种写法和
# `python -m easysub_helper` 完全等价（必须在下面的相对 import 之前执行）。
if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    __package__ = "easysub_helper"

from . import __version__, config, security
from .i18n import LANGUAGES, detect_language, set_language, source_label, t
from .net import HelperServer
from .pairing import PairingManager, format_code

LOG = logging.getLogger("easysub-helper")

#: 无窗口模式：配对码过期就自动换一个并打印（用户没有「换一个」按钮可按）
CODE_ROTATE_SEC = 15.0


# ---------------- 基础设施 ----------------
def _soften_console_encoding():
    """让 stdout/stderr 在"编码不了"时**不崩**，并尽量把中文保留下来。

    为什么必须做：Windows 上把输出**重定向到文件/管道**时（CI、`> log.txt`、服务方式启动），
    Python 用的是系统代码页（常见 cp1252），而不是控制台的 UTF-16 通道。于是：
      * `argparse.print_help()` 直接写 `sys.stdout` → 中文抛 UnicodeEncodeError，进程崩（CI 就是这么红的）；
      * 我们的 `_eprint()` 虽然吞异常不崩，但**每一行中文都被静默丢掉** → 代理版横幅里的端口、
        配对码全没了，用户以为助手没起来。
    解决：把两个流切成 UTF-8 + `errors="replace"`。重定向场景下中文能正常写出（GitHub 日志、
    文本编辑器都按 UTF-8 读）；万一目标真收不了，也只是显示成 `?`，不会崩。
    交互式控制台本来就是 UTF-8 通道，这一步等于没变；窗口界面（Tk）完全不受影响。
    """
    for name in ("stdout", "stderr"):
        stream = getattr(sys, name, None)
        if stream is None:
            # --windowed/--noconsole 打包后 sys.stdout/stderr 就是 None：argparse 的
            # --version/--help（以及任何 print）会直接 AttributeError 崩掉。给个"黑洞"顶上。
            try:
                setattr(sys, name, open(os.devnull, "w", encoding="utf-8", errors="replace"))
            except Exception:           # noqa: BLE001
                pass
            continue
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:     # Python 3.7+
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except Exception:           # noqa: BLE001 - 只影响可读性，别在这崩
                pass
            continue
        # Python 3.6 没有 reconfigure：用一层 TextIOWrapper 兜住（保留原流对象不动，只换包装）
        buffer = getattr(stream, "buffer", None)
        if buffer is None:
            continue
        try:
            setattr(sys, name, io.TextIOWrapper(buffer, encoding="utf-8",
                                                errors="replace", line_buffering=True))
        except Exception:               # noqa: BLE001
            pass


def _stream():
    """打包成 --windowed（无控制台）时 sys.stdout/stderr 是 None：写入会炸，所以统一兜住。"""
    out = getattr(sys, "stderr", None)
    if out is None:
        out = getattr(sys, "stdout", None)
    return out


def _eprint(text):
    out = _stream()
    if out is None:
        return
    try:
        out.write(text + "\n")
        out.flush()
    except Exception:  # noqa: BLE001 - 输出失败不该影响功能
        pass


def run_async(coro, cleanup=None):
    """py3.6 没有 asyncio.run；同时保证 Ctrl+C 时 cleanup（停采集/关服务）一定会跑。"""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    code = 0
    try:
        code = loop.run_until_complete(coro)
    except KeyboardInterrupt:
        _eprint("\n" + t("run.interrupted"))
    finally:
        if cleanup is not None:
            try:
                loop.run_until_complete(cleanup())
            except Exception as exc:  # noqa: BLE001 - 退出路径不抛异常
                LOG.debug("cleanup failed: %s", exc)
        try:
            loop.run_until_complete(loop.shutdown_asyncgens())
        except Exception:  # noqa: BLE001
            pass
        loop.close()
        asyncio.set_event_loop(None)
    return code


def _setup_logging(level):
    level = getattr(logging, str(level).upper(), logging.INFO)
    if getattr(sys, "stderr", None) is None:
        # 无控制台（窗口模式打包）：不要往 None 上写；日志仍然会进 GUI 的日志区
        logging.basicConfig(handlers=[logging.NullHandler()], level=level)
        return
    logging.basicConfig(level=level, format="%(asctime)s %(levelname)s %(message)s",
                        datefmt="%H:%M:%S")


def _prescan_lang(argv):
    """在 argparse 之前找出 --lang，避免 help 文案定死在默认语言。"""
    args = list(sys.argv[1:] if argv is None else argv)
    for i, item in enumerate(args):
        if item == "--lang" and i + 1 < len(args):
            return args[i + 1]
        if item.startswith("--lang="):
            return item.split("=", 1)[1]
    return None


def resolve_language(argv=None):
    """`--lang` > 窗口里保存的选择 > 环境变量 > 默认。"""
    saved = config.load_settings().get("lang")
    return detect_language(_prescan_lang(argv) or saved)


# ---------------- 服务 ----------------
def build_server(args, user_on=False):
    """按参数造服务。`user_on` 是窗口总开关的初值（无窗口模式下直接 True）。"""
    fixed_token = args.token or None
    pairing = None
    if not fixed_token:
        pairing = PairingManager()
        pairing.ensure_code()
    server = HelperServer(
        pairing=pairing,
        port=args.port,
        host=args.host,
        default_source=args.source,
        backend=args.backend,
        device=args.device,
        allow_origins=args.allow_origin,
        allow_cors_all=args.allow_cors_all,
        version=__version__,
        fixed_token=fixed_token,
        user_on=user_on,
    )
    return server, pairing


async def _rotate_pair_code(pairing):
    """无窗口模式：过期就换码并打印（窗口里有「换一个」按钮，命令行没有）。"""
    while True:
        await asyncio.sleep(CODE_ROTATE_SEC)
        before = pairing.state().get("code")
        pairing.ensure_code()
        after = pairing.state().get("code")
        if after and after != before:
            LOG.info(t("log.codeRotated", code=format_code(after)))


async def _serve(server, pairing):
    await server.start()
    state = pairing.state() if pairing is not None else {"valid": False}
    _eprint("")
    _eprint("{} v{}".format(t("app.title"), __version__))
    _eprint(t("run.banner.port", port=server.port))
    _eprint(t("run.banner.source", source=source_label(server.default_source),
              backend=server.backend))
    if server.fixed_token:
        _eprint(t("run.banner.debugToken"))
    elif state.get("valid"):
        _eprint(t("run.banner.pairCode", code=format_code(state["code"]),
                  ttl=max(1, int(round(state["remainingSec"] / 60.0)))))
        _eprint(t("run.banner.pairHint"))
    _eprint(t("run.banner.stop"))
    _eprint("")

    rotator = None
    if pairing is not None:
        rotator = asyncio.ensure_future(_rotate_pair_code(pairing))

    loop = asyncio.get_event_loop()
    stop = asyncio.Event()
    for signame in ("SIGINT", "SIGTERM"):
        sig = getattr(signal, signame, None)
        if sig is None:
            continue
        try:
            loop.add_signal_handler(sig, stop.set)
        except (NotImplementedError, RuntimeError, ValueError):
            # Windows 的事件循环不支持 add_signal_handler：靠 KeyboardInterrupt 兜底
            pass
    try:
        await stop.wait()
    except asyncio.CancelledError:
        pass
    if rotator is not None:
        rotator.cancel()
    _eprint(t("run.stopping"))
    await server.stop()
    return 0


# ---------------- 参数解析 ----------------
def build_parser():
    parser = argparse.ArgumentParser(
        prog="easysub-helper",
        description="{} —— {}".format(t("app.title"), t("app.desc")),
    )
    parser.add_argument("--version", action="version", version="%(prog)s {}".format(__version__))
    parser.add_argument("--lang", default=None, help=t("cli.help.lang", langs="/".join(LANGUAGES)))
    parser.add_argument("--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    parser.add_argument("--no-gui", action="store_true", help=t("cli.help.nogui"))
    parser.add_argument("--port", type=int, default=config.DEFAULT_PORT,
                        help=t("cli.help.port", default=config.DEFAULT_PORT))
    parser.add_argument("--host", default=config.DEFAULT_HOST, help=t("cli.help.host"))
    parser.add_argument("--allow-lan", action="store_true", help=t("cli.help.allowlan"))
    parser.add_argument("--source", default="system", choices=["system", "mic"],
                        help=t("cli.help.source"))
    parser.add_argument("--backend", default="auto",
                        choices=["auto", "soundcard", "parec", "pw-record", "mac-system"],
                        help=t("cli.help.backend"))
    parser.add_argument("--device", help=t("cli.help.device"))
    parser.add_argument("--token", help=t("cli.help.token"))
    parser.add_argument("--allow-origin", action="append", default=[],
                        help=t("cli.help.alloworigin"))
    parser.add_argument("--allow-cors-all", action="store_true",
                        help=t("cli.help.corsall"))
    parser.add_argument("--selftest", action="store_true",
                        help=t("cli.help.selftest"))
    parser.add_argument("--require-tkinter", action="store_true",
                        help=t("cli.help.requiretkinter"))
    return parser


def main(argv=None):
    _soften_console_encoding()          # 必须在 parse_args 之前：--help 会直接写 sys.stdout
    set_language(resolve_language(argv))
    args = build_parser().parse_args(argv)
    if args.lang:
        set_language(args.lang)
    _setup_logging(args.log_level)
    if args.selftest:
        # 冻结产物自检（CI 的打包作业用它抓"包打错了但能起来 --version"这一类问题）。
        # 必须排在 GUI/服务启动之前：它自己起一个临时服务，跑完就退。
        from . import selfcheck

        return selfcheck.main(require_tkinter=args.require_tkinter)
    if not security.is_loopback_host(args.host) and not args.allow_lan:
        _eprint(t("run.err.hostNotLoopback", host=args.host))
        return 2

    from . import gui

    use_gui = (not args.no_gui) and gui.available()
    server, pairing = build_server(args, user_on=not use_gui)

    def cleanup():
        return server.stop()

    if use_gui:
        gui.install_log_buffer()
        code = gui.run(server, pairing)
        if code is not None:
            return code
        # 窗口没能起来（极小概率）：退回落模式，总开关得打开，否则页面永远拿不到音频
        server.user_on = True
    elif not args.no_gui:
        _eprint(t("gui.errNoTk"))

    try:
        return run_async(_serve(server, pairing), cleanup=cleanup)
    except RuntimeError as exc:
        _eprint(str(exc))
        return 1


if __name__ == "__main__":
    sys.exit(main())
