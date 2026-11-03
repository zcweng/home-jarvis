---
name: jarvis-voice-loop-regression
description: "Gate any change to home-jarvis's audio/ASR/KWS/wake/LLM layers with preflight, the 9-suite selftest run, a live end-to-end mic check, and latency/mic-hygiene verification before declaring the change done."
candidate: true
---

<!-- evidence: session-81f84430-cd61-4565-b61f-146ed260cb91 — Only one archived session exists, so this cannot yet be a normal two-session skill; the P0 loop-closure session documents the full validation gate (preflight, 9 suites, live run, latency baselines), so store it as a candidate for user confirmation. -->

Use this whenever you touch jarvis/audio.py, asr.py, kws.py, wake.py, vad.py, tts.py, llm.py, intent.py, config/aliases.yaml, or the HA tool layer.

1. Environment check
   - Run `uv run scripts/preflight.py`; require Python 3.12.x, uv, ffmpeg, Docker, zh_CN voices.
   - If Docker must be touched: `DOCKER_CONFIG=/tmp/dcfg docker compose ...` (the Desktop credsStore helper is broken on this machine).
   - Confirm mic selection is by name substring (`INPUT_DEVICE=外置麦克风`), never by device index; if unmatched, the fallback must warn loudly.

2. Regression run
   - Run `./scripts/selftest_all.sh` and require all 9 suites green (preflight, audio round-trip/barge-in, VAD segmentation, HA tool layer, model assertions, full chain, KWS, FOLLOW_UP semantics).
   - Any new behavior must come with its own suite added to selftest_all.sh; a behavior verified only by hand is not done.

3. Live end-to-end proof
   - Run the real loop with the physical mic, e.g. `uv run python -m jarvis.app --ptt` and `--wake`, and verify: wake fires on the last half-syllable of the wake word, ASR yields sane text, HA entity state actually changes, and speech comes out.
   - Re-run the negative cases: silence/false wake words stay silent; a command-shaped utterance with zero tool calls must be forced through `chat_with_guard()` (tool_choice locked to control) rather than answered from thin air.

4. Latency check against baseline
   - ASR 52-72 ms, tool-class LLM 1.2-1.8 s (two rounds), playback 2.0-2.5 s, end-to-end first audio ~1.5 s, first token target 0.7-1.5 s. Investigate regressions, don't hand-wave them.

5. Mic/TTS hygiene re-checks
   - After TTS playback, `mic.drain()` clears the speaker echo; during barge-in, keep the buffered audio (it is the user).
   - Barge-in bar: 12 dB above the ~1.2 s adaptive median for 350 ms; 9 dB/300 ms misfires when volume rises.
   - FOLLOW_UP window: hold/release around the turn in try/finally so the 8 s window starts at playback end, not at wake.
   - `say` voice probe must fall back to Tingting if a personal voice renders ~16 ms of silence.

6. Report
   - State which suites ran, which end-to-end utterances were tested, observed timings, and any suite that still needs adding.
