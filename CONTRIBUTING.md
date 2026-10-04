# 贡献指南（易字幕本机助手）

感谢愿意花时间。提 PR 之前请先看这页 —— **只有一件事是硬性要求：签 CLA**。

## 一、签 CLA（必须）

本项目采用 [CLA.md](CLA.md)：**你保留自己贡献的版权，但授予项目所有者按任意许可证
（含商业许可）再许可的权利**。这样项目以后若改成「AGPL 开源 + 商业授权」的双许可模式，
不必回头找每一位历史贡献者补签。

**怎么签**：在**你的 PR 里发一条评论**，内容照抄这一句（与 [CLA Assistant](https://cla-assistant.io/)
用的是同一句）：

> I have read the CLA Document and I hereby sign the CLA.

建议下一行写上你的**法定姓名**，便于留档（可选，但推荐）：

> Name: 张三

发完之后 `cla` 工作流会：把你的用户名、姓名、日期与该评论链接写进
[`signatures/cla.json`](signatures/cla.json)（**签名落库**，所以事后删掉或编辑那条评论不影响记录 ——
这也是主流 CLA 机器人存签名的原因），并打上 `cla-signed` 标签。**没签之前这个检查会失败**
（`cla-pending`），如果仓库开了分支保护并把 `CLA` 设为必需状态检查，PR 就无法合并。

不想签也完全没问题 —— 那这个 PR 里的**代码**没法合并，但欢迎提 issue 讨论思路。

**改过 CLA 正文或这个工作流之后怎么测**：本地跑 `node scripts/test-cla-workflow.mjs` —— 它从
`cla.yml` 里抽出内嵌脚本，用假的 GitHub API 跑 11 个场景（免签名单 / 未签 / 已提醒 / 签署落库 /
姓名注入 / 签名库不存在 / 库里已有 / 大小写 / 别人评论 / 已关闭 PR / 手动重跑），零依赖、不联网。
真机路径用 Actions → `cla` → **Run workflow**（填 PR 号）手动重跑。

## 二、提 PR 的小约定

- **一个 PR 做一件事**，标题写清影响面。提交信息风格：`feat(scope): …` / `fix(scope): …`（中文正文）。
- 提交信息可选带 DCO 签名（`git commit -s`）——有助于追溯来源，但**不强制**（法律侧由 CLA 覆盖）。
- 不要提交本地产物：`.venv/`、`__pycache__/`、`build/`、`dist/`、模型与音频文件（`*.wav`）、
  配对令牌、本地笔记 `.md`。`.gitignore` 已覆盖大部分。

## 三、本地检查（提交前请自己跑一遍）

```bash
python -m venv .venv
.venv/bin/pip install -e ".[capture,dev]"      # Windows: .venv\Scripts\pip

# 1) 常规套件（不需要音频设备、不需要显示器）
.venv/bin/python -m unittest discover -s tests -t .

# 2) 真窗口 / Canvas 几何（需要显示器；CI 上默认跳过）
EASYSUB_HELPER_GUI_TESTS=1 .venv/bin/python -m unittest tests.test_gui

# 3) 真声卡（会真的打开设备采几百毫秒）
EASYSUB_HELPER_AUDIO_TESTS=1 .venv/bin/python -m unittest tests.test_server
```

改动涉及音频链路时，请说明你**实测过的平台与后端**（`parec` / `pw-record` / `soundcard` /
`pysysaudio` / BlackHole）。

## 四、领域边界（免得白做）

助手的定位就一句话：**采集本机音频 + 与页面配对**。以下方向会被拒：

- ❌ 托管 Web 产物 / 静态文件（页面由浏览器侧提供，助手不碰）
- ❌ 模型相关（下载、缓存、托管）：识别在页面里的 wasm 引擎；助手连 onnxruntime 都不 import
      （onnxruntime ≥1.15 已放弃 Win7，而我们要兼容 Win7）
- ❌ 把设备列表做成 HTTP API（页面不需要；浏览器授权框自带设备选择）
- ❌ 新增 CLI 子命令当用户路径（用户路径是那个窗口）

`tests/test_scope.py` 就是这条边界的机械闸门：谁把上述能力写回来，它会红。

音频契约固定：**16 kHz 单声道 f32le，20 ms 一帧（320 采样 / 1280 字节，无帧头）**，
经 `ws://127.0.0.1:<port>/ws` 推送（令牌走 query）。改这个要主仓库一起改。

## 五、Python 3.6 与 i18n

- **语法必须兼容 Python 3.6**（Win7 档）：`tests/test_py36_compat.py` 用
  `ast.parse(feature_version=(3,6))` + 禁用 API 扫描盯着（不用 `dataclass`、`asyncio.run`、
  `subprocess.run(capture_output= / text=)`、`str.removeprefix`、海象运算符、f-string `=` 等）。
- **所有用户可见文案必须中英双语**：`gui.*` / `log.*` / `lang.*` 等 key 在 zh / en 两个 catalog
  里必须一一对应（`tests/test_i18n.py` 会红）。**窗口日志正文也算用户可见**。
- 文档/注释用中文，代码标识符用英文。

## 六、交流

就事论事、正常交流。不接受人身攻击、广告、与项目无关的灌水。
