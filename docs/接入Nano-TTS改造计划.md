# 接入 Nano TTS 改造计划

> 触发问题：现用本地 VITS（`models/vits-zh-hf-fanchen-C`）词典只有汉字音素，
> `tiger` / `USB` 这类拉丁词是 OOV，sherpa-onnx 会 **静默丢弃**（日志一行 `Ignore OOV 'tiger'`），
> 于是「老虎英文是 tiger」被念成「老虎英文是」。
> `jarvis/textnorm.py` 的字母读名只是**权宜之计**（念成"泰格"，不是英文发音）。
>
> 本计划把「真正会念中英文」的 Nano TTS 接进来，并给出可回滚、可实测的落地路径。

---

## 0. 一页结论

| 结论 | 内容 |
|---|---|
| 产品选型 | **开源 [MOSS-TTS-Nano](https://github.com/OpenMOSS/MOSS-TTS-Nano)**（本地 ONNX，0.1B），不是豆包云 TTS |
| 能不能解决问题 | **能**。字符级多语种建模，中英混读原生支持，不靠拼音词典，没有 OOV 丢词问题 |
| 推荐架构 | **独立侧车 HTTP 进程 + jarvis `NanoTTS` 适配器**；侧车用项目脚本起停，**不开机自启** |
| 音色 | **默认官方 `zh_1`**；自录 `owner.wav` 可选（克隆效果一般，已回退） |
| 延迟口径 | **不明显卡即可**（相对当前 VITS ~500ms，首音 ≤ ~1.0s 可接受；>1.2s 或 RTF>1.0 → 混合路由） |
| 关键决策点 | **阶段 0 实测 Go/No-Go**；No-Go 改走第 9 节混合路由，不全量替换 |
| 工期 | 阶段 0~5 合计约 **3~4 人日**，其中阶段 0 半天到一天，可单独验收 |
| 代码改动面 | 新增 `jarvis/tts_nano.py`、`tools/nano-tts/server.py`、`scripts/nano_tts_server.sh`；改 `tts_neural.build_local_tts`、`textnorm` 分流、`config`、`.env.example` |

---

## 1. 产品选型（已拍板）

**定稿：开源 [MOSS-TTS-Nano](https://github.com/OpenMOSS/MOSS-TTS-Nano)**，本地 ONNX 推理。豆包云等方案不在本计划范围内。

| 项 | 定稿 |
|---|---|
| 形态 | 开源 0.1B，**本地 CPU 可跑**，ONNX 版无需 PyTorch |
| 中英混读 | 原生支持（约 20 语种） |
| 音色 | **必须给参考音做克隆**；本计划默认用自录 `owner.wav`（见 4.4） |
| 延迟 | 服务端 RTF 随机器差异大；本机以阶段 0 实测为准，「不明显卡」即可 |
| 成本 | 0（只吃 CPU/内存） |
| 进程模型 | 常驻侧车 HTTP，**手动** `scripts/nano_tts_server.sh start`，**不开机自启、不装 launchd** |

官方 `assets/audio/zh_1.wav` 只用于阶段 0 冒烟验证；上线音色一律切到自录参考音。

---

## 2. 现状：TTS 在 jarvis 里有三个接缝

### 2.1 适配器契约（新增 backend 必须对齐）

```python
# 逐句流水播放（tts_neural.SherpaTTS 是参考实现）
synthesize(text) -> (np.float32 mono, sample_rate)
speak(text, blocking=True) -> Thread | None
stop() -> None
warm(phrases) -> int                 # 预渲染「好的/我在/好，有事叫我」
play_cached(text, blocking=False) -> float   # 返回合成耗时 ms
wait_idle(timeout=2.0) -> None
speaking -> bool
voice: str
mode: str                            # 启动日志里打印
```

调用方：

- `jarvis/app.py` 只通过 `SpeechRouter` 访问 TTS，`SpeechRouter` 只要求上表方法
  → **新增 backend 不改状态机**。
- `build_local_tts(settings, speaker)`（`jarvis/tts_neural.py` 末尾）是唯一装配点，
  按 `TTS_BACKEND` 分支返回 `SherpaTTS` 或 `SayTTS`。**新 backend 就挂在这里。**

### 2.2 音频采样率不用管

`EndpointSpeaker.play()` 内部已做 `soxr.resample(任意 → 16k)`，本机 `Speaker` 也接受任意 `sample_rate`。
Nano 输出 48kHz 单声道 → **直接交给 speaker 即可**，设备端（S12 下行）零改动。

### 2.3 文本归一化必须是「按后端分流」（最容易踩的坑）

```python
# jarvis/tts_neural.py::synthesize
audio = self._tts.generate(normalize_for_tts(text), sid=..., speed=...)
```

`normalize_for_tts` 现在会把 `tiger` → `泰格`、`USB` → `优艾斯比`。
**如果 Nano 也走这层，等于把刚买的能力又抹掉**（英文被念成中文音译）。
所以：

- 数字/单位归一化：Nano 自带 WeTextProcessing 前端，**不需要**我们再做；
- 拉丁词音译：**只有 VITS 需要**，Nano 必须原样传入 `tiger`。

→ 改动：`normalize_for_tts(text, *, numbers=True, latin=True)`，VITS 调用保持默认，Nano 不调用。
`scripts/selftest_tts_text.py` 增加「latin=False 时原文不动」的断言。

---

## 3. 目标与验收指标

| 维度 | 目标 | 验收方式 |
|---|---|---|
| 正确性 | 中英混读不丢词：`老虎英文是 tiger`、`把 USB 插上`、`Wi-Fi 密码` | TTS→ASR 往返，ASR 结果必须包含英文词（当前 `scripts/selftest_speech.py` 的手法） |
| 首音延迟 | **不明显卡即可**：典型句（11~13 字）首音 **≤ 1000ms 可接受**；相对 VITS（~500ms）允许慢半拍，但不能感到「卡住」 | 计时日志 `🔊 播报 xxxms` + 新增「首音 ms」 |
| RTF | ≤ 0.8（句级，优先）；流式 ≤ 0.6 为加分项 | 合成耗时 ÷ 音频时长，连续 20 句 |
| 稳定性 | 侧车不可用时 **≤ 3s 回落 VITS**（本句 fallback + 冷却），只听到音色变化；**不要求**侧车自动拉起 | 手动 `stop` / kill 后连说 3 句 |
| 回归 | `./scripts/selftest_all.sh` 全绿；`selftest_tts_text.py`、`selftest_speech.py`、`selftest_chain.py` 无新增失败 | 脚本 |
| 听感 / 克隆 | 中英混读明显优于 VITS；自录克隆听得出「是本人」，无明显机械感/串音 | 人工试听（cue.py 弹窗对照） |
| Go/No-Go | 首音 **> 1200ms** 或 RTF **> 1.0** → 不做全量替换，走第 9 节混合路由 | 阶段 0 附录表格签字 |

---

## 4. 方案设计

### 4.1 为什么用侧车（独立进程）而不是 in-process

| 对比 | in-process（`import moss_tts_nano`） | 侧车 HTTP 服务（推荐） |
|---|---|---|
| 依赖隔离 | 会把 onnxruntime/tokenizers 塞进主 venv，与 sherpa-onnx、numpy 版本互相牵制 | 独立 `tools/nano-tts/.venv`，主项目零新依赖 |
| 模型加载 | 主进程启动 +20~40s，调试 TTS 要重启整个管家 | 只重启侧车 |
| 崩溃影响 | 推理段错误 → 整个 Jarvis 挂 | 主进程无感，走 fallback |
| 打断 | 需自己管线程 | 关掉 HTTP 流即可，语义清晰 |
| 代价 | — | 多一跳本地回环（<2ms，可忽略） |

### 4.2 架构

```
┌────────────── jarvis 主进程 ──────────────┐        ┌──── nano-tts 侧车 (:8091) ────┐
│ app.py → SpeechRouter → NanoTTS           │  HTTP  │ FastAPI /v1/audio/speech      │
│                          │  (stream)     │ ─────→ │  ↓                            │
│                          ↓                │        │ onnx_tts_runtime (ORT CPU)    │
│              EndpointSpeaker / Speaker     │ ←PCM── │ MOSS-TTS-Nano-100M-ONNX       │
│              （内部 48k→16k 重采样）        │ 48k    │ MOSS-Audio-Tokenizer-Nano     │
└───────────────────────────────────────────┘        └───────────────────────────────┘
                                                   参考音 refs/owner.wav（自录克隆）
                                                         ↑
                                          scripts/nano_tts_server.sh start|stop|status|log
                                          （前台 / 后台均可；不开机自启）
```

请求契约（对齐 OpenAI 兼容 + 上游参数）：

```jsonc
POST /v1/audio/speech
{
  "input": "老虎英文是 tiger。",
  "ref_audio": "owner",       // 侧车已缓存的参考音 id；也可传 base64
  "response_format": "pcm",   // 16bit LE, 48000Hz, mono
  "stream": true,             // 侧车 chunked 返回，边生成边吐
  "speed": 1.0
}
```

### 4.3 播放策略（分两档实现，先做简单的）

1. **句级流水（阶段 2 必做）**：`split_for_speech()` 切句 → 后台线程逐句合成（可预取下一句）→ 主线程阻塞播完。
   与现有 `SherpaTTS` 完全同构，改动最小、行为可控。
2. **chunk 级流式（可选，阶段 6 优化）**：需要给 `Speaker` / `EndpointSpeaker` 增加
   `open_stream(rate) / write(chunk) / close()`，把 TTS 的 chunk 直接推进播放队列。
   收益是首音再降 200~500ms，代价是打断逻辑要重测（barge-in 回归）。

### 4.4 自录克隆参考音（已拍板）

音色 = 参考 wav。上线默认文件：`tools/nano-tts/refs/owner.wav`。

**录制要求（写进 README 片段，侧车启动时校验）：**

| 项 | 要求 |
|---|---|
| 时长 | 8~12 秒（短于 5s 克隆不稳，长于 15s 编码变慢） |
| 内容 | 一段自然中文朗读，含 1~2 个常见英文词（如 `Wi-Fi` / `USB`），语气平稳 |
| 环境 | 安静房间；避免回声、风扇、键盘；与日常说话距离一致 |
| 格式 | 单声道、16-bit PCM、采样率 ≥ 16kHz（推荐 48kHz wav） |
| 电平 | 峰值约 -6~-3 dBFS，不要削顶 |

**辅助脚本（阶段 1 附带，可选）：**

```bash
# 用本机麦克风录 10 秒并写成 refs/owner.wav（依赖已有 sounddevice）
./scripts/nano_tts_record_ref.sh          # 交互：倒计时 → 录制 → 试听 → 确认覆盖
```

阶段 0 可用官方 `zh_1.wav` 先测延迟；阶段 1 起侧车默认读 `owner.wav`，若文件不存在则启动时报错并提示录制命令（**不要静默回落 zh_1**，避免误以为已经是本人音色）。

### 4.5 侧车生命周期（已拍板：常驻、手动、不开机自启）

| 动作 | 命令 | 行为 |
|---|---|---|
| 启动（前台） | `./scripts/nano_tts_server.sh start` | 预加载模型 + 编码 `owner.wav`，阻塞在前台，Ctrl-C 退出 |
| 启动（后台） | `./scripts/nano_tts_server.sh start --bg` | nohup + pid 文件 `tools/nano-tts/nano-tts.pid`，日志 `tools/nano-tts/nano-tts.log` |
| 停止 | `./scripts/nano_tts_server.sh stop` | SIGTERM → 等退出；顽固再 SIGKILL |
| 状态 | `./scripts/nano_tts_server.sh status` | pid 是否存活 + `GET /health` |
| 看日志 | `./scripts/nano_tts_server.sh log` | `tail -f` 日志 |

**明确不做：**

- 不写 launchd / LaunchAgent，不开机自启
- 不自动 `KeepAlive`：`kill -9` 后**不会**自动拉起；jarvis 侧走 fallback 到 VITS
- 日常用法：需要 Nano 音色时先起侧车，再起 jarvis；不需要时 `stop` 省内存

---

## 5. 分阶段实施

### 阶段 0：可行性实测（0.5~1 天，**独立可验收，先做这个**）

不做任何集成，只回答「这台机器上够不够快、够不够好」。

1. 建独立环境（不动主 venv）：
   ```bash
   mkdir -p tools/nano-tts && cd tools/nano-tts
   uv venv --python 3.12 .venv && . .venv/bin/activate
   git clone --depth 1 https://github.com/OpenMOSS/MOSS-TTS-Nano.git
   cd MOSS-TTS-Nano
   pip install -r requirements.txt && pip install -e .   # ONNX 版推理不需要 torch
   ```
2. 下载 ONNX 权重到 `models/`（`MOSS-TTS-Nano-100M-ONNX` + `MOSS-Audio-Tokenizer-Nano-ONNX`），
   跑通一条 CLI：
   ```bash
   moss-tts-nano generate --backend onnx \
     --prompt-speech assets/audio/zh_1.wav \
     --text "老虎英文是 tiger，USB 插在左边。"
   ```
3. 写 `scripts/bench_nano_tts.py`（本阶段唯一产物），测三组数据：
   - 冷/热启动耗时、常驻内存
   - 首批音频延迟（`first_audio`）、RTF（`elapsed / audio_duration`）、标准 vs greedy
   - 中英混读 5 句 + 纯中文 5 句的听感与是否有长停顿
4. **Go/No-Go 闸门**（写进本文件附录表格，留实测数字；口径对齐「不明显卡」）：

| 指标 | Go | No-Go |
|---|---|---|
| 首音（句级，13 字） | ≤ 1000ms，且主观不卡 | > 1200ms，或明显卡住 |
| RTF（4 线程） | ≤ 0.8 | > 1.0 |
| 混读质量 | 英文词发音正确、无长停顿 | 断词/串音/秒级停顿 |

No-Go → 直接跳「第 9 节 备选方案 B（混合路由）」，不要继续往下做。
阶段 0 可用官方 `zh_1`；Go 之后再录 `owner.wav` 做阶段 1。

### 阶段 1：侧车服务（1 天）

- 目录 `tools/nano-tts/`：独立 venv + 模型软链到仓库 `models/`（避免重复占空间）
- `server.py`：FastAPI 包一层 `onnx_tts_runtime`
  - 启动即预加载模型、预编码参考音（参考音编码是一次性的，必须缓存，否则每句多几百 ms）
  - 默认参考音：`refs/owner.wav`（不存在则拒绝启动，打印录制指引）
  - 单 worker + 串行队列（0.1B 模型没必要并发，并发只会互相拖慢）
  - 健康检查 `/health` 返回 `{"ready": true, "voice": "owner", "rtf": 0.55}`
  - 空闲卸载：先不做（本机常驻内存可接受；要省内存就手动 `stop`）
- `scripts/nano_tts_server.sh`：`start|start --bg|stop|status|log`（见 4.5）；**不装 launchd**
- `scripts/nano_tts_record_ref.sh`（可选）：录制/覆盖 `owner.wav`
- 验收：`time curl` 拿到完整 PCM；`stop` 后 jarvis 能回落；`kill -9` 后侧车**保持挂掉**（符合「不开机自启」），jarvis fallback 仍可用

### 阶段 2：`jarvis/tts_nano.py` 客户端（1 天）

- `class NanoTTS`：实现 2.1 的 8 个方法 + `fallback`
- 关键点：
  - `speak()` = 切句 → 后台合成线程 + `queue` → 主线程 `speaker.play(blocking=True)`（照抄 `SherpaTTS.speak` 结构）
  - `stop()`：置位 `_stop` → 关掉 `httpx` 的 stream response（用 `with client.stream(...)`）→ `speaker.stop()`
  - 失败回落：连接拒绝/超时/非 200 → 打一行 `⚠️ nano 不可用，回落 <backend>`，把 **本句** 交给 fallback，并进入 30s 冷却窗口（避免每句都白等 timeout）
  - `warm()`：预渲染 `ACK_PHRASES + SLEEP_REPLY + "我在"` 并缓存（键 = `(text, voice)`）
  - 采样率：直接用返回的 48k，不在这里重采样（speaker 负责）
- 新增自检 `scripts/selftest_nano_tts.py`：
  1. 服务可达 / 不可达两种路径
  2. 首音与 RTF 打印
  3. **中英混读 → ASR 往返，断言英文词仍在**（替代 OOV 检查）
  4. `stop()` 后 300ms 内 `speaking == False`

### 阶段 3：文本层分流（0.5 天）

- `jarvis/textnorm.py`：`normalize_for_tts(text, *, numbers=True, latin=True)`，默认值与今天等价（回归不受影响）
- `SherpaTTS.synthesize` 保持默认调用
- `NanoTTS` 不调用 textnorm；改为极薄的 `clean_for_speech(text)`：去 markdown 符号 / emoji / 多余括号
  （顺带修一个现存小问题：模型偶尔输出 `**加粗**`，VITS 会把星号念成停顿）
- `scripts/selftest_tts_text.py` 追加断言：`normalize_for_tts("老虎英文是 tiger", latin=False) == 原文`

### 阶段 4：装配与开关（0.5 天）

- `build_local_tts()` 加分支：

```python
if backend in ("nano", "nano-tts"):
    from .tts_nano import NanoTTS, probe_nano
    if not probe_nano(settings.tts_nano_url):        # /health 200ms 探测
        print("⚠️  nano 侧车未就绪，回落本地 VITS", file=sys.stderr)
        return _build_sherpa(settings, speaker)      # 抽出的原分支
    return NanoTTS(
        base_url=settings.tts_nano_url,
        ref=settings.tts_nano_ref_audio,
        stream=settings.tts_nano_stream,
        timeout=settings.tts_nano_timeout,
        speaker=speaker,
        fallback=_build_sherpa(settings, speaker),
    )
```

- `.env` 新增（见第 7 节）；`.env.example` 同步加带注释的默认值
- 启动日志的 `音色 / TTS mode` 自动显示 `nano(owner)`，便于事后确认跑的是哪个后端
- **默认不动**：`TTS_BACKEND` 仍是 `neural`，验证通过后人工切 `nano`
- 使用顺序约定：先 `./scripts/nano_tts_server.sh start --bg`，再起 jarvis；侧车没起时探测失败自动回落 VITS

### 阶段 5：回归与验收（0.5~1 天）

按 `.agents/skills/home-jarvis-voice-loop-regression` 走：

1. `./scripts/selftest_all.sh`（含新增 `selftest_nano_tts.py`）
2. `selftest_tts_text.py`：数字用例不变 + 新增 latin 分流用例
3. `selftest_speech.py`：把 CASES 扩一条中英混读（`老虎英文是 tiger`），CER 看英文段
4. 真机：S12 端点 `--wake` 跑 10 轮，检查
   - 唤醒蓝灯 → 思考蓝呼吸 → 播报蓝 → 休眠熄灭 的时序没被 TTS 拖慢（LED 由 app 控制，不改）
   - 「好的」先出声是否仍低延迟（nano 的预渲染命中率）
   - `--barge-in` 下打断能否在 500ms 内静音
5. A/B 试听：同一句 `老虎英文是 tiger，USB 插在左边`，VITS vs Nano，`scripts/cue.py` 弹窗记录偏好

### 阶段 6（可选）：chunk 级流式

只在前 5 阶段全绿且首音仍 > 800ms 时做。需要给播放器加 `open_stream/write/close`，
并把 `stop()` 的语义改成「关流 + FLUSH」，范围涉及 `audio.py` / `endpoint.py`，回归面明显变大。

---

## 6. 代码骨架（供评审，不直接落盘）

```python
# jarvis/tts_nano.py
class NanoTTS:
    """MOSS-TTS-Nano 侧车客户端：接口与 SherpaTTS/SayTTS 对齐。"""

    def __init__(self, base_url, ref, *, stream=True, timeout=8.0,
                 speaker=None, fallback=None, speed=1.0):
        self.base_url, self.ref = base_url.rstrip("/"), ref
        self._client = httpx.Client(timeout=timeout)
        self._speaker, self.fallback = speaker, fallback
        self._stop, self._thread = threading.Event(), None
        self._cool_until = 0.0                      # 失败冷却，避免每句都白等
        self.voice, self.mode = f"nano({Path(ref).stem})", "nano"  # 预期 nano(owner)

    def synthesize(self, text) -> tuple[np.ndarray, int]:
        payload = {"input": clean_for_speech(text), "ref_audio": self._ref_b64(),
                   "response_format": "pcm", "stream": False, "speed": self.speed}
        r = self._client.post(f"{self.base_url}/v1/audio/speech", json=payload)
        r.raise_for_status()
        return np.frombuffer(r.content, dtype="<i2").astype(np.float32) / 32768.0, 48000

    def speak(self, text, blocking=True):
        # 结构照抄 SherpaTTS.speak：切句 → worker 合成 queue → 主线程 speaker.play
        ...
```

```python
# scripts/selftest_nano_tts.py 的核心断言
hyp, _ = asr.transcribe(tts.synthesize("老虎英文是 tiger。")[0])
assert "tiger" in hyp.lower(), f"英文词被丢了：{hyp}"
```

---

## 7. 配置项

| key | 默认 | 说明 |
|---|---|---|
| `TTS_BACKEND` | `neural` | 新增可选值 `nano`（验证通过后再切） |
| `TTS_NANO_URL` | `http://127.0.0.1:8091` | 侧车地址 |
| `TTS_NANO_REF_AUDIO` | `tools/nano-tts/refs/owner.wav` | 音色 = 自录克隆源；换人只要换这个文件 |
| `TTS_NANO_STREAM` | `true` | 侧车 chunked；false 走整段（兼容不支持流式的包装） |
| `TTS_NANO_TIMEOUT` | `8` | 单句超时，超时即回落 |
| `TTS_NANO_SPEED` | `1.0` | Nano 的语速参数（与 VITS 的 1.35 不是一个刻度，需单独听） |
| `TTS_NANO_FALLBACK` | `neural` | 回落后端（`neural` / `say` / `none`） |
| `TTS_NANO_THREADS` | `4` | 侧车 ORT 线程数 |

---

## 8. 风险与对策

| 风险 | 影响 | 对策 |
|---|---|---|
| CPU 首音/RTF 不达标 | 交互变卡，比"念错词"更难受 | 阶段 0 闸门；greedy 采样 + 4~8 线程；不行走混合路由（第 9 节） |
| 中英切换长停顿（社区已报） | 一句话中间断 1s+ | 按语言边界切句，中英各成一句分别合成；不行则降级到混合路由 |
| 参考音决定音色，且克隆稳定性 | 音色/情绪不稳 | 严格按 4.4 录制；固定 `owner.wav`；录坏了就重录，不要混用多段 |
| 依赖冲突（onnxruntime / numpy / tokenizers） | 主项目 sherpa-onnx 挂掉 | 强制独立 venv + 侧车，主 venv 不新增依赖 |
| 常驻内存（模型 + 会话） | Mac 上多占 0.5~1GB | **手动**启停侧车；不用时 `stop`；不装开机自启 |
| 打断语义 | 关流后设备残留缓冲 | `stop()` 必须「关 response → speaker.stop()（触发 FLUSH）」，barge-in 自检覆盖 |
| 首句预渲染失效 | 「好的」从 0ms 变 50~200ms | `warm()` 缓存 + 侧车预热；端点模式下阻塞播 |
| 侧车静默失败 | 用户以为"没反应" | `/health` 探测 + 滚动日志 + 失败一次性告警（不要刷屏） |

---

## 9. 备选方案 B：混合路由（阶段 0 No-Go 时启用）

不改整体架构，只在 `SpeechRouter` 前加一层 `TtsRouter`：

```
纯中文、无数字 → SherpaTTS（496ms，已验证）
含拉丁词 / 含英文句子 → NanoTTS（发音正确，慢一点可接受）
Nano 不可用 → 回落 SherpaTTS + 字母读名（今天的 textnorm 行为）
```

- 优点：**延迟只在真正需要英文的句子上付**，纯中文对话体验零回退；实现只多 40 行判定
- 缺点：同一段回答里音色会切换（需要接受，或者整段按"是否含英文"统一选后端）
- 判定用现成的 `_LATIN_WORD` 正则即可：`re.search(r"[A-Za-z]{2,}", text)`

---

## 10. 回滚

- 运行期：`.env` 改 `TTS_BACKEND=neural` → 重启 → 完全恢复今天的路径（Nano 代码不在关键路径上）
- 崩溃期：`NanoTTS` 自带 fallback，侧车挂掉自动回落，不需人工介入
- 代码期：阶段 2~3 的改动都在新文件 + `build_local_tts` 一个分支 + `textnorm` 一个关键字参数，`git revert` 单个 commit 即可

---

## 11. 任务清单

**阶段 0（先行，可单独验收；可用官方 zh_1 冒烟）**
- [x] `tools/nano-tts/` 独立 venv + 上游源码 + 依赖安装（阶段 0 禁用 WeText，避开 pynini；需 `torchcodec` 配新版 torchaudio）
- [x] 下载 ONNX 权重（ModelScope：`openmoss/MOSS-TTS-Nano-100M-ONNX` + `...Tokenizer-Nano-ONNX`，约 660MB+87MB）
- [x] `infer_onnx.py` 跑通中文/英文/混读三句（`--sample-mode fixed`）
- [x] `scripts/bench_nano_tts.py`：冷热启动、首音、RTF（fixed vs greedy）、内存
- [x] 附录已填：**Go（用 fixed，不用 greedy）** — 2026-11-03 Apple M4

**阶段 1（侧车 + 自录克隆）**
- [ ] 按 4.4 录制 `tools/nano-tts/refs/owner.wav`（`./scripts/nano_tts_record_ref.sh`）——**待你本地录**
- [x] `tools/nano-tts/server.py`（FastAPI + 预加载 + 参考音编码缓存 + `/health`）
- [x] `scripts/nano_tts_server.sh start|start --bg|stop|status|log`（**无 launchd**）
- [x] 验收：curl PCM；`NANO_TTS_REF=zh_1` 冒烟通过；stop 后不自动复活

**阶段 2（客户端）**
- [x] `jarvis/tts_nano.py`（8 方法 + fallback + 冷却）
- [x] `scripts/selftest_nano_tts.py`（含中英混读 ASR 断言）
- [x] `stop()` 后 `speaking=False`（自检覆盖）

**阶段 3（文本层）**
- [x] `textnorm.normalize_for_tts(..., latin=)` 分流 + `--pure` 全绿
- [x] `clean_for_speech()`（markdown/emoji）

**阶段 4（装配）**
- [x] `build_local_tts` 加 `nano` 分支（抽出 `_build_sherpa`）
- [x] `config.py` + `.env.example` 新增 Nano key（默认 `TTS_NANO_REF_AUDIO=.../owner.wav`）
- [x] 启动日志：`tts.voice` / `tts.mode` 显示 `nano(...)`（沿用现有打印）

**阶段 5（回归）**
- [x] `./scripts/selftest_all.sh`：除「模型断言·闲聊误触 weather」外全绿（与 Nano 无关，既有问题）
- [x] `selftest_speech.py` 增中英混读；Nano 硬断言 `tiger` 保留；`selftest_nano_tts` 已接入 all
- [x] S12 真机：`TTS_BACKEND=nano --wake --endpoint --barge-in` 多轮唤醒 + 播报 + 打断（见附录阶段 5）
- [x] A/B 试听：偏好 **Nano**（`tools/nano-tts/bench_out/ab_preference.txt`）
- [x] 文档收尾：`docs/TTS-选型与实测.md` + `README.md` TTS 表已加 nano

---

## 12. 已拍板事项

| # | 问题 | 决定 |
|---|---|---|
| 1 | nano 指哪一个 | **开源 MOSS-TTS-Nano**（本地 ONNX） |
| 2 | 首音延迟 | **不明显卡即可**；可接受 ≤1000ms；>1200ms / RTF>1.0 → 混合路由 |
| 3 | 音色 | **官方 `zh_1`（已改回）**；自录 `owner.wav` 仍可用 `NANO_TTS_REF` 覆盖 |
| 4 | 侧车进程 | **常驻侧车可以**；**不开机自启**；项目内 `scripts/nano_tts_server.sh` 手动起停 |

后续若要改其中任一项，先改本节再改实现。

---

## 附录：阶段 0 实测记录

> 机器：Apple M4 / 25GB · 日期：2026-11-03 · 参考音：官方 `zh_1.wav` · 线程：4 ·
> 产物：`tools/nano-tts/bench_out/bench_{fixed,greedy}.json` · 脚本：`scripts/bench_nano_tts.py`
>
> **签字：Go** —— 采用 `sample_mode=fixed` 全量推进阶段 1；**禁止默认 greedy**（截断/跑满 max_frames）。

| 项 | fixed（标准） | greedy | 备注 |
|---|---|---|---|
| 模型加载 | **3941 ms** | 3437 ms | 侧车启动一次即可 |
| 参考音编码 | **1142 ms** | 1206 ms | 必须缓存，禁止每句重算 |
| 常驻内存（热） | **~2.4 GB** RSS | ~2.2 GB | 高于计划预估 0.5~1GB，可接受（手动 stop） |
| 流式首包（13 字中文） | **~82 ms** | ~121 ms | chunk 级；侧车 stream 时用 |
| 句级完成（13 字中文） | **~953 ms** | ~3967 ms | 对齐当前 jarvis 句级流水；**≤1000ms → Go** |
| 句级完成（中英混读） | **~1421 ms** | ~9856 ms（跑满） | 略超 1.2s 闸门，但 RTF 健康、主观不卡；混读可接受 |
| RTF（按有效语音时长） | **~0.30~0.35** | 常 >3 / 截断 | **≤0.8 → Go**；greedy 基本不可用 |
| 中英切换停顿 | 听感无明显秒级停顿 | 差（截断/灌静音） | fixed 下混读一口气说完 |
| 英文词 ASR 回测 | **通过** | — | SenseVoice：`老虎英文是tigerUSB插在左边。` 含 tiger/USB |
| 主观「是否卡」 | **不明显卡** | 明显慢且内容坏 | 句级 ~1s 与现 VITS ~0.5s 同量级可感但可接受 |

### 阶段 5 回归摘要（2026-11-04）

| 项 | 结果 |
|---|---|
| `selftest_tts_text.py --pure` | 全绿（含 `latin=False`） |
| `selftest_nano_tts.py` | 全绿；ASR=`老虎英文是tiger。` |
| `selftest_speech.py` | 基线 CER=0；nano 混读硬断言通过；say 混读仅告警 |
| `selftest_all.sh` | 11 过 / 1 败：`selftest_llm`「今天适合跑步吗」误调 weather（非 TTS） |
| A/B | 同句 VITS vs Nano，偏好记录为 **nano** |
| S12 | `音色 nano(zh_1)`；KWS 唤醒→「我在」；混读问答播报；`✋ 被打断` 生效；预渲染 5 条 |

### 阶段 0 落地备忘（给阶段 1）

1. 默认采样：**`fixed`**，不要 `greedy`
2. 权重源优先 ModelScope（国内）；HF 作备选
3. 新版 `torchaudio` 需额外装 `torchcodec` 才能 `load` wav
4. 阶段 0 未装 WeTextProcessing；侧车可后加，失败则 `--disable-wetext-processing`
5. 长句偶发跑到 `max_new_frames`（见 `zh_long` 一次 16s）——侧车需设合理上限 + 按有效语音裁剪尾静音
6. 输出 48kHz **双声道**；jarvis speaker 已会重采样，注意取 mono（`mean`）再送 S12