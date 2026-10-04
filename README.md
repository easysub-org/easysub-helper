# easysub-helper（易字幕本机助手）

一个**纯音频本机插件**：用操作系统 API 采集本机音频，和浏览器页面配对之后，把 16 kHz 单声道
PCM 通过本机 WebSocket 推过去。就这样两件事 —— **采集** + **配对**。

它不托管页面、不碰识别模型、不做推理。页面（浏览器扩展形态 / 已部署的 Web 版）自己带着 wasm
识别引擎跑，助手只负责把它拿不到的那段音频接上。

## 为什么需要它

系统音频在浏览器 API 里基本走不通：

| 平台 | Chrome/Edge | Firefox |
|---|---|---|
| Windows | `getDisplayMedia` 可整屏音频（每次都要选一次） | `getDisplayMedia` **audio 恒为空** |
| macOS | **只有标签页音频** | 同上，且选择器里连标签页都没有 |
| Linux | **只有标签页音频** | 同上（但可手选 PulseAudio `Monitor of …` 设备） |

`chrome.tabCapture` 是 Chrome 独有 API，Firefox 永远不会有；Firefox 的
[getDisplayMedia 音频支持自 2019 年立项至今未实现](https://bugzilla.mozilla.org/show_bug.cgi?id=1541425)。
所以唯一的跨浏览器解是：**由本机进程去采**，页面只负责显示与识别。

---

## 快速开始（用户路径：一个窗口，没有命令行）

```bash
# 1) 取源码（助手是独立仓库：https://github.com/easysub-org/easysub-helper）
git clone https://github.com/easysub-org/easysub-helper && cd easysub-helper

# 2) 安装（3.6+ 均可；Windows/macOS 建议加 capture extra 拿 soundcard）
python -m pip install -e ".[capture]"

# 3) 双击启动器，或者（等价地）敲这一行
easysub-helper
```

窗口长这样（中文界面；右上角一个按钮可以切 English ⇄ 中文）：

```
[logo] 易字幕本机助手                              [English]
      用系统 API 直接采集本机音频，交给浏览器里的识别引擎。
● 已暂停 · 端口 8790                              已连接页面：0
音源 [麦克风 ▾]   设备 [内置音频 Analog Stereo ▾]       有信号 · 49%
电平 ▁▃▅▇▅▃▂▁▃▅▇█▇▅▃▂▁   ← 12 秒滚动波形 + 绿/黄/红分段电平表 + 峰值保持线
[ 启动 ]  [ 退出 ]
配对码 · 599 秒内有效
   ┌────────┐
   │ KPA-4DA│   [复制] [换一个]
   └────────┘
在字幕页面选「从桌面助手获取音频」，填下面这串即可。
```

使用顺序：

1. 选好 **音源**（系统音频 / 麦克风）。选**麦克风**时会多出一个**设备**下拉，可以换具体用哪个
   麦克风（默认「系统默认」）；**系统音频不给设备选择** —— 各平台实现不同（WASAPI loopback /
   PulseAudio monitor / CoreAudio tap），用户能选的其实只有"默认输出的环回"这一个概念。
2. **点「启动」**：开始采本机音频。**默认是暂停的** —— 不点启动，助手不会碰你的麦克风/环回，
   也不会偷偷采集。窗口里的电平图就是"真的在采"的证据（**不需要配对**，没配对也能看它动）。
   换音源或换设备是**立刻生效**的：正在采集时助手会关掉旧设备、按新选择重开。
3. 到页面里选「从桌面助手获取音频」，点「开始」时会弹出配对框，把窗口里的 6 位配对码填进去
   （点「复制」）。**页面里这个音源是常驻显示的**：没检测到助手 / 未配对 / 助手暂停，提示语都会
   直说原因，不会再出现"选项凭空消失、不知道怎么配对"。
4. 页面点「开始识别」。
5. 想临时静音：点「**暂停**」。设备会被立刻释放，但**连接不断** —— 助手继续按 20 ms 节奏给页面
   推**全零帧**，页面侧时间轴不断、WS 不超时，恢复时也不用重连。
6. 退出：点「退出」或关掉窗口（关窗 = 停服务）。

要点：

- **助手不需要 Web 产物、不需要模型、不需要联网**：它只监听 `127.0.0.1` 上的三个端点。
- **设备列表只在本机窗口里用**：页面不需要选设备（浏览器授权框自带设备选择，系统音频要的是
  默认输出的 loopback），所以 `/api/devices` 这类 HTTP 面**没有**，设备枚举只服务这个下拉。
- **设备名会被解析成后端真正能用的 id**：Linux 上默认后端是 `parec`/`pw-record`，而它对
  **未知设备名是静默回落默认源**的（实测 `parec --device=随便编` 照样一直采）。所以窗口给出的是
  PulseAudio 源名（描述只当标签），`--device` 传描述或唯一子串也能解析；解析不出来就**大声报错
  并退回原设备**，绝不静默。窗口状态行里显示的是"后端**实际打开**的设备"，切换生没生效一眼可见。
- 页面那一侧**必须自己处于 `crossOriginIsolated`**（扩展页面自带；Web 版走它原有的 COOP/COEP
  或 `coi-serviceworker.js`）—— 这是主项目的事，助手不代劳。
- 没有图形界面（Linux 缺 `python3-tk`，或 SSH 无 `DISPLAY`）时自动回落命令行：

```bash
easysub-helper --no-gui      # 打印端口与配对码，等 Ctrl+C；此模式下默认就开始采集
```

Linux 上装 tkinter：`sudo apt install python3-tk`（Debian/Ubuntu）、
`sudo dnf install python3-tkinter`（Fedora）。Windows/macOS 的官方 Python 自带 tkinter。

### 命令行参数（排障/服务器用，用户不需要看）

```bash
--no-gui                不弹窗口；此模式下默认就开始采集（没有按钮可按）
--port / --host         监听端口（默认 8790，被占用自动向后顺延 20 个）/ 监听地址（默认只绑回环）
--allow-lan             允许绑非回环地址（高危：等于把音频流公开到局域网）
--source system|mic     默认音源（系统音频 / 麦克风）
--backend …             auto / soundcard / parec / pw-record / mac-system
--device <子串>         指定设备（如 "Monitor of ..."）
--allow-origin <Origin> 额外放行的 Origin（Web 版部署在别的域名时用），可重复
--token <固定令牌>      跳过配对（仅调试/测试）
--lang zh_CN|en         界面语言；也可以点窗口里的语言按钮（选择会记住）
--log-level DEBUG|INFO|WARNING|ERROR
```

---

## 配对（为什么不是"把 token 放 URL"）

本服务监听 `127.0.0.1`，**本机上任何网页都能访问它**。如果没有鉴权，任意网页都能连上 WS 往识别
管道里灌音频（伪造字幕），或反向把用户的实时字幕读走。所以：

1. 助手启动时生成**一次性配对码**（6 位、默认 10 分钟、去掉了 0/O/1/I/L 这类易看错的字符），
   **显示在助手窗口里**（大字 + 复制按钮）—— 用户看得见助手窗口，就是"用户在场"的证明；
2. 页面先 `GET /api/pair/info` 探测（只回最小信息），成功后 `POST /api/pair {code}`；
3. 校验通过 → 下发**长期设备令牌**，页面保存后用它连 WS，不再需要配对码。

防线：

- 配对码 **60 秒内失败 5 次锁定 300 秒**（锁定期间连正确码也拒，避免借正确码绕过限速语义）；
- 令牌本地只存 **sha256 哈希**（文件泄漏 ≠ 令牌泄漏）；
- **`/ws` 只认令牌**（用 `hmac.compare_digest` 常数时间比较）。刻意**不再校验 Origin**：
  令牌是配对的产物，比 Origin 更能说明"这个页面被用户亲手放行过"；而且扩展的 offscreen 文档
  不一定带 Origin，硬拦会误伤正常用户。
- `POST /api/pair` 仍校验 **Origin 白名单**（那一步还没有令牌，白名单是唯一防线）：
  放行**任意端口**的本机页面（`http://127.0.0.1:*` / `http://localhost:*` / `http://[::1]:*`）、
  扩展协议（`chrome-extension://` / `moz-extension://` / `safari-web-extension://`）。
  端口刻意不校验 —— Web 版可能跑在任意开发端口（vite 5173、serve-web 3000…），
  而真正的凭据是配对码；"必须与助手同端口"会把正常用户挡在门外（实测现象：dist-web 起来后
  页面里根本看不到这个音源）。非回环来源（部署在别的域名）仍需 `--allow-origin https://…`。
- **默认只绑回环地址**，`--host` 传非回环必须显式 `--allow-lan`。

---

- **锁定是全局的**：60 秒内失败 5 次 → 锁 300 秒（期间连正确码也拒）。这是有意的（本机只有一个用户），但也意味着本机任意页面、或开了 `--allow-lan` 之后的局域网机器都能
  "用错码把配对锁住"进行骚扰 —— 用户在助手窗口点「换一个」即可解锁。所以**别在不可信网络里开 `--allow-lan`**。
- `/api/pair` 的 **Origin 白名单是硬闸门**（有 Origin 但不允许 → 403），不只是 CORS 响应头；
  没有 Origin 的请求（curl/脚本等非浏览器客户端）默认也拒绝，需要时用 `--allow-origin` 或
  `allow_no_origin` 显式放行。

## 协议速查

```
GET  /api/pair/info            探测（无鉴权，最小信息）：助手在不在？我配过没有？在暂停吗？
OPTIONS|POST /api/pair         配对码换长期设备令牌 → {ok,token,label}
GET  /ws?token=…               WebSocket 音频通道
其它一切路径                     404（助手没有静态文件服务）
```

- 服务端 → 客户端：**文本帧 = JSON**（`hello` / `state` / `level` / `error` / `pong`），
  **二进制帧 = 裸 PCM**（16 kHz 单声道 f32le，20 ms = 320 样本 = 1280 字节，定长无包头）。
- 客户端 → 服务端：`{"type":"start","source":"system|mic","device":"<可选>"}` / `{"type":"stop"}` /
  `{"type":"ping","t":123}`。
- `hello` 与 `state` 都带 **`paused`**：助手窗口的总开关是否处于暂停（**默认暂停**）。
- **i18n 契约**：`error` 同时给稳定的 `code` 与 `message`；前端应**按 code 渲染自己的文案**，
  `message` 只是桌面端语言下的兜底。码表：`bad_message` / `capture_failed` / `not_paired` /
  **`paused`** / `bad_code` / `code_expired` / `locked` / `no_code` …。

---

## 支持的平台与采集后端

| 平台 | 默认后端 | 系统音频怎么来 | 权限 |
|---|---|---|---|
| **Windows**（含 **Win7**） | `soundcard` | WASAPI loopback（整机混音）。**不需要虚拟声卡**，[loopback 不依赖硬件是否带 loopback 设备](https://learn.microsoft.com/en-us/windows/win32/coreaudio/loopback-recording) | 无弹窗 |
| **Linux** | `parec` / `pw-record`（子进程，零 Python 依赖），回落到 `soundcard` | PulseAudio/PipeWire 的 monitor 源（`@DEFAULT_MONITOR@`） | 无 |
| **macOS 14.2+** | `mac-system`：先试 **pysysaudio**（**需自装该库，官方产物未内置**） | ScreenCaptureKit / Core Audio process tap，免驱动、不用 BlackHole | 「屏幕与系统音频录制」授权一次 |
| **macOS < 14.2** | `mac-system` 回落 `soundcard` | 需要 BlackHole 这类 loopback 输入设备 | 同左 |

### Python 库选型（2026-10 实测调研）

| 库 | 覆盖 | 系统音频 | Python 要求 | 我们的取舍 |
|---|---|---|---|---|
| **soundcard 0.4.6** | Win/Linux/macOS | Win ✅ WASAPI loopback；Linux ✅ PulseAudio monitor；macOS ❌（只有真实输入设备） | **>=3.5**，纯 CFFI **无需编译器** | **主力**：唯一同时满足 Win7 + py3.6 的 |
| **pysysaudio 0.1.3** | macOS 14.2+ / Win10+ | macOS ✅ ScreenCaptureKit/tap（免驱动）；Win ✅ WASAPI | >=3.9，有 wheel | **macOS 免驱动后端**（可选 extra，装了就用） |
| catap 0.6.0 | macOS 14.2+ | ✅ Core Audio tap，**可按进程**抓 | >=3.11 | 更底层，暂不用（可按 App 抓是后续能力） |
| PyAudioWPatch 0.2.12.8 | 仅 Windows | ✅ WASAPI loopback | wheel 只有 **cp38–cp314**（无 cp36/37），Win10+ | 不用：Win7/py3.6 装不上 |
| sounddevice 0.5.6 | 跨平台 | ❌ 高层 API 无 loopback（`PaWasapi_IsLoopback` 只在 cdef 里，issue #281/#510 还开着） | >=3.7 | 不用：不适合系统音频 |
| `parec` / `pw-record` | 仅 Linux | ✅ monitor 源 | 任意（命令行） | **Linux 首选**：零依赖、服务端重采样 |

---

## Windows 7 与 Python 版本

**结论先说**：Win7 这条路径**原理上可行，但我们没有 Win7 真机/虚拟机，尚未实测**。能做的都做了：
把版本组合钉死、把唯一"起不来"级的运行时依赖打进产物、并让 CI 机械校验。剩下那一次真机验证
只能靠你或用户（见文末"如何补上验证"）。

### 版本组合（为什么必须是这几个"老"版本）

| 组件 | 选定 | 为什么不能更新 |
|---|---|---|
| CPython | **3.8.10** | **最后一个支持 Win7 的版本**。3.9+ 的 `pythonXY.dll` 依赖 `api-ms-win-core-path-l1-1-0.dll`（Win8+ 的 API set），在 Win7 上直接起不来；python.org 下载页对 3.9+ 明写 "cannot be used on Windows 7 or earlier"，PyInstaller 维护者的答复也是"想支持 Win7 只能用 3.8 或更老" |
| PyInstaller | **5.13.2**（CI 固定） | 6.x 官方支持矩阵只到 **Windows 8+**（其 README 原话："should work on Windows 7 or newer, but we only officially support Windows 8+"）。bootloader 编译时会显式请求 Win7 特性级别（`_WIN32_WINNT=0x0601`） |
| numpy | `>=1.21,<2`（py3.8 装到 1.24.x） | numpy 官方发布文档仍写 **"Windows 7, 8 and 10 are supported"**；但 1.25+ 要 py3.9 |
| aiohttp | `>=3.8.6,<4`（py3.8 装到 3.10.x） | 3.11+ 要 py3.9 |
| soundcard | `>=0.4.2` | 纯 CFFI + WASAPI；WASAPI loopback 自 Vista 起就有，Win7 可用 |
| soxr | **不装** | 新版要 py3.9；重采样自动退回内置多相 FIR，功能不受影响 |

### 唯一"起不来"级的坑：UCRT

Python 3.8 的 exe 依赖 `api-ms-win-crt-*.dll` / `ucrtbase.dll`。这些在 Win10 是系统组件，
**没打补丁的 Win7 SP1 上没有**，用户会看到 `api-ms-win-crt-runtime-l1-1-0.dll 缺失`
（PyInstaller issue #1588、微软 KB2999226）。

所以 `tools/build_win7_exe.py` 会把它们**一起打进 onefile**（优先取 Windows SDK 的
`Redist\ucrt\DLLs\<arch>`，退回 Python 安装目录/System32），**并在打包后校验 DLL 真的在归档里** ——
缺了就构建失败。这条校验的意义：我们没有 Win7，绝不允许 CI"绿着产出一个在 Win7 上起不来的包"。

用户侧前提（记得写进发行说明）：

- 需要 **Win7 SP1**（Python 3.8 与 UCRT 都要求 SP1）；
- 系统没有 UCRT 也没关系，**产物已自带**（无需 KB2999226）；
- 如果以后给 exe **做代码签名**，Win7 还需 **KB4474419**（SHA-2 支持）才能验证签名。

### 如何补上验证（我们还没做的那一步）

1. **最可靠**：找一台 Win7 SP1 虚拟机（VirtualBox/VMware + 你自己的授权介质），跑
   `dist/easysub-helper.exe`，确认①窗口能弹出、②点「启动」后电平图会动、③配对码能显示。
2. **弱验证（无需 Win7）**：在 Linux 上用 `wine` 把 `winver` 设成 Windows 7 跑一次 ——
   能抓到"调用了 Win8+ 才有的 API"这类启动崩溃，但 WASAPI 采集在 Wine 下不可靠，只能当参考。
3. 在真机验证之前，**README/商店页不要写"支持 Win7"**，写"已构建 Win7 档产物，未实测"更诚实。

### 其它与老环境有关的约定

- 源码**只用 3.6 能解析的语法**（不用 `from __future__ import annotations`、不用 dataclass、
  不用海象运算符），这样只有老解释器的构建机/CI 也能跑。`tests/test_py36_compat.py` 会把关：
  `ast.parse(feature_version=(3,6))` + 禁用 API 扫描。
- **不要引入 onnxruntime**：它自 **1.15.0 起不再支持 Win7**（识别推理留在页面 wasm 里，这正是
  助手连模型都不碰的原因之一）。
- Win7 机器普遍内存小：页面侧建议用 lite 包（模型与推理都在页面那边，助手不管）。

## 品牌与图标（一律用插件 logo）

桌面端**不另做 logo**，全部与浏览器扩展同源：主仓库 `logo.jpg`（256×256）
+ `public/icons/icon{16,48,128}.png`。分发时由 `tools/make_icons.py` 生成并随包提交：

| 文件（`easysub_helper/assets/`） | 用在哪 |
|---|---|
| `icon128.png` | 助手窗口头部与任务栏图标（tkinter `PhotoImage`，只用标准库解码 PNG） |
| `easysub.ico` | Windows 可执行文件图标（PyInstaller `--icon`；16/24/32/48/64/128 为传统 BMP 条目、256 为 PNG，Win7 起都能正确渲染） |
| `easysub.icns` | macOS 应用图标（PyInstaller `--icon`） |
| `logo.jpg` / `icon16.png` / `icon48.png` | 当前没有被助手代码引用，仍随包产出：与插件同源，留给以后的托盘/关于框用 |

插件 logo 更新后重跑一次（产物要一并提交，构建机因此不需要 Pillow/ImageMagick）：

```bash
python tools/make_icons.py                 # 默认从上级目录（主仓库）取源
python tools/make_icons.py --source /path/to/easysub
```

---

## 排障

| 现象 | 先做什么 |
|---|---|
| 双击没反应 / 没弹窗口 | Linux 缺 tkinter：`sudo apt install python3-tk`；或在终端跑 `easysub-helper` 看报错 |
| 电平图空着、写着「已暂停」 | 正常：点窗口里的「启动」才开始采（**不需要配对**） |
| 电平图空着、写着「已启动，等待音频帧…」 | 设备开着但没出数据：看日志区；Linux 上确认有 `parec`/`pw-record`（`pulseaudio-utils`/`pipewire-audio`） |
| 页面里选了「桌面助手」但提示"没有检测到助手" | 助手是否在跑？`curl http://127.0.0.1:8790/api/pair/info`；Web 版请刷新页面（面板打开时探测一次，点「开始」会再探测一次） |
| 页面提示"助手处于暂停" | 到助手窗口点「启动」（这是提示，不是故障） |
| 配对失败 | 看窗口里的码是否还在倒计时内；连续失败会被锁 300 秒 |
| 页面能配对但没字幕 | 先看窗口电平图有没有信号：有信号=采集没问题，问题在页面侧（识别引擎/COOP-COEP） |
| Linux 上没有系统音频 | 确认 PulseAudio/PipeWire 在跑；`--device "Monitor of ..."` 可手选设备 |
| macOS 上没有系统音频 | 14.2+ 装 `pysysaudio`（免驱动）；否则装 BlackHole 再 `--device` 指定 |
| 端口被占 | 默认自动向后顺延 20 个（窗口与页面都会用实际端口）；`--port` 可指定 |
| Web 版（非扩展）连不上 | 本机任意端口的页面默认放行；部署在别的域名时用 `--allow-origin https://你的域名` |
| 麦克风选错了 | 窗口里换「设备」下拉（默认「系统默认」跟着系统走）；换音源会重置为系统默认。**状态行副标题显示后端实际打开的设备**，可以据此确认切换生效 |
| 换了麦克风但声音没变 | 现在不会发生了：设备名解析不出就报错并退回原设备（`parec` 未知设备名会静默回落默认源，这是旧版本的坑） |

## 开发

```bash
# 不想安装也能直接跑（相对 import 自带兜底，见 cli.py 顶部）
.venv/bin/python easysub_helper/gui.py             # 只要窗口
.venv/bin/python easysub_helper/cli.py --no-gui    # 命令行模式

python -m venv .venv && .venv/bin/pip install -e ".[capture,dev]"
.venv/bin/python -m unittest discover -s tests -t .        # 不需音频设备/显示器
EASYSUB_HELPER_GUI_TESTS=1 .venv/bin/python -m unittest tests.test_gui -v    # 真窗口/画布几何
EASYSUB_HELPER_AUDIO_TESTS=1 .venv/bin/python -m unittest tests.test_server -v  # 真声卡
```

`tests/test_scope.py` 是"范围闸门"：助手只做采集 + 配对，谁把静态托管、模型目录、设备列举、
HTML 配对页写回来，它就会红。

## 贡献

欢迎 issue 与 PR。**提代码 PR 前请先签 [CLA](CLA.md)**：在你的 PR 里发一条评论，内容照抄

> I have read the CLA Document and I hereby sign the CLA.

即可 —— 你**保留**自己贡献的版权，项目所有者获得"可按任意许可证（含商业许可）再许可"的权利
（`cla` 工作流会自动打标签）。其余约定见 [CONTRIBUTING.md](CONTRIBUTING.md)：本地测试命令、
领域边界（助手只做采集 + 配对，`tests/test_scope.py` 是机械闸门）、Python 3.6 语法与 i18n 要求。

安全问题请走 GitHub 的 **Security → Report a vulnerability**，不要开公开 issue。

## 路线图

- [x] 采集三平台 + 定长 16k 出块（带状态重采样）
- [x] 配对码 → 设备令牌；本机服务安全边界
- [x] 图形界面（tkinter/ttk）：启动/暂停总开关、大字配对码、12 秒滚动电平图、语言切换
- [x] 暂停不断流（推全零帧）、没配对也能采（窗口电平图自测）
- [x] 窗口里切换音源与**具体设备**（麦克风/输出设备列表只在窗口里用，不进 HTTP）
- [x] 页面里「桌面助手」音源**常驻显示**，配对成功才能启动（四态提示语）
- [x] 主项目接线（探测 → 配对 → `从桌面助手获取音频` 音源，扩展与 Web 两条宿主）
- [ ] macOS 按 App 抓（catap）、Windows 按进程抓（`AUDIOClient` process loopback）
- [x] PyInstaller 打包（Win7 档 = py3.8 + UCRT 自校验；三平台 CI 产物 + 手动填版本号发 Release）

## 许可证

Copyright (C) 2026 hcz1017

助手以 **GNU Affero 通用公共许可证第 3 版或更高版本（AGPL-3.0-or-later）** 发布，全文见
[LICENSE](LICENSE)；窗口右下角给了源码地址（AGPL 第 13 条：以网络服务形式提供时须给使用者
"对应源码"的入口）。闭源集成需另行洽谈商业授权；"易字幕 / EasySub"名称与图标不在授权范围内。

**依赖的许可证**（助手只做采集 + 配对，依赖很少）：

| 依赖 | 许可证 | 备注 |
|---|---|---|
| [soundcard](https://github.com/bastibe/SoundCard) | BSD-3-Clause | Windows/macOS/回退用 |
| [aiohttp](https://github.com/aio-libs/aiohttp) | Apache-2.0 AND MIT | HTTP/WS 服务 |
| [numpy](https://numpy.org/) | BSD-3-Clause | 电平/RMS 计算 |
| [soxr](https://github.com/dofuuz/python-soxr) | **LGPL-2.1-or-later** | **可选** extra（`[quality]`）：官方二进制**不打包**它 |

LGPL 要求使用者能替换该库，PyInstaller 单文件做不到，因此**官方发行包只装 `[capture]`**，
重采样退回内置 polyphase FIR；需要极致音质请自行 `pip install "easysub-helper[quality]"`。
