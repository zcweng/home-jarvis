#!/bin/sh
# Jarvis 灯环守护：读 FIFO，驱动 /bin/show_led。
#
# 状态：idle|listen|think|speak|error
# FIFO：/tmp/jarvis-led.fifo
# 写入：echo -n listen > /tmp/jarvis-led.fifo
#
# 启动：/data/jarvis-ledd.sh &

export PATH=/usr/sbin:/usr/bin:/sbin:/bin
FIFO=${LED_FIFO:-/tmp/jarvis-led.fifo}
PIDFILE=/tmp/jarvis-ledd.pid
BREATH_PID=

log() { logger -t jarvis-ledd -p 3 "$*" 2>/dev/null; echo "ledd: $*" >&2; }

stop_breath() {
    if [ -n "$BREATH_PID" ] && kill -0 "$BREATH_PID" 2>/dev/null; then
        kill "$BREATH_PID" 2>/dev/null
        wait "$BREATH_PID" 2>/dev/null
    fi
    BREATH_PID=
}

breath_rgb() {
    r0=$1; g0=$2; b0=$3
    stop_breath
    (
        while true; do
            for mul in 25 45 70 95 100 85 60 40 25 12; do
                r=$((r0 * mul / 100))
                g=$((g0 * mul / 100))
                b=$((b0 * mul / 100))
                show_led 8 "$r,$g,$b" >/dev/null 2>&1
                usleep 140000
            done
        done
    ) &
    BREATH_PID=$!
}

apply() {
    s=$(echo "$1" | tr -d '\r\n' | tr 'A-Z' 'a-z')
    [ -z "$s" ] && return
    case "$s" in
        idle|off|0)
            stop_breath
            show_led c >/dev/null 2>&1
            ;;
        listen|1)
            breath_rgb 20 220 50
            ;;
        think|2)
            stop_breath
            show_led c >/dev/null 2>&1
            show_led 3 >/dev/null 2>&1
            ;;
        speak|3)
            stop_breath
            show_led c >/dev/null 2>&1
            show_led 8 "30,110,255" >/dev/null 2>&1
            ;;
        error|4)
            breath_rgb 255 25 20
            ;;
        *)
            log "unknown state: $s"
            ;;
    esac
}

if [ -f "$PIDFILE" ]; then
    old=$(cat "$PIDFILE" 2>/dev/null)
    if [ -n "$old" ] && kill -0 "$old" 2>/dev/null; then
        log "already running pid=$old"
        exit 0
    fi
fi
echo $$ > "$PIDFILE"
rm -f "$FIFO"
mkfifo "$FIFO" || { log "mkfifo failed"; exit 1; }
trap 'stop_breath; show_led c >/dev/null 2>&1; rm -f "$PIDFILE" "$FIFO"; exit 0' INT TERM

show_led c >/dev/null 2>&1
log "listening fifo=$FIFO"

while true; do
    # 阻塞读一行；对端 echo > fifo 写完关闭后 read 返回
    if read -r msg < "$FIFO"; then
        apply "$msg"
    else
        usleep 50000
    fi
done
