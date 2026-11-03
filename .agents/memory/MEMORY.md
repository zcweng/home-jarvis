# Project Memory

# home-jarvis — 家庭智能管家

项目根：`/Users/suke-3/home-jarvis`。目标：家庭版「理想同学」式语音管家——多轮连续对话、上下文关联、直接控制米家设备。

## 一、阶段路线（已定）

- **P0（当前）**：MacBook Pro M4 24G 本机软件闭环，音频直走声卡，**不使用** ESP32 与 MacMini。
- P0 稳定后原样迁 MacMini M1 16G（clone → uv sync → fetch_models.sh → 改 `.env` → compose up），不是重写。
- **P1（暂缓）**：ESP32-S3-N16R8 当「耳朵和嘴」，音频走 WebSocket 送主机；只替换 `audio/` 一层，状态机与工具层不动。
- 小爱音箱两条路：未刷机只能当**喇叭**（HA notify 的 play_text），要当**耳朵**必须刷机；家中两台——一代小米 AI 音箱（不可刷）与 Xiaomi 智能音箱 Pro OH2P（可刷，但上游已归档）。

## 二、选型（已定并落地）

| 层 | 选择 |
|---|---|
| ASR | sherpa-onnx + SenseVoice（`models/` 约 230MB） |
| VAD | silero-vad（`models/silero-vad/silero_vad.onnx`） |
| TTS（默认） | 本地神经 TTS：sherpa-onnx VITS **fanchen-C**（`models/vits-zh-hf-fanchen-C`），`TTS_BACKEND=neural` |
| TTS（备） | macOS `say`（`SayTTS`，带音色体检 + `[[slnc]]` 停顿） |
| TTS（评测过不选） | edge-tts（音质最好但首包 1695ms）、aishell3（8kHz 发闷）、piper 系列（macOS wheel 无 espeak 前端） |
| 唤醒 | sherpa-onnx KWS（13MB、纯 CPU、1.5s 音频约 30ms）为默认；ASR 文本门控兜底 |
| LLM | DeepSeek `deepseek-flash` @ `https://api.deepseek.com` |
| 家电 | Home Assistant（Docker）+ 小米官方 `ha_xiaomi_home` v0.5.0，外层只暴露约 5 个工具的控制契约 |

首字延迟目标 0.7~1.5s；工具类指令必然两轮（决策 + 组织回答），LLM 段 1.2~1.8s 属正常。

## 三、代码结构（已落地）

```
docs/家庭智能管家-技术方案.md
docs/P1-实施清单-ESP32S3-N16R8.md
docs/P0-本机语音闭环方案.md
docs/接米家设备-操作手册.md        OAuth 五步 + 6 个排障项
docs/OH2P-刷机评估.md              小爱音箱 Pro 刷机可行性 / 风险 / 接入方案
docs/TTS-选型与实测.md             四条 TTS 路线实测对比
docker/docker-compose.yml          Home Assistant
docker/config/custom_components/xiaomi_home   ha_xiaomi_home v0.5.0
scripts/preflight.py               环境自检
scripts/set_ha_token.py            校验并写入 HA 令牌（失败不落盘）
scripts/fetch_models.sh            all / asr / kws / tts，KWS 走 gh-proxy.com 镜像、失败回落直连
scripts/gen_aliases.py             从 HA 实体生成 config/aliases.yaml（房间识别、噪音过滤、只追加）
scripts/selftest_all.sh            一键回归，11 套件
jarvis/audio.py                    录音/播放/电平表/可打断
jarvis/asr.py                      SenseVoice
jarvis/tts.py                      SayTTS + 音色探针
jarvis/tts_neural.py               SherpaTTS（VITS）+ 预览 CLI
jarvis/textnorm.py                 数字/单位 → 中文读法（神经 TTS 前置）
jarvis/output.py                   SpeechRouter：local / speaker / both 输出路由
jarvis/vad.py                      断句
jarvis/kws.py                      KeywordWake + pypinyin 关键词生成
jarvis/wake.py                     WakeGate：唤醒词剥离、FOLLOW_UP 窗口、hold/release/sleep、收线词判定
jarvis/llm.py                      chat_with_guard()
jarvis/intent.py                   命令式意图判定
jarvis/app.py                      CLI：--ptt / --wake / --vad / --barge-in / --fake-llm
config/aliases.yaml                中文别名 → entity_id
.env                               密钥与设备名，不入库
```

## 四、设备接入现状

- HA 跑 `xiaomi_home` v0.5.0，OAuth 已完成（账号 昌文:1263518240 [中国大陆]，状态 loaded）。依赖全由 HA 镜像自带（construct / paho-mqtt / numpy / cryptography / psutil），**不需要 pip**。
- `internal_url` 必须是 `http://localhost:8123`，否则 OAuth 回调对不上、卡在「正在验证」。
- `config/aliases.yaml` 现有 **10 台真设备 + `小爱勿扰` 开关**：晾衣架(cover) / 空调 / 智能插座 / 客厅的小米电视 / 小爱音箱 / 吸顶灯 / 吸顶灯2 / 皎月吸顶灯 / 晾衣架灯 / 客厅灯(philips)。
- 客厅灯 `light.philips_cn_356246127_zyceiling_s_2_light`（别名 客厅主灯/客厅吸顶灯/大厅灯）靠墙上开关供电，开关关着时 HA 显示 unavailable，属正常。
- 米家集成不支持蓝牙 / 红外 / 虚拟设备；云端下发有 0.3~0.8s 延迟，想更快需中枢网关（固件 ≥3.3.0_0023）走本地。
- OH2P 的 10 个属性被暴露成 HA 实体，其中 `notify...play_text`（播报文本）、`button...wake_up`、`notify...execute_text_directive` 有用 → **零刷机即可当喇叭**（走小米云 TTS、音色固定、约 1s 延迟、勿扰时段静音）。

## 五、输出路由与音箱播报（本轮落地）

- `jarvis/output.py` 的 `SpeechRouter` 与 `SayTTS` 同方法签名（speak/stop/warm/play_cached/wait_idle），`Jarvis` 里无需分支。
- `.env` 开关：`TTS_TARGET=speaker`（默认，从小爱出声）/ `local`（电脑喇叭）/ `both`（调试，会听到回声）；`SPEAKER_FALLBACK_LOCAL=true` 云端报错或无音箱时回落电脑喇叭。
- 音箱模式的三处连带改动：①「首句先出声」禁用（预渲染 WAV 是本地路径，云端不可预渲染）②打断（barge-in）禁用并提示（云端音频无法 stop，且麦克风会收到音箱自身声音自断）③FOLLOW_UP 窗口 release 改为估算播报时长 `字数/4.5 + 0.4s`。
- `announce` 工具：自动扫带 `play_text` 的 notify 实体，优先选别名表里的 `小爱音箱`；按句切 80 字以内、多条间留 0.8s；检测 `no_disturb=on` 时在返回值提示「可能听不到」；支持 `announce(text, target=...)` 与 dry-run。
- 三条播报路径：回答当前对话→`TTS_TARGET` 指定（默认音箱）；全屋广播→`announce`；定时提醒→仍走本地喇叭（可选 `ANNOUNCE_TIMERS`）。

## 六、已验证事实

- `scripts/preflight.py` 通过：Python 3.12.13、uv、ffmpeg、Docker、zh_CN 语音齐备。
- HA 容器运行，长期令牌在 `.env`（600），`GET /api/` 正常。
- 音频层：录音 1.00s / -57.8 dBFS、WAV 往返最大误差 3.06e-05、打断 120ms、可重复播放。
- VAD 断句：5.47s 音频正确切成 2 段。
- 工具层 14 项全过；改用真灯后开机 / 绝对亮度 / 相对调暗 / 查询（含色温 5250K）/ 关灯 5 项全过。
- 模型断言 12 项全过（deepseek-flash）：纯聊天不误调工具、明确指令必调、指代命中、虚构设备反问、闲聊不误触。
- 全链路 3 断言过：语音进 → HA 状态真的改变 → 语音出（真设备复测同样通过）。
- 首句先出声：预渲染「好的」WAV 0.7ms 出声，比模型终答早 405~600ms；终答前等短应答播完，防叠音（仅 local 模式）。
- KWS 11 项过；端到端「说到最后半个音节」即唤醒；反例句全程静默。
- FOLLOW_UP 窗口 hold/release：3 项语义测 + 端到端（窗口压到 2 秒，第二句不带唤醒词仍接上）。
- 收线（`退下吧`/`退出`/`结束对话`/`再见`/`拜拜` 等）：`try/finally` 包住「说告别 → gate.sleep()」，**播完告别才关窗**；`selftest_wake.py` 12 项断言、19 项全过；端到端确认窗口关闭后紧接的设备指令被挡掉。
- 离线拒控：不可用设备的米家服务调用会「返回成功但什么都不做」，现已前置查在线状态，离线直接如实拒绝（控制与查询各 1 项断言 + 真模型复测）。
- demo 清理：删配置条目后实体 359 → 260，demo 残留 0。
- OH2P 播报实测：`POST /api/services/notify/send_message` → `notify.xiaomi_cn_866453665_oh2p_play_text_a_7_3`，HTTP 200，实体状态更新。
- `announce` 端到端：真模型 → 真工具 → 真音箱出声；模型自行发现勿扰状态并提议关掉（`小爱勿扰` 别名可用，on→off→on 可控）。
- 输出路由自检 7 项：speaker 只走 notify、speaker 不碰本机喇叭、无音箱回落、notify 报错回落、local 只走本机、both 两边都走、长文本分多条 notify。
- TTS 语速实测（25 字）：1.0→6.88s（3.6 字/秒，偏慢）、1.15→6.44s、1.35→5.38s（4.7 字/秒）、1.5→4.92s（5.1 字/秒）；合成耗时随语速下降（1.0 时 2010ms、1.5 时 1400ms）。
- 神经 TTS 实测：fanchen-C 16kHz，13 字约 496ms；aishell3 8kHz 96~187ms 但频谱发闷；edge-tts 首包 1695ms。
- 文本规范化：`10秒` 曾整段丢失（OOV `Ignore OOV '10'`），修复后可念「十秒」，18 项自检含 13 个中文数字边界断言 + `say` 对照组（say 本身能念数字）。
- 计时基线：ASR 23~91ms、工具类 LLM 1.2~1.9s、神经 TTS 播报 2.7~3.0s、端到端首字 ≈1.5s。
- `./scripts/selftest_all.sh` → 11 套件全绿。

## 七、环境坑与对策（勿重踩）

1. **Docker 凭据助手损坏**（`credsStore: "desktop"`）→ `DOCKER_CONFIG=/tmp/dcfg docker compose ...` 绕过；镜像已落地，日常不再碰 registry。
2. **macOS 设备索引不稳定** → 一律按名字子串匹配（`INPUT_DEVICE=外置麦克风`），禁止写索引，未命中回落默认并告警。
3. **麦克风 TCC 权限** → 必须先在 GUI 终端跑通授权，再写 launchd。
4. **TRRS CTIA/OMTP** → 老耳机麦克风电平恒为 0，先换耳机再怀疑代码。
5. **soxr 流式重采样空块** → 拿到非空结果前继续取，不要把空数组传给下游（VAD 同理）。
6. **PortAudio -9986 是打断的正常路径**，应静默；`abort()` 过的流不要再 `stop()`，直接 close（否则 120ms 恶化到 515ms）。
7. **KWS 流式解码必须句末补 0.66s 静音 + `input_finished()`** —— 模型需约 0.64s 右上下文，不补 padding 离线文件永远解不出；实时场景靠自然静音天然满足。
8. **GitHub release 直连极慢**（9 分钟 13MB）→ 用 `gh-proxy.com` 镜像（64 秒 32.6MB），已写进 `fetch_models.sh`，米的集成包同理。**但镜像会给出残缺文件**（`zh_CN-huayan-medium.onnx` 下到 60MB，应 63,201,423 字节），加载只报 `Protobuf parsing failed`，极易误判成模型不兼容 → 下载后必须校验大小。
9. **`say` 个人语音类音色渲染空音频**：Eddy/Flo/Reed/Rocko/Grandma/Grandpa/Shelley/Sandy 只出 16ms 且不报错；Tingting/Meijia 正常。`SayTTS` 启动探针 <0.2s 自动回落 Tingting。
10. **唤醒词容错**：「贾维斯」常被 ASR 听成「亚维斯」，≥3 字唤醒词允许错 1 字。
11. **同音字干扰**：「再暗一点」被听成「再按一点」，测试改用「把亮度调低一些」；真实口音下短指令仍需重点观察。
12. **播报期间麦克风在收自己的声音** → 播报后 `mic.drain()` 清队列；打断时保留（那段是用户的）。
13. **打断阈值是测出来的**：自适应回声基线（取近 1.2s 能量中位数），需高出 12dB 且持续 350ms；9dB/300ms 会因音量变大误判。
14. **FOLLOW_UP 窗口必须从播报结束计时**：一轮处理约 4s，若从唤醒起算，8 秒窗口实际只剩 4 秒。用 `WakeGate.hold()/release()`，并以 try/finally 包裹保证异常也释放。
15. **模型会「编造已执行」**（说「已经关了」但一次工具都没调）→ `chat_with_guard()`：`intent.py` 判定像命令且零工具调用时，把 `tool_choice` 锁到 `control` 强制重问。prompt 单独堵不住。
16. **删 `demo:` YAML 不够**：HA 会把 YAML 自动导入成 config entry（`source: import`），必须在「设置 → 设备与服务 → Demo → ⋮ → 删除」或调 API 删配置条目，只改文本重启无效。
17. **OAuth 回调只能用 `localhost:8123`**，`127.0.0.1:8123` 域名不同会对不上；`internal_url` 为 null 时同样卡住。
18. **离线设备的「假成功」**：unavailable 实体的服务调用返回 200 但不执行 → 是「编造已执行」的第二形态，必须前置查在线状态并如实拒绝。`play_text` notify 实体同样没有在线/离线语义，音箱断电时调用返回 200 但没声音，只能靠勿扰提示兜一半。
19. **`--rebuild` 会静默删掉离线设备**：米家设备靠墙开关断电时即 unavailable，若重建时跳过就悄悄消失 → 规则改为「已收录即保留」，重建时标 `[离线]`。
20. **收线词必须整句比对**：先剥「好的/行/嗯」「吧/啊/呀/了」再比，否则「把空调退出节能模式」会被当成退出指令。
21. **测试不要硬编码设备名**：自检统一动态选第一盏在线灯，否则增删设备后假失败。
22. **别加没有代码读取的字段**（曾加 `pinned: true` 后又删除）—— 不起作用的配置等于撒谎。
23. **notify 实体前缀匹配要先剥 domain**：按 `entity_id.split("_")[:5]` 取前缀会被 `media_player.xiaomi_...` 算歪 → 匹配不到 play_text；须先剥 `media_player.` 再取前 4 段。
24. **sherpa-onnx 2.0.0 的 macOS wheel 没编 espeak 前端** → `vits-piper-*` 加载报 `no lexicon, and no frontend available`，把 `data_dir` 指到 `espeak-ng-data` 也没用。**只能用带 `lexicon.txt` 的模型**（aishell3 / fanchen-C / matcha）。
25. **本地神经 TTS 词典里没有阿拉伯数字，OOV 是静默丢弃**（只在日志留 `Ignore OOV '10'`）→
