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
5. Optional: `GROKBOT_DRIVE_PREVIEW_RETENTION_DAYS` sets how long previews stay in Drive.
6. In each bot, add the Drive connector signed into this dedicated account only.

## 6. Queue repo and tokens

1. Create a new private GitHub repo named `ai-video-editor-queue`. Add a `queue/` folder and a `rejected/` folder (each with a placeholder file) and a short README.
2. Create a fine-grained token for the Lead and Editor: this repo only, contents write.
3. Create a fine-grained token for DevOps and Viewer: the main repo only, contents and pull requests, no workflows.
4. Give each bot only its own token. Keep both out of the chat history; paste them into the bot's secret settings.
5. On master of the main repo, branch protection should require pull requests and green tests.
6. Clone the queue repo on the PC. Env: `GROKBOT_QUEUE_REPO_DIR` is the clone's folder path.

## 7. The runner

Env knobs: `GROKBOT_RUNNER_POLL_SECONDS`, `GROKBOT_HEARTBEAT_SECONDS`, `GROKBOT_QUEUE_DEPTH_CAP` (defaults are fine to start).

Create a Task Scheduler job that starts at logon and restarts on failure, running:

```
uv run --project C:\Users\taile\source\repos\ai-video-editor\api grokbot-runner
```

In the task's settings, turn on "If the task fails, restart every" a minute or two, with a high retry count, and "Run task as soon as possible after a scheduled start is missed".

## 8. Power

Turn PC sleep off while you are working (Settings, System, Power). If the PC sleeps the heartbeat stops; after 15 minutes the Lead texts you once and sleeps the Editor.

## 9. Check it works

1. Text the Lead "sleep". Confirm a `SLEEP` file appears in the queue repo, then text "wake".
2. Wait for one heartbeat event in the Lead.
3. Ask the Lead for a 15 second preview of any clip and watch for the preview in Drive.
4. After a day, read the Lead's evening digest for usage, then decide the wake model and plan.

## Day-one limits

On day one only previews, sleep and wake are executed. Edit plans and verdicts are accepted into the decisions log so the bots can practice the format. The rest comes on days two and three.
