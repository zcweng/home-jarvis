# home-jarvis · 家庭版「理想同学」

> **中文**｜一句话：**连续对话 + 上下文关联 + 直接控制米家设备的全本地语音管家。**
> 对标理想车机的多次唤醒交互——喊一声「贾维斯」就能连着聊、能插话打断、记得上一句说的是哪盏灯，
> 并且真的会去操作设备，而不是回你一句「好的」。
>
> English: a self-hosted Chinese voice assistant for the smart home — wake word, multi-turn
> follow-up, barge-in, and **real** Mijia device control via Home Assistant. ASR/TTS/wake word all
> run locally (sherpa-onnx); only the LLM call goes out to DeepSeek.

![Python](https://img.shields.io/badge/python-3.10%2B-blue)
![Platform](https://img.shields.io/badge/platform-macOS%20%C2%B7%20Linux-lightgrey)
![License](https://img.shields.io/badge/license-MIT-green)
![Status](https://img.shields.io/badge/status-P0%20working-brightgreen)

---

## 它能做什么

| 能力 | 实际效果 |
|---|---|
| **唤醒词** | 喊「贾维斯」（可自定义）→ 说到最后一个音节就亮；带容错，听成「亚维斯」也能唤醒 |
| **连续对话** | 唤醒后 8 秒内免唤醒续聊，**窗口从播报结束才开始计时**（不想话时不上锁） |
| **收线** | 「退下吧 / 再见 / 不聊了」→ 先说完告别，再关窗，之后必须重新喊唤醒词 |
| **上下文关联** | 「再暗一点」「关掉它」直接命中上一轮那盏灯，不反问 |
| **真控设备** | 灯 / 空调 / 晾衣架 / 插座 / 电视，走 Home Assistant 统一接口；亮着几度都会真读回来 |
| **口播反馈** | 默认从小爱音箱出声（`notify play_text`），也可走电脑喇叭或两边都念 |
| **全屋广播** | 「让音箱跟家里人说该吃饭了」→ `announce` 工具，按句切成多段播报 |
| **可打断** | 播报中插话即停（自适应回声基线，纯回声不会自断） |
| **拒控离线设备** | 设备断电时**如实拒绝**（「客厅灯现在不在线，我控制不了它」），不装执行成功 |
| **防「编造已执行」** | 模型说「已经关了」但一次工具都没调时，守卫强制重问一次 |

延迟实测（MacBook Pro M4）：ASR 50~90ms · LLM 单轮 500~750ms、两轮工具往返 1.2~1.8s ·
TTS 首字 ~200ms · **端到端首字 ≈1.5s**。

---

## 架构

```
        ┌────────── 本机 ─────────────────────────────────────────────┐
麦克风 →│ audio.py  录音/播放/电平/可打断                              │
        │   ↓ 16k 单声道                                              │
        │ vad.py（silero-vad 断句）→ asr.py（SenseVoice，~60ms）        │
        │   ↓ 文本                                                    │
        │ wake.py 唤醒门（KWS / ASR 门控 + FOLLOW_UP 窗口 + 收线词）    │
        │   ↓                                                         │
        │ app.py 会话状态机 → llm.py（DeepSeek function calling）      │
        │                     ↕ 工具层 tools.py：control/query/announce│
        │   ↓ 回复文本                                                 │
        │ textnorm.py 数字/单位 → 中文读法 → tts_neural.py 或 tts.py   │
        │ output.py SpeechRouter：local 电脑喇叭 / speaker 小爱 / both  │
        └──────────────────────────────┬──────────────────────────────┘
                                       │ REST
                              ┌────────▼─────────┐
                              │ Home Assistant   │  Docker · :8123
                              │ ha_xiaomi_home   │  官方米家集成 v0.5.0
                              └────────┬─────────┘
                                       │ 米家云 / 局域网
                                   全屋米家设备
```

一个进程、四个可替换适配器（ASR / TTS / LLM / 工具），全部配置驱动；
设备一律按**中文名子串**匹配，绝不写设备索引（插拔一次索引就变）。
将来接 ESP32-S3 当远端端点，只替换音频层，状态机与工具层不动。

---

## 快速开始

### 0. 前置条件

- **Python ≥ 3.10** + [uv](https://docs.astral.sh/uv/)（省事）或 pip
- **Docker**（跑 Home Assistant）
- **ffmpeg**（音频兜底转码，可选但建议）
- **macOS**：默认 TTS 用本地 VITS，备选系统 `say`。Linux 上把 `TTS_BACKEND=say` 换掉即可
- **DeepSeek API Key**：<https://platform.deepseek.com/api_keys>
- **一个米家账号 + Home Assistant**：手机能控的设备都能被接管

### 1. 装依赖

```bash
git clone https://github.com/zcweng/home-jarvis.git
cd home-jarvis
UV_CACHE_DIR=.uv-cache uv sync --extra asr --extra vad --extra kws
cp .env.example .env          # 然后填 HA_TOKEN / DEEPSEEK_KEY
python3 scripts/preflight.py  # 环境自检：Python/uv/ffmpeg/Docker/语音设备
```

### 2. 起 Home Assistant

```bash
docker compose -f docker/docker-compose.yml up -d   # → http://localhost:8123 走完初始化
```

装官方米家集成见 [`docs/接米家设备-操作手册.md`](docs/接米家设备-操作手册.md)，要点三条：

1. HA「设置 → 设备与服务 → 添加集成 → Xiaomi Home」，地区选 **中国大陆**；
2. 回调地址必须是 **`localhost:8123`**，用 `127.0.0.1` 会卡在「正在验证」；
3. 授权完拿到长期令牌，用脚本写进 `.env`（校验失败不会落盘）：

```bash
python3 scripts/set_ha_token.py    # HA → 个人资料 → 安全 → 长期访问令牌
```

### 3. 下模型（约 400MB，不入库）

```bash
./scripts/fetch_models.sh all   # SenseVoice(240MB) + silero-vad + KWS(36MB) + VITS-TTS(119MB)
# 分项：./scripts/fetch_models.sh sensevoice | silero-vad | kws | tts
```

> 国内走 `gh-proxy.com` / `hf-mirror.com` 镜像加速。**镜像会给出残缺文件**，
> 下载脚本因此都带字节数下限校验——少了就重下，否则只会报 `Protobuf parsing failed`。

### 4. 认领你自己的设备

```bash
python scripts/gen_aliases.py --dry-run   # 先看会认领哪些设备（不写文件）
python scripts/gen_aliases.py             # 追加写入 config/aliases.yaml（自动备份）
```

别手动抄长清单：脚本会做房间识别、噪音过滤、重名让路，并**保留已收录但当前离线的设备**
（标 `[离线]`）。模板与字段说明见 [`config/aliases.example.yaml`](config/aliases.example.yaml)。

### 5. 说话

```bash
.venv/bin/python -m jarvis.app --wake        # 唤醒词模式（推荐）
```

---

## 使用方式

```bash
.venv/bin/python -m jarvis.app --wake              # 喊「贾维斯」进入 8 秒续聊窗口
.venv/bin/python -m jarvis.app --wake --barge-in   # 允许播报中插话打断（无 AEC 时慎用）
.venv/bin/python -m jarvis.app --ptt               # 回车说话，调试最稳
.venv/bin/python -m jarvis.app                     # 常开 VAD（安静环境）
.venv/bin/python -m jarvis.app --wav debug/two.wav # 离线注入音频，回归用
.venv/bin/python -m jarvis.app --fake-llm          # 不起模型，用它验证链路
```

| 开关 | 说明 |
|---|---|
| `--wake` | 唤醒词门控；`WAKE_BACKEND=kws`（默认，专用关键词模型）或 `asr`（整句识别后判） |
| `--barge-in` | 播报中可打断；阈值是实测值（回声基线上浮 12dB 且持续 350ms） |
| `--fake-llm` | 用 `jarvis/llm_fake.py` 的固定回答跑通链路，不烧 token |

---

## 配置（`.env`）

复制 `.env.example` 后按需改；**代码里零硬编码，迁移换机只改这里**。

| 变量 | 默认 | 说明 |
|---|---|---|
| `HA_TOKEN` / `HA_URL` | — / `http://127.0.0.1:8123` | HA 长期令牌与地址（令牌只在这里，别提交） |
| `DEEPSEEK_KEY` / `LLM_MODEL` | — / `deepseek-flash` | 控制类走 flash；闲聊可换大模型 |
| `INPUT_DEVICE` / `OUTPUT_DEVICE` | `外置麦克风` / 系统默认 | **按名字子串匹配**，不写索引；未命中回落默认并告警 |
| `TTS_BACKEND` | `neural` | `neural`=本地 VITS（自然）｜`say`=系统合成（快但生硬） |
| `TTS_TARGET` | `speaker` | `speaker`=小爱出声｜`local`=电脑喇叭｜`both`=调试（有回声） |
| `SPEAKER_FALLBACK_LOCAL` | `true` | 音箱不可达/云端报错时回落电脑喇叭 |
| `TTS_SPEED` | `1.35` | 语速倍数（1.0 明显偏慢，1.3~1.5 接近日常） |
| `WAKE_WORDS` | `贾维斯\|小贾\|管家` | 中文自动转拼音 token；≥3 字允许错 1 字 |
| `FOLLOW_UP_SECONDS` | `8` | 免唤醒续聊窗口，从**播报结束**计时 |
| `SLEEP_WORDS` / `SLEEP_REPLY` | 见示例 | 收线词（整句比对，「把空调退出节能模式」不会误判） |
| `VAD_THRESHOLD` / `VAD_SILENCE_MS` | `0.5` / `500` | 断句灵敏度与静音阈值 |
| `ANNOUNCE_ENTITY` | 空 | 指定播报音箱；留空自动发现带 `play_text` 的 notify 实体 |
| `ACK_PHRASES` | `好的\|嗯\|收到` | 首句先出声的预渲染短应答 |

TTS 音色坑：`say` 下「个人语音」类音色（Eddy / Flo / Reed / Sandy / Rocko…）只渲染 **16ms 空音频**，
必须用 Tingting / Meijia。`SayTTS` 启动时自带音色体检，异常自动回落并告警。

---

## 自检与回归

```bash
./scripts/selftest_all.sh          # 一键跑 11 个套件：环境→音频→打断→唤醒→输出路由→
                                   # 文本归一化→KWS→工具层→语音往返→模型断言→全链路
./scripts/selftest_all.sh --fast   # 跳过要真实模型和出声的项（不烧 token）
```

CI 只跑**离线、无需音频设备**的部分（`python -m compileall`、文本归一化、别名命名逻辑），
见 [`.github/workflows/ci.yml`](.github/workflows/ci.yml)。

单个套件按需跑：

```bash
.venv/bin/python scripts/selftest_tools.py        # 别名→控制→回读，14 项
.venv/bin/python scripts/selftest_wake.py         # 唤醒门/续聊窗口/收线，19 项
.venv/bin/python scripts/selftest_llm.py          # 真模型断言，12 项（烧 token）
.venv/bin/python scripts/selftest_chain.py        # 语音进 → 设备动 → 语音出
.venv/bin/python scripts/selftest_tts_text.py --pure   # 纯 Python，CI 同款
```

---

## 目录结构

```
jarvis/                会话与适配器
  audio.py             录音/播放/电平表/可打断
  vad.py  asr.py       silero-vad 断句 · SenseVoice 识别
  kws.py  wake.py      专用唤醒词 · 唤醒门（续聊窗口/收线）
  llm.py  intent.py    DeepSeek 调用 + 「编造已执行」守卫
  tools.py             工具层：control / query / announce / 定时
  tts_neural.py tts.py 本地 VITS · macOS say（带音色体检）
  textnorm.py          数字/单位 → 中文读法
  output.py            SpeechRouter 输出路由
  session.py app.py    状态机 · CLI 入口
config/
  aliases.example.yaml 别名表模板（真实 aliases.yaml 不入库）
  prompt.md            人设与规则
docker/                Home Assistant compose
docs/                  方案、选型、踩坑（见下）
scripts/               preflight / fetch_models / gen_aliases / set_ha_token / selftest_*
models/                模型权重（脚本下载，不入库）
```

---

## 文档

| 文档 | 内容 |
|---|---|
| [`docs/P0-本机语音闭环方案.md`](docs/P0-本机语音闭环方案.md) | 本机闭环的选型、macOS 坑位、落地顺序、迁移约定 |
| [`docs/接米家设备-操作手册.md`](docs/接米家设备-操作手册.md) | 米家 OAuth 五步 + 6 个排障项 |
| [`docs/TTS-选型与实测.md`](docs/TTS-选型与实测.md) | 四条 TTS 路线实测对比（含首包与音质） |
| [`docs/OH2P-刷机评估.md`](docs/OH2P-刷机评估.md) | 小爱音箱 Pro 能不能当耳朵/喇叭，风险几何 |
| [`docs/家庭智能管家-技术方案.md`](docs/家庭智能管家-技术方案.md) | 总方案：路线对比、延迟预算、合规 |
| [`docs/P1-实施清单-ESP32S3-N16R8.md`](docs/P1-实施清单-ESP32S3-N16R8.md) | 硬件端点清单：BOM、接线、固件路线（挂起） |

---

## 踩过的坑与对策（选摘）

这些都是实测踩出来的，完整清单散在 `docs/` 和各模块 docstring 里：

- **KWS 流式解码必须补 0.66s 静音 + `input_finished()`**——模型右侧要约 0.64s 上下文，
  漏了它文件永远解不出唤醒词。
- **`--rebuild` 曾静默删掉离线设备**（设备靠墙上开关供电时是 unavailable）→ 规则改为「已收录即保留」。
- **米家云对不可用设备「返回成功但什么都不做」**→ 前置查在线状态，离线直接如实拒绝。
- **模型会凭上下文编造已执行**（说「已经关了」却零工具调用）→ `intent.py` 判定像命令就锁 `tool_choice` 强制重问。
- **本地 TTS 词典没有阿拉伯数字，OOV 会被静默丢弃**（`10秒` → 念成 `秒`）→ `textnorm.py` 前置转中文读法。
- **sherpa-onnx 的 macOS wheel 没编 espeak 前端** → 只能用带 `lexicon.txt` 的模型（aishell3 / fanchen-C）。
- **Docker 凭据助手坏了**（`credsStore: desktop`）→ `DOCKER_CONFIG=/tmp/dcfg docker compose ...` 绕过。
- **HA 的 `demo:` YAML 删不掉**——它会被自动导入成 config entry，得在设备与服务里删条目。

---

## 隐私与安全

- **`.env`、`docker/config/`、`config/aliases.yaml`、`models/` 全部 gitignore**。
  `docker/config/` 里会有小米账号 token 明文，`aliases.yaml` 里有你家设备 ID。
- Home Assistant **只监听本机**，不要直接暴露公网；要远程就上 Tailscale / 内网穿透 + 鉴权。
- 语音不出机器：ASR / VAD / 唤醒 / TTS 全在本地（sherpa-onnx），
  只有**识别后的文本**会发给 DeepSeek 换回复。
- 建议给磁盘开 FileVault（macOS）/ LUKS（Linux），令牌泄漏才是真风险。
- 仓库里的 `config/aliases.example.yaml` 与文档中的 entity_id 全是**占位示例**。
- 安全问题请按 [`SECURITY.md`](SECURITY.md) 私下报告。

---

## 路线图

- [x] **P0** 本机软件闭环：音频 → ASR → 唤醒门 → LLM+工具 → TTS → 音箱，全链路跑通
- [ ] **P0.5** 迁移到常开 MacMini（clone → `uv sync` → `fetch_models.sh` → 改 `.env` → compose up）
- [ ] **P1** ESP32-S3-N16R8 当「耳朵和嘴」：只替换音频层，音频走 WebSocket 送主机
- [ ] **P2** 定时提醒上台架、行程/家电状态主动播报、多房间说话人区分

---

## 参与贡献

欢迎 issue / PR。动代码前：

1. `./scripts/selftest_all.sh --fast` 至少全绿；
2. 新增行为请补一个 `scripts/selftest_*.py` 断言，**别只加注释**；
3. 提交前自查 `git diff --cached` 里没有 `.env`、真实 entity_id、HA 令牌、个人路径。

## 许可证

[MIT](LICENSE) © 2026 zcweng

第三方组件与模型权重各自遵循其原许可（sherpa-onnx、SenseVoice、silero-vad、VITS 音色、
Home Assistant、`ha_xiaomi_home` 等）；本项目不分发模型权重，均由 `scripts/fetch_models.sh` 从上游下载。

「贾维斯 / Jarvis」仅作唤醒词与项目昵称使用，与漫威 / 迪士尼及小米、米家均无关联。
