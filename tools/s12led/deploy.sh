#!/bin/sh
# 推送灯控脚本到 S12。用法：./tools/s12led/deploy.sh [host]
set -e
HOST=${1:-192.168.1.3}
SSH="ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o BatchMode=yes -o ConnectTimeout=5 -o HostKeyAlgorithms=+ssh-rsa -o PubkeyAcceptedKeyTypes=+ssh-rsa root@$HOST"
ROOT=$(CDPATH= cd -- "$(dirname "$0")/../.." && pwd)

# 停掉旧 FIFO 守护
$SSH 'export PATH=/usr/sbin:/usr/bin:/sbin:/bin
for p in $(ps w | grep -E "jarvis-ledd|jarvis-led-breath" | grep -v grep | awk "{print \$1}"); do kill $p 2>/dev/null; done
rm -f /tmp/jarvis-ledd.pid /tmp/jarvis-led.fifo /tmp/jarvis-led-breath.pid
show_led c >/dev/null 2>&1 || true
'

cat "$ROOT/tools/s12led/led-set.sh" | $SSH 'export PATH=/usr/sbin:/usr/bin:/sbin:/bin; /bin/cat > /data/jarvis-led-set.sh && chmod +x /data/jarvis-led-set.sh && ls -l /data/jarvis-led-set.sh'
echo "deployed. test: ssh root@$HOST /data/jarvis-led-set.sh listen"
