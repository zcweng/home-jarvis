#!/bin/sh
# 推送灯控 + 可选 volumed。用法：
#   ./tools/s12led/deploy.sh [host]
#   ./tools/s12led/deploy.sh [host] --with-volumed   # 需先 docker 编好 build/volumed
set -e
HOST=${1:-192.168.1.3}
WITH_VOL=0
[ "${2:-}" = "--with-volumed" ] && WITH_VOL=1
SSH="ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o BatchMode=yes -o ConnectTimeout=5 -o HostKeyAlgorithms=+ssh-rsa -o PubkeyAcceptedKeyTypes=+ssh-rsa root@$HOST"
ROOT=$(CDPATH= cd -- "$(dirname "$0")/../.." && pwd)

# 旧版 ledd.sh（FIFO 守护）已从仓库删除：灯环只有一条实现，就是
# led-set.sh + volumed 的本地画灯。设备上若还跑着上一版留下的 ledd，
# 这里顺手清掉（只做一次性迁移，不再安装任何 FIFO 守护）。
$SSH 'export PATH=/usr/sbin:/usr/bin:/sbin:/bin
for p in $(ps w | grep -E "jarvis-ledd|jarvis-led-breath" | grep -v grep | awk "{print \$1}"); do kill $p 2>/dev/null; done
rm -f /tmp/jarvis-ledd.pid /tmp/jarvis-led.fifo /data/jarvis-ledd.sh
'

cat "$ROOT/tools/s12led/led-set.sh" | $SSH 'export PATH=/usr/sbin:/usr/bin:/sbin:/bin; /bin/cat > /data/jarvis-led-set.sh && chmod +x /data/jarvis-led-set.sh && ls -l /data/jarvis-led-set.sh'

if [ "$WITH_VOL" = 1 ]; then
  if [ ! -x "$ROOT/tools/s12led/build/volumed" ]; then
    echo "missing tools/s12led/build/volumed — build with docker first" >&2
    exit 1
  fi
  cat "$ROOT/tools/s12led/build/volumed" | $SSH 'export PATH=/usr/sbin:/usr/bin:/sbin:/bin; /bin/cat > /data/jarvis-volumed && chmod +x /data/jarvis-volumed'
  $SSH 'export PATH=/usr/sbin:/usr/bin:/sbin:/bin
# 停原厂 touchpad（procd 可能拉起，多杀两次），改由 volumed 读音量环
for p in $(ps w | grep jarvis-volumed | grep -v grep | awk "{print \$1}"); do kill $p 2>/dev/null; done
if [ -x /etc/init.d/touchpad ]; then /etc/init.d/touchpad stop 2>/dev/null; fi
killall touchpad 2>/dev/null || true
sleep 1
killall touchpad 2>/dev/null || true
/data/jarvis-volumed >/data/jarvis-volumed.log 2>&1 &
sleep 1
# touchpad 若又起来再杀一次并确认 grab
killall touchpad 2>/dev/null || true
ps w | grep jarvis-volumed | grep -v grep || { echo volumed_FAIL; cat /data/jarvis-volumed.log; exit 1; }
ps w | grep "/bin/touchpad" | grep -v grep && echo "WARN touchpad still running" || echo "touchpad stopped"
'
  echo "volumed started (touchpad stopped)"
fi

echo "deployed led-set. test: ssh root@$HOST /data/jarvis-led-set.sh volume 50"
