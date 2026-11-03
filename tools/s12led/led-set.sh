#!/bin/sh
# 一次设置一个灯态。Mac 经 SSH 调用：/data/jarvis-led-set.sh listen
# 状态：idle|listen|think|speak|error
export PATH=/usr/sbin:/usr/bin:/sbin:/bin
BREATH_PIDFILE=/tmp/jarvis-led-breath.pid
STATE=$(echo "$1" | tr -d '\r\n' | tr 'A-Z' 'a-z')

stop_breath() {
    if [ -f "$BREATH_PIDFILE" ]; then
        pid=$(cat "$BREATH_PIDFILE" 2>/dev/null)
        [ -n "$pid" ] && kill "$pid" 2>/dev/null
        # 子 shell 里的 show_led 循环
        kill "$pid" 2>/dev/null
        rm -f "$BREATH_PIDFILE"
        usleep 50000
    fi
    # 扫掉残留 breath（防止 pidfile 丢失）
    for p in $(ps w | grep 'jarvis-led-breath' | grep -v grep | awk '{print $1}'); do
        kill "$p" 2>/dev/null
    done
}

breath_rgb() {
    r0=$1; g0=$2; b0=$3
    stop_breath
    # 忽略 HUP，避免 SSH 断开带走呼吸进程
    # 亮度下限 35%，避免「灭掉」被看成没亮
    (
        trap '' HUP
        while true; do
            for mul in 35 55 75 100 100 80 55 35; do
                r=$((r0 * mul / 100))
                g=$((g0 * mul / 100))
                b=$((b0 * mul / 100))
                show_led 8 "$r,$g,$b" >/dev/null 2>&1
                usleep 160000
            done
        done
    ) >/dev/null 2>&1 &
    echo $! > "$BREATH_PIDFILE"
}

case "$STATE" in
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
            # 先闪两下大红再呼吸，避免被当成粉/橙或一闪而过
            stop_breath
            show_led c >/dev/null 2>&1
            show_led 8 "255,0,0" >/dev/null 2>&1
            usleep 250000
            show_led c >/dev/null 2>&1
            usleep 120000
            show_led 8 "255,0,0" >/dev/null 2>&1
            usleep 250000
            breath_rgb 255 0 0
            ;;
    *)
        echo "usage: $0 idle|listen|think|speak|error" >&2
        exit 1
        ;;
esac
exit 0
