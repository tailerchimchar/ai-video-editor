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
| `GROKBOT_HEARTBEAT_SECONDS` | `300` | heartbeat cadence (its own thread, never blocked by a render) |
| `GROKBOT_QUEUE_DEPTH_CAP` | `10` | pending items before a `queue.depth` warning |

When you turn on `GROKBOT_EXECUTE_PLANS` or `GROKBOT_RECORD_VERDICTS`, held items younger than 24 hours run on the next tick; older ones are marked expired and reported in one `queue.expired` event.

**Pin the runner.** The runner never pulls code. Run it from its own checkout of the main repo at a release tag, not from the folder you develop in:

```
git worktree add C:\ave-runner v0.1.0
cd C:\ave-runner\api
uv sync
```

A deploy means moving that checkout to the new tag (`git -C C:\ave-runner checkout v0.1.1`, then restart the two tasks). Copy `api\.env` into `C:\ave-runner\api\`.

Create two Task Scheduler tasks, both "At log on", "If the task fails, restart every 1 minute" with a high retry count, and "Run task as soon as possible after a scheduled start is missed". In each task's action set **Start in** to `C:\ave-runner\api` so `api\.env` loads (Task Scheduler otherwise starts in `C:\Windows\System32` and the runner exits with "run from the api directory so api/.env loads").

1. API: program `uv`, arguments `run dev`.
2. Runner: program `uv`, arguments `run grokbot-runner`. Give it a one-minute start delay so the API is up first.

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

After each panel round, run this from the `api` folder (the API must be running when you pass a compilation id):

```
uv run grokbot-publish-round <round folder> --game league [--compilation-id <id>] [--asset-id <id>] [--no-jev]
```

It builds the name-free digest, refuses it if any transcript text slipped in, uploads the clips to Drive `clips/` and the digest to `digests/`, saves `digest.json` in the round folder, and sends the Lead a `round.published` event with the ids the Editor needs.
