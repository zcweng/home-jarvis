#!/bin/bash
# ============================================================
# S12 系统恢复脚本
# 用法: ./restore-s12.sh <system0|system1|data> [备份目录]
#
# 核心策略：从完好的 A 槽启动，再修复 B 槽（根分区无法在线恢复）
# ============================================================
set -uo pipefail

HOST=192.168.1.3
TARGET=${1:-}
BK=${2:-$(dirname "$0")/s12-1.74.18-20261102}

SSH_OPTS=(-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o BatchMode=yes -o ConnectTimeout=10 -o HostKeyAlgorithms=+ssh-rsa -o PubkeyAcceptedKeyTypes=+ssh-rsa)
RE() { ssh "${SSH_OPTS[@]}" root@$HOST "export PATH=/usr/sbin:/usr/bin:/sbin:/bin; $*"; }

die() { echo "ERROR: $*" >&2; exit 1; }
wait_up() {
  echo -n "  等待设备上线"
  for i in $(seq 1 90); do
    if RE 'true' 2>/dev/null; then echo " OK"; return 0; fi
    echo -n "."; sleep 2
  done
  echo " TIMEOUT"; return 1
}

case "$TARGET" in
  system0) IDX=3; IMG=mtd3-system0.raw.gz; SLOT=boot0 ;;
  system1) IDX=4; IMG=mtd4-system1.raw.gz; SLOT=boot1 ;;
  data)    IDX=5; IMG=mtd5-data.raw.gz;   SLOT="" ;;
  *) echo "用法: $0 <system0|system1|data> [备份目录]"; exit 1 ;;
esac

[ -f "$BK/$IMG" ] || die "找不到镜像 $BK/$IMG"
echo "=== 恢复目标: mtd${IDX} ($IMG) ==="

if [ -n "$SLOT" ]; then
  CUR=$(RE 'fw_printenv boot_part 2>/dev/null' | sed 's/.*=//' | tr -d '\r')
  echo "  当前启动槽: $CUR"
  if [ "$CUR" = "$SLOT" ]; then
    OTHER=$([ "$SLOT" = "boot0" ] && echo boot1 || echo boot0)
    echo "  !! 目标槽正在使用，必须先切到 $OTHER 启动"
    RE "fw_setenv boot_part $OTHER; sync; nohup reboot >/dev/null 2>&1 &" || die "切换失败"
    sleep 5; wait_up || die "设备未上线"
    echo "  已从 $OTHER 启动"
  fi
fi

echo "=== 写入镜像（流式，不落设备磁盘）==="
echo "  镜像大小: $(gunzip -c "$BK/$IMG" | wc -c | tr -d ' ') 字节"
gunzip -c "$BK/$IMG" | ssh "${SSH_OPTS[@]}" root@$HOST \
  "export PATH=/usr/sbin:/usr/bin:/sbin:/bin;
   if [ $IDX -eq 5 ]; then
     umount /data 2>/dev/null
     ubidetach -d 1 2>/dev/null
     ubiformat /dev/mtd5 -y -f - 
     ubiattach -p /dev/mtd5
     mount -t ubifs ubi1:data /data
   else
     ubidetach -d 0 2>/dev/null
     ubiformat /dev/mtd${IDX} -y -f -
   fi"
RC=$?
[ $RC -eq 0 ] || die "写入失败 rc=$RC"

echo "=== 切回目标槽并重启 ==="
if [ -n "$SLOT" ]; then
  RE "fw_setenv boot_part $SLOT; fw_setenv boot_failcnt 0; sync; nohup reboot >/dev/null 2>&1 &"
  sleep 5; wait_up || die "重启后设备未上线"
  echo "=== 版本确认 ==="
  RE 'cat /usr/share/mico/version | grep ROM'
fi

echo "=== 恢复完成 ==="