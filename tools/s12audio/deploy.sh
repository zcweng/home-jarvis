#!/bin/sh
# 推送 S12 网络音频端点（s12audio + 自启脚本）到设备。
#
#   ./tools/s12audio/deploy.sh [host]              # 只推 s12audio
#   ./tools/s12audio/deploy.sh [host] --install    # 推 + 装自启（开机拉起）
#   ./tools/s12audio/deploy.sh [host] --restart    # 推 + 重启服务
#
# 前置：docker build -t s12build tools/s12audio，然后
#       docker run --rm -v $PWD/tools:/src/tools -w /src/tools/s12audio s12build
set -e
HOST=${1:-192.168.1.3}
ACTION=${2:-}
SSH="ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o BatchMode=yes -o ConnectTimeout=8 -o HostKeyAlgorithms=+ssh-rsa -o PubkeyAcceptedKeyTypes=+ssh-rsa root@$HOST"
DPATH='export PATH=/usr/sbin:/usr/bin:/sbin:/bin'
ROOT=$(CDPATH= cd -- "$(dirname "$0")/../.." && pwd)
BIN="$ROOT/tools/s12audio/build/s12audio"
INIT="$ROOT/tools/s12audio/jarvis-audio.sh"

[ -f "$BIN" ] || {
    echo "missing $BIN — build with: docker run --rm -v \$PWD/tools:/src/tools -w /src/tools/s12audio s12build" >&2
    exit 1
}

# 推送前先停服务，否则 /data/xxx 正在执行 → "Text file busy"
$SSH "$DPATH
[ -x /etc/init.d/jarvis-audio ] && /etc/init.d/jarvis-audio stop >/dev/null 2>&1
killall s12audio >/dev/null 2>&1
sleep 1
killall -9 s12audio >/dev/null 2>&1
mkdir -p /data/s12audio /data/log
true" >/dev/null 2>&1 || true

cat "$BIN" | $SSH "$DPATH; /bin/cat > /data/s12audio/s12audio.new && chmod +x /data/s12audio/s12audio.new && mv /data/s12audio/s12audio.new /data/s12audio/s12audio && ls -l /data/s12audio/s12audio"

cat "$INIT" | $SSH "$DPATH; /bin/cat > /etc/init.d/jarvis-audio.new && chmod 755 /etc/init.d/jarvis-audio.new && mv /etc/init.d/jarvis-audio.new /etc/init.d/jarvis-audio && ls -l /etc/init.d/jarvis-audio"

case "$ACTION" in
--install)
    # enable 由 rc.common 自己建 /etc/rc.d/S98jarvis-audio 软链
    $SSH "$DPATH
/etc/init.d/jarvis-audio enable
ls -l /etc/rc.d/ | grep jarvis-audio || true
/etc/init.d/jarvis-audio restart
sleep 3
ps w | grep -E 's12audio|jarvis-volumed' | grep -v grep
netstat -ltn | grep -E '8765|8766' || echo 'NO_LISTENER'
"
    ;;
--restart)
    $SSH "$DPATH
/etc/init.d/jarvis-audio restart
sleep 3
ps w | grep -E 's12audio|jarvis-volumed' | grep -v grep
netstat -ltn | grep -E '8765|8766' || echo 'NO_LISTENER'
"
    ;;
*)
    echo "pushed. 下一步："
    echo "  $0 $HOST --install   # 装开机自启并立刻启动"
    echo "  $0 $HOST --restart   # 只重启"
    ;;
esac