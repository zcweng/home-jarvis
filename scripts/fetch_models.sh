#!/usr/bin/env bash
# 下载 sherpa-onnx 模型到 models/（可重复执行，已存在则跳过）
#
#   ./scripts/fetch_models.sh                # 默认下 SenseVoice（中文 ASR）
#   ./scripts/fetch_models.sh silero-vad     # VAD 断句
#   ./scripts/fetch_models.sh kws            # 专用唤醒词模型（13MB，走 gh-proxy 镜像）
#   ./scripts/fetch_models.sh tts            # 中文神经 TTS（30MB 8k + 63MB 22k）
#
# 模型来源：HuggingFace（用 hf-mirror.com 加速，失败回落官方源）
#   SenseVoice int8 ≈ 228MB + tokens.txt 316KB
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
MODELS="$ROOT/models"
mkdir -p "$MODELS"

HF_MIRROR="https://hf-mirror.com"
HF_OFFICIAL="https://huggingface.co"

fetch() {
  local repo="$1" file="$2" dest="$3" min_size="${4:-1000}"
  if [[ -f "$dest" ]]; then
    local size
    size=$(wc -c <"$dest" | tr -d ' ')
    if [[ "$size" -ge "$min_size" ]]; then
      echo "✅ 已存在，跳过：$dest ($(du -h "$dest" | cut -f1))"
      return 0
    fi
    echo "⚠️  文件过小（$size B），重新下载：$dest"
    rm -f "$dest"
  fi
  for base in "$HF_MIRROR" "$HF_OFFICIAL"; do
    local url="$base/$repo/resolve/main/$file"
    echo "⬇️  $url"
    if curl -fL --retry 3 --retry-delay 2 -C - --connect-timeout 15 \
        -o "$dest.part" "$url"; then
      mv "$dest.part" "$dest"
      local size
      size=$(wc -c <"$dest" | tr -d ' ')
      if [[ "$size" -lt "$min_size" ]]; then
        echo "❌ 下载不完整（$size B < $min_size B）"
        rm -f "$dest"
        continue
      fi
      echo "✅ $dest ($(du -h "$dest" | cut -f1))"
      return 0
    fi
    echo "⚠️  源不可用，换下一个"
    rm -f "$dest.part"
  done
  echo "❌ 下载失败：$file"
  return 1
}

sense_voice() {
  local dir="$MODELS/sensevoice"
  mkdir -p "$dir"
  local repo="csukuangfj/sherpa-onnx-sense-voice-zh-en-ja-ko-yue-2024-07-17"
  fetch "$repo" "model.int8.onnx" "$dir/model.int8.onnx" 200000000
  fetch "$repo" "tokens.txt" "$dir/tokens.txt" 100000
  ls -lh "$dir"
}

silero_vad() {
  local dir="$MODELS/silero-vad"
  mkdir -p "$dir"
  # 模型在 sherpa-onnx 的 GitHub release 里（643KB），HuggingFace 上没有同名仓库
  local out="$dir/silero_vad.onnx"
  if [[ -f "$out" ]] && [[ $(wc -c <"$out") -gt 500000 ]]; then
    echo "✅ 已存在，跳过：$out"; return 0
  fi
  echo "⬇️  https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/silero_vad.onnx"
  curl -fL --retry 3 -C - --connect-timeout 15 -o "$out.part" \
    "https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/silero_vad.onnx" || {
    rm -f "$out.part"; echo "❌ 下载失败"; return 1; }
  mv "$out.part" "$out"
  echo "✅ $out ($(du -h "$out" | cut -f1))"
}

tts() {
  # 中文神经 TTS（VITS）。体积最小的 8k 模型 + 更自然但更大的 22k 模型
  local base="https://github.com/k2-fsa/sherpa-onnx/releases/download/tts-models"
  local want="${2:-all}"
  for name in vits-icefall-zh-aishell3; do
    local dir="$MODELS/$name"
    if [[ -f "$dir/model.onnx" ]]; then echo "✅ 已存在，跳过：$name"; continue; fi
    echo "⬇️  $name（中文 VITS，8kHz，约 30MB）"
    curl -fL --retry 3 --connect-timeout 15 -m 900 -o /tmp/tts.tar.bz2 "https://gh-proxy.com/$base/$name.tar.bz2" \
      && tar xjf /tmp/tts.tar.bz2 -C "$MODELS" && echo "✅ $dir"
  done
  if [[ "$want" == "all" || "$want" == "16k" ]]; then
    # 注意：只能用带 lexicon.txt 的 VITS（aishell3 / fanchen-C）。
    # piper 系列（vits-piper-*）依赖 espeak 前端，sherpa-onnx 2.0.0 的 macOS wheel 没编进去。
    local name="vits-zh-hf-fanchen-C"
    local dir="$MODELS/$name"
    if [[ -f "$dir/$name.onnx" ]]; then
      echo "✅ 已存在，跳过：$name"
    else
      echo "⬇️  $name（中文 VITS，16kHz，约 121MB）"
      mkdir -p "$dir"
      curl -fL --retry 3 --connect-timeout 15 -m 1800 -o "$dir/$name.onnx" \
        "https://hf-mirror.com/csukuangfj/$name/resolve/main/$name.onnx" || return 1
      for extra in lexicon.txt tokens.txt date.fst number.fst phone.fst new_heteronym.fst G_C.json; do
        curl -fL -m 120 -o "$dir/$extra" "https://hf-mirror.com/csukuangfj/$name/resolve/main/$extra" || true
      done
      echo "✅ $dir"
    fi
  fi
}

kws() {
  local dir="$MODELS/sherpa-onnx-kws-zipformer-wenetspeech-3.3M-2024-01-01"
  local name="sherpa-onnx-kws-zipformer-wenetspeech-3.3M-2024-01-01"
  if [[ -f "$dir/tokens.txt" ]]; then
    echo "✅ 已存在，跳过：$dir"; return 0
  fi
  local url="https://github.com/k2-fsa/sherpa-onnx/releases/download/kws-models/${name}.tar.bz2"
  # GitHub release 直连在国内经常只有几十 KB/s，优先走 gh-proxy 镜像
  for base in "https://gh-proxy.com/" "" ; do
    echo "⬇️  ${base}${url}"
    if curl -fL --retry 3 --connect-timeout 15 -m 600 -o /tmp/kws.tar.bz2 "${base}${url}"; then
      tar xjf /tmp/kws.tar.bz2 -C "$MODELS" && echo "✅ $dir" && return 0
    fi
    echo "⚠️  该源失败，换下一个"
  done
  echo "❌ 下载失败"; return 1
}

memory_embed() {
  # 中文检索向量：bge-small-zh-v1.5（ONNX + tokenizer）
  # 优先用 Xenova 转换的 onnx；缺省时提示用户自行导出
  local dir="$MODELS/bge-small-zh-v1.5"
  mkdir -p "$dir"
  local repo="Xenova/bge-small-zh-v1.5"
  # Xenova 仓库结构：onnx/model.onnx + tokenizer.json + config.json
  if [[ -f "$dir/model.onnx" || -f "$dir/onnx/model.onnx" ]] && [[ -f "$dir/tokenizer.json" ]]; then
    echo "✅ 已存在，跳过：$dir"
    return 0
  fi
  fetch "$repo" "onnx/model.onnx" "$dir/model.onnx" 20000000 || \
    fetch "$repo" "onnx/model_O4.onnx" "$dir/model.onnx" 5000000 || true
  fetch "$repo" "tokenizer.json" "$dir/tokenizer.json" 10000 || true
  fetch "$repo" "config.json" "$dir/config.json" 100 || true
  fetch "$repo" "tokenizer_config.json" "$dir/tokenizer_config.json" 50 || true
  if [[ ! -f "$dir/model.onnx" ]]; then
    echo "⚠️  bge-small-zh ONNX 未下到，记忆将降级为纯关键词检索"
    echo "   可手动：huggingface-cli download Xenova/bge-small-zh-v1.5 --local-dir $dir"
  else
    ls -lh "$dir"
  fi
}

case "${1:-all}" in
  all)
    sense_voice
    silero_vad
    kws
    tts
    ;;
  sensevoice) sense_voice ;;
  kws) kws ;;
  tts) shift || true; tts "$@" ;;
  silero-vad | vad) silero_vad ;;
  memory | embed | bge) memory_embed ;;
  *)
    echo "用法：$0 [all|sensevoice|silero-vad|kws|tts|memory]"
    exit 1
    ;;
esac
