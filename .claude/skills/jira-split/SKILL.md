---
name: jira-split
description: Split an approved plan into fully-briefed Jira Tasks under one Epic, then run one implementer subagent per Task with the controller verifying each diff. Use when the user says "jira split", "split this into tickets", "section this off", or wants a multi-part feature tracked in Jira before building. Also use when picking up an existing MOSHI Epic to continue implementation.
---

# jira split — plan → Epic + Tasks → implementers → verified

Reconstructed 2026-11-18 from the live Jira history (MOSHI-1 "Lemonade
stand" dry run + its retro comment, MOSHI-165/172/177 Epics and their
Tasks). The original lives in `CLAUDE.local.md` of the moshi repo on the
Mac; if that file is ever brought over, reconcile this one against it.

## Site

- Cloud: `hellomoshi.atlassian.net` · cloudId `9319da9f-b1f7-4726-ba88-65d823e0ef70`
- Project: `MOSHI` (next-gen). Issue types: Epic, Task, Story, Feature, Bug, Subtask.
- Tools: claude.ai Atlassian MCP (`createJiraIssue`, `editJiraIssue`,
  `addOrEditJiraIssueComment`, `transitionJiraIssue`, `getJiraIssue`,
  `searchJiraIssuesUsingJql`). Statuses: To Do → In Progress → In Review → Done.
- **Open question for ai-video-editor work**: same MOSHI project with a
  label (e.g. `ai-video-editor`), or a second project? Ask the user the
  first time; record the answer here.

## Roles

**Controller** (this session). Owns the plan, writes the Epic and every
Task brief, dispatches implementers, verifies against the *diff* (never
against the implementer's comment alone), posts rulings, transitions
issues. Never does an implementer's work when it could dispatch it.

**Implementer** (one subagent per Task, fresh context). Reads its Task
*and its comments* (controller amendments land there), loads the named
context pack, builds with TDD, posts a closing comment, hands back.

## Procedure

1. **Plan first.** A jira split takes an approved plan (brainstorm /
   grill-me / interview-me output, or a Notion/Claude Doc). No plan → run
   the interview first; do not split vague ideas.
2. **Create the Epic.** Title = outcome in plain words. Body sections:
   `## Story` (one paragraph, why) · `## Working directory / repos` ·
   `## Success criteria for the Epic` · `## Child Tasks` (names +
   dependency order, e.g. `A · B · C (independent, fan out) → D (depends
   on A–C)`). Labels: `jira-split` plus the product/repo label.
3. **Create the Tasks** (parent = Epic). Title format:
   `<letter> · <repo or area>: <what changes, as a sentence>`.
   Body = the brief template below. Every name two Tasks share (a table,
   a function, a field) is a **contract** — pin it identically in both
   briefs; divergence was the #1 failure in the dry run.
4. **Dispatch implementers.** Independent Tasks fan out in parallel
   (worktrees if same repo). Dependent Tasks wait for their inputs to be
   verified. Model tiering: small, fully-specified 1–2 file Tasks → Haiku;
   integration / cross-module → Sonnet; novel or risky → Opus. Note the
   model + date in the Task body (`**Implementer:** dispatched <date> (<model>), branch <name>`).
5. **Verify.** For each hand-back: read the diff, run the tests yourself,
   check success criteria one by one, check "Not done" is honest. Post a
   `Controller:` comment with the verdict, accepted deviations (with the
   ruling), and anything the user must do by hand (prod env vars, merges,
   deploy order). Then transition: Done, or In Review (waiting on user).
6. **Close the Epic** only when every Task is Done/accepted and the Epic
   success criteria hold. Dry-run / throwaway trees get deleted (Tasks
   first, then Epic).

## Task brief template (paste into the Task description)

```
**Story:** <one sentence: what is true when this is done>
**Environment / Contracts:** <repo, branch base, interpreter + test command
  exactly (e.g. `uv run pytest -q api/tests`), shared names pinned verbatim>
**Context pack:** Load <skills> · Read: <files:functions> · Verify: <existing tests/fakes to lean on>
**Success criteria:** <bullet list; each one checkable by a test or a command>
**Not doing:** <explicit exclusions so the implementer doesn't wander>
**Steps:** TDD per step: <step 1> → <step 2> → … (red test first, each step)
**Implementer:** <filled by controller at dispatch>
```

## Implementer closing comment (required)

- Built: `file:line` list of what changed, by step.
- Deviations from the brief, each with a reason (controller rules ACCEPTED / REJECTED).
- Tests: exact command + verbatim summary line (`N passed`), and any
  pre-existing failures named as such.
- NOT done: anything from the brief left out, with why.
- Anything the user must do by hand.

## Lessons baked in (from MOSHI-1 retro, 2026-10-15)

1. Shared names are contracts; pin them identically in every brief.
2. Briefs name the exact interpreter/test command; `python3` alone failed.
3. Implementers read comments, not just descriptions.
4. Haiku handles well-specified two-file Tasks from the brief alone;
   Sonnet for integration. Tier by Task shape, not by habit.
5. Tests red-first; a test that passes on first run is pinning existing
   behaviour and the comment must say so.
