---
tags: [rule, story, audio]
taught: 2026-11-25
status: active
---
# Kill and death sounds on the HUD change
Kill and death sounds land within 0.5 s of the HUD K/D/A change (in practice 0.02 s).
- The recording offset is always measured with `riot-hud-sync`, never
  guessed. A stored HUD offset beats every other source.
- The cha-ching hits on its bell (`sfx/kaching.json` `hit_s`).
- **Volume:** the cha-ching plays at 50% (owner 2026-11-26: "too loud").
- **Death sound:** a Roblox-style "oof", [Oof (original) by unfa](https://freesound.org/people/unfa/sounds/719053/) (CC0), peaking at -6 dB. Not the Roblox original, which has a licensing dispute. The old glass break is kept as `sfx/death.glass.wav`.

**Taught by:** [[Zeri 8-7-12 (2026-11-23)]] ("2-3 seconds too late or early")
