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
| 能不能解决问题 | **能**。Nano 类模型（Audio Tokenizer + LLM）是字符级多语种建模，中英混读是原生能力，不靠拼音词典，因此没有 OOV 丢词问题 |
| 推荐架构 | **独立进程做侧车服务（HTTP）+ jarvis 新增 `NanoTTS` 适配器**，不把模型塞进主进程 |
| 主要风险 | 首音延迟（当前 VITS 约 496ms/13 字）与 CPU RTF；中英切换偶发长停顿 |
| 关键决策点 | **阶段 0 的实测 Go/No-Go**：首音 > 1.2s 或 RTF > 1.0 就不做全量替换，改走「混合路由」备选方案 B |
| 工期 | 阶段 0~5 合计约 **3~4 人日**，其中阶段 0 半天到一天，可单独验收 |
| 代码改动面 | 新增 2 个文件（`jarvis/tts_nano.py`、`scripts/nano_tts_server.sh`），改 4 处（`tts_neural.build_local_tts`、`textnorm` 分流、`config`、`.env.example`） |

---

## 1. 前置确认：这里的 "nano TTS" 指哪一个

不同实现决定完全不同的方案，先对齐。默认按 **A** 写本计划。

| | A. MOSS-TTS-Nano（本计划假设） | B. 豆包 seed-tts-2.0 等云 TTS |
|---|---|---|
| 形态 | 开源 0.1B，**本地 CPU 可跑**，ONNX 版无需 PyTorch | 云端 HTTP/WS 流式接口 |
| 中英混读 | 原生支持（20 语种） | 原生支持 |
| 音色 | **必须给参考音**（`ref_audio` 声音克隆），无内置 preset* | 官方音色 ID 直接选 |
| 延迟 | 服务端 RTF ~0.5~1.15（CPU 差异大），首包 0.1~1.4s | 首包约 200~400ms |
| 成本 | 0（只吃 CPU/内存） | 按字符计费 |
| 落地工作量 | 需要部署、听感调优、可能要做混合路由 | **只加一个 HTTP backend**，阶段 0~1 可砍掉一半 |

\* 社区包装（如 `MOSS-TTS-Nano-API`、vLLM-Omni）提供 `zh_1` 这类 preset，本质是内置的参考音文件映射，
所以「选音色」=「选一个参考 wav」。

**一句话判别**：接口要不要 `ref_audio`（base64 参考音）？要 → A；只要 `voice: "xxx"` → B。

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
| 首音延迟 | 句级流水下 **≤ 700ms（目标）/ ≤ 1000ms（可接受）**（11~13 字典型句，S12 端点） | 计时日志 `🔊 播报 xxxms` + 新增「首音 ms」 |
| RTF | ≤ 0.6（流式）/ ≤ 0.8（句级） | 合成耗时 ÷ 音频时长，连续 20 句 |
| 稳定性 | 侧车崩了 **≤ 3s 回落 VITS**，只听到音色变化 | 手动 kill 服务后连说 3 句 |
| 回归 | `./scripts/selftest_all.sh` 全绿；`selftest_tts_text.py`、`selftest_speech.py`、`selftest_chain.py` 无新增失败 | 脚本 |
| 听感 | 与 VITS A/B，中英混读句明显更好，纯中文句不劣化 | 人工试听（cue.py 弹窗对照） |

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
                                                   参考音 refs/zh_1.wav（音色即克隆源）
```

请求契约（对齐 OpenAI 兼容 + 上游参数）：

```jsonc
POST /v1/audio/speech
{
  "input": "老虎英文是 tiger。",
  "ref_audio": "<base64 或 refs/zh_1.wav 的 id>",
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
4. **Go/No-Go 闸门**（写进本文件附录表格，留实测数字）：

| 指标 | Go | No-Go |
|---|---|---|
| 首音（句级，13 字） | ≤ 1000ms | > 1200ms |
| RTF（4 线程） | ≤ 0.8 | > 1.0 |
| 混读质量 | 英文词发音正确、无长停顿 | 断词/串音/秒级停顿 |

No-Go → 直接跳「第 9 节 备选方案 B（混合路由）」，不要继续往下做。

### 阶段 1：侧车服务（1 天）

- 目录 `tools/nano-tts/`：独立 venv + 模型软链到仓库 `models/`（避免重复占空间）
- `server.py`：FastAPI 包一层 `onnx_tts_runtime`
  - 启动即预加载模型、预编码参考音（参考音编码是一次性的，必须缓存，否则每句多几百 ms）
  - 单 worker + 串行队列（0.1B 模型没必要并发，并发只会互相拖慢）
  - 健康检查 `/health` 返回 `{"ready": true, "voice": "zh_1", "rtf": 0.55}`
  - 空闲卸载：`IDLE_UNLOAD_S=600` 后释放会话，下次请求懒加载（可选，M4 上先别做）
- `scripts/nano_tts_server.sh`：起/停/看日志；`--daemon` 用 launchd plist 常驻
- 验收：`time curl` 一条请求拿到完整 PCM；`kill -9` 后能自动重新拉起

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
- 启动日志的 `音色 / TTS mode` 自动显示 `nano(zh_1)`，便于事后确认跑的是哪个后端
- **默认不动**：`TTS_BACKEND` 仍是 `neural`，验证通过后人工切 `nano`

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
        self.voice, self.mode = f"nano({Path(ref).stem})", "nano"

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
| `TTS_NANO_REF_AUDIO` | `tools/nano-tts/refs/zh_1.wav` | 音色 = 参考音；换人只要换这个文件 |
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
| 参考音决定音色，且克隆稳定性 | 音色/情绪不稳 | 固定一段 8~10s 干净中文参考音；提示词文本不要过长；必要时自己录 |
| 依赖冲突（onnxruntime / numpy / tokenizers） | 主项目 sherpa-onnx 挂掉 | 强制独立 venv + 侧车，主 venv 不新增依赖 |
| 常驻内存（模型 + 会话） | Mac 上多占 0.5~1GB | launchd 常驻 + 空闲卸载；先跟 ASR/LLM 一起看总内存 |
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

**阶段 0（先行，可单独验收）**
- [ ] `tools/nano-tts/` 独立 venv + 仓库克隆 + 依赖安装（记录 pynini/WeTextProcessing 是否踩坑）
- [ ] 下载 ONNX 权重（两个 HF 仓库），校验文件大小
- [ ] `moss-tts-nano generate --backend onnx` 跑通中文/英文/混读三句
- [ ] `scripts/bench_nano_tts.py`：冷热启动、首音、RTF（标准 vs greedy）、内存
- [ ] 把实测数字填进本文档附录，签字 Go / No-Go

**阶段 1（侧车）**
- [ ] `tools/nano-tts/server.py`（FastAPI + 预加载 + 参考音编码缓存 + `/health`）
- [ ] `scripts/nano_tts_server.sh start|stop|log|health`
- [ ] `kill -9` 拉起演练

**阶段 2（客户端）**
- [ ] `jarvis/tts_nano.py`（8 方法 + fallback + 冷却）
- [ ] `scripts/selftest_nano_tts.py`（含中英混读 ASR 断言）
- [ ] 打断测试：`stop()` 后 300ms 静音

**阶段 3（文本层）**
- [ ] `textnorm.normalize_for_tts(..., latin=)` 分流 + 既有用例全绿
- [ ] `clean_for_speech()`（markdown/emoji）

**阶段 4（装配）**
- [ ] `build_local_tts` 加 `nano` 分支（抽出 `_build_sherpa`）
- [ ] `config.py` + `.env.example` 新增 8 个 key
- [ ] 启动日志显示实际后端

**阶段 5（回归）**
- [ ] `./scripts/selftest_all.sh` 全绿
- [ ] `selftest_speech.py` 增中英混读用例
- [ ] S12 真机 10 轮 + barge-in + LED 时序
- [ ] A/B 试听记录
- [ ] 文档收尾：`docs/TTS-选型与实测.md` 追加 Nano 实测行，`README.md` 的 TTS 配置表加 `nano`

---

## 12. 需要拍板的 4 件事

1. **nano 指哪一个**（MOSS 本地 ONNX / 豆包云 / 其他）——决定第 1 节走 A 还是 B
2. **能接受多大首音延迟**（目标 700ms？还是"只要比 VITS 不明显慢就行"）
3. **音色**：先用官方参考音 `zh_1`，还是录你自己的 10s 做克隆
4. **是否允许一个常驻侧车进程**（内存 +0.5~1GB，开机自启）

---

## 附录：阶段 0 实测记录（待填）

| 项 | 标准模式 | greedy | 备注 |
|---|---|---|---|
| 模型加载 | | | |
| 常驻内存 | | | |
| 首音（13 字中文） | | | |
| 首音（中英混读） | | | |
| RTF（13 字 / 55 字） | | | |
| 中英切换停顿 | | | |
| 英文词正确率（ASR 回测 10 句） | | | |