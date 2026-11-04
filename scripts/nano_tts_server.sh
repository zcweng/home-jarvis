#!/usr/bin/env bash
# nano-tts 侧车起停：start | start --bg | stop | status | log | health
# 不开机自启、不装 launchd；kill 后不会自动拉起。
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
NANO="$ROOT/tools/nano-tts"
VENV="$NANO/.venv/bin/python"
SERVER="$NANO/server.py"
PIDFILE="$NANO/nano-tts.pid"
LOGFILE="$NANO/nano-tts.log"
HOST="${NANO_TTS_HOST:-127.0.0.1}"
PORT="${NANO_TTS_PORT:-8091}"

usage() {
  cat <<EOF
用法: $0 {start [--bg]|stop|status|log|health}

  start         前台启动（Ctrl-C 退出）
  start --bg    后台启动，日志 → tools/nano-tts/nano-tts.log
  stop          停止后台进程
  status        pid + /health
  log           tail -f 日志
  health        curl /health

环境变量：
  NANO_TTS_REF      参考 wav（默认官方 zh_1.wav；自录可指 refs/owner.wav）
  NANO_TTS_PORT     默认 8091
  NANO_TTS_THREADS  默认 4
  NANO_TTS_SAMPLE_MODE  默认 fixed
EOF
}

need_venv() {
  if [[ ! -x "$VENV" ]]; then
    echo "❌ 缺少 $VENV —— 先完成阶段 0 安装" >&2
    exit 1
  fi
  if [[ ! -f "$SERVER" ]]; then
    echo "❌ 缺少 $SERVER" >&2
    exit 1
  fi
}

is_running() {
  if [[ -f "$PIDFILE" ]]; then
    local pid
    pid="$(cat "$PIDFILE" 2>/dev/null || true)"
    if [[ -n "${pid:-}" ]] && kill -0 "$pid" 2>/dev/null; then
      return 0
    fi
  fi
  return 1
}

cmd_start() {
  need_venv
  local bg=0
  if [[ "${1:-}" == "--bg" ]]; then
    bg=1
  fi
  if is_running; then
    echo "已在运行 pid=$(cat "$PIDFILE") （先 stop）"
    exit 0
  fi
  if [[ "$bg" -eq 1 ]]; then
    : >"$LOGFILE"
    nohup env PYTHONUNBUFFERED=1 "$VENV" "$SERVER" --host "$HOST" --port "$PORT" >>"$LOGFILE" 2>&1 &
    echo $! >"$PIDFILE"
    echo "started pid=$(cat "$PIDFILE") log=$LOGFILE"
    # 等就绪最多 60s
    for _ in $(seq 1 60); do
      if curl -fsS "http://$HOST:$PORT/health" >/dev/null 2>&1; then
        curl -fsS "http://$HOST:$PORT/health"
        echo
        return 0
      fi
      sleep 1
    done
    echo "⚠️  启动超时，看日志：$0 log" >&2
    return 1
  fi
  exec "$VENV" "$SERVER" --host "$HOST" --port "$PORT"
}

cmd_stop() {
  if ! is_running; then
    echo "未运行"
    rm -f "$PIDFILE"
    return 0
  fi
  local pid
  pid="$(cat "$PIDFILE")"
  kill "$pid" 2>/dev/null || true
  for _ in $(seq 1 20); do
    if ! kill -0 "$pid" 2>/dev/null; then
      break
    fi
    sleep 0.2
  done
  if kill -0 "$pid" 2>/dev/null; then
    echo "SIGKILL $pid"
    kill -9 "$pid" 2>/dev/null || true
  fi
  rm -f "$PIDFILE"
  echo "stopped"
}

cmd_status() {
  if is_running; then
    echo "running pid=$(cat "$PIDFILE")"
  else
    echo "stopped"
    return 1
  fi
  curl -fsS "http://$HOST:$PORT/health" || true
  echo
}

cmd_health() {
  curl -fsS "http://$HOST:$PORT/health"
  echo
}

cmd_log() {
  touch "$LOGFILE"
  exec tail -f "$LOGFILE"
}

case "${1:-}" in
  start) shift; cmd_start "${1:-}" ;;
  stop) cmd_stop ;;
  status) cmd_status ;;
  health) cmd_health ;;
  log) cmd_log ;;
  -h|--help|help|"") usage; exit 0 ;;
  *) usage; exit 1 ;;
esac
