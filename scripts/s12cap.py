#!/usr/bin/env python3
"""把 S12 的 6 路麦克风**原始**数据抓到 Mac（测量 / 标定专用）。

为什么要这么麻烦：s12audio 会独占 hw:0,0，而上行只发单声道。波束成形开发
第一步必须拿到 6 路裸数据回答「这个阵列到底能不能用」——各路是不是同一时刻
采的、口径多大、增益/极性一致不一致。所以这个脚本做的事就一件：

    stop jarvis-audio → pns stop → /data/s12cap 录 8ch WAV → 拉回 Mac → 重启

产品路径不受影响（跑完一定重启，`try/finally` 保证）。

用法：

    # 请人对着音箱说话 8 秒（默认会弹置顶 HUD 提示 + 倒数）
    .venv/bin/python scripts/s12cap.py --seconds 8 --out /tmp/cap_speech.wav \
        --cue "请面对音箱，用平常音量说：把客厅灯打开，然后再调暗一点"

    # 从电脑喇叭放一段已知信号（白噪声 / 扫频），用于测阵列几何
    .venv/bin/python scripts/s12cap.py --seconds 8 --play /tmp/white.wav \
        --cue "接下来电脑喇叭会放 6 秒白噪声，请保持安静"

    # 只想快速看一眼各路电平
    .venv/bin/python scripts/s12cap.py --seconds 3 --stats-only
"""

from __future__ import annotations

import argparse
import hashlib
import os
import subprocess
import sys
import threading
import time
import wave
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))

DEFAULT_HOST = os.environ.get("S12_HOST", "192.168.1.3")
SSH_OPTS = [
    "-o", "StrictHostKeyChecking=no",
    "-o", "UserKnownHostsFile=/dev/null",
    "-o", "BatchMode=yes",
    "-o", "ConnectTimeout=8",
    "-o", "HostKeyAlgorithms=+ssh-rsa",
    "-o", "PubkeyAcceptedKeyTypes=+ssh-rsa",
]
DPATH = "export PATH=/usr/sbin:/usr/bin:/sbin:/bin"

LOCAL_BIN = ROOT / "tools/s12cap/build/s12cap"
REMOTE_BIN = "/data/s12cap"


def sh(cmd: str, host: str, check: bool = True) -> str:
    """在设备上跑一段 shell；返回 stdout。"""
    p = subprocess.run(
        ["ssh", *SSH_OPTS, f"root@{host}", f"{DPATH}; {cmd}"],
        capture_output=True, text=True, timeout=180,
    )
    if check and p.returncode != 0:
        raise RuntimeError(f"设备命令失败({p.returncode}): {cmd}\n{p.stderr[-800:]}")
    return p.stdout


def push_binary(host: str) -> None:
    """大小不同就重推（设备上没有 sha256sum，用字节数足够）。"""
    if not LOCAL_BIN.exists():
        raise SystemExit(
            f"缺少 {LOCAL_BIN}，先交叉编译：\n"
            "  docker run --rm -v $PWD/tools:/src/tools -w /src/tools/s12cap s12build sh -c 'make'"
        )
    local_size = LOCAL_BIN.stat().st_size
    out = sh(f"ls -l {REMOTE_BIN} 2>/dev/null | awk '{{print $5}}'", host, check=False).strip()
    if out == str(local_size):
        print(f"[s12cap] 设备上已有同版本 {REMOTE_BIN}（{local_size} B）")
        return
    print(f"[s12cap] 推送 {LOCAL_BIN.name} → {REMOTE_BIN}（{local_size} B）")
    data = LOCAL_BIN.read_bytes()
    dig = hashlib.sha1(data).hexdigest()
    # 设备上的 dropbear **没有 sftp-server**，scp 会直接失败（实测：
    # /usr/libexec/sftp-server: not found）。只能用 cat 管道走纯 ssh。
    p = subprocess.run(
        ["ssh", *SSH_OPTS, f"root@{host}", f"{DPATH}; /bin/cat > /tmp/s12cap.new"],
        input=data, capture_output=True, timeout=120,
    )
    if p.returncode != 0:
        raise RuntimeError(f"推送失败: {p.stderr.decode()[-400:]}")
    remote_dig = sh(f"md5sum /tmp/s12cap.new | cut -d' ' -f1", host).strip()
    # 本地算 md5 用于跨工具校验（sha1 只做日志）
    local_md5 = hashlib.md5(data).hexdigest()
    if remote_dig and remote_dig != local_md5:
        raise RuntimeError(f"推送校验失败: local md5={local_md5} remote={remote_dig} ({dig})")
    sh(f"chmod +x /tmp/s12cap.new && mv /tmp/s12cap.new {REMOTE_BIN}", host)
    print("[s12cap] 推送完成，md5 校验一致")


def stop_audio(host: str) -> None:
    """停守护进程 + 释放 hw:0,0，并**等真的释放完**再返回。

    踩过的坑：`jarvis-audio stop` 会启动 supervise 管理的子进程收尾并发起
    `/etc/init.d/pns start`（交还原厂），而 pns 起来是异步的。只 sleep 1s 就
    开跑，会撞上「原厂 pns 刚抢回 hw:0,0」→ `Device or resource busy`。
    所以这里按 /proc 里的真实状态轮询，直到 closed 为止。
    """
    sh(
        """
        if [ -x /etc/init.d/jarvis-audio ]; then /etc/init.d/jarvis-audio stop >/dev/null 2>&1; fi
        i=0
        while [ $i -lt 12 ]; do
            killall s12audio >/dev/null 2>&1
            if [ -x /etc/init.d/pns ]; then /etc/init.d/pns stop >/dev/null 2>&1; fi
            if [ -x /etc/init.d/touchpad ]; then /etc/init.d/touchpad stop >/dev/null 2>&1; fi
            killall touchpad >/dev/null 2>&1
            st=$(head -1 /proc/asound/card0/pcm0c/sub0/status 2>/dev/null)
            if [ "$st" = "closed" ] && ! pidof s12audio >/dev/null 2>&1; then
                echo "STATUS:closed"
                exit 0
            fi
            sleep 1
            i=$((i + 1))
        done
        echo "STATUS:${st:-unknown}"
        """,
        host,
    )
    args_out = sh("head -1 /proc/asound/card0/pcm0c/sub0/status 2>/dev/null", host, check=False)
    print(f"[s12cap] 设备采集设备状态: {args_out.strip() or 'unknown'}")
    if args_out.strip() != "closed":
        print("[s12cap] ⚠️ hw:0,0 仍被占用，采集大概率失败", file=sys.stderr)

def start_audio(host: str, timeout: float = 25.0) -> bool:
    sh(
        "if [ -x /etc/init.d/jarvis-audio ]; then /etc/init.d/jarvis-audio start; fi",
        host, check=False,
    )
    t0 = time.time()
    while time.time() - t0 < timeout:
        out = sh("netstat -ltn 2>/dev/null | grep -c ':8765 '", host, check=False).strip()
        if out not in ("", "0"):
            return True
        time.sleep(1.0)
    return False


def record(host: str, seconds: float, remote: str, gain: float, diag: bool) -> None:
    args = f"-t {seconds:g} -o {remote} -g {gain:g}"
    if diag:
        args += " -d"
    # s12cap 的进度日志走 stderr，必须合并后一起看
    p = subprocess.run(
        ["ssh", *SSH_OPTS, f"root@{host}", f"{DPATH}; {REMOTE_BIN} {args}"],
        capture_output=True, text=True, timeout=180,
    )
    for line in ((p.stdout or "") + (p.stderr or "")).splitlines():
        if line.strip():
            print("  " + line.strip())
    if p.returncode != 0:
        raise RuntimeError(f"s12cap 退出码 {p.returncode}")


def fetch(host: str, remote: str, local: Path) -> None:
    local.parent.mkdir(parents=True, exist_ok=True)
    p = subprocess.run(
        ["ssh", *SSH_OPTS, f"root@{host}", f"{DPATH}; /bin/cat {remote}"],
        capture_output=True, timeout=180,
    )
    if p.returncode != 0 or not p.stdout:
        raise RuntimeError(f"取文件失败: {p.stderr.decode()[-400:]}")
    local.write_bytes(p.stdout)


def describe(path: Path) -> None:
    with wave.open(str(path), "rb") as w:
        ch, rate, n = w.getnchannels(), w.getframerate(), w.getnframes()
        raw = w.readframes(n)
    x = np.frombuffer(raw, dtype="<i2").reshape(-1, ch).astype(np.float32) / 32768.0
    print(f"\n[s12cap] {path}  {ch}ch {rate}Hz  {n / rate:.2f}s")
    for c in range(min(ch, 6)):
        v = x[:, c]
        rms = float(np.sqrt(np.mean(v * v)) + 1e-12)
        peak = float(np.max(np.abs(v)))
        clip = int(np.sum(np.abs(v) >= 0.999))
        print(
            f"  ch{c}: rms={20 * np.log10(rms):7.1f} dBFS  peak={20 * np.log10(peak + 1e-12):7.1f}"
            f" dBFS  clip={clip:5d}  dc={float(np.mean(v)):+.5f}"
        )
    if ch > 6:
        v = x[:, 6]
        print(f"  ch6: rms={20 * np.log10(np.sqrt(np.mean(v * v)) + 1e-12):7.1f} dBFS (未接)")
        v = x[:, 7]
        print(f"  ch7: rms={20 * np.log10(np.sqrt(np.mean(v * v)) + 1e-12):7.1f} dBFS (播放环回)")


def main() -> int:
    ap = argparse.ArgumentParser(description="S12 六麦原始多通道采集")
    ap.add_argument("--seconds", type=float, default=8.0)
    ap.add_argument("--out", default="/tmp/s12cap.wav")
    ap.add_argument("--host", default=DEFAULT_HOST)
    ap.add_argument("--gain", type=float, default=20.0, help="与 s12audio 一致的预放大")
    ap.add_argument("--play", default="", help="采集窗口内用电脑喇叭播放该音频文件")
    ap.add_argument("--play-delay", type=float, default=0.6)
    ap.add_argument("--cue", default="", help="置顶提示文案（默认按是否 --play 自动生成）")
    ap.add_argument("--no-cue", action="store_true")
    ap.add_argument("--diag", action="store_true", help="同时让设备端打印 TDOA 诊断")
    ap.add_argument("--stats-only", action="store_true", help="只打印电平，不保留远端文件")
    ap.add_argument("--keep-down", action="store_true", help="结束后不重启 jarvis-audio（调试用）")
    ap.add_argument("--mac-mic", action="store_true",
                    help="同时用电脑自带麦克风录一份「基准音轨」，用于证明到底放了什么")
    args = ap.parse_args()

    host = args.host
    out = Path(args.out)
    remote = f"/tmp/s12cap_{int(time.time())}.wav"

    print(f"[s12cap] host={host} 秒数={args.seconds:g}")
    push_binary(host)
    stop_audio(host)

    played: list[float] = []

    mic_wav = out.with_suffix(".mac.wav")
    mic_proc: list[subprocess.Popen] = []

    def start_mac_mic() -> None:
        if not args.mac_mic:
            return
        mic_proc.append(
            subprocess.Popen(
                ["ffmpeg", "-y", "-loglevel", "error", "-f", "avfoundation", "-i", ":0",
                 "-t", str(args.seconds + 1.0), "-ar", "16000", "-ac", "1", str(mic_wav)]
            )
        )

    def do_capture() -> None:
        th = None
        if args.play:
            def _play() -> None:
                time.sleep(args.play_delay)
                t0 = time.time()
                subprocess.run(["afplay", args.play], check=False)
                played.append(time.time() - t0)

            th = threading.Thread(target=_play, daemon=True)
            th.start()
        start_mac_mic()
        t0 = time.time()
        record(host, args.seconds, remote, args.gain, args.diag)
        wall = time.time() - t0
        print(f"  [host] 采集墙钟 {wall:.2f}s / 目标 {args.seconds:g}s（差 {wall - args.seconds:+.2f}s）")
        played.append(wall)
        if th:
            th.join()

    try:
        cue_text = args.cue or (
            f"接下来电脑喇叭会放 {args.seconds:g} 秒测试音，请保持安静"
            if args.play
            else f"请面对音箱说话（约 {args.seconds:g} 秒），说完保持安静直到「叮」"
        )
        rc = 0
        if args.no_cue:
            do_capture()
        else:
            from cue import run_cue

            # action 落在红态采集窗口内：提示 → 倒数 → 采集 → 叮
            rc = run_cue(cue_text, seconds=args.seconds + 2.0, action=do_capture)
        if rc == 2:
            print("[s12cap] 人没有确认，本次采集作废", file=sys.stderr)
            return 2
        if rc not in (0,):
            print(f"[s12cap] cue 返回 {rc}", file=sys.stderr)

        fetch(host, remote, out)
        sh(f"rm -f {remote}", host, check=False)
        describe(out)
        if mic_proc:
            mic_proc[0].wait(timeout=20)
            if mic_wav.exists():
                print(f"\n[s12cap] 电脑麦基准音轨 → {mic_wav}" + "（用于核对确实放了声音）")
        return 0
    finally:
        if args.keep_down:
            print("[s12cap] --keep-down：jarvis-audio 保持停止")
        else:
            ok = start_audio(host)
            print(f"[s12cap] jarvis-audio {'已恢复' if ok else '❌ 未恢复（手动检查 /data/log/jarvis-audio.log）'}")


if __name__ == "__main__":
    raise SystemExit(main())