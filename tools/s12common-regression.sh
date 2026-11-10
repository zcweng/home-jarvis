#!/bin/sh
# 设备侧回归：把纯逻辑单测 + 守护进程生命周期跑到真实 S12 上。
#
#   ./tools/s12common-regression.sh [host]
#
# 覆盖：
#   1. tools/common 单测（ring/proto/gain/state_file）在设备 glibc 2.19 上跑通
#   2. s12audio 启动 → SIGTERM 在 1s 内退出（曾经会因 recv 无超时永久挂住）
#   3. volumed 启动 → EVIOCGRAB 成功 → SIGTERM 退出 → grab 已释放（可再次 grab）
#   4. 退出后不留僵尸进程
#
# 前置：tools/s12audio/build/s12audio、tools/s12led/build/volumed 已用
#       tools/s12audio/Dockerfile 的 s12build 镜像编好。
set -u
HOST=${1:-192.168.1.3}
ROOT=$(CDPATH= cd -- "$(dirname "$0")/.." && pwd)

# 设备是精简 busybox：默认 PATH 里没有 cat/grep/sleep，
# 每一条远程命令都必须自己带 PATH，否则会看到莫名其妙的 "not found"。
DPATH=/usr/sbin:/usr/bin:/sbin:/bin
rsh() {
  ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
      -o BatchMode=yes -o ConnectTimeout=5 \
      -o HostKeyAlgorithms=+ssh-rsa -o PubkeyAcceptedKeyTypes=+ssh-rsa \
      "root@$HOST" "export PATH=$DPATH; $1"
}

fail=0
ok()  { printf '  ok    %s\n' "$1"; }
bad() { printf '  FAIL  %s\n' "$1"; fail=1; }

# 上传前必须先停掉同名进程，否则 /data/xxx 正在执行 → "Text file busy"
# 顺带把 pns 恢复，保证脚本失败也不会把设备留在无音频状态。
cleanup_remote() {
  rsh 'for p in /proc/[0-9]*; do
         c=$(cat $p/comm 2>/dev/null)
         case "$c" in s12audio*|volumed*|test_common*) kill -9 ${p#/proc/} 2>/dev/null;; esac
       done
       sleep 1
       /etc/init.d/pns start >/dev/null 2>&1
       rm -f /data/s12audio_t /data/volumed_t /data/test_common
       rm -f /data/s12a_t.log /data/vlm_t.log /data/vlm_t2.log
       true' >/dev/null 2>&1
}
trap 'cleanup_remote' EXIT
cleanup_remote

echo "== 目标 $HOST =="

# ---------- 0. 交叉编译单测（宿主机 docker） ----------
echo "-- 0. 用 s12build 编 aarch64 单测"
TMP=$(mktemp -d)
cp "$ROOT"/tools/common/test_common.c "$ROOT"/tools/common/gain.h \
   "$ROOT"/tools/common/ring.h "$ROOT"/tools/common/proto.h \
   "$ROOT"/tools/common/state_file.h "$TMP"/
docker run --rm -v "$TMP:/src" -w /src s12build \
  sh -c 'gcc -O1 -std=gnu11 -o test_common_arm test_common.c -lpthread -lm' >/dev/null 2>&1
if [ ! -f "$TMP/test_common_arm" ]; then
  bad "交叉编译单测失败（需要 docker + s12build 镜像）"
else
  ok "交叉编译通过"
  cat "$TMP/test_common_arm" | rsh '/bin/cat > /data/test_common; /bin/chmod +x /data/test_common'
fi
rm -rf "$TMP"

# ---------- 1. 设备上跑单测 ----------
echo "-- 1. 设备侧单测"
out=$(rsh 'cd /data && ./test_common 2>&1')
echo "$out" | sed 's/^/     /'
case "$out" in
  *"0 failed"*) ok "单测在设备 glibc 2.19 上通过" ;;
  *) bad "设备侧单测失败" ;;
esac

# ---------- 2. s12audio 生命周期 ----------
echo "-- 2. s12audio 启停"
if [ ! -f "$ROOT/tools/s12audio/build/s12audio" ]; then
  bad "缺少 tools/s12audio/build/s12audio，先 docker 编译"
else
  cat "$ROOT/tools/s12audio/build/s12audio" | rsh '/bin/cat > /data/s12audio_t; /bin/chmod +x /data/s12audio_t'
  out=$(rsh '
    /etc/init.d/pns stop >/dev/null 2>&1; sleep 2
    /data/s12audio_t -q > /data/s12a_t.log 2>&1 &
    PID=$!
    sleep 2
    if ! ls /proc/$PID >/dev/null 2>&1; then echo "START_FAIL"; cat /data/s12a_t.log; exit 0; fi
    kill -TERM $PID
    for i in 1 2 3 4 5 6 7 8 9 10; do
      ls /proc/$PID >/dev/null 2>&1 || { echo "EXIT_OK"; break; }
      usleep 100000
    done
    ls /proc/$PID >/dev/null 2>&1 && echo "EXIT_HUNG"
    /etc/init.d/pns start >/dev/null 2>&1
  ')
  case "$out" in
    *EXIT_OK*) ok "启动正常 / SIGTERM 1s 内退出" ;;
    *EXIT_HUNG*) bad "SIGTERM 后仍存活（阻塞 IO 未修复？）" ;;
    *) bad "启动失败：$out" ;;
  esac
fi

# ---------- 3. volumed 生命周期 ----------
echo "-- 3. volumed 启停与 grab 释放"
if [ ! -f "$ROOT/tools/s12led/build/volumed" ]; then
  bad "缺少 tools/s12led/build/volumed，先编译"
else
  cat "$ROOT/tools/s12led/build/volumed" | rsh '/bin/cat > /data/volumed_t; /bin/chmod +x /data/volumed_t'
  # 生产 volumed 正握着 event0 的 EVIOCGRAB，临时实例必然 grab 不到 →
  # 会报成「grab 异常」这种假失败（实测过：G1=0 ALIVE=0 G2=0）
  # 或「grab 或释放异常：G1=0 ALIVE=0 G2=0」。
  # 只停 volumed、**不要 stop 整个服务**：stop 会交还给原厂 pns，pns 立刻
  # 占住 hw:0,0，紧接着 start 的 5 次重启全失败 → 整机回滚到 pns（实测踩过）。
  svc_was_up=0
  if rsh 'ps w | grep -q "[j]arvis-volumed"'; then
    svc_was_up=1
    rsh 'killall jarvis-volumed >/dev/null 2>&1; sleep 2'
  fi
  out=$(rsh '
    echo 0 > /tmp/jarvis-mic.mute
    /data/volumed_t > /data/vlm_t.log 2>&1 &
    PID=$!
    sleep 2
    G1=$(grep -c "grab=1" /data/vlm_t.log)
    kill -TERM $PID
    for i in 1 2 3 4 5; do ls /proc/$PID >/dev/null 2>&1 || break; usleep 100000; done
    ALIVE=0; ls /proc/$PID >/dev/null 2>&1 && ALIVE=1
    # 再起一次：若上次没释放 EVIOCGRAB，这里会一直 grab 失败
    /data/volumed_t > /data/vlm_t2.log 2>&1 &
    PID2=$!
    sleep 2
    G2=$(grep -c "grab=1" /data/vlm_t2.log)
    kill -TERM $PID2 2>/dev/null; sleep 1
    echo "G1=$G1 ALIVE=$ALIVE G2=$G2"
  ')
  echo "     $out"
  if [ "$svc_was_up" = 1 ]; then
    # 照 init 脚本的方式拉回来（它也是 ( trap '' HUP; exec $VOLUMED ) &）
    rsh 'cd /data && ( trap "" HUP; exec /data/jarvis-volumed ) </dev/null >>/data/log/jarvis-audio.log 2>&1 & sleep 1'
  fi
  case "$out" in
    *"G1=1 ALIVE=0 G2=1"*) ok "grab 成功 / 干净退出 / grab 已释放" ;;
    *"ALIVE=1"*) bad "SIGTERM 后仍存活（read 未改成 poll？）" ;;
    *) bad "grab 或释放异常：$out" ;;
  esac
fi

# ---------- 4. 僵尸检查 ----------
echo "-- 4. 僵尸进程"
# ⚠️ 只统计「我们自己留下的」僵尸，不要数全系统。
#    设备上 crond 每轮会起一个 `sh` 子进程，偶尔在我们检查的那一瞬间
#    正好是 Z 状态（`comm=sh ppid=<crond>`），旧版脚本把它算成 led_spawn
#    的泄漏，导致这条用例约 1/3 概率假失败（已实测抓到现场）。
#    真正的双 fork 泄漏特征是**已被 init 收养**（ppid=1）或父进程仍是我们
#    的守护进程，所以按这两条判定。
Z=$(rsh 'N=0; DETAIL=""
        for p in /proc/[0-9]*; do
          grep -q "Z (zombie)" $p/status 2>/dev/null || continue
          pp=$(awk "/^PPid:/{print \$2}" $p/status)
          pc=$(cat /proc/$pp/comm 2>/dev/null)
          case "$pp:$pc" in
            1:*|*:volumed*|*:s12audio*|*:test_common*)
              N=$((N+1)); DETAIL="$DETAIL [pid=${p#/proc/} comm=$(cat $p/comm 2>/dev/null) ppid=$pp pcomm=$pc]";;
          esac
        done
        echo "$N$DETAIL"')
case "$Z" in
  0) ok "无僵尸（已排除 crond 等非本仓进程的瞬时子进程）" ;;
  "") bad "无法检查" ;;
  *) bad "存在僵尸：$Z（led_spawn 的双 fork 失效？）" ;;
esac

echo
if [ "$fail" = 0 ]; then
  echo "全部通过"
else
  echo "有失败项，见上"
fi
# EXIT trap 会把设备交还原厂 pns（设计如此：保证失败时也有音频）。
# 但家里那套 Jarvis 耳朵靠 s12audio，跑完回归要能一眼看到怎么切回来。
echo "注意：结束时设备已交还原厂 pns。切回 Jarvis 音频："
echo "  ssh root@$HOST '/etc/init.d/pns stop; /etc/init.d/jarvis-audio start'"
exit "$fail"