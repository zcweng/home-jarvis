#!/usr/bin/env bash
# 一键回归：按"硬件 → 语音 → 设备 → 模型 → 全链路"的顺序跑所有自检。
#
#   ./scripts/selftest_all.sh            # 跑全部（会调真实模型、会开关真实设备、会出声）
#   ./scripts/selftest_all.sh --fast     # 跳过真实模型/真设备/出声的项（夜里就该用这个）
#
# 出问题时从下往上查：链路失败 → 先看工具层 → 再看模型层 → 最后看语音层。
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
PY=".venv/bin/python"
FAST=0
[[ "${1:-}" == "--fast" ]] && FAST=1

pass=0
fail=0
failed_names=()

run() {
  local name="$1"
  shift
  printf '\n\033[1m▶ %s\033[0m\n' "$name"
  if "$@" >/tmp/selftest_out.txt 2>&1; then
    printf '\033[32m  ✅ %s\033[0m\n' "$name"
    pass=$((pass + 1))
  else
    printf '\033[31m  ❌ %s\033[0m\n' "$name"
    tail -12 /tmp/selftest_out.txt | sed 's/^/     /'
    fail=$((fail + 1))
    failed_names+=("$name")
  fi
}

run "环境自检"            $PY scripts/preflight.py
run "音频层"              $PY scripts/selftest_audio.py
run "打断检测"            $PY scripts/selftest_bargein.py
run "唤醒词 + 首句先出声"  $PY scripts/selftest_wake.py
run "语音输出路由"        $PY scripts/selftest_output.py
run "TTS 数字归一化"      $PY scripts/selftest_tts_text.py
run "TTS 热切换"          $PY scripts/selftest_tts_switch.py
run "Nano TTS 侧车"       $PY scripts/selftest_nano_tts.py
run "KWS 专用唤醒词"      $PY scripts/selftest_kws.py
run "设备工具层"          $PY scripts/selftest_tools.py
run "本地时间工具"        $PY scripts/selftest_get_time.py
run "网络音频端点"        $PY scripts/selftest_endpoint.py
run "Agents 运行时"       $PY scripts/selftest_agent_runtime.py
run "多任务编排"          $PY scripts/selftest_tasks.py

if [[ $FAST -eq 0 ]]; then
  run "多任务编排（真模型+真设备）" $PY scripts/selftest_tasks.py --real
  run "语音合成/识别往返"  $PY scripts/selftest_speech.py
  run "模型断言"          $PY scripts/selftest_llm.py
  run "全链路"            $PY scripts/selftest_chain.py --real-llm
fi

printf '\n%s\n' "────────────────────────────────────────────"
printf '通过 %d · 失败 %d\n' "$pass" "$fail"
if [[ $fail -gt 0 ]]; then
  printf '失败项：%s\n' "${failed_names[*]}"
  exit 1
fi
printf '全部通过 🎉\n'
