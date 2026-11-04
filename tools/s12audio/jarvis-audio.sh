#!/bin/sh /etc/rc.common
# S12 网络音频端点自启（OpenWrt procd / rc.common）。
#
#   /etc/init.d/jarvis-audio start|stop|restart|enable|disable
#
# 启动顺序（见 docs/S12-设备状态契约.md §5）：
#   1. touchpad stop  → 释放 event0，改由 volumed 读音量环/麦静音键
#   2. pns stop       → 释放 hw:0,0（唯一真正必需的一步）
#   3. volumed        → 音量环 + 麦静音键 → /tmp 状态文件
#   4. s12audio       → 采集 + AEC + 8765/8766 上下行
#
# 失效回滚：s12audio 连续 MAX_RESTART 次起不来（hw:0,0 被抢、ALSA 参数对不上），
# 就 `/etc/init.d/pns start` 交还原厂语音。宁可哑掉，也不留一个
# 「端口在听、麦克风全静音」的假服务。

START=98
STOP=20

BIN=/data/s12audio/s12audio
VOLUMED=/data/jarvis-volumed
LED_SET=/data/jarvis-led-set.sh
LOG=/data/log/jarvis-audio.log
RUN=/var/run/jarvis-audio.run
SUP_PID=/var/run/jarvis-audio-sup.pid
UP_PORT=8765

MAX_RESTART=5
STABLE_SEC=60

log() {
    echo "[$(date '+%m-%d %H:%M:%S')] $*" >>"$LOG"
    logger -t jarvis-audio -p 4 "$*" 2>/dev/null
}

# /data 只有十几 MB，日志必须自己封顶（没有 logrotate 管这里）
cap_log() {
    [ -f "$LOG" ] || return 0
    sz=$(wc -c <"$LOG" 2>/dev/null || echo 0)
    [ "$sz" -gt 262144 ] && tail -c 65536 "$LOG" >"$LOG.tmp" 2>/dev/null &&
        mv "$LOG.tmp" "$LOG"
    return 0
}

port_up() {
    netstat -ltn 2>/dev/null | grep -q ":$UP_PORT "
}

wait_port() {
    i=0
    while [ "$i" -lt "$1" ]; do
        port_up && return 0
        sleep 1
        i=$((i + 1))
    done
    port_up
}

# 播放节点空闲：/proc 里 status 为 "closed"
pcm_free() {
    f=/proc/asound/card0/pcm0p/sub0/status
    [ -r "$f" ] || return 0
    grep -qi 'closed' "$f"
}

wait_pcm_free() {
    i=0
    while [ "$i" -lt "$1" ]; do
        pcm_free && return 0
        sleep 1
        i=$((i + 1))
    done
    pcm_free
}

# 监护主体：放在子 shell 里跑，$! 就是它自己的 pid。
# 开机时 miplay 的启动音可能还占着 hw:0,0，前几次退出属于正常，靠重试熬过去。
supervise() {
    trap '' HUP
    tries=0
    while [ -f "$RUN" ]; do
        t0=$(date +%s)
        "$BIN" -q
        rc=$?
        t1=$(date +%s)
        [ -f "$RUN" ] || break
        # 稳定跑过 STABLE_SEC 视为偶发崩溃，重新计数
        [ $((t1 - t0)) -ge "$STABLE_SEC" ] && tries=0
        tries=$((tries + 1))
        log "s12audio 退出 rc=$rc（运行 $((t1 - t0))s），重启 $tries/$MAX_RESTART"
        if [ "$tries" -ge "$MAX_RESTART" ]; then
            log "连续 $tries 次起不来，回滚：恢复原厂语音"
            rm -f "$RUN"
            /etc/init.d/pns start >/dev/null 2>&1
            log "已交还 pns；排查后 /etc/init.d/jarvis-audio restart 可重试"
            break
        fi
        sleep 3
    done
}

stop_audio() {
    rm -f "$RUN"
    if [ -f "$SUP_PID" ]; then
        kill "$(cat "$SUP_PID")" >/dev/null 2>&1
        rm -f "$SUP_PID"
    fi
    killall s12audio >/dev/null 2>&1
    sleep 1
    killall -9 s12audio >/dev/null 2>&1
    killall jarvis-volumed >/dev/null 2>&1
}

start() {
    mkdir -p /data/log /var/run
    cap_log
    if [ -f "$RUN" ] && port_up; then
        log "已在运行，跳过"
        return 0
    fi
    [ -x "$BIN" ] || {
        log "缺少 $BIN（先用 tools/s12audio/deploy.sh 推送），跳过启动"
        return 1
    }
    stop_audio

    # ① 释放 event0：原厂 touchpad 会抢音量环，两边同时读会导致双倍步进
    [ -x /etc/init.d/touchpad ] && /etc/init.d/touchpad stop >/dev/null 2>&1
    killall touchpad >/dev/null 2>&1

    # ② 释放 hw:0,0
    /etc/init.d/pns stop >/dev/null 2>&1
    wait_pcm_free 8 || log "⚠️ hw:0,0 仍被占用（可能是开机音），交给重试"

    # ③ 音量环 / 麦静音键
    if [ -x "$VOLUMED" ]; then
        ( trap '' HUP; exec "$VOLUMED" ) </dev/null >>"$LOG" 2>&1 &
        sleep 1
    else
        log "缺少 $VOLUMED，音量环按键不可用（tools/s12led/deploy.sh --with-volumed）"
    fi

    # ④ 灯环归零（/tmp 是 tmpfs，重启后状态文件全空）
    [ -x "$LED_SET" ] && "$LED_SET" idle >/dev/null 2>&1

    # ⑤ 音频端点
    touch "$RUN"
    log "启动 s12audio"
    supervise </dev/null >>"$LOG" 2>&1 &
    echo $! >"$SUP_PID"

    if wait_port 15; then
        log "就绪：8765 上行 / 8766 下行，音量 $(cat /tmp/jarvis-vol.pct 2>/dev/null || echo ?)%"
        return 0
    fi
    log "❌ s12audio 15s 内没监听 $UP_PORT，回滚到原厂语音"
    stop_audio
    /etc/init.d/pns start >/dev/null 2>&1
    return 1
}

stop() {
    log "停止 s12audio / volumed"
    stop_audio
    [ -x "$LED_SET" ] && "$LED_SET" idle >/dev/null 2>&1
    # 交还原厂语音：别让音箱彻底哑掉
    [ -x /etc/init.d/touchpad ] && /etc/init.d/touchpad start >/dev/null 2>&1
    /etc/init.d/pns start >/dev/null 2>&1
}

# procd 的 rcS 对 S## 脚本调 boot
boot() {
    start "$@"
}