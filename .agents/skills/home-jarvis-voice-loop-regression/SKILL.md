---
name: home-jarvis-voice-loop-regression
description: "Project-specific regression workflow for changes to the home-jarvis voice loop, device aliases, tool calling, TTS routing, and wake/follow-up behavior."
---

Use after any change to home-jarvis audio, ASR, TTS, wake/follow-up, tool, or device-alias layers.

0. 人工操作必须给交互提示（硬性要求）
- 任何需要人出声、拍手、走近/远离设备、按键、拔插线、重启设备的步骤，**不允许只打印在终端里**。
- 统一用 `scripts/cue.py` 弹置顶对话框 + 提示音 + 大字倒数 3-2-1，采集结束再"叮"一声并弹通知：
  - 等待型：`.venv/bin/python scripts/cue.py "请面对音箱大声说：客厅灯打开" --seconds 5`
  - 执行型：`.venv/bin/python scripts/cue.py "请拍手 3 次" --seconds 4 --run "<远端录音/取数命令>"`
  - 纯告知：`.venv/bin/python scripts/cue.py "接下来我会重启设备，你不用操作" --ack-only`
- 在 Python 自检里用 `from cue import run_cue`，**录音/取数必须通过 `action=` 或 `--run` 落在红态采集窗口内**（不要先 cue 结束再另起录音）：
  ```python
  box = {}
  rc = run_cue("请拍手 3 次", seconds=4, action=lambda: do_record(), result_box=box)
  ```
  退出码 2 = 人没确认（超时/点跳过）→ 跳过该段并在报告写"未验证"，不要当成通过。
- 无桌面环境（CI/ssh）设 `CUE_NO_GUI=1` 退回终端提示，行为一致。
- 采集窗口内不要播放音乐/语音提示，避免被被测麦克风拾取；提示音只在窗口前后放。

1. Baseline environment
- Run `./scripts/preflight.py`; require Python, uv, ffmpeg, Docker, and zh_CN voices to pass.
- If models changed, verify downloaded file sizes before loading. Example: `zh_CN-huayan-medium.onnx` must be 63,201,423 bytes; a truncated mirror download fails with `Protobuf parsing failed`.

2. Full regression
- Run `./scripts/selftest_all.sh`; require every suite green before deeper manual checks.
- For device or alias changes, run `scripts/gen_aliases.py` without `--rebuild`, inspect `config/aliases.yaml`, and confirm previously collected offline devices remain and are marked `[离线]`. Never hardcode a device name in tests; select the first online light dynamically.

3. Tool and LLM changes
- First isolate plumbing with the `jarvis.app` `--fake-llm` path, then repeat with real DeepSeek.
- Exercise an online light through on, absolute brightness, relative dim, query, and off. After each control call, query Home Assistant state to confirm the real state changed.
- Confirm unavailable devices are refused before service invocation; Xiaomi cloud calls can return 200 and do nothing for unavailable entities.
- For command-like utterances, verify the intent guard forces a tool call instead of allowing a fabricated “already done” answer.

4. TTS and output routing
- Run the output-route self-test: speaker mode must use only `notify` `play_text`, local mode must use only the local speaker, missing speaker must fall back locally, and `both` must cover both paths.
- In speaker mode, confirm `POST /api/services/notify/send_message` to the discovered `play_text` entity returns HTTP 200, and check `小爱勿扰` when sound is expected but absent.
- In local mode, confirm `SayTTS` falls back to Tingting if the selected macOS voice renders near-empty audio.
- For neural TTS changes, rerun text normalization checks for Arabic numbers, e.g. `10秒` must be spoken as `十秒`, and inspect logs for silent `Ignore OOV` warnings.

5. Wake, follow-up, and close-word changes
- Run `selftest_wake.py` and require all assertions to pass.
- End-to-end test: wake with the keyword, speak a second utterance without the wake word inside the FOLLOW_UP window, then use a close word such as `退下吧`. Confirm the farewell is spoken before `gate.sleep()` closes the window, and that a device command immediately afterward is blocked.
- Confirm the FOLLOW_UP window starts after playback ends, using `WakeGate.hold()/release()` wrapped in `try/finally`.

6. Reporting
- Base completion on observed evidence only: HA state/API results, notify HTTP status, self-test output, and actual audio routing. Do not report a device action as done unless state or notify evidence confirms it.
