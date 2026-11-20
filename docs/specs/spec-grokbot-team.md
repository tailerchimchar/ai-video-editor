# Spec: the Grok Bot team (off-hours crew for ai-video-editor)

**Status:** approved design, pending build · **Baseline commit:** `1337c39` · **Date:** 2026-11-20
**Interview state:** `docs/specs/.spec-grokbot-team.interview.json` · **Companion spec:** `spec-expert-panel.md` (the daytime Claude panel, D1–D33)

## 1. Overview

Noodlz wants a team of always-on agents working on the ai-video-editor project while he is at work, texting him on his phone, and getting videos in front of him without him being the blocker. xAI's Grok Bot provides always-on cloud agents with a phone app, routines, an inbound webhook, remote MCP servers and a shared cloud computer with git.

The hard constraint: footage, the renderer (ffmpeg + GPU) and the API live on Noodlz's PC and never leave it. So the team is split:

- **Cloud (Grok Bot):** deciding, researching, coding, planning, reporting. Four bots on one shared cloud computer.
- **PC (this repo):** rendering, critiquing, uploading, and validating everything the bots send back. A local **runner** process is the only thing that acts on bot output.

The daytime **Claude panel** (gaming-expert, viewer-expert, editing-expert, mentor chair) is unchanged and remains the footage-time crew. The Grok team feeds the same skill files through PRs so both crews learn from one source.

## 2. Goals, non-goals, anti-goals

**Goals**
- Texts on Noodlz's phone from a lead that reports, asks, and never waits on him.
- Videos (shorts, previews) reach him via a Drive link while he is away.
- The devops bot ships reviewed code against the backlog, the cost audit and the Zeri findings.
- The viewer bot turns creator studies (zekken, jawgemo, ryuk, MrBeast) and Noodlz's analytics into rules in `viewer-expert/log.md`.
- Predictable spend: the free trial first, a usage report every evening, a plan chosen from numbers.

**Non-goals**
- Rendering in the cloud. Footage is never uploaded in full.
- Replacing the Claude panel or the mentor's editorial policy.
- A public API or tunnel into the PC.

**Anti-goals (failure even if it works)**
- The trial pool is empty by day two.
- A bot's guess gets recorded as Noodlz's ruling.
- Something that landed in the git queue runs a command on the PC.
- Teammate speech or summoner names in a committed file or a text.

## 3. The team

All four bots share one cloud computer, one git identity per repo (see §8), and the interrupt rules in §4. Each bot's skill is a markdown file in `docs/grokbot/<bot>.md` that Noodlz pastes into the bot as written instructions; the files are versioned here.

| Bot | Seat | Wakes | Produces | Reads |
|---|---|---|---|---|
| **Lead** | Senior/principal, master of all parts, reports to Noodlz | Always on (D15) | Texts, the evening digest with usage, assignments to the other three, Jira updates, the sleep/wake state | Webhook events, heartbeat, Jira AVE, the queue repo, Drive folder |
| **Editor** | Pure editor, master of the craft | On assignment from the lead | Edit plans (typed, §6.2), preview requests, titles when the viewer is asleep | Game digests, clip list, critic reports, arsenal.md, mentor policy, clips on Drive on demand |
| **DevOps** | Writes the best code; hygiene, scalable, extendable, cost | On assignment | Branches + PRs with passing tests and a plain summary; cost audit report; tools audit | Repo, deferred list (`docs/plans/`, ledger notes), Zeri findings, Langfuse cost exports when provided |
| **Viewer** | Retention researcher and the reviewer seat | On assignment; one scheduled creator study per day | Creator studies and analytics entries (PRs to `viewer-expert/log.md`), hook/title/caption/hashtags/thumbnail timestamp per edit plan, opt-in trend briefs | Digests, critic reports, creator channels, Noodlz's TikTok/YouTube Studio via Vault (he grants) |

Content direction the bots inherit: philosophical, funny, wide variety, montage-y, heartfelt with story. The mentor taxonomy (`taxonomy.Format`) is the vocabulary.

## 4. How the team talks to Noodlz

- **Texts freely.** Milestones, PR summaries, previews, blockers, questions. "Only when it needs me" was rejected (D5).
- **Never blocks.** Every question carries the default the team will proceed with ("I'll keep the Diana gripe out unless you say keep"). A late reply is applied as a correction.
- **Verdict provenance (D16).** A reply from Noodlz becomes a verdict file that quotes his text with its timestamp. Only those reach mentor's `record_correction`. Bot defaults go to a separate `decisions.log` the runner dedupes. Nothing else may write to mentor.
- **sleep / wake (D3, D17).** "sleep" to the lead: the lead commits `SLEEP` to the queue repo; the runner honors it (stops pulling, buffers webhook events), every other bot's routines pause. "wake": the runner sends one summary of buffered events, not N webhooks. The lead also sleeps the team when the usage pool falls below Noodlz's floor.
- **Evening digest.** What shipped, what is queued, what needs him, usage consumed today and remaining.
- **PR pings.** Every PR opens with a text: one-paragraph summary, risk level, who merges (§8).

## 5. Architecture

```
PC (this repo, master)                                   Cloud (Grok Bot, one shared computer)
───────────────────────────────                          ──────────────────────────────────────
pipeline jobs ──► notify/ ── webhook POST ─────────────► inbound webhook ─► Lead routine
                 (job-done events, heartbeat)            
panel round ──► digest builder ─► Drive uploader ──────► Drive folder (dedicated Google acct)
highlights  ──► clips, renders, previews, critique ─────► read-only for bots
                                                          
runner  ◄── git pull ◄─── queue repo (bots: plans, preview requests, verdicts, deploy)
  ├─ validator (allowlist schema, envelope, base_version)
  ├─ plan executor (existing POST /compilations/{id}/plan)
  ├─ preview cutter (render_short on a 10 s window)
  ├─ VLM critic (existing watch_montage) ─► critique.json to Drive + webhook
  └─ heartbeat every 5 min ─► webhook
                                                          
Jira AVE ◄──────────────── Atlassian remote MCP (bot Atlassian user) ◄── Lead
GitHub main repo ◄──────── PR-only token ◄── DevOps / Viewer branches
```

### 5.1 PC side (new code, all behind config and inert when unset)

| Module | Responsibility | Notes |
|---|---|---|
| `notify/` | Outbound events. `Sink` protocol with `webhook` and `log` sinks; registry like `decisions/backends/`. Adding a channel = one file. | Config `GROKBOT_WEBHOOK_URL`, `GROKBOT_WEBHOOK_KEY`, `GROKBOT_MAX_EVENTS_PER_HOUR`. |
| `jobs.finish_job` | One shared completion helper replacing the three copies and inline SQL; emits `job.done` with the structured result dict instead of the flattened string. | The result dicts from `build_highlights` / `build_shorts` stop being thrown away. |
| `grokbot/digest.py` | Pure builder: panel round folder → `digest.json` (per clip: event, start/end, panel scores, VLM verdict, one-line paraphrase, **no names, no quotes**). | Paraphrase comes from the chair's `panel-decision.md` and expert JSON, never from `transcript.json`. Unit-tested with a fixture round. |
| `grokbot/drive.py` | Uploads to the dedicated Google account's folder: clips, renders, previews, `digest.json`, `critique.json`. Retention: previews 7 days, renders until `shipped`. | OAuth once on the PC; token in `api/.env`-adjacent secret file, gitignored. |
| `grokbot/runner.py` | Long-running process (Task Scheduler, restart on failure): pull queue repo every 60 s, validate, execute, upload, emit, heartbeat. Honors `SLEEP`. | Pinned to a tagged commit; updates only on a `deploy` queue item approved by Noodlz or the Claude session. |
| `grokbot/queue.py` | Envelope + allowlist schemas (§6), `extra="forbid"`, applied-ids ledger (SQLite table `queue_applied`). | Rejections are written back to the queue repo as `rejected/<id>.json` with the reason and current state. |

### 5.2 Cloud side (no code; configuration Noodlz performs, scripted in `docs/grokbot/setup.md`)

- Four bots created from the skill files.
- Lead: inbound webhook routine (POST URL + key → `GROKBOT_WEBHOOK_*`), Atlassian remote MCP added by URL with the **bot Atlassian user** (D23), git identity with the queue-write token.
- DevOps and Viewer: git identity with the PR-only token on the main repo. Viewer has **no** queue-repo token (D15 strict): its research goes through PRs and its per-plan hook verdicts go to the lead, who writes them into the plan envelope.
- Drive connector signed into the **dedicated Google account** (D22).
- Viewer's Vault holds Noodlz's creator logins only if he grants them (D9).

## 6. Data contracts

### 6.1 PC → bots: webhook events (JSON, bearer key)

```json
{"event": "job.done", "job_type": "highlights", "asset_id": "…", "folder": "…",
 "summary": {"clips_written": 7, "clips_total": 9, "dropped": 2},
 "drive": {"digest": "<file id>", "clips": ["<file id>", "…"]}, "ts": "2026-11-20T18:02:11Z"}
{"event": "render.done", "plan_id": "…", "drive": {"mp4": "…", "critique": "…"}, "critic": {"verdict": "ship_it", "opener": 5, "climax": 4}}
{"event": "preview.ready", "request_id": "…", "drive": {"mp4": "…"}, "question": "…"}
{"event": "queue.rejected", "id": "…", "reason": "base_version 12 != current 14", "current": {…}}
{"event": "heartbeat", "runner_version": "v0.3.1", "queue_depth": 2, "sleep": false}
```

Rate-limited by `GROKBOT_MAX_EVENTS_PER_HOUR`; events beyond the cap are folded into the next heartbeat.

### 6.2 Bots → PC: queue items (one file per item, JSON, in the queue repo)

Every item has the envelope `{"id": uuid, "kind": …, "author_bot": "lead|editor|devops", "created": iso, "schema_version": 1}`. The runner validates with `extra="forbid"` and rejects anything with a URL, path, or shell-looking string in a free-text field.

| kind | Payload | Runner action |
|---|---|---|
| `edit_plan` | `compilation_id`, `base_version` (journal version the plan was written against), the existing `EditPlan` body restricted to registry effects, viewer's `copy` block | Reject if `base_version != current` (send current state back); else preflight + execute via the plan endpoint, render, critic, upload, `render.done`. |
| `preview_request` | `asset_id` or `compilation_id`, `start_s`, `end_s` (≤ 20 s), `question` | `render_short` on the window, upload, `preview.ready`. |
| `verdict` | `clip_ref`, `verdict` (payoff/false_positive/style), `rule`, `quote` (Noodlz's text verbatim), `quote_ts` | **Only with a non-empty `quote`** → `record_correction` with `source_context="grokbot:<id>"`. Deduped by `id`. Without a quote → `decisions.log` only. |
| `deploy` | `tag` | Only applied if approved (Noodlz text or Claude session) → runner pulls master at `tag`, restarts. |
| `sleep` / `wake` | none | Pause/resume as §4. |

Timed effects (`PlanEffect.at`) are rejected until the executor applies them (today it records but ignores them), so the editor never believes a punch-in happened that did not.

### 6.3 Digest (PC → Drive)

```json
{"game": "league", "round": "2026-11-19_zeri", "champion": "Zeri", "kda": "9/5/6",
 "clips": [{"ref": "s1", "event": "double_kill", "t": "20:21", "start_s": 1221.4, "end_s": 1245.8,
            "panel": {"gaming": 4, "viewer": "pass", "format": "funny"}, "critic": "ship_it",
            "voice": "player jokes about handing the enemy Baron after the double", "drive": "<file id>"}]}
```

No summoner names, no quoted lines. A test asserts the digest builder never emits a string that appears verbatim in `transcript.json`.

## 7. Cost model and budgets

- **Jev** (already live, `$0.042/M input`, output free): classification and the include pre-filter; ~0.3 cents per game. Bots never re-do what Jev answers; the digest carries Jev's format and include probabilities.
- **Local critic**: ~$0.02 per rendered minute (Haiku). Runs once per render, once per preview.
- **Grok Bot usage**: unknown per bot-hour. Rules: Lead always on (D15 interim); Editor, DevOps, Viewer wake only on assignment; no bot polls git or Drive in a loop (events come from the webhook); per-bot daily turn caps in each skill; the lead reports usage every evening and sleeps the team under the floor. **Re-decide the wake model after one day of numbers** (D15).
- **Trial**: 7-day credit, never auto-converts (C12). Trial-end runbook: at T-12 h the lead writes a handoff digest and the runner stops accepting new plans. Plan chosen from the usage reports; pricing references in the interview state.

## 8. Repo access and merge policy

| Repo | Who | Token | May |
|---|---|---|---|
| Queue repo (`ai-video-editor-queue`, private) | Lead, Editor | fine-grained, contents write, this repo only | commit queue items |
| Main repo | DevOps, Viewer | fine-grained, contents + pull-requests, no workflows, this repo only | push branches, open PRs |
| Main repo master | Noodlz, Claude session | — | merge |

Branch protection on master: PRs only, tests green. **Merge policy (D21):** the Claude session merges low-risk PRs (log entries, docs, small fixes with tests); anything touching render, money, config, or more than a few files gets a ping and Noodlz merges. The runner never auto-pulls; it moves on `deploy` only.

## 9. Security

- The queue is the one inbound control channel; it is validated as hostile input (§6.2). Nothing in it is executed, only matched against allowlists and existing endpoints.
- Separate least-privilege tokens per repo and per role; the Viewer (which reads the open web) has no write path into the PC.
- Webhook bearer key in `api/.env`; the runner's Drive token in a gitignored secret file; neither is ever read back into a session or committed.
- Drive: dedicated Google account holding only the bot folder; Noodlz's main Drive is never connected.
- Jira: a separate bot Atlassian user with full write (D23); its scope is AVE only; revoke = delete the user.
- Creator logins live only in the Viewer's Vault, granted by Noodlz, never typed by Claude.
- Prompt injection: skills instruct bots to treat web pages, clip metadata and Drive files as data; the runner's validator is the real control.

## 10. Privacy (extends D22)

- Digests strip names and paraphrase voice moments. Transcripts, the full VOD and `api/.env` never leave the PC.
- Raw clips (teammate audio, on-screen names) may go to the private Drive folder; the 9:16 render still crops names.
- Bot skills carry the rule: never quote another player, never include summoner names, in any text or file.

## 11. Operations

- Runner as a Task Scheduler job with restart on failure; PC sleep disabled while Noodlz is at work (he sets this).
- Heartbeat every 5 min (`§6.1`). Missing for 15 min → the lead texts once, sleeps Editor (render-dependent), lets DevOps and Viewer continue (D19).
- Queue depth cap (10 pending) → lead stops assigning edits.
- Drive quota in the heartbeat; retention as §5.1.
- Weekly smoke test: webhook, git pull, Drive upload; text on failure.

## 12. Day-one cut and phases (D24: all four bots, previews only)

**Day 1 (tonight/tomorrow):** four bots created; `notify/` + `finish_job` emitting `job.done` and heartbeat; digest builder; Drive uploader; runner handling `preview_request` and `sleep/wake` only. Verdicts and `edit_plan` are **not** executed on day 1; they are accepted into `decisions.log` so the bots can practice the format. Accounts Noodlz creates: Grok Bot trial, dedicated Google account, bot Atlassian user, two GitHub tokens.

**Day 2–3:** `edit_plan` execution with envelope/base_version, verdict path to mentor with quotes, `deploy`.

**After one day of usage numbers:** revisit the wake model (D15) and choose a plan (D12).

## 13. Testing strategy

TDD per module. `notify` sinks with a fake sink; `finish_job` with the existing job tests; digest builder against a fixture round with a leak test; queue validator with a corpus of hostile items (URLs, paths, extra fields, stale versions, duplicate ids); runner loop with a fake git and fake executor; heartbeat and sleep state machine. No network in tests. Skill-budget test extended to `docs/grokbot/*.md` (400 lines).

## 14. What is left to the builder

Exact Task Scheduler setup; the Drive API client choice; whether the queue repo is a repo or a branch (my call: a separate private repo, keeps master clean); the paraphrase source if `panel-decision.md` lacks one for a clip (fall back to the event name only); skill-file wording per bot.

## Decisions Log

| ID | Topic | Decision | Rationale | Source | Date |
|---|---|---|---|---|---|
| D1 | Role of the Grok team | Off-hours cloud crew; the Claude panel stays the footage-time crew on the PC | Footage never leaves the PC; no duplicate panel | Interview | 2026-11-20 |
| D2 | Team roster | Lead, Editor, DevOps, Viewer (retention + reviewer seat) | User's stated team | Interview | 2026-11-20 |
| D3 | Cost model | Continuous, idles on empty queue; sleep/wake; usage floor; evening usage report | User wants continuous work, fears cost | Interview | 2026-11-20 |
| D4 | Video handoff | Local runner renders and uploads to Drive; bots read only; nothing inbound to the PC except the validated queue | Render needs footage + GPU, costs no tokens | Interview | 2026-11-20 |
| D5 | Interrupt rules | Lead texts freely; team never blocks; questions carry defaults; late replies are corrections | Human in the loop without being the blocker | Interview | 2026-11-20 |
| D6 | Editor inputs | Paraphrased digest + clips on Drive on demand | D22 privacy; watching costs usage | Interview | 2026-11-20 |
| D7 | Review loop | Local VLM critic after every render; viewer hook opinion; lead shows user on ship_it or disagreement | Critic exists at ~$0.02/min | Interview | 2026-11-20 |
| D8 | DevOps mandate | Cost audit, hygiene, Zeri findings, deferred list, tools audit | User's list | Interview | 2026-11-20 |
| D9 | Viewer deliverables | Creator studies, per-plan copy, weekly analytics, opt-in trend watch | User's list | Interview | 2026-11-20 |
| D10 | Repo access | Branches + PRs; humans merge; PR ping with summary | Master stays safe | Interview | 2026-11-20 |
| D11 | Privacy line | Digests strip names; raw clips to private Drive; transcripts/.env never leave | Clips are the published content anyway | Interview | 2026-11-20 |
| D12 | Accounts | Free trial first; plan from usage reports | No plan yet; trial never auto-converts | Interview | 2026-11-20 |
| D13 | Connectors | Jira via Atlassian remote MCP; GitHub via git + token; Drive read-only for bots | No Jira/GitHub connector; Drive write undocumented | Interview | 2026-11-20 |
| D14 | Bot → PC transport | Git queue repo polled by the runner; PC → bot via webhook + Drive | Only verifiable write channel bots have | Interview | 2026-11-20 |
| D15 | Queue lockdown + wake model | Strict: separate tokens, allowlist validator, Viewer has no queue write; Lead always on, others on assignment; re-decide after one day | Shared, injectable cloud computer; trial pool | Red Team | 2026-11-20 |
| D16 | Verdict provenance | Only verdicts quoting Noodlz's text reach mentor; bot defaults to a deduped decisions.log | Mentor is human ground truth | Red Team | 2026-11-20 |
| D17 | Sleep semantics | SLEEP flag in the queue pauses the runner and buffers events; one summary on wake | "sleep" must stop the PC side too | Red Team | 2026-11-20 |
| D18 | Plan envelope | id, compilation_id, base_version, schema_version, author; extra=forbid; stale plans rejected with current state; timed effects rejected until applied | Silent drops and races with the daytime panel | Red Team | 2026-11-20 |
| D19 | Heartbeat | 5-min heartbeat; on loss the lead texts once, Editor sleeps, DevOps/Viewer continue | Runner can die while user is away | Red Team | 2026-11-20 |
| D20 | Day-one failure budget | Accepted: rough edges in the trial week are fine as long as D15–D19 exist | User chose all four bots day one | Red Team | 2026-11-20 |
| D21 | Merge policy | Claude session merges low-risk PRs; big or sensitive ones pinged to Noodlz; runner moves on `deploy` only | User must not be the blocker | Red Team | 2026-11-20 |
| D22 | Drive account | Dedicated Google account holding only the bot folder; retention 7 days previews, renders until shipped | Connector likely reads the whole Drive | Red Team | 2026-11-20 |
| D23 | Jira access | Full write via a separate bot Atlassian user | User's choice over read-only | Red Team | 2026-11-20 |
| D24 | Day-one cut | All four bots, previews only; edit_plan execution and mentor verdicts follow once D15–D18 are built | User's choice | Red Team | 2026-11-20 |

## Dependency Graph & Implementation Order

```
A notify/ sinks + GROKBOT_* config ──┐
B jobs.finish_job (one helper, emits job.done) ──┤
C digest builder (pure, leak test) ──┤──► E runner (preview_request, sleep/wake, heartbeat) ──► G day-1 live
D drive uploader (dedicated account) ─┘                 │
F queue envelope + validator + applied ledger ──────────┴──► H edit_plan execution + verdict path ──► I deploy item
J bot skill files (docs/grokbot/*.md) + setup.md ── independent, needed for G
K Jira: AVE epic "Grok Bot team" + tasks A–J ── first
```

Order: K → A, B, C, D, J in parallel (independent) → F → E → G (day-one live) → H → I. Re-decide D15 wake model and D12 plan after one day of G.
