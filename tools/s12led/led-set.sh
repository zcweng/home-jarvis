#!/bin/sh
# 灯态 / 音量弧 / 麦静音。Mac：/data/jarvis-led-set.sh listen
#                         /data/jarvis-led-set.sh volume 50
#                         /data/jarvis-led-set.sh mute|unmute
export PATH=/usr/sbin:/usr/bin:/sbin:/bin
BREATH_PIDFILE=/tmp/jarvis-led-breath.pid
MUTE_FILE=/tmp/jarvis-mic.mute
LED_RGB=/sys/devices/i2c-3/3-003c/led_rgb
STATE=$(echo "$1" | tr -d '\r\n' | tr 'A-Z' 'a-z')
ARG2=$2

stop_breath() {
    if [ -f "$BREATH_PIDFILE" ]; then
        pid=$(cat "$BREATH_PIDFILE" 2>/dev/null)
        rm -f "$BREATH_PIDFILE"
        if [ -n "$pid" ]; then
            kill "$pid" 2>/dev/null
            usleep 100000
            kill -9 "$pid" 2>/dev/null
        fi
    fi
    usleep 30000
}

# sysfs 直写的灯不会被 show_led c 清掉，必须写 0
leds_off() {
    show_led c >/dev/null 2>&1
    usleep 20000
    if [ -w "$LED_RGB" ]; then
        i=0
        while [ "$i" -lt 12 ]; do
            echo "$i 0" > "$LED_RGB"
            i=$((i + 1))
        done
    fi
}

breath_rgb() {
    r0=$1; g0=$2; b0=$3
    stop_breath
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

# 直接写 IS31 sysfs（show_led 自定义黄会被 ledserver 洗成近白，看不见）
paint_all_rgb() {
    r=$1; g=$2; b=$3
    pack=$((r | (g << 8) | (b << 16)))
    i=0
    while [ "$i" -lt 12 ]; do
        echo "$i $pack" > "$LED_RGB"
        i=$((i + 1))
    done
}

# show_led 自定义色会被 ledserver 冲掉（黄/蓝几乎看不见），状态灯一律走 sysfs
solid_sysfs_rgb() {
    r0=$1; g0=$2; b0=$3
    stop_breath
    show_led c >/dev/null 2>&1
    usleep 30000
    if [ ! -w "$LED_RGB" ]; then
        echo "led_rgb not writable: $LED_RGB" >&2
        return 1
    fi
    paint_all_rgb "$r0" "$g0" "$b0"
}

breath_sysfs_rgb() {
    r0=$1; g0=$2; b0=$3
    stop_breath
    show_led c >/dev/null 2>&1
    usleep 30000
    if [ ! -w "$LED_RGB" ]; then
        echo "led_rgb not writable: $LED_RGB" >&2
        return 1
    fi
    (
        trap '' HUP
        while true; do
            for mul in 30 50 75 100 100 80 55 35; do
                paint_all_rgb $((r0 * mul / 100)) $((g0 * mul / 100)) $((b0 * mul / 100))
                usleep 160000
            done
        done
    ) >/dev/null 2>&1 &
    echo $! > "$BREATH_PIDFILE"
}

is_muted() {
    [ "$(cat "$MUTE_FILE" 2>/dev/null | tr -d '\r\n')" = "1" ]
}

# 麦禁用：黄色呼吸；启用后熄灭。静音期间其它态保持黄灯。
yellow_mute() {
    breath_sysfs_rgb 255 150 0
}

# 音量弧：12 灯，点亮 round(pct*12/100) 颗蓝色（50%→6）
# led_rgb: bits0-7 R, 8-15 G, 16-23 B
paint_volume() {
    pct=$1
    MIN_PCT=5
    [ -z "$pct" ] && pct=$MIN_PCT
    case "$pct" in
        *[!0-9]*) pct=$MIN_PCT ;;
    esac
    [ "$pct" -lt "$MIN_PCT" ] 2>/dev/null && pct=$MIN_PCT
    [ "$pct" -gt 100 ] 2>/dev/null && pct=100
    stop_breath
    # 关掉 ledserver 动画，避免抢 sysfs
    show_led c >/dev/null 2>&1
    usleep 30000
    if [ ! -w "$LED_RGB" ]; then
        echo "led_rgb not writable: $LED_RGB" >&2
        exit 1
    fi
    n=$(( (pct * 12 + 50) / 100 ))
    if [ "$pct" -gt 0 ] && [ "$n" -lt 1 ]; then
        n=1
    fi
    if [ "$pct" -ge 100 ]; then
        n=12
    fi
    BLUE=$((200 << 16))   # 偏亮蓝
    DIM=$((40 << 16))     # 未点亮位微弱蓝底，便于看见整环位置；0=全灭
    # 用户要「一半有亮度」——未点亮保持熄灭更清晰
    OFF=0
    i=0
    while [ "$i" -lt 12 ]; do
        if [ "$i" -lt "$n" ]; then
            echo "$i $BLUE" > "$LED_RGB"
        else
            echo "$i $OFF" > "$LED_RGB"
        fi
        i=$((i + 1))
    done
    echo "$pct" > /tmp/jarvis-vol.pct
}

case "$STATE" in
    mute|mic_off|mic-off)
        echo 1 > "$MUTE_FILE"
        yellow_mute
        ;;
    unmute|mic_on|mic-on)
        echo 0 > "$MUTE_FILE"
        stop_breath
        leds_off
        ;;
    idle|off)
        if is_muted; then yellow_mute; exit 0; fi
        stop_breath
        leds_off
        ;;
    listen|1)
        # 已唤醒：常亮蓝（等指令）
        if is_muted; then yellow_mute; exit 0; fi
        solid_sysfs_rgb 20 80 255
        ;;
    think|2)
        # LLM / web_search：蓝色呼吸
        if is_muted; then yellow_mute; exit 0; fi
        breath_sysfs_rgb 20 80 255
        ;;
    speak|3)
        if is_muted; then yellow_mute; exit 0; fi
        solid_sysfs_rgb 20 80 255
        ;;
    error|4)
        if is_muted; then yellow_mute; exit 0; fi
        solid_sysfs_rgb 255 0 0
        usleep 250000
        leds_off
        usleep 120000
        solid_sysfs_rgb 255 0 0
        usleep 250000
        breath_sysfs_rgb 255 0 0
        ;;
    volume|vol)
        paint_volume "${ARG2:-0}"
        # 静音中调音量：蓝弧闪一下后回到黄呼吸
        if is_muted; then
            usleep 400000
            yellow_mute
        fi
        ;;
    *)
        echo "usage: $0 idle|listen|think|speak|error|mute|unmute|volume <0-100>" >&2
        exit 1
        ;;
esac
exit 0
