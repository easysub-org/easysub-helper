# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 hcz1017
"""桌面端 i18n。

纪律（和主项目 `src/i18n.ts` 同样的要求）：
  1. **所有面向用户的字符串都走 `t()`**，不许在代码里硬编码中文/英文——包括窗口里每一个控件、
     **窗口日志区里显示的日志正文**、CLI 输出、采集后端的报错、WS 错误文案；
     （日志的 level 名 INFO/WARNING 是技术字段，保持英文。）
  2. WS 错误消息**同时**给出稳定的 `code`（见 protocol.py），主项目前端用自己的 i18n 覆盖文案；
     桌面端的文案只是兜底；
  3. `tests/test_i18n.py` 会扫描源码里所有 `t("key")` 并核对每份语言目录，
     缺 key 直接失败——这是"全程 i18n"的机械保证，而不是靠自觉。

语言判定顺序：`--lang` > 窗口里保存的选择（settings.json）> `EASYSUB_LANG`
> `LC_ALL`/`LC_MESSAGES`/`LANG` > 默认 zh_CN。窗口里有一个语言切换按钮，点击就地重译并记住选择。
语言集合与主项目保持一致：zh_CN / en。
"""

import os
import threading

DEFAULT_LANG = "zh_CN"
LANGUAGES = ("zh_CN", "en")

_lock = threading.Lock()
_current = DEFAULT_LANG
_missing_reported = set()

#: 参数名 -> 该参数在消息里的**实际取值**也要 i18n 的情况（如 backend 名、source 名）
_SOURCE_LABEL = {
    "zh_CN": {"system": "系统音频", "mic": "麦克风"},
    "en": {"system": "system audio", "mic": "microphone"},
}

_STRINGS = {
    "zh_CN": {

        # ---- 应用 ----
        "app.title": "易字幕本机助手",
        "app.desc": "用操作系统 API 采集本机音频，配对后把 16 kHz PCM 推给浏览器页面（只做采集与配对）。",
        "lang.zh_CN": "中文",
        "lang.en": "English",

        # ---- 命令行（排障/服务器）----
        "cli.help.lang": "界面语言（{langs}）",
        "cli.help.nogui": "不弹窗口，用纯命令行模式（服务器/排障；此模式下默认就开始采集）",
        "cli.help.port": "监听端口（默认 {default}；被占用时从它向后顺延 20 个）。注意：字幕页面只自动探测 {default}–{last}，所以别改这个默认值（改成别的基础端口后顺延会越过 {last}，页面就找不到助手了；真改了的话在页面离线框里手动填端口）",
        "cli.help.allowmulti": "允许同时运行多个助手实例（默认拒绝：两个实例各有各的配对码，还会互相覆盖已配对设备）",
        "cli.warn.portOutOfRange": "⚠ 用了 --port {port}：助手被占用时会从 {port} 起向后顺延，可能落到字幕页面探测不到的端口（页面只自动探测 {first}–{last}）。除非你在页面离线框里手动填端口，否则请去掉 --port（默认值本来就会自动顺延）。",
        "cli.warn.portZero": "⚠ 用了 --port 0：助手会绑一个随机端口，字幕页面永远自动探测不到它。除非你打算每次都在页面离线框里手动填端口，否则请去掉这个参数。",
        "cli.warn.hostSpecific": "⚠ --host {host} 只会绑定这一张网卡：字幕页面永远访问 127.0.0.1，会因此找不到助手。要给局域网用请写 --host 0.0.0.0 --allow-lan（这样本机页面也能用）。",
        "cli.err.multiInstance": "已经有一个助手在运行（端口 {port}）。同时开两个会让两个窗口各有一份配对码、并互相覆盖已配对设备（表现为「过一会儿又要重新配对」）——请用已经开着的那个窗口。确实要同时跑多个实例请加 --allow-multi。",
        "cli.warn.multiInstance": "⚠ 检测到另一个助手已在端口 {port} 运行：两个实例会互相覆盖已配对设备（「过一会儿又要重新配对」），配对码也各是各的。",
        "cli.help.host": "监听地址（默认只绑回环）",
        "cli.help.allowlan": "允许绑非回环地址（高危，见 README）",
        "cli.help.source": "默认音源：system=系统音频，mic=麦克风",
        "cli.help.backend": "采集后端：auto/soundcard/parec/pw-record/mac-system",
        "cli.help.device": "设备名子串（如 \"Monitor of ...\"）",
        "cli.help.token": "跳过配对，直接用固定设备令牌（仅调试/测试）",
        "cli.help.restrictorigin": "【可选·收紧】只放行列出的 Origin（外加本机页面与浏览器扩展），可重复。默认是 CORS 全放行，正常使用不需要它",
        "cli.help.alloworigin": "（历史参数，已不需要：默认就是全放行）在 --restrict-origin 收紧模式下的额外白名单，可重复",
        "cli.help.corsall": "（历史参数，已不需要：默认就是 CORS 全放行）助手只监听回环，配对码是唯一凭据，外部网页最多用错码触发限速",
        "cli.help.selftest": "自检：检查冻结产物里的 tkinter / 资源 / 重采样 / 本机 HTTP+WS 环路，全通过退出码 0（打包作业用）",
        "cli.help.requiretkinter": "配合 --selftest：把「无头环境缺 tkinter」也算失败。打包冒烟用它确认**产物**真的带 tkinter",
        "cli.err.requiretkinterNeedsSelftest": "ERROR: --require-tkinter 必须和 --selftest 一起用（它只是把自检里的 tkinter 提醒升级成失败）。",

        # ---- 无窗口模式下的启动横幅 ----
        "run.banner.port": "端口     : {port}",
        "run.banner.source": "音源     : {source}（后端 {backend}）",
        "run.banner.pairCode": "配对码   : {code}   （{ttl} 分钟内有效；在页面里输入它完成配对）",
        "run.banner.pairHint": "在页面里把音源选成「桌面助手」，再把这串码填进去即可",
        "run.banner.debugToken": "[debug] 已用 --token 指定固定令牌，跳过配对码校验",
        "run.banner.stop": "退出     : Ctrl+C",
        "run.err.hostNotLoopback": "ERROR: --host {host} 不是回环地址。本服务会把本机音频交给连上来的页面，\n       暴露到局域网等于把音频流公开。确需如此请显式加 --allow-lan（自担风险）。",
        "run.err.portBusy": "无法监听 {host}:{port}（已尝试 {count} 个端口）：{error}",
        "run.stopping": "正在停止…",
        "run.interrupted": "收到中断，正在退出…",

        # ---- 图形界面（用户唯一入口）----
        "gui.title": "易字幕本机助手",
        "gui.subtitle": "用系统 API 直接采集本机音频，交给浏览器里的识别引擎。\n不需要共享屏幕，也不需要装虚拟声卡。",
        "gui.subtitleMac": "用系统 API 直接采集本机音频，交给浏览器里的识别引擎。\n不需要共享屏幕。macOS 14.2+ 免驱动；更早的系统需要 BlackHole（或自行安装 pysysaudio 免驱动）。",
        "gui.statusStarting": "正在启动…",
        "gui.statusFailed": "启动失败",
        "gui.statusPaused": "已暂停（服务运行中，连接不断）· 端口 {port}",
        "gui.captureOn": "正在采集 · {source}",
        "gui.captureFailed": "采集出错",
        "gui.waitingFrames": "已启动，等待音频帧…",
        "gui.backend": "后端 {backend} · {rate} Hz",
        "gui.start": "启动",
        "gui.pause": "暂停",
        "gui.pairTitle": "配对码",
        "gui.pairHint": "在字幕页面的「音频来源」里选最后一项（桌面助手），填下面这串即可。",
        "gui.pairLocked": "已锁定 {sec} 秒：点「换一个」立即解锁（不用等）",
        "gui.statusPort": "端口 {port}",
        "gui.captureFailedHint": "点「启动」重试",
        "gui.deviceWhenUnknown": "时间未知",
        "log.trayReady": "托盘已就绪：「关闭窗口」只收窗口，服务与采集继续跑",
        "log.trayFailed": "托盘起不来（{error}）：已取消「关窗最小化到托盘」，关窗仍会结束服务",
        "cli.dlg.multiInstance": "已经有一个助手在运行（端口 {port}）。\n\n同时开两个会让两个窗口各有各的配对码、并互相覆盖已配对设备（表现为「过一会儿又要重新配对」）。\n\n要以另一个实例继续吗？",
        "gui.devicesCount": "已配对设备：{count}",
        "gui.devicesManage": "管理…",
        "gui.devicesTitle": "已配对设备",
        "gui.devicesEmpty": "还没有页面配对过。",
        "gui.devicesHint": "解绑之后，那个页面需要重新配对（把新码填一次）。",
        "gui.deviceWhen": "{when}",
        "gui.deviceUnbind": "解绑",
        "gui.deviceSelectFirst": "先在列表里选中要解绑的设备",
        "gui.deviceUnbindAllConfirm": "再点一次＝全部解绑",
        "gui.deviceUnbindAll": "全部解绑",
        "gui.devicesUnbound": "已解绑 {count} 个设备",
        "gui.close": "关闭",
        "gui.minimizeToTray": "关闭窗口时最小化到托盘（服务继续跑）",
        "gui.trayUnavailable": "托盘不可用（缺 pystray 或 Pillow），关窗仍会结束服务。装法：{install}",
        "gui.trayStartFailed": "托盘起不来（{error}）：已关闭「关窗最小化到托盘」，关窗仍会结束服务",
        "gui.trayXorgHint": "注意：你的桌面（{desktop}）用的是 StatusNotifier 托盘协议，而现在只能回落到旧版 XEmbed 后端，图标可能不显示、点击也可能没反应。装一个纯 Python 的 D-Bus 库就能改用系统原生托盘：{install}（装完重开助手）",
        "gui.trayShow": "显示窗口",
        "gui.trayStart": "启动采集",
        "gui.trayPause": "暂停采集",
        "gui.trayQuit": "退出",
        "gui.pairTtl": "{sec} 秒内有效",
        "gui.pairCopy": "复制",
        "gui.pairCopied": "已复制 ✓",
        "gui.pairNew": "换一个",
        "gui.pairNone": "配对已关闭（调试模式）",
        "gui.pairDebug": "当前用 --token 固定令牌，无需配对码",
        "gui.sourceLabel": "音源",
        "gui.deviceLabel": "设备",
        "gui.openedDevice": "设备 {device}",
        "device.default": "系统默认",
        # 内部哨兵串的**显示**文案（真正传给 parec/pw-record 的是 @DEFAULT_MONITOR@）
        "audio.deviceDefaultSystem": "默认输出（系统声音）",
        "audio.deviceDefaultInput": "系统默认输入",
        "gui.levelLabel": "电平",
        "gui.levelSilent": "静音",
        "gui.levelSignal": "有信号",
        "gui.pausedHint": "已暂停 —— 点「启动」开始采集（不需要配对）",
        "gui.quit": "退出",
        "gui.logLabel": "日志",
        "gui.keepOpen": "关窗即结束服务（勾选「最小化到托盘」可让服务继续跑）；「暂停」只停采集，连接不断。",
        "gui.licenseSource": "AGPL-3.0-or-later · 源代码",
        "gui.clients": "已连接页面：{count}",
        "gui.errNoTk": "没有可用的图形界面（缺少 tkinter 或无显示环境），已回落到命令行模式。Linux 上装 tkinter：sudo apt install python3-tk（Debian/Ubuntu）、sudo dnf install python3-tkinter（Fedora）。",

        # ---- 日志（会显示在窗口日志区，也要 i18n）----
        "log.listening": "正在监听 {url}",
        "log.pairFailed": "配对失败：{reason}（Origin={origin}）",
        "log.pairOk": "配对成功：{label}（已配对设备 {devices}）",
        "log.stopFailed": "停止采集异常：{error}",
        "log.captureFailed": "采集故障：{error}",
        "log.wsBadToken": "拒绝 WS：令牌无效（Origin={origin}）",
        "log.wsOpen": "WS 已连接（Origin={origin}，页面数 {clients}）",
        "log.wsClosed": "WS 已断开（页面数 {clients}）",
        "log.wsError": "WS 错误：{error}",
        "log.serviceStartFailed": "服务启动失败：{error}",
        "log.switchSource": "切换音源：{source}",
        "log.switchDevice": "切换设备：{device}",
        "log.openSourceFailed": "打不开浏览器打开源码地址：{error}",
        "log.newCode": "已生成新配对码：{code}",
        "log.minimizedToTray": "窗口已收进托盘：服务与采集继续跑；要退出请用托盘菜单里的「退出」",
        "log.codeRotated": "配对码已更新：{code}",
        "log.guiActionFailed": "界面操作失败：{error}",
        "log.langSwitched": "界面语言：{lang}",
        "log.guiRefreshFailed": "界面刷新异常：{error}",

        # ---- 服务端 ----
        "server.err.forbidden": "没有权限（设备令牌无效或来源不被允许）",
        "server.err.originDenied": "这个页面来源不被允许配对（助手当前处于 --restrict-origin 收紧模式）：把它的来源加进白名单，或去掉该开关（默认是全放行的）",
        "server.err.originMissing": "这个请求没有带 Origin（通常是 curl/脚本等非浏览器客户端）。缺 Origin 的请求默认拒；浏览器请求一定带 Origin，不受影响。",
        "log.stopTimeout": "采集线程没有在超时内退出（设备可能仍被占用），已停止等待",
        "log.pysysaudioPermCheck": "pysysaudio 权限检测失败（忽略）：{error}",
        "log.pysysaudioFallback": "pysysaudio 不可用，回落到 soundcard：{error}",
        "audio.err.pysysaudioNoRecorder": "这个 pysysaudio 版本没有 SystemAudioRecorder（需要 macOS 14.2+ 的版本）",
        "audio.err.macNoSystemAudioDetail": "拿不到 macOS 系统音频。两条路都失败了：① pysysaudio（免驱动，需要 macOS 14.2+ 且自行安装：pip install pysysaudio）：{detail}；② soundcard 回落（需要 BlackHole：https://existential.audio/blackhole/ —— 装好后把「音源」切到「麦克风」，再到「设备」下拉里选它）：{detail2}",
        "log.pairOriginDenied": "拒绝配对：来源不被允许（Origin={origin}）",
        "log.wsTooManyConnections": "拒绝新的 WebSocket 连接：已达上限 {limit} 条（可能有页面失控开连接）",
        "server.err.tooManyConnections": "连接数已达上限，请关闭多余的易字幕标签页后重试",
        "log.noOrigin": "（无 Origin）",
        "server.err.pairBody": "配对请求体不是合法 JSON",
        "server.err.badMessage": "客户端消息不合法：{detail}",
        "server.err.binaryFrame": "客户端不应发送二进制帧",
        "server.err.captureFailed": "启动采集失败：{detail}",
        "server.err.captureAborted": "采集中断：{detail}",
        "server.err.paused": "助手窗口处于暂停：请在助手窗口点「启动」",
        "server.err.notPaired": "尚未配对：请在页面里输入桌面端显示的配对码",
        "server.err.badCode": "配对码不正确",
        "server.err.codeExpired": "配对码已过期，请在桌面端重新生成",
        "server.err.locked": "失败次数过多，请 {seconds} 秒后再试",
        "server.err.noCode": "桌面端当前没有有效配对码，请在桌面端生成",

        # ---- 采集层 ----
        "audio.err.openDevice": "无法打开采集设备 {device}（试过采样率 {rates}）：{error}",
        "audio.err.deviceNotFound": "找不到设备 {device}（在助手窗口的「设备」下拉里选）",
        "audio.err.deviceAmbiguous": "设备名 {device} 匹配到 {count} 个设备，请在助手的「设备」下拉里直接选",
        "audio.err.noSpeaker": "系统里没有默认扬声器，取不到 loopback 音源。Linux/Windows：先在系统设置里选一个默认输出设备。macOS：① 装 pysysaudio 免驱动（pip install pysysaudio，需要 macOS 14.2+）；或 ② 装 BlackHole（https://existential.audio/blackhole/）→ 然后把窗口「音源」切到「麦克风」→ 在「设备」下拉里选 BlackHole（系统音频模式下没有这个下拉）。",
        "audio.err.loopbackFailed": "取默认扬声器 {device} 的 loopback 失败：{error}",
        "audio.err.noMic": "系统里没有默认麦克风",
        "audio.err.streamEnded": "采集流已结束（来源被关闭 / 设备被拔出 / 被其它程序抢占）",
        "audio.err.toolMissing": "找不到 parec / pw-record（Linux 上需要 PulseAudio 或 PipeWire 的「命令行工具」，只装服务本身不够）。装法：sudo apt install pulseaudio-utils（Debian/Ubuntu）或 sudo apt install pipewire-audio（PipeWire 系）；装完重开助手。",
        "audio.err.toolStart": "启动 {tool} 失败：{error}",
        "audio.err.toolExited": "{tool} 启动即退出（code={code}）：{detail}",
        "audio.err.toolEnded": "{tool} 输出结束（code={code}）：{detail}",
        "audio.err.noStderr": "无 stderr 输出",
        "audio.err.unknownBackend": "未知的采集后端：{backend}（可用：auto/soundcard/parec/pw-record）",
        "audio.err.backendNotLinux": "{backend} 后端只在 Linux 上可用",
        "audio.err.backendNotMac": "{backend} 后端只在 macOS 上可用",
        "audio.err.notOpened": "采集未打开",
        "audio.err.pysysaudioOpen": "pysysaudio 打开失败：{error}",
        "audio.err.pysysaudioDenied": "系统音频权限被拒：请在「系统设置 → 隐私与安全性 → 屏幕与系统音频录制」里授权",

        # ---- 重采样 ----
        "resample.err.unavailable": "无法重采样 {from_rate} -> {to_rate}（{detail}）。这是助手自身的依赖缺失或产物损坏，请重新安装助手（正常情况下内置重采样就够用）。",
        "resample.err.badRate": "采样率必须为正",
        "resample.err.unknownKind": "未知的重采样实现：{kind}",
        "resample.err.designFailed": "原型滤波器设计失败（采样率比异常：{from_rate}/{to_rate}）",
        "resample.err.noStream": "soxr 缺少 ResampleStream（需要 soxr>=0.3.5）",

        # ---- 协议参数校验 ----
        "protocol.bad.notUtf8": "二进制帧不是合法的 UTF-8 JSON",
        "protocol.bad.notJson": "不是合法 JSON",
        "protocol.bad.notObject": "顶层必须是对象",
        "protocol.bad.unknownType": "未知的消息类型：{type}",
        "protocol.bad.badSource": "source 只能是 system / mic，收到 {source}",
        "protocol.bad.badDevice": "device 必须是字符串",
        "protocol.bad.badPing": "ping.t 必须是数字",
    },

    "en": {

        # ---- 应用 ----
        "app.title": "EasySub Helper",
        "app.desc": "Captures this machine's audio through the OS APIs and streams 16 kHz PCM to a paired browser page (capture + pairing only).",
        "lang.zh_CN": "中文",
        "lang.en": "English",

        # ---- 命令行（排障/服务器）----
        "cli.help.lang": "UI language ({langs})",
        "cli.help.nogui": "no window: plain command line (servers/debugging; capture starts immediately)",
        "cli.help.port": "listen port (default {default}; walks forward up to 20 ports when busy). Note: the subtitle page auto-probes {default}-{last} only, so leave the default alone (with any other base port the walk can pass {last} and the page will not find the helper; if you do change it, type the port in the page dialog)",
        "cli.help.allowmulti": "allow several helper instances at once (refused by default: each instance has its own pair code and they overwrite each other's paired devices)",
        "cli.warn.portOutOfRange": "⚠ --port {port} was given: when busy the helper walks forward from {port}, which can land on a port the subtitle page does not auto-probe (it probes {first}-{last}). Unless you type the port manually in the page dialog, drop --port (the default already walks forward).",
        "cli.warn.portZero": "⚠ --port 0 was given: the helper binds a random port that the subtitle page can never auto-probe. Unless you plan to type the port in the page dialog every time, drop it.",
        "cli.warn.hostSpecific": "⚠ --host {host} binds only that one interface: the subtitle page always talks to 127.0.0.1, so it will not find the helper. For LAN use pass --host 0.0.0.0 --allow-lan (that keeps loopback working too).",
        "cli.err.multiInstance": "another helper is already running (port {port}). Two instances have separate pair codes and overwrite each other's paired devices (the symptom is \"it asks me to pair again after a while\") - please use the window that is already open. Add --allow-multi if you really want several.",
        "cli.warn.multiInstance": "⚠ another helper is already running on port {port}: the two will overwrite each other's paired devices, and each has its own pair code.",
        "cli.help.host": "listen address (loopback only by default)",
        "cli.help.allowlan": "allow binding a non-loopback address (dangerous, see README)",
        "cli.help.source": "default source: system=system audio, mic=microphone",
        "cli.help.backend": "capture backend: auto/soundcard/parec/pw-record/mac-system",
        "cli.help.device": "device name substring (e.g. \"Monitor of ...\")",
        "cli.help.token": "skip pairing and use a fixed device token (debugging/testing only)",
        "cli.help.restrictorigin": "[optional, locks down] only allow the listed Origins (plus loopback pages and browser extensions), repeatable. CORS is fully open by default, so you do not need this",
        "cli.help.alloworigin": "(legacy, no longer needed: everything is allowed by default) extra allowlist entries when --restrict-origin is used, repeatable",
        "cli.help.corsall": "(legacy, no longer needed: CORS is fully open by default) the helper only listens on loopback and the pair code is the only credential, so the worst an external page can do is burn pairing attempts",
        "cli.help.selftest": "self-check the frozen build (tkinter, assets, resampler, loopback HTTP+WS round trip); exits 0 only if everything passes (used by the packaging jobs)",
        "cli.help.requiretkinter": "with --selftest: also fail when tkinter is missing in a headless environment. The packaging smoke uses it to prove the *artifact* really bundles tkinter",
        "cli.err.requiretkinterNeedsSelftest": "ERROR: --require-tkinter only makes sense together with --selftest (it upgrades the tkinter warning into a failure).",

        # ---- 无窗口模式下的启动横幅 ----
        "run.banner.port": "Port     : {port}",
        "run.banner.source": "Source   : {source} (backend {backend})",
        "run.banner.pairCode": "Pair code: {code}   (valid for {ttl} minutes; type it in the page to pair)",
        "run.banner.pairHint": "pick \"Desktop helper\" as the audio source in the page, then type this code there",
        "run.banner.debugToken": "[debug] a fixed --token was given, pair-code verification is skipped",
        "run.banner.stop": "Quit     : Ctrl+C",
        "run.err.hostNotLoopback": "ERROR: --host {host} is not a loopback address. This service hands local audio to any page\n       that connects; exposing it on the LAN publishes your audio stream. If you really need\n       this, pass --allow-lan explicitly (at your own risk).",
        "run.err.portBusy": "cannot listen on {host}:{port} (tried {count} ports): {error}",
        "run.stopping": "stopping…",
        "run.interrupted": "interrupted, shutting down…",

        # ---- 图形界面（用户唯一入口）----
        "gui.title": "EasySub Helper",
        "gui.subtitle": "Captures this machine's audio through the OS APIs and hands it to the\nrecognition engine in your browser. No screen sharing, no virtual audio driver.",
        "gui.subtitleMac": "Captures this machine's audio through the OS APIs and hands it to the\nrecognition engine in your browser. No screen sharing. macOS 14.2+ needs no driver; older systems need BlackHole (or install pysysaudio yourself).",
        "gui.statusStarting": "starting…",
        "gui.statusFailed": "failed to start",
        "gui.statusPaused": "paused (service up, connection kept) · port {port}",
        "gui.captureOn": "capturing · {source}",
        "gui.captureFailed": "capture failed",
        "gui.waitingFrames": "started, waiting for audio frames…",
        "gui.backend": "backend {backend} · {rate} Hz",
        "gui.start": "Start",
        "gui.pause": "Pause",
        "gui.pairTitle": "Pair code",
        "gui.pairHint": "On the subtitle page pick the last item of \"audio source\" (desktop helper) and type this code.",
        "gui.pairLocked": "Locked for {sec}s: click \"New code\" to unlock immediately (no need to wait)",
        "gui.statusPort": "port {port}",
        "gui.captureFailedHint": "press Start to retry",
        "gui.deviceWhenUnknown": "unknown time",
        "log.trayReady": "tray ready: closing the window now only hides it, service and capture keep running",
        "log.trayFailed": "tray failed to start ({error}): minimized-to-tray was turned off, closing still stops the service",
        "cli.dlg.multiInstance": "Another helper is already running (port {port}).\n\nTwo instances keep separate pair codes and overwrite each other's paired devices (the symptom is \"it asks me to pair again after a while\").\n\nContinue with a second instance anyway?",
        "gui.devicesCount": "Paired devices: {count}",
        "gui.devicesManage": "Manage…",
        "gui.devicesTitle": "Paired devices",
        "gui.devicesEmpty": "No page has paired yet.",
        "gui.devicesHint": "After unbinding, that page has to pair again (enter a fresh code).",
        "gui.deviceWhen": "{when}",
        "gui.deviceUnbind": "Unbind",
        "gui.deviceSelectFirst": "Select the device(s) you want to unbind first",
        "gui.deviceUnbindAllConfirm": "Click again to unbind all",
        "gui.deviceUnbindAll": "Unbind all",
        "gui.devicesUnbound": "Unbound {count} device(s)",
        "gui.close": "Close",
        "gui.minimizeToTray": "Minimize to tray on close (keep the service running)",
        "gui.trayUnavailable": "Tray unavailable (pystray or Pillow missing); closing the window still stops the service. Install with: {install}",
        "gui.trayStartFailed": "could not start the tray ({error}): minimize-to-tray is off, closing the window still stops the service",
        "gui.trayXorgHint": "Note: your desktop ({desktop}) uses the StatusNotifier tray protocol, but only the legacy XEmbed backend is available, so the icon may not show and clicks may do nothing. Install a pure-Python D-Bus library to use the native tray: {install} (then restart the helper)",
        "gui.trayShow": "Show window",
        "gui.trayStart": "Start capture",
        "gui.trayPause": "Pause capture",
        "gui.trayQuit": "Quit",
        "gui.pairTtl": "valid for {sec}s",
        "gui.pairCopy": "Copy",
        "gui.pairCopied": "Copied ✓",
        "gui.pairNew": "New code",
        "gui.pairNone": "pairing disabled (debug mode)",
        "gui.pairDebug": "using a fixed --token, no pair code needed",
        "gui.sourceLabel": "Source",
        "gui.deviceLabel": "Device",
        "gui.openedDevice": "device {device}",
        "device.default": "System default",
        # display labels for internal sentinel strings (the tool argument stays @DEFAULT_MONITOR@)
        "audio.deviceDefaultSystem": "default output (system audio)",
        "audio.deviceDefaultInput": "system default input",
        "gui.levelLabel": "Level",
        "gui.levelSilent": "silent",
        "gui.levelSignal": "signal",
        "gui.pausedHint": "paused — press Start to capture (no pairing needed)",
        "gui.quit": "Quit",
        "gui.logLabel": "Log",
        "gui.keepOpen": "Closing stops the service (tick \"minimize to tray\" to keep it running); Pause only stops capture, the connection stays.",
        "gui.licenseSource": "AGPL-3.0-or-later · source code",
        "gui.clients": "pages connected: {count}",
        "gui.errNoTk": "no usable GUI (tkinter missing or no display); falling back to the command line. On Linux install tkinter: sudo apt install python3-tk (Debian/Ubuntu), sudo dnf install python3-tkinter (Fedora).",

        # ---- 日志（会显示在窗口日志区，也要 i18n）----
        "log.listening": "listening on {url}",
        "log.pairFailed": "pairing failed: {reason} (origin={origin})",
        "log.pairOk": "paired: {label} ({devices} devices)",
        "log.stopFailed": "error while stopping capture: {error}",
        "log.captureFailed": "capture failed: {error}",
        "log.wsBadToken": "WS rejected: invalid token (origin={origin})",
        "log.wsOpen": "WS connected (origin={origin}, clients={clients})",
        "log.wsClosed": "WS closed (clients={clients})",
        "log.wsError": "WS error: {error}",
        "log.serviceStartFailed": "service failed to start: {error}",
        "log.switchSource": "switching source: {source}",
        "log.switchDevice": "switching device: {device}",
        "log.openSourceFailed": "could not open the source URL in a browser: {error}",
        "log.newCode": "new pair code: {code}",
        "log.minimizedToTray": "window hidden to tray: the service and capture keep running; quit from the tray menu",
        "log.codeRotated": "pair code rotated: {code}",
        "log.guiActionFailed": "GUI action failed: {error}",
        "log.langSwitched": "UI language: {lang}",
        "log.guiRefreshFailed": "GUI refresh failed: {error}",

        # ---- 服务端 ----
        "server.err.forbidden": "forbidden (bad device token or disallowed origin)",
        "server.err.originDenied": "this page origin is not allowed to pair (the helper is in --restrict-origin mode): add it to the allowlist, or drop that flag (the default is fully open)",
        "server.err.originMissing": "this request carries no Origin (usually curl/scripts, i.e. not a browser). Requests without Origin are rejected by default; browsers always send one.",
        "log.stopTimeout": "capture thread did not exit within the timeout (device may still be in use)",
        "log.pysysaudioPermCheck": "pysysaudio permission check failed (ignored): {error}",
        "log.pysysaudioFallback": "pysysaudio unavailable, falling back to soundcard: {error}",
        "audio.err.pysysaudioNoRecorder": "this pysysaudio version has no SystemAudioRecorder (needs the macOS 14.2+ build)",
        "audio.err.macNoSystemAudioDetail": "cannot get macOS system audio: both paths failed. (1) pysysaudio (driver-free, needs macOS 14.2+ and a manual install: pip install pysysaudio): {detail}; (2) soundcard fallback (needs BlackHole: https://existential.audio/blackhole/ - after installing it, switch Source to Microphone and pick it in the Device dropdown): {detail2}",
        "log.pairOriginDenied": "pairing refused: origin not allowed (Origin={origin})",
        "log.wsTooManyConnections": "refused a new WebSocket connection: at the {limit}-connection limit (a page may be leaking connections)",
        "server.err.tooManyConnections": "too many WebSocket connections — close extra EasySub tabs and retry",
        "log.noOrigin": "(no Origin)",
        "server.err.pairBody": "pairing request body is not valid JSON",
        "server.err.badMessage": "invalid client message: {detail}",
        "server.err.binaryFrame": "clients must not send binary frames",
        "server.err.captureFailed": "failed to start capture: {detail}",
        "server.err.captureAborted": "capture aborted: {detail}",
        "server.err.paused": "the helper window is paused — press Start there",
        "server.err.notPaired": "not paired yet: type the pair code shown on the desktop side",
        "server.err.badCode": "wrong pair code",
        "server.err.codeExpired": "the pair code expired; generate a new one on the desktop side",
        "server.err.locked": "too many failed attempts, try again in {seconds} seconds",
        "server.err.noCode": "the desktop side has no valid pair code right now",

        # ---- 采集层 ----
        "audio.err.openDevice": "cannot open capture device {device} (tried rates {rates}): {error}",
        "audio.err.deviceNotFound": "no capture device named {device} (pick one in the helper window's Device list)",
        "audio.err.deviceAmbiguous": "{device} matches {count} devices — pick one in the helper window's Device list",
        "audio.err.noSpeaker": "no default speaker, so there is no loopback source. Linux/Windows: pick a default output device in the system settings. macOS: either (1) install pysysaudio (pip install pysysaudio, needs macOS 14.2+), or (2) install BlackHole (https://existential.audio/blackhole/), then switch the Source dropdown to Microphone and pick BlackHole in the Device dropdown (that dropdown does not exist in system-audio mode).",
        "audio.err.loopbackFailed": "loopback of default speaker {device} failed: {error}",
        "audio.err.noMic": "no default microphone",
        "audio.err.streamEnded": "capture stream ended (source closed / device unplugged / taken over)",
        "audio.err.toolMissing": "parec / pw-record not found (Linux needs the PulseAudio or PipeWire command line tools - the service alone is not enough). Install with: sudo apt install pulseaudio-utils (Debian/Ubuntu) or sudo apt install pipewire-audio (PipeWire-based), then restart the helper.",
        "audio.err.toolStart": "failed to start {tool}: {error}",
        "audio.err.toolExited": "{tool} exited immediately (code={code}): {detail}",
        "audio.err.toolEnded": "{tool} stopped producing audio (code={code}): {detail}",
        "audio.err.noStderr": "no stderr output",
        "audio.err.unknownBackend": "unknown capture backend: {backend} (available: auto/soundcard/parec/pw-record)",
        "audio.err.backendNotLinux": "the {backend} backend only works on Linux",
        "audio.err.backendNotMac": "the {backend} backend only works on macOS",
        "audio.err.notOpened": "capture is not open",
        "audio.err.pysysaudioOpen": "pysysaudio failed to open: {error}",
        "audio.err.pysysaudioDenied": "system audio permission denied: allow it in System Settings → Privacy & Security → Screen & System Audio Recording",

        # ---- 重采样 ----
        "resample.err.unavailable": "cannot resample {from_rate} -> {to_rate} ({detail}). This means a missing dependency or a broken build of the helper itself — please reinstall the helper (the built-in resampler is enough in normal installs).",
        "resample.err.badRate": "sample rates must be positive",
        "resample.err.unknownKind": "unknown resampler implementation: {kind}",
        "resample.err.designFailed": "prototype filter design failed (bad rate ratio: {from_rate}/{to_rate})",
        "resample.err.noStream": "soxr has no ResampleStream (requires soxr>=0.3.5)",

        # ---- 协议参数校验 ----
        "protocol.bad.notUtf8": "binary frame is not valid UTF-8 JSON",
        "protocol.bad.notJson": "not valid JSON",
        "protocol.bad.notObject": "the top level must be an object",
        "protocol.bad.unknownType": "unknown message type: {type}",
        "protocol.bad.badSource": "source must be system or mic, got {source}",
        "protocol.bad.badDevice": "device must be a string",
        "protocol.bad.badPing": "ping.t must be a number",
    },
}

def available_languages():
    return list(LANGUAGES)


def normalize_lang(tag):
    """把 zh-CN / zh_CN.UTF-8 / en-US 这类标签归一到目录里的键。未知返回 None。"""
    if not tag:
        return None
    text = str(tag).strip().replace("-", "_")
    lower = text.lower()
    if lower.startswith("zh"):
        # 繁体暂不单独维护，统一回落 zh_CN（与主项目一致：只维护 zh_CN / en）
        return "zh_CN"
    if lower.startswith("en"):
        return "en"
    return None


def detect_language(explicit=None):
    for candidate in (
        explicit,
        os.environ.get("EASYSUB_LANG"),
        os.environ.get("LC_ALL"),
        os.environ.get("LC_MESSAGES"),
        os.environ.get("LANG"),
    ):
        lang = normalize_lang(candidate)
        if lang:
            return lang
    return DEFAULT_LANG


def set_language(tag):
    global _current
    lang = normalize_lang(tag) or _current
    with _lock:
        _current = lang
    return lang


def get_language():
    return _current


def has_key(key, lang=None):
    return key in _STRINGS.get(lang or _current, {})


def all_keys():
    keys = set()
    for catalog in _STRINGS.values():
        keys.update(catalog.keys())
    return keys


def catalog(lang=None):
    return dict(_STRINGS.get(lang or _current, {}))


def source_label(source):
    table = _SOURCE_LABEL.get(_current, {})
    return table.get(source, source)


def t(key, **kwargs):
    """取词条并格式化。

    找不到 key 时：回退到 zh_CN（默认语言），再不行返回 key 本身并记账
    （tests/test_i18n.py 会在测试期把所有缺 key 的情况抓出来，运行时绝不抛异常）。
    """
    with _lock:
        lang = _current
    text = _STRINGS.get(lang, {}).get(key)
    if text is None:
        text = _STRINGS[DEFAULT_LANG].get(key)
    if text is None:
        if key not in _missing_reported:
            _missing_reported.add(key)
        return key
    if kwargs:
        try:
            return text.format(**kwargs)
        except (KeyError, IndexError):
            return text
    return text
