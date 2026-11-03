---
name: home-jarvis-xiaomi-device-onboarding-regression
description: "Onboard or revalidate a Xiaomi Home device in home-jarvis, update aliases without losing offline devices, and run the HA/Jarvis real-device regression checks."
---

Use this when adding a new Xiaomi/米家 device, repairing aliases, or validating real-device control after code changes.

1. Confirm HA xiaomi_home is loaded and authorized before touching aliases. In HA settings, internal_url must be exactly http://localhost:8123; 127.0.0.1 breaks the OAuth callback. Keep the long-lived token only in .env and never print it.
2. Refresh aliases from HA with scripts/gen_aliases.py. Preserve existing entries: default to append-only. Use --include-unavailable to keep wall-switch-off devices. Do not run --rebuild blindly; if used, verify offline devices remain and are marked [离线]. Remove only known noise or demo entities.
3. Eliminate demo/ghost entities by deleting the HA config entry (Settings → Devices & Services → Demo → ⋮ → Delete) or via the config-entry API, not by editing YAML alone. Refresh and confirm the entity count drops and no demo remnants remain.
4. Before control or status query, resolve the alias to entity_id and front-check online state. If unavailable, return an explicit refusal or status; never trust the Xiaomi cloud call's HTTP 200 because unavailable devices often return success without acting.
5. For real-device validation, dynamically select the first online light/device from aliases; never hardcode a device name. Exercise at minimum: turn on, absolute brightness, relative dim, status query, and turn off; verify HA state actually changes.
6. For voice/LLM changes, run ./scripts/selftest_all.sh and the focused suite such as scripts/selftest_wake.py. For follow-up/close-word work, verify: goodbye TTS completes before gate.sleep(); gate hold/release is wrapped in try/finally; close words compare the whole utterance after stripping fillers like 好的/行/嗯/吧/啊/呀/了.
7. End-to-end check with a real utterance and real device: ASR → intent/tool call → HA state change → spoken response. For command-like utterances that produced zero tool calls, confirm the guard forces a tool call and does not allow fabricated execution.
8. If using a Xiaomi speaker as output, call HA notify send_message play_text and verify HTTP 200 plus entity state update. Treat speaker-as-ear/flashing as a separate decision; do not mix it into normal device onboarding.
