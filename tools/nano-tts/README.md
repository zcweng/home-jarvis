# nano-tts 侧车

[MOSS-TTS-Nano](https://github.com/OpenMOSS/MOSS-TTS-Nano) 独立环境 + HTTP 服务，供 jarvis `TTS_BACKEND=nano` 调用。

## 日常用法

```bash
# 1) 启动侧车（默认官方 zh_1；不开机自启）
./scripts/nano_tts_server.sh start --bg
./scripts/nano_tts_server.sh status

# 2) jarvis .env
#    TTS_BACKEND=nano

# 3) 不用时停掉省内存
./scripts/nano_tts_server.sh stop
```

可选自录克隆：

```bash
./scripts/nano_tts_record_ref.sh
NANO_TTS_REF=tools/nano-tts/refs/owner.wav ./scripts/nano_tts_server.sh start --bg
```

## 布局

```
tools/nano-tts/
  server.py              # FastAPI 侧车
  .venv/                 # 独立 Python 3.12
  MOSS-TTS-Nano/         # 上游源码（含 assets/audio/zh_1.wav）
  refs/owner.wav         # 可选自录克隆
  nano-tts.pid/.log      # 后台进程
  bench_out/             # 阶段 0 基准产物
```

## API

- `GET /health` → `{ready, voice, rtf, …}`
- `POST /v1/audio/speech` JSON `{input, response_format:pcm|wav, stream, speed}` → 48kHz mono PCM/WAV

详见 `docs/接入Nano-TTS改造计划.md`。
