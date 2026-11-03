#!/bin/bash
# ============================================================
# S12 小米音箱 全盘备份
# 边读边压缩传输，不在设备侧落临时文件（/data 只有 13MB 可用）
# 用法: ./backup.sh <输出目录>
# ============================================================
set -uo pipefail

HOST=192.168.1.3
OUT=${1:-.}
SSH_OPTS=(-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o BatchMode=yes -o ConnectTimeout=10 -o HostKeyAlgorithms=+ssh-rsa -o PubkeyAcceptedKeyTypes=+ssh-rsa)
REMOTE() { ssh "${SSH_OPTS[@]}" root@$HOST "$@"; }

mkdir -p "$OUT"
cd "$OUT" || exit 1

echo "=== 0. 收集元数据 ==="
{
  echo "# S12 备份元数据"
  echo "date: $(date -u '+%Y-%m-%dT%H:%M:%SZ')"
  echo
  echo "## /proc/mtd"
  REMOTE 'export PATH=/usr/sbin:/usr/bin:/sbin:/bin; cat /proc/mtd'
  echo
  echo "## version"
  REMOTE 'export PATH=/usr/sbin:/usr/bin:/sbin:/bin; cat /usr/share/mico/version'
  echo
  echo "## uboot env"
  REMOTE 'export PATH=/usr/sbin:/usr/bin:/sbin:/bin; fw_printenv'
  echo
  echo "## mmc/nand info"
  REMOTE 'export PATH=/usr/sbin:/usr/bin:/sbin:/bin; cat /proc/cmdline; echo; uname -a'
  echo
  echo "## ubi layout"
  REMOTE 'export PATH=/usr/sbin:/usr/bin:/sbin:/bin; ubinfo -a'
} > META.txt 2>&1
echo "  META.txt 已写入"

echo
echo "=== 1. 备份各 MTD 分区（gzip 压缩流）==="
for spec in "0:nandboot:2" "1:boot0:16" "2:boot1:16" "3:system0:96" "4:system1:96" "5:data:24"; do
  idx="${spec%%:*}"; rest="${spec#*:}"
  name="${rest%%:*}"; mib="${rest##*:}"
  f="mtd${idx}-${name}.raw.gz"
  printf "  mtd%s %-8s %2s MiB -> %s ... " "$idx" "$name" "$mib" "$f"
  t0=$(date +%s)
  if REMOTE "export PATH=/usr/sbin:/usr/bin:/sbin:/bin; /bin/dd if=/dev/mtd${idx}ro bs=64k 2>/dev/null" | gzip -6 > "$f"; then
    t1=$(date +%s)
    sz=$(stat -f%z "$f")
    raw=$((sz))
    printf "OK  %6.1f MiB  %ss\n" "$(echo "$sz/1048576" | bc -l)" "$((t1-t0))"
  else
    echo "FAILED"
  fi
done

echo
echo "=== 2. 计算校验和 ==="
shasum -a 256 ./*.gz > SHA256SUMS.txt
cat SHA256SUMS.txt

echo
echo "=== 3. 备份关键配置文件（明文，便于人工查阅/比对）==="
mkdir -p rootfs-keyfiles
for p in \
  etc/inittab etc/dropbear etc/pam.d/common-auth \
  etc/crontabs/root etc/fstab etc/init.d/pns etc/init.d/dropbear \
  bin/ota bin/flash.sh bin/boardupgrade.sh usr/share/mico/version \
  etc/mico/version ; do
  REMOTE "export PATH=/usr/sbin:/usr/bin:/sbin:/bin; /bin/cat /$p 2>/dev/null" > "rootfs-keyfiles/$(echo "$p" | tr / _)" 2>/dev/null
done
ls -l rootfs-keyfiles | head -20

echo
echo "=== 完成 ==="
du -sh .