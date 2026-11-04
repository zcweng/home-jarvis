#!/usr/bin/env bash
# 录制 Nano TTS 自克隆参考音 → tools/nano-tts/refs/owner.wav
# 要求：8~12 秒、安静、自然中文（可带 1~2 个英文词）。
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
OUT_DIR="$ROOT/tools/nano-tts/refs"
OUT="$OUT_DIR/owner.wav"
DUR="${1:-10}"
PY="${ROOT}/.venv/bin/python"

if [[ ! -x "$PY" ]]; then
  echo "❌ 需要项目 .venv（sounddevice）：uv sync" >&2
  exit 1
fi

mkdir -p "$OUT_DIR"
echo "将录制 ${DUR}s → $OUT"
echo "建议朗读：今天天气不错，请把客厅的 Wi-Fi 打开，USB 插在左边。"
echo "3 秒后开始…"
sleep 1; echo 2; sleep 1; echo 1; sleep 1; echo "开始！"

"$PY" - <<PY
from pathlib import Path
import numpy as np
import sounddevice as sd
import wave

dur = float("${DUR}")
sr = 48000
print(f"recording {dur}s @ {sr}Hz …", flush=True)
audio = sd.rec(int(dur * sr), samplerate=sr, channels=1, dtype="float32")
sd.wait()
mono = audio.reshape(-1).astype(np.float32)

# 裁掉首尾静音——尾部大段静音会进克隆编码，音色会被冲淡
peak = float(np.max(np.abs(mono)) + 1e-12)
thr = max(0.02 * peak, 0.008)
active = np.where(np.abs(mono) > thr)[0]
if active.size == 0:
    raise SystemExit("❌ 几乎没有检测到语音，请靠近麦克风重录")
pad = int(0.15 * sr)
start = max(0, int(active[0]) - pad)
end = min(mono.size, int(active[-1]) + 1 + pad)
mono = mono[start:end]
speech_s = mono.size / sr
if speech_s < 5.0:
    print(f"⚠️  有效语音只有 {speech_s:.1f}s（建议 8~12s），克隆会偏弱，建议重录更长一段", flush=True)
elif speech_s > 15.0:
    print(f"⚠️  有效语音 {speech_s:.1f}s 偏长，将影响首包延迟", flush=True)

peak = float(np.max(np.abs(mono)) + 1e-12)
gain = min(0.7 / peak, 8.0)
mono = np.clip(mono * gain, -1.0, 1.0)

out = Path("${OUT}")
tmp = out.with_suffix(".tmp.wav")
with wave.open(str(tmp), "wb") as w:
    w.setnchannels(1)
    w.setsampwidth(2)
    w.setframerate(sr)
    w.writeframes((mono * 32767.0).astype("<i2").tobytes())
tmp.replace(out)
print(f"saved {out} speech={speech_s:.2f}s peak_in={peak:.3f} gain={gain:.2f}", flush=True)
PY

echo "试听参考音本身…"
if command -v afplay >/dev/null; then
  afplay "$OUT" || true
fi
echo "完成。务必重启侧车才能换音色："
echo "  ./scripts/nano_tts_server.sh stop && ./scripts/nano_tts_server.sh start --bg"
