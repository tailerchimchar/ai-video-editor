# Grok Bot team setup

Do these in order. Each step says what you end up with. The design is in `docs/specs/spec-grokbot-team.md`. Anything marked "env" goes in `api/.env` on the PC (the file is gitignored; never paste its contents into a chat).

## 1. Accounts

1. Start the Grok Bot trial. It is a 7-day credit and does not auto-convert; note the end date.
2. Create a new Google account used only for this project. Never connect your main Drive.
3. Create a bot Atlassian user for Jira (the Lead signs in as this user; deleting the user revokes access).

## 2. Create the four bots

Create four bots on one cloud computer: Lead, Editor, DevOps, Viewer. Paste the matching file as each bot's written instructions:

| Bot | File |
|---|---|
| Lead | `docs/grokbot/lead.md` |
| Editor | `docs/grokbot/editor.md` |
| DevOps | `docs/grokbot/devops.md` |
| Viewer | `docs/grokbot/viewer.md` |

Also give the Lead and Editor `docs/grokbot/queue-format.md` as reference. When a file changes in the repo, paste it again.

## 3. Lead: inbound webhook

1. In the Lead, add an inbound webhook routine.
2. Copy its POST URL and key.
3. Env: `GROKBOT_WEBHOOK_URL` is the POST URL, `GROKBOT_WEBHOOK_KEY` is the key.
4. Also set the rate and timeout knobs (defaults are fine to start): `GROKBOT_MAX_EVENTS_PER_HOUR`, `GROKBOT_TIMEOUT_SECONDS`.

## 4. Lead: Jira

In the Lead, add the Atlassian remote MCP server at `https://mcp.atlassian.com/v1/sse`. Sign in as the bot Atlassian user, not as yourself. Scope is the AVE project only.

## 5. Drive

1. Signed in as the new Google account, create a Google Cloud project and enable the Drive API.
2. Create an OAuth client of type desktop app. Note the client id and secret.
3. On the PC run `uv run drive-auth` from the `api` folder. It opens a consent screen, creates the root folder itself, and prints the refresh token and the folder id.
4. Env: `GROKBOT_DRIVE_CLIENT_ID`, `GROKBOT_DRIVE_CLIENT_SECRET`, `GROKBOT_DRIVE_REFRESH_TOKEN`, `GROKBOT_DRIVE_FOLDER_ID` (both the token and the folder id come from the `drive-auth` output).
5. Optional: `GROKBOT_DRIVE_PREVIEW_RETENTION_DAYS` (default 7). Once a day the runner deletes Drive files in `previews/` older than this, and local runner previews (`WORKSPACE_DIR\grokbot\previews\<id>.mp4`) older than this. Nothing else in Drive or the workspace is ever deleted.
6. Set the OAuth consent screen to "In production" (the `drive.file` scope needs no verification). A client left in "Testing" issues refresh tokens that expire after 7 days.
7. In each bot, add the Drive connector signed into this dedicated account only.

## 6. Queue repo and tokens

1. Create a new private GitHub repo named `ai-video-editor-queue`. Add a `queue/` folder and a `rejected/` folder (each with a placeholder file) and a short README.
2. Create a fine-grained token for the Lead and Editor: this repo only, contents write.
3. Create a fine-grained token for DevOps and Viewer: the main repo only, contents and pull requests, no workflows.
4. Give each bot only its own token. Keep both out of the chat history; paste them into the bot's secret settings.
5. On master of the main repo, branch protection should require pull requests and green tests.
6. Clone the queue repo on the PC with symlinks off, then keep them off in that clone:

   ```
   git clone -c core.symlinks=false <queue repo url> C:\ave-queue
   git -C C:\ave-queue config core.symlinks false
   ```

   The runner also refuses to read or write any queue path that is a symlink or sits under one, and skips it with a warning. Env: `GROKBOT_QUEUE_REPO_DIR` is the clone's folder path.

## 7. The API and the runner

The runner talks to the local API, so the API must be running first. Env for the runner:

| Variable | Default | What it does |
|---|---|---|
| `GROKBOT_API_BASE_URL` | `http://127.0.0.1:8000` | where the runner reaches the API |
| `GROKBOT_EXECUTE_PLANS` | `false` | run `edit_plan` items (phase 2); off means they are held |
| `GROKBOT_RECORD_VERDICTS` | `false` | send quoted `verdict` items to the mentor (phase 2) |
| `GROKBOT_RUNNER_POLL_SECONDS` | `60` | how often the runner pulls the queue |
| `GROKBOT_HEARTBEAT_SECONDS` | `300` | heartbeat cadence (its own thread, never blocked by a render). Every heartbeat wakes the Lead and costs a turn: production runs `86400` (daily) |
| `GROKBOT_QUEUE_DEPTH_CAP` | `10` | pending items before a `queue.depth` warning |

When you turn on `GROKBOT_EXECUTE_PLANS` or `GROKBOT_RECORD_VERDICTS`, held items younger than 24 hours run on the next tick; older ones are marked expired and reported in one `queue.expired` event.

**Pin the runner.** The runner never pulls code. Run it from its own checkout of the main repo at a release tag, not from the folder you develop in. The checkout lives next to the repo:

```
git tag -a v0.1.0 -m "..."
git worktree add C:\Users\taile\source\repos\ai-video-editor-runner v0.1.0
cd C:\Users\taile\source\repos\ai-video-editor-runner\api
uv sync
```

A deploy means moving that checkout to the new tag (`git -C C:\Users\taile\source\repos\ai-video-editor-runner checkout v0.1.1`, then restart the two tasks). Copy `api\.env` into the checkout's `api\` folder.

Two Task Scheduler tasks exist, `AVE API` and `AVE Grok Runner` (registered 2026-11-20 with PowerShell `Register-ScheduledTask`). Both are "At log on", restart every minute on failure, and "run as soon as possible after a missed start". Each action is `conhost.exe --headless cmd.exe /c <checkout>\api\run-api.cmd` (and `run-runner.cmd`), both exes by full `C:\WINDOWS\system32\` path. Do not launch through `powershell.exe -WindowStyle Hidden`: with Windows Terminal as the default terminal the console is handed to a visible Terminal window anyway (found 2026-11-21); `conhost --headless` creates no window at all. **Start in** is set to the checkout's `api` folder. The two `.cmd` files hold `cd /d <api dir>` and `<uv.exe> run --no-sync <script> >> <logs>\<name>.log 2>&1` (`--no-sync` matters: a plain `uv run` tries to re-sync the venv at launch and fails while the other task holds `dev.exe`, so the runner dies on every auto-restart; run `uv sync` only during a deploy with both tasks stopped): cmd does the redirect, so the log is plain UTF-8. Never let PowerShell do the redirect (`>>` or `*>>` in `-Command`): it writes UTF-16 and wraps lines, and grep/tail then see nothing. The hidden window matters: a visible console that gets closed kills the server with exit code 0xC000013A (STATUS_CONTROL_C_EXIT), which is what happened three times on day one so `api\.env` loads (Task Scheduler otherwise starts in `C:\Windows\System32` and the runner exits with "run from the api directory so api/.env loads"). Set `DATABASE_PATH` in `api\.env` to the ONE real database (absolute path); the default is relative to the working directory, so a second checkout silently gets its own empty database and the runner rejects every item with "asset not found". Do not quote the paths in the cmd argument: with two quoted paths `cmd /c` strips the outer quotes and the task exits with result 1 and no log.

1. `AVE API`: `uv run dev`, log at `<checkout>\logs\api.log`.
2. `AVE Grok Runner`: `uv run grokbot-runner`, one-minute start delay so the API is up first, log at `<checkout>\logs\runner.log`.

Start or restart them with `Start-ScheduledTask 'AVE API'` / `Stop-ScheduledTask`. The runner logs one line per tick at INFO.

## 8. Power

Turn PC sleep off while you are working (Settings, System, Power). If the PC sleeps the heartbeat stops; after 15 minutes the Lead texts you once and sleeps the Editor.

## 9. Check it works

1. Text the Lead "sleep". Confirm a `SLEEP` file appears in the queue repo, then text "wake".
2. Wait for one heartbeat event in the Lead.
3. Publish a panel round (section 10), then ask the Lead for a 15 second preview of one of its clips. The Lead takes the `asset_id` from the digest; watch for the preview in Drive.
4. After a day, read the Lead's evening digest for usage, then decide the wake model and plan.

## Day-one limits

On day one only previews, sleep and wake are executed. Edit plans, verdicts and deploys are validated and held (ledgered as deferred, with a `queue.deferred` event); nothing is written to the decisions log or the mentor. The rest comes on days two and three.

## 10. After each panel round

Round folders are named `YYYY-MM-DD_HHMM_<champion>_<K-D-A>` (date and local start time of the recording, champion, KDA), e.g. `2026-11-19_0145_zeri_9-5-6`. Drive mirrors that: `clips/<round>/` and `digests/<date>/`; `previews/` is flat (the 7-day prune reads it).

After each panel round, run this from the `api` folder (the API must be running when you pass a compilation id):

```
uv run grokbot-publish-round <round folder> --game league --clips-dir "<highlights session folder>" [--compilation-id <id>] [--asset-id <id>] [--no-jev]
```

`--clips-dir` is the `highlights\<game>\<session>` folder the clips were cut into; `clips.json` names bare files that live there, not in the round folder. It builds the name-free digest, refuses it if any transcript text slipped in, uploads the clips to Drive `clips/` and the digest to `digests/`, saves `digest.json` in the round folder, and sends the Lead a `round.published` event with the ids the Editor needs.
