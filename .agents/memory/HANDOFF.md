# Handoff from DSH session session-81f84430-cd61-4565-b61f-146ed260cb91

- Created: 2026-10-24T06:47:05.392Z
- Project: /Users/suke-3/home-jarvis
- Session log: .agents/memory/session-logs/session-81f84430-cd61-4565-b61f-146ed260cb91/session.md
- Session index: .agents/memory/session-logs/INDEX.md

## Goal
Build a home-grade voice assistant ("家庭版理想同学") at `/Users/suke-3/home-jarvis`: multi-turn continuous conversation with context carry-over, directly controlling Mijia (米家) smart-home devices over WiFi. P0 is a fully local software loop on the MacBook Pro M4 24G using the sound card directly (no ESP32, no MacMini). Once stable, it migrates as-is to MacMini M1 16G.

## Current state
- P0 loop is functionally complete and signed off by `./scripts/selftest_all.sh` → **9 suites green**.
- Docs written: `docs/家庭智能管家-技术方案.md`, `docs/P1-实施清单-ESP32S3-N16R8.md`, `docs/P0-本机语音闭环方案.md`.
- Environment verified via `scripts/preflight.py`: Python 3.12.13, uv, ffmpeg, Docker, zh_CN voices.
- Home Assistant runs in Docker, onboarding complete; long-lived token lives in `.env` (mode 600); `GET /api/` returns `{"message":"API running."}`.
- HA currently controls only the `demo:` virtual lights (6) from `docker/docker-compose.yml`'s embedded `configuration.yaml`. **No real Mijia device is connected yet.**
- Measured baseline: ASR 52–72ms; tool-call LLM 1.2–1.8s; TTS playback 2.0–2.5s; end-to-end first word ≈1.5s. Pre-rendered「好的」WAV sounds in 0.7ms, 405–600ms before final answer.
- Verified: audio record/playback/level meter/barge-in, VAD segmentation (5.47s → 2 segments), 14 tool-layer cases, 12 model assertions, 3 full-chain assertions, KWS 11 cases, FOLLOW_UP hold/release 3 semantic + end-to-end.
- No earlier handoff document exists; this is the first.

## Decisions
- ASR: sherpa-onnx + SenseVoice (`models/`, ~230MB). VAD: silero-vad at `models/silero-vad/silero_vad.onnx`.
- TTS: macOS `say` via `SayTTS`, with a startup voice probe. Piper/cloud deferred.
- Wake: sherpa-onnx KWS (~13MB, CPU) is default; ASR text gating as fallback. PTT (`--ptt`) remains available.
- LLM: DeepSeek `deepseek-flash` at `https://api.deepseek.com`.
- Home control: Home Assistant + official `ha_xiaomi_home`, with only ~5 tools exposed as the control contract — never let the model see hundreds of entity_ids.
- Migrating to real devices = editing `config/aliases.yaml` only; code unchanged.
- P1 (ESP32-S3-N16R8 as ears/mouth over WebSocket) is deferred; it replaces only `audio/`, state machine and tools untouched.
- Gen-1 Xiaomi AI speaker is **out of the loop** (not LX06/OH2P, not flashable; Open-XiaoAI stopped maintenance 2026-04, MiGPT archived).
- No AEC hardware in P0; barge-in works via adaptive echo baseline + double-talk detection.

## Files
- `docs/家庭智能管家-技术方案.md`, `docs/P1-实施清单-ESP32S3-N16R8.md`, `docs/P0-本机语音闭环方案.md`
- `docker/docker-compose.yml` — HA, with `demo:` virtual lights in embedded `configuration.yaml`
- `scripts/preflight.py`, `scripts/set_ha_token.py`, `scripts/fetch_models.sh` (`all`/`asr`/`kws`), `scripts/selftest_all.sh`
- `jarvis/audio.py`, `jarvis/asr.py`, `jarvis/tts.py`, `jarvis/vad.py`, `jarvis/kws.py`, `jarvis/wake.py`, `jarvis/llm.py`, `jarvis/intent.py`, `jarvis/app.py`, `jarvis/tools.py`
- `config/aliases.yaml` — Chinese alias → entity_id map
- `.env` (secret, 600, not in git), `.env.example`, `.gitignore`

## Next steps
1. Connect real Mijia devices: install `ha_xiaomi_home` via HACS, OAuth login (choose correct region — data is region-isolated), then rewrite `config/aliases.yaml` for the real entity_ids and re-run `./scripts/selftest_all.sh`.
2. Resolve the remaining `audio/` open item below before treating barge-in as production-quality.
3. Only after step 1–2 are stable: clone → `uv sync` → `fetch_models.sh` → edit `.env` → compose up on MacMini M1.
4. P1 hardware (deferred): ~¥110 parts; `python scripts/build.py bread-compact-wifi`, ESP-IDF ≥ v6.0.1.

## Open questions
- Which Mijia app account region to use for OAuth, and whether the account has devices already visible under the official Mijia app.
- Whether real-device latency matches the demo-light baseline (HA round-trip to the Xiaomi cloud may add a hop).
- Whether the current adaptive echo baseline (12dB over the 1.2s median, 350ms sustained) holds with the laptop's own speakers at real listening volume.

## Known pitfalls (do not re-learn)
- Docker creds helper broken (`credsStore: "desktop"`) → prefix commands with `DOCKER_CONFIG=/tmp/dcfg`.
- Never hardcode audio device indices; match by name substring (`INPUT_DEVICE=外置麦克风`), fall back with a warning.
- Microphone TCC permission must be granted once from a GUI terminal before wiring to `launchd`.
- TRRS CTIA/OMTP: an old headset can give permanently-zero mic level — swap the headset before suspecting code.
- Never feed empty blocks from soxr stream resampling downstream (VAD likewise).
- PortAudio -9986 during barge-in is the normal abort path — silence it; after `abort()` close the stream, do not `stop()` (120ms → 515ms).
- KWS streaming decode needs ~0.66s of appended silence + `input_finished()`; the model wants ~0.64s right context.
- GitHub release direct download is very slow (9 min for 13MB) → `fetch_models.sh` uses the `gh-proxy.com` mirror with direct fallback.
- `say` personal-voice classes (Eddy/Flo/Reed/Rocko/Grandma/Grandpa/Shelley/Sandy) render 16ms of silence with no error; `SayTTS` probes and falls back to Tingting.
- Wake-word tolerance: 「贾维斯」is often transcribed as「亚维斯」; allow 1 wrong char for wake words ≥3 chars.
- Homophone interference: 「再暗一点」heard as「再按一点」— tests use「把亮度调低一些」instead.
- Drain the mic queue after TTS playback, but not when barge-in fired (that audio is the user's).
- FOLLOW_UP window must start at playback end, not at wake — hence `WakeGate.hold()/release()` in try/finally.
- The model can claim "already turned it off" without calling any tool; `chat_with_guard()` in `jarvis/llm.py` uses `jarvis/intent.py` to force `tool_choice` to `control` when an apparent command produced zero tool calls. Prompting alone does not fix this.

## Hard constraints
- `ha_xiaomi_home` LICENSE permits **non-commercial HA use only** — no building apps/web services on it. Home use is fine; delivering to clients requires the Mijia open-platform enterprise path.
- The Mijia integration writes device tokens **in plaintext** into HA config → enable FileVault on MacMini, never expose HA to the public internet (remote access via Tailscale).
- P1 official `bread-compact-wifi` is single-mic with no AEC: conversation works, barge-in does not. Run it a week before deciding on ESP32-S3-BOX-3 (~¥300, ES7210 + ES8311 hardware echo reference).
