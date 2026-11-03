#!/bin/sh
# ============================================================
# S12 音频端点（纯 shell 版，无 AEC）
#
# 上行：8765 TCP  —— arecord 8ch S32@16k 裸流给 Mac
# 下行：8766 TCP  —— 收 Mac 的 framed PCM，喂 aplay
#
# 为什么不做 AEC：先在 Mac 上用 numpy 迭代参数，定型后再写 C 守护进程。
# 这个脚本只负责"搬字节"，所以用 busybox sh + nc 就能跑。
#
# 用法：
#   ./s12audio.sh start | stop | status
# ============================================================

UP_PORT=8765
DOWN_PORT=8766
RATE=16000
CH=8
FMT=S32_LE
LOG=/data/log/s12audio.log

PID_UP=/var/run/s12audio_up.pid
PID_DOWN=/var/run/s12audio_down.pid

log() { echo "$(date '+%Y-%m-%d %H:%M:%S') $*" >> "$LOG"; }

# nc 版本决定参数：busybox nc 用 -l -p PORT，ncat 用 -l PORT
nc_listen() {  # $1=port
    nc -l -p "$1" 2>/dev/null &
}

stop_all() {
    for f in "$PID_UP" "$PID_DOWN"; do
        [ -f "$f" ] && kill "$(cat "$f")" 2>/dev/null && rm -f "$f"
    done
    # 兜底：按名字清理
    killall arecord 2>/dev/null
    killall aplay 2>/dev/null
    killall nc 2>/dev/null
    # 让厂商 pns 重新拿回音频
    [ -x /etc/init.d/pns ] && /etc/init.d/pns start >/dev/null 2>&1
    log "stopped"
}

start_up() {
    # 采集必须先停 pns，否则 pcmC0D0c 被占用
    /etc/init.d/pns stop >/dev/null 2>&1
    sleep 1

    # 等 Mac 连上来，再开始采集 —— 避免 nc 未就绪时 arecord 空转
    (
        while true; do
            { printf '' | nc -l -p "$UP_PORT" >/dev/null 2>&1; } 
            # 上面的写法不可靠，改用下面的主循环
            break
        done
    ) >/dev/null 2>&1

    # 主循环：每来一个连接，就起一次 arecord 推流
    (
        while true; do
            nc -l -p "$UP_PORT" 2>/dev/null | \
              arecord -D hw:0,0 -f $FMT -c $CH -r $RATE -t raw 2>/dev/null
            log "uplink session ended, retry"
            sleep 1
        done
    ) &
    echo $! > "$PID_UP"
    log "uplink listening on $UP_PORT"
}

start_down() {
    # 下行：每个连接一个会话，解析 8 字节帧头
    # 用 awk 不方便做二进制，这里用 shell 只处理 PCM/FLUSH 两种
    (
        while true; do
            nc -l -p "$DOWN_PORT" 2>/dev/null | \
              /bin/downlink_parse 2>/dev/null
            log "downlink session ended, retry"
            sleep 1
        done
    ) &
    echo $! > "$PID_DOWN"
    log "downlink listening on $DOWN_PORT"
}

status() {
    echo "uplink:   $( [ -f "$PID_UP" ] && kill -0 "$(cat "$PID_UP")" 2>/dev/null && echo RUNNING || echo STOPPED )"
    echo "downlink: $( [ -f "$PID_DOWN" ] && kill -0 "$(cat "$PID_DOWN")" 2>/dev/null && echo RUNNING || echo STOPPED )"
    ps 2>/dev/null | grep -E "arecord|aplay|nc " | grep -v grep
}

case "$1" in
    start)  stop_all >/dev/null 2>&1; start_up; start_down; status ;;
    stop)   stop_all; status ;;
    status) status ;;
    *) echo "用法: $0 {start|stop|status}"; exit 1 ;;
esac