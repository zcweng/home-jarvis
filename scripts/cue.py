#!/usr/bin/env python3
"""测试交互提示（cue）：保证"请你说话 / 拍手 / 按键"这类指令一定被看到。

痛点：把提示只打在终端里，人容易漏看，测试就白跑。
做法：屏幕上一个**常驻置顶的 HUD**，全程不变窗口、只变内容：

    ① 提示 + 【开始】【跳过】按钮（同时"叮"一声）
    ② 点开始 → 大字倒数 3-2-1（每档一声提示音）
    ③ 采集中 → 红色边框 + 剩余秒数 + 进度条（这一段时间不出声，免得被麦克风拾取）
    ④ 结束 → 绿色「✅ 完成」+ 三声"叮" + 系统通知

用法：

    # 最常用：请人说话 5 秒
    .venv/bin/python scripts/cue.py "请对着音箱大声说：客厅灯打开" --seconds 5

    # 请人拍手
    .venv/bin/python scripts/cue.py "请连续拍手 3 次" --seconds 3

    # 把被提示的命令一起跑进去（提示 → 倒数 → 执行 → 结束提示）
    .venv/bin/python scripts/cue.py "请大声倒数 1 到 10" --seconds 10 \\
        --run "ssh root@192.168.1.3 'cat /tmp/rec.raw' > /tmp/rec.raw"

    # 只弹个提示，不等待（用于长流程中的"该你了"）
    .venv/bin/python scripts/cue.py "接下来我会重启设备，你不用操作" --ack-only

    # 人可能不在屏幕前：把提示也念出来
    .venv/bin/python scripts/cue.py "请拍手三次" --seconds 3 --speak

退出码：0 正常完成；2 人没确认（超时/点了跳过）；3 被提示的命令失败。
环境变量 CUE_NO_GUI=1 强制退回终端提示（CI / ssh / 不想被窗口打扰）。
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parent.parent

DEFAULT_TITLE = "home-jarvis 测试"
SOUND_START = "/System/Library/Sounds/Glass.aiff"
SOUND_END = "/System/Library/Sounds/Submarine.aiff"
VOICE_CANDIDATES = ("Tingting", "Meijia")

# HUD 配色
C_IDLE_BG = "#f5f5f7"
C_IDLE_FG = "#1d1d1f"
C_HOLD_BG = "#fff4e5"
C_HOLD_FG = "#8a4b00"
C_REC_BG = "#fdecea"
C_REC_FG = "#b3261e"
C_DONE_BG = "#e7f6ec"
C_DONE_FG = "#12643a"

EXIT_OK = 0
EXIT_UNCONFIRMED = 2
EXIT_COMMAND_FAILED = 3


# ─────────────────────────── 基础能力 ───────────────────────────


def _which(name: str) -> str | None:
    return shutil.which(name)


def _no_gui() -> bool:
    """CUE_NO_GUI=1 强制走终端提示（CI / 无桌面环境 / 不想被弹窗打扰时）。"""
    return os.environ.get("CUE_NO_GUI", "").strip().lower() in {"1", "true", "yes"}


def _autoclick_ms() -> float:
    """自检/演示用：CUE_AUTOCLICK=1.5 → 1.5 秒后自动点「开始」。默认 0（关闭）。"""
    try:
        return max(0.0, float(os.environ.get("CUE_AUTOCLICK", "0") or 0))
    except ValueError:
        return 0.0


def have_gui() -> bool:
    if _no_gui():
        return False
    return sys.platform == "darwin" and _which("osascript") is not None


def have_sound() -> bool:
    return sys.platform == "darwin" and _which("afplay") is not None and Path(SOUND_START).exists()


def pick_voice() -> str | None:
    """挑一个可用的中文音色；都没有就不指定音色。"""
    if not _which("say"):
        return None
    try:
        out = subprocess.run(["say", "-v", "?"], capture_output=True, text=True, timeout=10).stdout
    except Exception:  # noqa: BLE001
        return None
    names = {line.split("  ")[0].strip().lower() for line in out.splitlines() if line.strip()}
    for cand in VOICE_CANDIDATES:
        if cand.lower() in names:
            return cand
    for line in out.splitlines():
        if " zh_CN " in line and line.strip():
            return line.split("  ")[0].strip()
    return None


def beep(sound: str = SOUND_START, *, wait: bool = False) -> None:
    """放一声提示音。默认不阻塞（避免拖慢采集窗口）。"""
    if not have_sound():
        sys.stdout.write("\a")
        sys.stdout.flush()
        return
    try:
        if wait:
            subprocess.run(["afplay", sound], check=False, timeout=5)
        else:
            threading.Thread(
                target=lambda: subprocess.run(["afplay", sound], check=False, timeout=5),
                daemon=True,
            ).start()
    except Exception:  # noqa: BLE001
        pass


def speak(text: str, *, wait: bool = True) -> None:
    if not _which("say"):
        return
    cmd = ["say", "-r", "200"]
    voice = pick_voice()
    if voice:
        cmd += ["-v", voice]
    cmd.append(text)
    if not wait:
        threading.Thread(target=lambda: subprocess.run(cmd, check=False), daemon=True).start()
        return
    try:
        subprocess.run(cmd, check=False, timeout=30)
    except Exception:  # noqa: BLE001
        pass


def notify(title: str, text: str) -> None:
    if not have_gui():
        return
    script = f'display notification {_as(text)} with title {_as(title)} sound name "Glass"'
    try:
        subprocess.run(["osascript", "-e", script], check=False, timeout=10)
    except Exception:  # noqa: BLE001
        pass


def _as(s: str) -> str:
    """AppleScript 字符串字面量（转义引号与反斜杠）。"""
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _spoken(text: str) -> str:
    """把提示文案变成能念顺口的一句：去掉括注、换行、多余标点，限制长度。"""
    flat = " ".join(part.strip() for part in text.replace("（", " ").replace("）", " ").splitlines())
    flat = flat.split("(")[0].strip("，,。.；;：: ")
    flat = re.sub(r"\s+", " ", flat)
    return flat[:60] if flat else "请按提示操作"


# ─────────────────────────── HUD（置顶大字窗口） ───────────────────────────


class Hud:
    """一个常驻置顶窗口：提示 → 倒数 → 采集中 → 完成，只换内容不换窗口。"""

    def __init__(self, title: str = DEFAULT_TITLE) -> None:
        import tkinter as tk  # noqa: PLC0415

        self.tk = tk
        self.root = tk.Tk()
        self.root.title(title)
        self.root.configure(bg=C_IDLE_BG)
        self.root.attributes("-topmost", True)
        self.root.resizable(False, False)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self._closed_by_user = False
        self._choice: str | None = None

        self.f_title = tk.Label(self.root, bg=C_IDLE_BG, fg="#6e6e73", font=("PingFang SC", 13))
        self.f_title.pack(fill="x", padx=22, pady=(16, 0))

        self.f_head = tk.Label(
            self.root, bg=C_IDLE_BG, fg=C_IDLE_FG, font=("PingFang SC", 24, "bold"),
            wraplength=560, justify="center",
        )
        self.f_head.pack(fill="both", expand=True, padx=22, pady=(6, 4))

        self.f_sub = tk.Label(
            self.root, bg=C_IDLE_BG, fg=C_IDLE_FG, font=("PingFang SC", 15),
            wraplength=560, justify="center",
        )
        self.f_sub.pack(fill="both", expand=True, padx=22, pady=(0, 6))

        self.f_big = tk.Label(self.root, bg=C_IDLE_BG, fg=C_IDLE_FG, font=("PingFang SC", 96, "bold"))
        self.f_big.pack(fill="both", expand=True)

        self.bar = tk.Canvas(self.root, height=10, bg=C_IDLE_BG, highlightthickness=0)
        self.bar.pack(fill="x", padx=26, pady=(0, 8))
        self._bar_bg = self.bar.create_rectangle(0, 0, 0, 10, fill="#d9d9de", outline="")

        self.btn_row = tk.Frame(self.root, bg=C_IDLE_BG)
        self.btn_row.pack(fill="x", padx=22, pady=(0, 18))
        self._buttons: dict[str, Any] = {}

        self.root.withdraw()
        self._geometry(560, 320)

    # 窗口定位：屏幕上方居中偏右，不挡脸也不挡终端
    def _geometry(self, w: int, h: int) -> None:
        sw = self.root.winfo_screenwidth()
        x = max(20, (sw - w) // 2)
        self.root.geometry(f"{w}x{h}+{x}+60")

    def _on_close(self) -> None:
        self._closed_by_user = True
        self.root.withdraw()

    def _paint(self, bg: str, fg: str) -> None:
        for w in (self.root, self.f_title, self.f_head, self.f_sub, self.f_big, self.bar, self.btn_row):
            w.configure(bg=bg)
        self.f_head.configure(fg=fg)
        self.f_sub.configure(fg=fg)
        self.f_big.configure(bg=bg, fg=fg)

    def _clear_buttons(self) -> None:
        for b in self._buttons.values():
            b.destroy()
        self._buttons.clear()

    def _populate_buttons(self, labels: tuple[str, ...]) -> None:
        self._clear_buttons()
        for i, label in enumerate(labels):
            btn = self.tk.Button(
                self.btn_row, text=label, font=("PingFang SC", 15),
                width=10, relief="flat", bd=0, cursor="hand2",
                bg="#ffffff" if i else "#0a84ff",
                fg="#1d1d1f" if i else "#ffffff",
                activebackground="#e8e8ed" if i else "#0070e0",
                activeforeground="#1d1d1f" if i else "#ffffff",
                command=lambda lb=label: self._click(lb),
            )
            btn.pack(side="left", expand=True, padx=6, ipady=8)
            self._buttons[label] = btn

    def _click(self, label: str) -> None:
        self._choice = label
        self.root.quit()

    def _bar_set(self, frac: float) -> None:
        frac = max(0.0, min(1.0, frac))
        w = max(1, self.bar.winfo_width() or 1)
        self.bar.coords(self._bar_bg, 0, 0, int(w * frac), 10)

    def _show(self, big_height: bool) -> None:
        """让窗口真的到前台（macOS 上 lift+topmost+focus 三连）。"""
        h = 380 if big_height else 300
        self._geometry(560, h)
        self.root.deiconify()
        self.root.lift()
        self.root.attributes("-topmost", True)
        try:
            self.root.focus_force()
        except Exception:  # noqa: BLE001
            pass
        self.root.update_idletasks()
        self.root.update()

    def _pump(self, seconds: float) -> None:
        """空转刷新 UI，保持窗口响应。"""
        end = time.monotonic() + seconds
        while True:
            self.root.update()
            left = end - time.monotonic()
            if left <= 0:
                break
            time.sleep(min(0.03, left))

    # ── 四个阶段 ──

    def prompt(
        self,
        text: str,
        *,
        title: str = DEFAULT_TITLE,
        buttons: tuple[str, ...] = ("开始",),
        timeout: int = 180,
    ) -> str | None:
        lines = [ln for ln in text.strip().splitlines() if ln.strip()]
        head = lines[0] if lines else "请按提示操作"
        sub = "\n".join(lines[1:])
        self._paint(C_IDLE_BG, C_IDLE_FG)
        self.f_title.configure(text=f"● {title}")
        self.f_head.configure(text=head)
        self.f_sub.configure(text=sub)
        self.f_big.configure(text="")
        self._bar_set(0)
        self._close_btn_missing = False
        self._populate_buttons(buttons)
        self._choice = None
        self._show(big_height=False)

        end = time.monotonic() + timeout
        auto = _autoclick_ms()
        if auto:
            # 自检/演示用：CUE_AUTOCLICK=1.5 表示 1.5 秒后自动点第一颗按钮
            self.root.after(int(auto * 1000), lambda: self._click(buttons[0]))
        while self._choice is None:
            self.root.update()
            if self._closed_by_user:
                return None
            if time.monotonic() > end:
                self.root.withdraw()
                return None
            time.sleep(0.03)
        return self._choice

    def hold(self, seconds: int) -> None:
        """倒数阶段：大字 3-2-1，每档"叮"一声。"""
        self._paint(C_HOLD_BG, C_HOLD_FG)
        self.f_title.configure(text="● 准备开始")
        self.f_head.configure(text="马上开始，请就位")
        self.f_sub.configure(text="")
        self._clear_buttons()
        self._bar_set(0)
        self._show(big_height=True)
        for n in range(seconds, 0, -1):
            self.f_big.configure(text=str(n))
            self.root.update()
            beep(SOUND_START)
            self._pump(1.0)

    def live(self, headline: str, sub: str, seconds: float | None) -> None:
        """切到「采集中」红态。seconds=None 只摆好 UI，由调用方继续泵；有秒数则自己倒数。"""
        self._paint(C_REC_BG, C_REC_FG)
        self.f_title.configure(text="● 采集中 · 请保持动作")
        self.f_head.configure(text=headline)
        self.f_sub.configure(text=sub)
        self._clear_buttons()
        self.f_big.configure(text="")
        self._bar_set(0)
        self._show(big_height=True)

        if seconds is None:
            # 不定时长：只建好红态，调用方边跑边 update
            return
        self.countdown_capture(seconds, headline, sub)

    def countdown_capture(self, seconds: float, headline: str, sub: str) -> None:
        """确定时长采集：大字倒计时 + 进度条 + 每秒闪一下。"""
        self._paint(C_REC_BG, C_REC_FG)
        self.f_title.configure(text="● 采集中 · 请保持动作")
        self.f_head.configure(text=headline)
        self.f_sub.configure(text=sub)
        self._clear_buttons()
        self._show(big_height=True)

        start = time.monotonic()
        last_int = None
        while True:
            self.root.update()
            el = time.monotonic() - start
            left = max(0.0, seconds - el)
            self._bar_set(el / seconds if seconds else 1.0)
            shown = int(left + 0.999) if left > 0 else 0
            if shown != last_int:
                last_int = shown
                self.f_big.configure(text=str(shown) if shown > 0 else "✓")
                # 剩下 3 秒内每秒闪一下底色，眼睛余光也能看见
                if shown <= 3 and shown > 0:
                    self._paint(C_REC_BG, C_REC_FG)
            if left <= 0:
                break
            time.sleep(0.04)

    def done(self, sub: str = "", *, dwell: float = 2.0) -> None:
        self._paint(C_DONE_BG, C_DONE_FG)
        self.f_title.configure(text="● 完成")
        self.f_head.configure(text="✅ 这一段结束了")
        self.f_sub.configure(text=sub)
        self.f_big.configure(text="")
        self._bar_set(1.0)
        self._clear_buttons()
        self._show(big_height=False)
        self._pump(dwell)
        self.root.withdraw()

    def close(self) -> None:
        try:
            self.root.destroy()
        except Exception:  # noqa: BLE001
            pass


def _make_hud(title: str = DEFAULT_TITLE) -> Hud | None:
    """有桌面就建 HUD；失败（无 DISPLAY / tk 缺失）返回 None 走终端兜底。"""
    if _no_gui():
        return None
    try:
        return Hud(title)
    except Exception:  # noqa: BLE001
        return None


# ─────────────────────────── 终端兜底 ───────────────────────────


def dialog(
    text: str,
    title: str = DEFAULT_TITLE,
    buttons: tuple[str, ...] = ("开始",),
    default: str | None = None,
    timeout: int = 180,
) -> str | None:
    """无 HUD 时的兜底：AppleScript 原生弹窗 → 再退到终端选择。"""
    if not have_gui():
        sys.stdout.write(f"\n=== {title} ===\n{text}\n")
        if not sys.stdin.isatty():
            print("（非交互终端，自动确认）", flush=True)
            return buttons[0]
        for i, b in enumerate(buttons, 1):
            sys.stdout.write(f"  {i}) {b}\n")
        sys.stdout.write(f"选择 [1-{len(buttons)}]，直接回车 = {buttons[0]}: ")
        sys.stdout.flush()
        line = sys.stdin.readline().strip()
        if not line:
            return buttons[0]
        try:
            idx = int(line) - 1
            return buttons[idx] if 0 <= idx < len(buttons) else buttons[0]
        except ValueError:
            return line if line in buttons else buttons[0]

    default = default or buttons[0]
    btn_list = ", ".join(_as(b) for b in buttons)
    script = (
        f"display dialog {_as(text)} with title {_as(title)} "
        f"buttons {{{btn_list}}} default button {_as(default)} "
        f"with icon note giving up after {int(timeout)}"
    )
    try:
        proc = subprocess.run(
            ["osascript", "-e", script], capture_output=True, text=True, timeout=timeout + 15
        )
    except subprocess.TimeoutExpired:
        return None
    if proc.returncode != 0:
        return None
    out = proc.stdout.strip()
    if "gave up:true" in out:
        return None
    for b in buttons:
        if f"button returned:{b}" in out:
            return b
    return None


# ─────────────────────────── 主流程 ───────────────────────────


def _pump_until(
    th: threading.Thread,
    hud: Hud | None,
    *,
    seconds: float = 0,
    headline: str = "请保持动作",
    sub: str = "",
) -> None:
    """主线程泵 HUD，等后台线程（或最短 seconds）结束。tk 必须在主线程。"""
    if hud is not None:
        if seconds > 0:
            hud.live(headline, sub, None)
        else:
            hud.live(headline or "正在执行取数/操作", sub or "请稍等，不要动设备", None)

    start = time.monotonic()
    last_int: int | None = None
    while True:
        alive = th.is_alive()
        el = time.monotonic() - start
        # 有明确窗口：至少撑满 seconds；同时等 action/命令跑完
        if seconds > 0:
            done = (not alive) and el >= seconds
        else:
            done = not alive
        if done:
            break
        if hud is not None:
            if seconds > 0:
                left = max(0.0, seconds - el)
                hud._bar_set(el / seconds if seconds else 1.0)
                shown = int(left + 0.999) if left > 0 else 0
                if shown != last_int:
                    last_int = shown
                    hud.f_big.configure(text=str(shown) if shown > 0 else "✓")
            else:
                sec = int(el)
                if sec != last_int:
                    last_int = sec
                    hud.f_big.configure(text=f"{sec}s")
            hud.root.update()
        time.sleep(0.04)
    th.join()


def _run_with_hud(cmd: str, hud: Hud | None, *, seconds: float = 0) -> int:
    """跑外部命令；HUD 在场时显示已用时间，避免"卡住了"的错觉。"""
    rc_box: dict[str, int] = {}

    def worker() -> None:
        rc_box["rc"] = subprocess.call(cmd, shell=True)

    th = threading.Thread(target=worker, daemon=True)
    th.start()
    _pump_until(th, hud, seconds=seconds, headline="正在执行取数/操作", sub="请稍等，不要动设备")
    return rc_box.get("rc", 1)


def _run_action(
    action: Callable[[], Any],
    hud: Hud | None,
    *,
    seconds: float,
    headline: str,
    sub: str,
    result_box: dict[str, Any],
) -> int:
    """在后台跑 Python 回调，主线程负责 HUD 倒计时。"""

    def worker() -> None:
        try:
            result_box["value"] = action()
            result_box["rc"] = 0
        except Exception as exc:  # noqa: BLE001
            result_box["error"] = exc
            result_box["rc"] = 1

    th = threading.Thread(target=worker, daemon=True)
    th.start()
    _pump_until(th, hud, seconds=seconds, headline=headline, sub=sub)
    if result_box.get("error") is not None:
        print(f"❌ action 失败：{result_box['error']}", file=sys.stderr, flush=True)
        return 1
    return int(result_box.get("rc", 1))


def run_cue(
    text: str,
    *,
    title: str = DEFAULT_TITLE,
    seconds: float = 0,
    countdown_s: int = 3,
    command: str | None = None,
    action: Callable[[], Any] | None = None,
    result_box: dict[str, Any] | None = None,
    ack_only: bool = False,
    quiet: bool = False,
    speak_text: bool = False,
    timeout: int = 180,
    configurator: Callable[[Hud], None] | None = None,
) -> int:
    """核心流程：提示 →（倒数）→（采集/执行）→ 完成提示。

    - command：确认后跑 shell（取数/ssh）
    - action：确认后跑 Python 回调（录音等）；返回值写入 result_box['value']
    - seconds：采集窗口最短时长；与 action/command 并行显示红态倒计时
    - configurator：可选，拿 HUD 做自定义改字（供 selftest 复用同一窗口）
    """
    if command and action is not None:
        raise ValueError("run_cue: command 与 action 不能同时传")

    msg = text.strip()
    if seconds and "秒" not in msg:
        msg += f"\n（约 {seconds:g} 秒，结束后我会「叮」一声）"

    hud = _make_hud(title)
    try:
        if not quiet:
            beep(SOUND_START)
        if speak_text and not quiet:
            speak(_spoken(msg), wait=False)

        if hud is not None:
            choice = hud.prompt(msg, title=title, buttons=("开始", "跳过"), timeout=timeout)
        else:
            choice = dialog(msg, title=title, buttons=("开始", "跳过"), timeout=timeout)

        if choice is None:
            print("⚠️  提示没人确认（超时或关窗），本段人工测试记为「未验证」。", file=sys.stderr, flush=True)
            return EXIT_UNCONFIRMED
        if choice == "跳过":
            print("⏭  已跳过本段人工测试（记为「未验证」）。", file=sys.stderr, flush=True)
            return EXIT_UNCONFIRMED

        if configurator is not None and hud is not None:
            configurator(hud)

        if countdown_s > 0:
            if hud is not None:
                hud.hold(countdown_s)
            else:
                for n in range(countdown_s, 0, -1):
                    speak(str(n))
                    time.sleep(0.1)

        if ack_only:
            print("✅ 已确认提示。", flush=True)
            if hud is not None:
                hud.done("按提示继续后面的流程", dwell=1.2)
            return EXIT_OK

        started = time.monotonic()
        rc = 0
        head = msg.splitlines()[0]
        sub = "\n".join(msg.splitlines()[1:])
        if command:
            print(f"▶ 执行：{command}", flush=True)
            rc = _run_with_hud(command, hud, seconds=seconds)
            if rc != 0:
                print(f"❌ 命令失败（退出码 {rc}）", file=sys.stderr, flush=True)
        elif action is not None:
            box = result_box if result_box is not None else {}
            rc = _run_action(action, hud, seconds=seconds, headline=head, sub=sub, result_box=box)
        elif seconds > 0:
            if hud is not None:
                hud.countdown_capture(seconds, head, sub)
            else:
                time.sleep(seconds)

        elapsed = time.monotonic() - started
        beep(SOUND_END)
        notify("home-jarvis 测试", f"结束：{msg.splitlines()[0][:60]}（{elapsed:.1f}s）")
        if hud is not None:
            hud.done(f"用时 {elapsed:.2f}s" + ("（命令失败）" if rc else ""), dwell=2.2)
        print(f"{'❌' if rc else '✅'} 本段结束（{elapsed:.2f}s）。", flush=True)
        return EXIT_COMMAND_FAILED if rc else EXIT_OK
    finally:
        if hud is not None:
            hud.close()


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description="测试交互提示：置顶 HUD + 提示音 + 大字倒数，避免漏看人工操作要求。",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument("text", help="要人做的事，例如：请面对音箱大声说：客厅灯打开")
    p.add_argument("--title", default=DEFAULT_TITLE, help="窗口标题")
    p.add_argument("--seconds", type=float, default=0, help="采集窗口时长（秒），到点自动结束提示")
    p.add_argument("--countdown", type=int, default=3, dest="countdown_s", help="开始前大字倒数秒数（默认 3，0 关闭）")
    p.add_argument("--run", dest="command", default=None, help="确认后执行的命令（替代 sleep 等待）")
    p.add_argument("--ack-only", action="store_true", help="只确认提示，不等待、不执行")
    p.add_argument("--quiet", action="store_true", help="不出提示音、不念提示")
    p.add_argument("--speak", dest="speak_text", action="store_true", help="把提示文案也念出来（人不在屏幕前也能听到）")
    p.add_argument("--timeout", type=int, default=180, help="等待人确认的超时秒数（默认 180）")
    args = p.parse_args(argv)

    return run_cue(
        args.text,
        title=args.title,
        seconds=args.seconds,
        countdown_s=args.countdown_s,
        command=args.command,
        ack_only=args.ack_only,
        quiet=args.quiet,
        speak_text=args.speak_text,
        timeout=args.timeout,
    )


if __name__ == "__main__":
    raise SystemExit(main())