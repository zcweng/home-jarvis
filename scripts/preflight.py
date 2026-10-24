#!/usr/bin/env python3
"""home-jarvis 环境自检。

用法：
    python3 scripts/preflight.py

只依赖标准库；sounddevice 若未安装则回落到 system_profiler 探测音频设备。
退出码：0 = 全部就绪，1 = 有阻塞项。
"""

from __future__ import annotations

import json
import platform
import shutil
import subprocess
import sys

OK, WARN, FAIL = "✅", "⚠️ ", "❌"
issues: list[str] = []


def sh(cmd: list[str], timeout: int = 10) -> tuple[int, str]:
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return p.returncode, (p.stdout + p.stderr).strip()
    except (FileNotFoundError, subprocess.TimeoutExpired) as e:
        return 127, str(e)


def check_python() -> None:
    v = sys.version_info
    mark = OK if v >= (3, 10) else FAIL
    if v < (3, 10):
        issues.append("Python 版本过低，需要 >= 3.10")
    print(f"{mark} Python {v.major}.{v.minor}.{v.micro}  ({sys.executable})")
    print(f"{OK} 平台 {platform.platform()}  arm64={platform.machine()}")


def check_tools() -> None:
    # 工具名 -> (是否阻塞, 用途)
    tools = {
        "uv": (True, "依赖管理"),
        "ffmpeg": (False, "音频解码/重采样兜底"),
        "docker": (True, "跑 Home Assistant"),
        "say": (False, "macOS 离线 TTS（P0 过渡用）"),
    }
    for name, (blocking, why) in tools.items():
        path = shutil.which(name)
        if path:
            print(f"{OK} {name:<7} {path}   —— {why}")
        else:
            print(f"{FAIL if blocking else WARN} {name:<7} 未安装   —— {why}")
            if blocking:
                issues.append(f"缺少 {name}（{why}）")


def check_docker() -> None:
    if not shutil.which("docker"):
        return
    code, out = sh(["docker", "info", "--format", "{{.ServerVersion}} | {{.OperatingSystem}}"], 20)
    if code == 0 and out:
        print(f"{OK} Docker daemon 运行中：{out}")
    else:
        print(f"{FAIL} Docker 已安装但守护进程未运行（打开 Docker Desktop 再试）")
        issues.append("Docker daemon 未运行")


def audio_via_sounddevice() -> bool:
    try:
        import sounddevice as sd  # type: ignore
    except ImportError:
        return False
    print(f"{OK} sounddevice 已安装 {sd.__version__}")
    ins, outs = [], []
    for idx, d in enumerate(sd.query_devices()):
        if d["max_input_channels"] > 0:
            ins.append((idx, d["name"], d["default_samplerate"]))
        if d["max_output_channels"] > 0:
            outs.append((idx, d["name"], d["default_samplerate"]))
    print("   输入设备：")
    for i, n, sr in ins:
        print(f"     [{i}] {n}  ({int(sr)} Hz)")
    print("   输出设备：")
    for i, n, sr in outs:
        print(f"     [{i}] {n}  ({int(sr)} Hz)")
    has_headset = any("外置" in n or "Headset" in n or "耳机" in n for _, n, _ in ins)
    if not has_headset:
        print(f"{WARN} 没看到「外置麦克风」，耳机可能没插好（或 TRRS 接法不兼容 CTIA）")
    return True


def audio_via_profiler() -> None:
    code, out = sh(["system_profiler", "SPAudioDataType"], 30)
    if code != 0 or not out:
        print(f"{WARN} 无法读取音频设备列表")
        return
    print("   音频设备（来自 system_profiler）：")
    for line in out.splitlines():
        s = line.strip()
        if s.endswith(":") and not s.startswith(("Audio:", "Devices:")):
            print(f"     - {s[:-1]}")
    print(f"{WARN} sounddevice 未安装，看不到具体索引；装好后会打印完整设备表")


def check_tts_voices() -> None:
    if not shutil.which("say"):
        return
    code, out = sh(["say", "-v", "?"], 20)
    zh = [l.split()[0] for l in out.splitlines() if "zh_CN" in l]
    if zh:
        print(f"{OK} 系统中文语音可用：{', '.join(zh[:6])}（离线 TTS 兜底）")
    else:
        print(f"{WARN} 未发现 zh_CN 语音，需在「系统设置 → 辅助功能 → 朗读内容」里下载中文语音")


def check_env_file() -> None:
    import os
    from pathlib import Path

    env = Path(__file__).resolve().parent.parent / ".env"
    if not env.exists():
        print(f"{WARN} 没有 .env（复制 .env.example 后填 HA_TOKEN / DEEPSEEK_KEY）")
        return
    missing = [k for k in ("HA_TOKEN", "DEEPSEEK_KEY") if not os.environ.get(k)]
    txt = env.read_text(encoding="utf-8", errors="ignore")
    miss_in_file = [k for k in ("HA_TOKEN", "DEEPSEEK_KEY") if f"{k}=" not in txt]
    if miss_in_file:
        print(f"{WARN} .env 缺字段：{', '.join(miss_in_file)}")
    else:
        print(f"{OK} .env 存在且关键字段齐全")
    if missing:
        print(f"   （注意：shell 环境变量里没导出 {', '.join(missing)}，程序需自己读 .env）")


def main() -> int:
    print("=" * 68)
    print(" home-jarvis 环境自检")
    print("=" * 68)
    check_python()
    print()
    check_tools()
    print()
    check_docker()
    print()
    if not audio_via_sounddevice():
        audio_via_profiler()
    print()
    check_tts_voices()
    print()
    check_env_file()
    print()
    print("=" * 68)
    if issues:
        print(f" 阻塞项 {len(issues)} 个：")
        for i in issues:
            print(f"   - {i}")
        print("=" * 68)
        return 1
    print(" 没有阻塞项，可以开始 P0-a（PTT 语音闭环）")
    print("=" * 68)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
