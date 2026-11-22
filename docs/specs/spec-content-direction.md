# Spec: content direction — story video, lane content, montage, commentary bucket

Status: approved by interview 2026-11-22 (owner napping for the build; red-team findings ruled by the Claude session, all overturnable). Captured at commit `e6a1d1e` (v0.2.0).
Owner: Noodlz. Builder: the Claude session on the PC now; the Grok bots after they unfreeze on Fri 2026-11-27.

## 1. Problem

The Jhin round proved the shorts pipeline (layout B, event-driven windows, digest v2) but the channel has no long-form output, and for gaming channels long-form is what converts subscribers. The owner is a mid-skill ADC who wants content that does not depend on outplays: their own commentary, laning, and the story of a game. The pipeline today has no mic track, no story renderer, no laning data, no commentary bucket, and a montage path that is written but half wired.

## 2. Goals

1. Experiment 1: a 4-6 min story video of the Jhin 8/2/13 game, chronological beats with title cards, carried by the owner's live mic (D3, D4).
2. Experiment 2 (after the owner approves the cut): recorded narration over the setup, live mic inside the fights (D4).
3. A 60-90 s music montage across the four Nov 20 games, built in parallel on the custom montage compiler (D7).
4. Lane content in both forms: a laning chapter in story videos, and lane micro-moment shorts fed by an HP-bar reader in shadow mode (D5, R4).
5. A commentary bucket for voice-only keepers that feeds the story (D6, R7).
6. A per-game multikill scorecard the owner uses to pick games (D11, R6).
7. The pipeline fixes the Jhin and Corki rounds exposed (section 4.7).

## 3. Non-goals (this spec)

- YouTube or TikTok upload code; the owner posts by hand (ruling under D8).
- TTS; narration is the owner's recorded voice (D4).
- Team-gold comeback arcs; only owner and lane-opponent frames (D5, R9).
- Bots writing media code (ffmpeg, Whisper, HP reader) this round (R8).
- 1080x1920 shorts output and fewer re-encodes (still open from the Jhin spec).

## 4. Design

### 4.1 Mic track (`candidates/mic_track.py`, transcribe backends)
- Probe every audio stream with ffprobe. Pick the mic by stream title or handler name when Outplayed labels it; otherwise by a voice-activity energy score per stream; log the choice in the asset's meta and fail closed (no mic transcript, story flagged "mixed audio") when no stream is clearly speech (R1).
- Transcribe the mic stream separately into a `speaker='owner'` transcript alongside the mixed one. The commentary bucket and the story planner read only owner lines.

### 4.2 Story renderer (`story/` on the compilation path, not the shorts path)
- Built on `compile.py` (16:9, 1920x1080, intros, per-clip captions) with chapter title cards from `create_text_intro`, a chunked encode per chapter and a timeout scaled to length (R2).
- Beats come from the gaming-expert's `story_beats` plus the Riot timeline: opening lane chapter (first two levels, big trades, first blood), the pick, the "terrible fight", the closeout; every cut obeys the fight-window rules (8-10 s lead-in, payoff inside, end at game_end + 1 s at most).
- Audio: mic track on top, game audio ducked under it, comms low (sidechain in ffmpeg). Experiment 2 adds a narration WAV the owner records from a generated script, mixed over the setup segments only.
- Review: a separate per-video budget of $5 outside the asset cap; the vision critic samples long-form at 0.25 fps and only around chapter edges (R3).

### 4.3 Montage (`shorts.py` custom compiler)
- `9:16_hud` added as a montage layout; beat snap enabled for montage only and only on clip end points (R10); music from `WORKSPACE/music/royalty_free/` with a `licence.json` entry per track; the owner's recommendations live in `WORKSPACE/music/owner_recs/` and any render that uses them stays local. Drive gets royalty-free or silent cuts only (R5).
- Ezreal (asset 35ae633e) and Zeri cut through the same pipeline as Jhin and Corki; the montage takes the multikill fights from all four.

### 4.4 Lane content (`league/lane.py`, `cv/hp_bar.py`)
- Lane summary per game from Riot frames: owner and lane opponent CS/gold/level per minute through minute 14, first trade and first blood from events; opponents named by champion only, no judgement of the player; participant index checked against champion id (R9).
- HP-bar reader: pixel read every 1-2 s on the owner's bar and the nearest enemy bar, producing `trade` and `escape` candidates. Shadow mode for a week: events logged to the digest's findings, never ranked or paid for, until one hand-labelled game shows 80% precision (R4).

### 4.5 Commentary bucket (`bucketing.py`, `grokbot/digest.py`)
- Voice-only owner moments (a described plan, an odd reaction) are keepers at lower confidence in `commentary`, never shorts alone. At most 5 per game reach the digest, in a separate `commentary` section attached to story beats, so the clips list stays clean (R7).

### 4.6 Scorecard (`league-scorecard` CLI)
- Per game from the cached timeline: fights with 3+ enemy deaths and owner involvement, aces, owner multikills, game length, result. The owner picks the games; the scorecard is advisory until the first long video is approved (R6).

### 4.7 Pipeline fixes from the Jhin and Corki rounds
- Fight pre-pad: 8 s before a fight that opens with an ally death, 4 s otherwise; the editing-expert may widen but never trims in front of the pipeline window.
- Ranker widening capped at 5 s beyond the fight; end capped at game_end + 1 s (no Leave Game dialog).
- Voice fallback matches expert clip refs after normalisation; `edits.py` reads `FFMPEG_VIDEO_CODEC_OPTS`.
- CV-KDA kill claims are advisory when Riot confidence is high.

### 4.8 Security and privacy
- Keys only in `api/.env`; secret-scan before merge and publish (unchanged). Mic transcripts and narration scripts stay local and in Drive digests only as paraphrase. Opponents by champion only. `.gitignore` gains `*.m4a *.opus *.flac *.wav` and the music folders (R5).

### 4.9 Bots (D9, R8)
- Long term the bots own everything. This round: the Claude session writes the Jira, implements the media modules, reviews. Bots take the non-media tasks after Friday (scorecard CLI, digest commentary section, config, docs) and a nightly runner job that re-runs bot PRs on one reference game and posts the result to Drive is a backlog item.

## 5. Testing
- Mic probe: fixtures with 1, 2 and 4 audio streams, labelled and unlabelled; fail-closed case.
- Story planner: invented timeline + beats → chapter list with lead-in and end rules asserted.
- Montage: beat snap moves ends only; layout B filter graph snapshot.
- Lane summary: invented frames; index/champion mismatch raises.
- HP reader: synthetic frames (full, half, shielded, grey) → precision reported; shadow flag keeps it out of candidates.
- Digest: commentary capped at 5, separate section, leak check unchanged.

## 6. Decisions Log

| ID | Topic | Decision | Rationale | Source | Date |
|---|---|---|---|---|---|
| D1 | Channel goal | A mix of buckets; the panel decides per week from the footage | Owner does not want one identity yet | Interview | 2026-11-22 |
| D2 | Cadence | Owner picks games; session makes shorts; long videos are experiments until one is approved; then automate both | Owner's words | Interview | 2026-11-22 |
| D3 | First long | Story of the Jhin 8/2/13 game, 4-6 min, chronological with title cards | Long-form grows gaming channels; the game has a clean arc | Interview | 2026-11-22 |
| D4 | Story voice | Hybrid: live mic in fights, recorded narration for setup; build live-mic cut first, narration in experiment 2 | Narration needs the cut to exist | Interview | 2026-11-22 |
| D5 | Early game | Laning chapter in story videos (first two levels, trades, kills, deaths, ganks) and lane micro-moment shorts (trades, kills, assists), length by moment | Owner's definition | Interview | 2026-11-22 |
| D6 | Commentary bucket | Voice-only owner moments are keepers at lower confidence; story material, not shorts alone | "keepers can be my comms too" | Interview | 2026-11-22 |
| D7 | Montage | Build in parallel now on the custom compiler | Mostly built; Corki and Zeri cut | Interview | 2026-11-22 |
| D8 | Music and posting | Session stocks a royalty-free folder; owner recs in a separate folder with licence notes; Content ID risk acknowledged; owner posts by hand, no upload code | Owner's answer; upload unanswered → least-risk ruling | Interview | 2026-11-22 |
| D9 | Bots | Bots own everything long term; session writes Jira, reviews, codes when owner is home; bots implement while owner is at work and show videos | Owner's words | Interview | 2026-11-22 |
| D10 | Lead's four ideas | Riot timeline mostly shipped; Overwolf assists redundant; funny voice-only → D6; HP-bar reader adopted | Already built or superseded | Interview | 2026-11-22 |
| D11 | Scorecard | Per-game multikill scorecard as a standard readout | "recommend me videos with the amount of multikills" | Interview | 2026-11-22 |
| D12 | Research | Long-form drives gaming subscribers; moderate shorts as funnel | AIR Media-Tech, Metricool 2026 | Interview | 2026-11-22 |
| R1 | Mic stream | Probe by label, else VAD energy; log; fail closed | Stream 3 is not guaranteed | Red Team | 2026-11-22 |
| R2 | Story render path | Compilation path (16:9 1080p), chunked, scaled timeout; never the shorts path | Shorts caps: 180 s, 300 s timeout, 720x1280 | Red Team | 2026-11-22 |
| R3 | Long-form review cost | $5 per-video budget outside the asset cap; critic at 0.25 fps at chapter edges | A 6-min cut would eat the $2 asset cap | Red Team | 2026-11-22 |
| R4 | HP-bar precision | Shadow mode; 80% precision on one labelled game before it feeds candidates | Shields, grey bars, 720p | Red Team | 2026-11-22 |
| R5 | Licensed music | Drive gets royalty-free or silent cuts only; owner-rec mixes stay local; audio formats and music dirs gitignored | Drive is distribution to a third party | Red Team | 2026-11-22 |
| R6 | Owner vs scorecard | Owner always wins; scorecard advisory until the first approved long | D2 is explicit | Red Team | 2026-11-22 |
| R7 | Commentary volume | Top 5 per game in a separate digest section | Mic transcript would flood the clip list | Red Team | 2026-11-22 |
| R8 | Bot media code | Session implements media modules this round; bots get non-media tasks; nightly reference-game run is backlog | Bots cannot run ffmpeg/Whisper/footage | Red Team | 2026-11-22 |
| R9 | Opponents | Champion name only, no player judgement; index checked against champion id | Paraphrase rule does not cover opponents | Red Team | 2026-11-22 |
| R10 | Beat snap | Montage only; end points only | Must not move a fight window's start | Red Team | 2026-11-22 |

## 7. Dependency Graph & Implementation Order

```
T1 pipeline fixes (pre-pad by role, widen cap, end cap, voice ref fallback, edits.py encoder, CV advisory)   independent
T2 mic track probe + owner transcript (R1)                                                                     independent
T3 commentary bucket + digest section + scorecard CLI (D6, D11, R7)                                            depends on T2 (owner lines)
T4 lane summary from Riot frames (D5a, R9)                                                                      independent
T5 story planner + renderer on the compilation path + audio mix (D3, D4 exp. 1, R2, R3)                        depends on T1, T2, T4
T6 montage: layout B, beat snap ends-only, music folders + licence.json, Ezreal/Zeri runs, Nov 20 montage (D7, D8, R5, R10)   depends on T1
T7 HP-bar reader in shadow mode (D5b, R4)                                                                       independent, lowest priority
T8 run: Jhin story experiment 1 to Drive renders/story/, montage to Drive, scorecard for all four games         depends on T5, T6
```

Order: T1, T2, T4 in parallel → T3, T6 → T5 → T8. T7 last, or handed to the bots after Friday as a spec-and-tests task.

## 8. Open items carried forward
- Experiment 2: narration script generator + VO mixer (after the owner approves the experiment 1 cut).
- Team-gold comeback arcs; the arc planner for arc_montage.
- Nightly reference-game verification of bot PRs; bots taking media code.
- 1080x1920 shorts, fewer re-encodes, shorts compiler layout B for all shorts.
