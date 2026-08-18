# Meeting Notes Processor

Automatically transform meeting transcripts into organized, searchable org-mode notes using AI.

Drop a transcript in, get structured summaries with action items, participants, and smart categorization.

Read my blog post for the [story of how this came to be](https://wilder-james.com/blog/meeting-notes/).

## Table of Contents

- [Overview](#overview)
- [Choosing Your Setup](#choosing-your-setup)
- [Requirements](#requirements)
- [Setup](#setup)
  - [1. Create Your Data Repository](#1-create-your-data-repository)
  - [2. Clone the Processor](#2-clone-the-processor)
- [Manual Processing](#manual-processing)
- [Automated Processing with meetingnotesd](#automated-processing-with-meetingnotesd)
  - [Standalone Mode](#standalone-mode-local-processing)
  - [Relay Mode](#relay-mode-cloud-processing-via-github-actions)
  - [Daemon Configuration Reference](#daemon-configuration-reference)
- [Calendar Integration](#calendar-integration)
- [GitHub Actions Setup](#github-actions-setup)
- [Output Format](#output-format)
- [Command Reference](#command-reference)
- [Troubleshooting](#troubleshooting)

---

## Overview

This tool processes meeting transcripts from any source—MacWhisper, Zoom, Teams, Google Meet, or plain text files—and produces:

- **Summarized notes** in org-mode format with TL;DR, action items, decisions, and open questions
- **Smart filenames** based on content (e.g., `20251230-q1-planning-discussion.org`)
- **Organized archives** with original transcripts preserved

**AI backends supported:** GitHub Copilot (Claude Opus 4.5, GPT 5.2, etc.) or Google Gemini. See [Requirements](#requirements).

### Four Ways to Run

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                         How do you want to process?                         │
├─────────────────────────────────────────────────────────────────────────────┤
│                                                                             │
│   MANUAL                          AUTOMATED                                 │
│   ──────                          ─────────                                 │
│                                                                             │
│   ┌─────────────┐                 ┌─────────────────────────────────────┐   │
│   │   Manual    │                 │        meetingnotesd daemon         │   │
│   │             │                 │   (receives webhooks from MacWhisper│   │
│   │ Drop files  │                 │    or other automation tools)       │   │
│   │ in inbox/,  │                 ├──────────────────┬──────────────────┤   │
│   │ run script  │                 │   Standalone     │      Relay       │   │
│   └─────────────┘                 │   (local AI)     │   (cloud AI)     │   │
│                                   └──────────────────┴──────────────────┘   │
│         │                                   │                  │            │
│         ▼                                   ▼                  ▼            │
│   ┌───────────┐                      ┌───────────┐      ┌───────────┐       │
│   │  Local    │                      │  Local    │      │  GitHub   │       │
│   │Processing │                      │Processing │      │  Actions  │       │
│   └───────────┘                      └───────────┘      └───────────┘       │
│                                                                             │
│   Also: GitHub Actions on push (no daemon, cloud processing)                │
└─────────────────────────────────────────────────────────────────────────────┘
```

| Mode | Trigger | Processing | Best For |
|------|---------|------------|----------|
| **Manual** | Run `run_summarization.py` | Local | Occasional use, full control |
| **GitHub Actions (push)** | `git push` to inbox/ | Cloud | Set-and-forget, no daemon |
| **Daemon: Standalone** | Webhook (e.g., MacWhisper) | Local | Real-time, privacy, offline |
| **Daemon: Relay** | Webhook → workflow_dispatch | Cloud | Real-time + cloud power |

---

## Choosing Your Setup

**Start here based on your needs:**

**"I just want to try it out"**
→ Use [Manual Processing](#manual-processing). Drop a file in `inbox/`, run the script.

**"I want hands-off processing when I push transcripts to GitHub"**
→ Use [GitHub Actions (push-based)](#push-based-trigger-no-daemon). No daemon needed—just push files and GitHub does the rest.

**"I use MacWhisper and want transcripts processed automatically"**
→ Use the [meetingnotesd daemon](#automated-processing-with-meetingnotesd). Choose between:
  - **Standalone mode**: Everything runs on your Mac. Simpler setup, works offline.
  - **Relay mode**: Your Mac receives webhooks, but processing happens in GitHub Actions. Better for slower machines or when you want cloud audit trails.

**"I'm on a team sharing meeting notes"**
→ Use GitHub Actions (either push-based or relay). Everyone commits to the same data repo; processing happens in the cloud with full history.

---

## Architecture

We recommend keeping this processor code separate from your meeting data:

```
~/projects/
├── meeting-notes-processor/   # This repo (code)
└── my-meeting-notes/          # Your repo (data)
    ├── inbox/                 # Drop transcripts here
    ├── transcripts/           # Processed originals
    └── notes/                 # AI-generated summaries
```

This separation keeps your notes history clean and lets you update the processor independently. Teams can share a data repo while each member runs their own processor.

---

## Requirements

- **Python 3.11+** with [uv](https://docs.astral.sh/uv/) package manager
- **Node.js 22+** with npm
- **AI Backend** (choose one):
  - **GitHub Copilot CLI**: `npm install` (included in package.json)
    - Requires GitHub Copilot subscription
    - Run once to authenticate interactively, or use a PAT (see later)
  - **Google Gemini CLI**: `npm install` (included in package.json)
    - Requires Google AI API key or interactive authentication

---

## Setup

### 1. Create Your Data Repository

```bash
mkdir my-meeting-notes && cd my-meeting-notes
git init

mkdir -p inbox transcripts notes
touch inbox/.gitkeep transcripts/.gitkeep notes/.gitkeep

git add . && git commit -m "Initial structure"
```

Optionally push to GitHub for backup/sharing:
```bash
git remote add origin https://github.com/YOUR_USERNAME/my-meeting-notes.git
git push -u origin main
```

### 2. Clone the Processor

```bash
cd ..
git clone https://github.com/ewilderj/meeting-notes-processor.git
cd meeting-notes-processor
npm install
```

---

## Manual Processing

The simplest way to use this tool: drop transcripts in `inbox/`, run the script.

```bash
# Copy a transcript to your data repo's inbox
cp meeting-recording.txt ../my-meeting-notes/inbox/

# Process it
uv run run_summarization.py --workspace ../my-meeting-notes
```

Results appear in your data repo:
- `transcripts/20251230-q1-planning.txt` — renamed original
- `notes/20251230-q1-planning.org` — AI-generated summary

### Options

```bash
uv run run_summarization.py [OPTIONS]

--workspace PATH    # Path to data repo (default: current directory)
--target copilot    # Use GitHub Copilot CLI (default)
--target gemini     # Use Google Gemini CLI
--model MODEL       # Specific model (e.g., claude-opus-4.5, gpt-5.2, gemini-2.0-flash-exp)
--prompt FILE       # Custom prompt template (default: see below)
--git               # Commit results to git (for automation)
```

### Customizing the Prompt

The AI summarization prompt is stored in `prompt.txt`. The script looks for it in this order:

1. **Workspace directory** (`<workspace>/prompt.txt`) — allows per-data-repo customization
2. **Processor directory** (`prompt.txt` alongside the script) — shared default

Edit the prompt to customize:

- Output format and sections
- What information to extract (actions, decisions, questions, etc.)
- Your name and how it appears in transcripts
- Org-mode formatting preferences

The prompt uses `{input_file}` and `{output_file}` placeholders which are filled in at runtime.

**Example customizations:**

```bash
# Copy prompt to your data repo for customization
cp prompt.txt ../my-meeting-notes/

# Use an explicit prompt file
uv run run_summarization.py --prompt my-custom-prompt.txt

# Keep separate prompts for different meeting types
uv run run_summarization.py --prompt prompts/standup.txt
uv run run_summarization.py --prompt prompts/planning.txt
```

### Batch Processing

Drop multiple transcripts in `inbox/`—they'll all be processed in one run.

### Pre-Processing Pipeline

Before summarization, the processor automatically:

1. **Filters junk transcripts** — Recordings shorter than 60 seconds or with less than 200 characters of content are skipped. This catches audio fragments, test recordings, and accidental mic activations.

2. **Splits multi-meeting recordings** — If recording wasn't stopped between back-to-back meetings, the processor uses a lightweight LLM call (Haiku) to detect meeting boundaries (farewell/greeting patterns) and splits the file into separate transcripts for individual processing.

Split files get `-part1`, `-part2` suffixes and interpolated timestamps from the original recording's metadata.

---

## Automated Processing with meetingnotesd

For hands-free processing, run the `meetingnotesd` daemon. It:

- Receives webhooks from MacWhisper (or any HTTP client)
- Writes transcripts to your data repo's inbox
- Processes them automatically

The daemon supports two modes:

| Mode | Processing Location | Best For |
|------|--------------------| ---------|
| **Standalone** | Local machine | Single user, privacy, simplicity |
| **Relay** | GitHub Actions | Teams, audit trails, cloud compute |

### Standalone Mode (Local Processing)

The simpler option—everything runs on your machine.

**1. Configure `config.yaml`:**

```yaml
data_repo: ../my-meeting-notes

git:
  auto_commit: true
  auto_push: true   # Optional: push to remote
  repository_url: "github.com/USER/my-meeting-notes.git"
  branch: main

processing:
  standalone:
    enabled: true
    command: "uv run run_summarization.py --git"
```

**2. Start the daemon:**

```bash
uv run meetingnotesd.py
```

**3. Send a transcript** (e.g., configure MacWhisper to POST here):

```bash
curl -X POST http://localhost:9876/webhook \
  -H "Content-Type: application/json" \
  -d '{"title": "Team Standup", "transcript": "Full transcript text..."}'
```

The daemon will:
1. Write the transcript to `inbox/`
2. Run `run_summarization.py` locally
3. Commit results to git (and push if configured)

### Relay Mode (Cloud Processing via GitHub Actions)

Offload AI processing to GitHub's servers. Useful for teams or when you want processing to continue even when your laptop is closed.

**1. Configure `config.yaml`:**

```yaml
data_repo: ../my-meeting-notes

git:
  auto_commit: true
  auto_push: true
  repository_url: "github.com/USER/my-meeting-notes.git"
  branch: main

github:
  workflow_dispatch:
    enabled: true
    repo: "USER/my-meeting-notes"
    workflow: "process-transcripts.yml"
    ref: "main"

processing:
  standalone:
    enabled: false
```

**2. Set up GitHub Actions** in your data repo (see [GitHub Actions Setup](#github-actions-setup))

**3. Export your token and start the daemon:**

```bash
export GH_TOKEN=ghp_xxxxxxxxxxxx
uv run meetingnotesd.py
```

The daemon will:
1. Write the transcript to `inbox/`
2. Commit and push to your data repo
3. Trigger the GitHub Actions workflow via `workflow_dispatch`
4. GitHub Actions runs the summarization and commits results

### Daemon Configuration Reference

Full `config.yaml` options:

```yaml
# HTTP server
server:
  host: 127.0.0.1
  port: 9876

# Path to your data repository
data_repo: ../my-meeting-notes

# Git operations
git:
  auto_commit: true
  auto_push: true
  repository_url: "github.com/USER/my-meeting-notes.git"
  commit_message_template: "Add transcript: {title}"
  branch: main
  remote: origin

# Keep local repo in sync (pull before processing)
sync:
  enabled: true
  on_startup: true
  before_accepting_webhooks: true
  poll_interval_seconds: 1800    # Background polling (0 = disabled); use --debug to verify
  ff_only: true

# STANDALONE MODE: process locally
processing:
  standalone:
    enabled: false
    command: "uv run run_summarization.py --git"
    working_directory: "."
    timeout_seconds: 600
    async: false  # Return immediately from webhook, process in background

# RELAY MODE: trigger GitHub Actions
github:
  workflow_dispatch:
    enabled: false
    repo: "USER/my-meeting-notes"
    workflow: "process-transcripts.yml"
    ref: "main"
    inputs: {}

# Optional: run a command when background sync pulls new commits
hooks:
  on_new_commits:
    enabled: false
    command: "echo 'New commits arrived'"
    working_directory: "."
    timeout_seconds: 600
```

### Daemon Command-Line Options

```bash
uv run meetingnotesd.py [OPTIONS]

--sync-once    # Sync repo and exit (useful for testing)
--debug        # Enable verbose logging
```

### Running as a System Service

For production use, run the daemon as a proper system service so it starts automatically on boot and restarts on crash.

See [service-configs/README.md](service-configs/README.md) for detailed setup instructions.

#### Environment Variables

When running as a system service, the following environment variables are useful:

| Variable | Description | Example |
|----------|-------------|---------|
| `GH_TOKEN` | GitHub token for push/workflow_dispatch | `ghp_xxxxxxxxxxxx` |
| `WEBHOOK_CONFIG` | Path to config file | `/home/user/config.yaml` |
| `COPILOT_PATH` | Path to copilot CLI (for nvm/npm installs) | `/home/user/bin/copilot-wrapper` |

**COPILOT_PATH** is especially important for systemd/launchd where the normal shell PATH isn't available. If using nvm, create a wrapper script:

```bash
# ~/bin/copilot-wrapper
#!/bin/bash
export NVM_DIR="$HOME/.nvm"
[ -s "$NVM_DIR/nvm.sh" ] && \. "$NVM_DIR/nvm.sh"
exec copilot "$@"
```

Then set `COPILOT_PATH=/home/user/bin/copilot-wrapper` in your service configuration.

#### Git Repository Setup for the Daemon

When running the daemon (especially as a system service), it needs to push changes without interactive authentication. **HTTPS remotes with embedded tokens** work more reliably than SSH:

```bash
# Check your current remote
git remote -v

# If using SSH (git@github.com:...), switch to HTTPS with token:
git remote set-url origin https://YOUR_TOKEN@github.com/username/repo.git
```

This avoids SSH key passphrase prompts and agent issues that can cause the daemon to hang.

### Testing the Daemon

```bash
# Health check
curl http://localhost:9876/

# Send a test transcript
uv run send_transcript.py examples/q1-planning-sarah.txt
```

### Calendar Endpoint

The daemon provides a `/calendar` endpoint to update `calendar.org` in your data repo. This enables calendar-enhanced processing where meeting participants are cross-referenced with calendar entries.

```bash
# Send calendar data as JSON
curl -X POST http://localhost:9876/calendar \
  -H "Content-Type: application/json" \
  -d '{"calendar": "* Meeting <2026-01-22 Thu 10:00>"}'

# Or as plain text (useful for piping files)
curl -X POST http://localhost:9876/calendar \
  -H "Content-Type: text/plain" \
  --data-binary @calendar.org
```

When a recently updated `calendar.org` exists in your data repo, `run_summarization.py` automatically uses it to:
- Match transcripts to calendar entries by time and participants
- Correct speaker misidentification in transcripts

Calendar data older than six hours is ignored rather than treated as authoritative
identity evidence. Set `CALENDAR_PATH` to use a calendar outside the data workspace,
or `CALENDAR_MAX_AGE_SECONDS` to change the safety window.
- Add accurate meeting times and attendee information

---

## Calendar Integration

The processor can cross-reference your calendar to improve meeting notes — correcting misidentified speakers, matching transcripts to the right meeting, and adding accurate metadata.

### How It Works

1. Your calendar data lives as `calendar.org` in your data repo (org-mode format)
2. When processing a transcript, the script finds calendar entries for that day
3. Calendar context is included in the AI prompt so the LLM can:
   - **Correct speaker names** — transcription often mishears names; calendar participants are authoritative
   - **Match to the right meeting** — especially useful when you have multiple meetings per day
   - **Add metadata** — `:CALENDAR_MATCH:`, `:CALENDAR_TIME:`, and `:MEETING_LINK:` properties

### Calendar File Format

The `calendar.org` file uses standard org-mode format. Each entry looks like:

```org
* Meeting Title <2026-01-26 Mon 14:00-14:30>
:PROPERTIES:
:PARTICIPANTS: Alice Smith <alice@example.com>, Bob Jones <bob@example.com>
:END:
[[https://teams.microsoft.com/l/meetup-join/abc123][📹 Join Meeting]]
```

- **Title**: The `*` heading with a `<timestamp>` in angle brackets
- **Participants**: Comma-separated in `:PROPERTIES:` drawer (email addresses are stripped automatically)
- **Meeting links**: Org-mode links with 📹 emoji are extracted as video call URLs
- **All-day events**: Omit the time range: `<2026-01-26 Mon>`

### Enabling/Disabling

Calendar integration is **enabled by default** when `calendar.org` exists in your
data repo and its latest git commit is no more than six hours old. Outside a git
repository, the file modification time is used. Stale calendar data is skipped
with a warning. In a git repository, uncommitted or untracked calendar data is
also rejected. Control calendar integration with CLI flags:

```bash
# Default: calendar enabled (if calendar.org exists)
uv run run_summarization.py --workspace ../my-meeting-notes

# Explicitly disable
uv run run_summarization.py --workspace ../my-meeting-notes --no-calendar

# Explicitly enable (errors if calendar.org is missing)
uv run run_summarization.py --workspace ../my-meeting-notes --calendar
```

### Updating Calendar Data

Use the daemon's `/calendar` endpoint to push calendar data programmatically:

```bash
# Send calendar data as JSON
curl -X POST http://localhost:9876/calendar \
  -H "Content-Type: application/json" \
  -d '{"calendar": "* Meeting <2026-01-22 Thu 10:00>"}'

# Or pipe a file as plain text
curl -X POST http://localhost:9876/calendar \
  -H "Content-Type: text/plain" \
  --data-binary @calendar.org
```

### What Gets Added to Notes

When a transcript matches a calendar entry, these properties are added:

```org
:PROPERTIES:
:PARTICIPANTS: Thabani, Edd
:SLUG: thabani-edd-1-1
:CALENDAR_MATCH: Thabani / Edd 1:1
:CALENDAR_TIME: 14:00-14:30
:MEETING_LINK: https://teams.microsoft.com/l/meetup-join/abc123
:END:
```

For 1:1 meetings, the slug is automatically formatted as `firstname-edd-1-1`.

---

## GitHub Actions Setup

You can trigger GitHub Actions processing in two ways:

| Trigger | How it works | Best for |
|---------|--------------|----------|
| **Push-based** | Workflow runs when files are pushed to `inbox/` | Simple setup, no daemon needed |
| **Daemon-based** | `meetingnotesd` triggers workflow via `workflow_dispatch` | Real-time webhook processing |

### Common Setup (Both Approaches)

**1. Copy the workflow template to your data repo:**

```bash
mkdir -p ../my-meeting-notes/.github/workflows
cp workflows-templates/process-transcripts-data-repo.yml \
   ../my-meeting-notes/.github/workflows/process-transcripts.yml
```

**2. Create a fine-grained Personal Access Token:**

Go to [GitHub Settings → Developer settings → Fine-grained tokens](https://github.com/settings/tokens?type=beta):
- **Repository access**: Select your data repository
- **Permissions**:
  - Contents: Read and write
  - Actions: Read and write (required for workflow_dispatch)
  - Copilot Requests (if using Copilot CLI in Actions)

**3. Add the token as a repository secret:**

In your data repo: Settings → Secrets and variables → Actions → New repository secret
- Name: `GH_TOKEN`
- Value: Your token

### Push-Based Trigger (No Daemon)

Edit the workflow file to enable the push trigger:

```yaml
# In .github/workflows/process-transcripts.yml
on:
  push:
    paths:
      - 'inbox/**'
  workflow_dispatch:  # Keep this for manual runs
```

Now just push transcripts to your data repo:

```bash
cp transcript.txt ../my-meeting-notes/inbox/
cd ../my-meeting-notes
git add inbox/ && git commit -m "Add transcript" && git push
```

The workflow runs automatically when files land in `inbox/`.

### Daemon-Based Trigger (workflow_dispatch)

Keep the workflow file as-is (push trigger commented out). The daemon triggers processing via the GitHub API.

Configure `meetingnotesd` for relay mode:

```yaml
# config.yaml
github:
  workflow_dispatch:
    enabled: true
    repo: "USER/my-meeting-notes"
    workflow: "process-transcripts.yml"
```

Start the daemon with your token:

```bash
export GH_TOKEN=ghp_xxxxxxxxxxxx
uv run meetingnotesd.py
```

When the daemon receives a webhook, it pushes the transcript and triggers the workflow.

---

## Output Format

Each transcript produces two files:

**`transcripts/20251230-q1-planning.txt`** — Original transcript (renamed)

**`notes/20251230-q1-planning.org`** — Org-mode summary:

```org
** Q1 Planning Discussion :note:transcribed:
[2025-12-30 Mon 14:00]
:PROPERTIES:
:PARTICIPANTS: Sarah, Edd
:TOPIC: Q1 Planning
:SLUG: q1-planning
:END:

TL;DR: Agreed on product roadmap priorities and hiring plan for Q1.

*** Actions
- [ ] Edd: Draft product roadmap by Friday
- [ ] Sarah: Schedule candidate interviews

*** Open Questions
- Budget allocation for new tooling

*** Summary
Discussion covered three main areas...
```

---

## Command Reference

### run_summarization.py

```bash
uv run run_summarization.py [OPTIONS]

Options:
  --workspace PATH          Path to data repo (default: current directory)
  --target copilot|gemini   AI backend (default: copilot)
  --model MODEL             Specific model name
  --prompt FILE             Custom prompt template
  --git                     Commit results to git
  --debug                   Verbose output (stream copilot output, show commands)
```

### meetingnotesd.py

```bash
uv run meetingnotesd.py [OPTIONS]

Options:
  --sync-once    Sync repo and exit
  --debug        Verbose logging
```

### send_transcript.py

```bash
uv run send_transcript.py <transcript-file> [webhook-url]

# Examples:
uv run send_transcript.py examples/q1-planning-sarah.txt
uv run send_transcript.py transcript.txt http://localhost:9876/webhook
```

---

## Troubleshooting

**Processing hangs indefinitely with copilot**
- Run `npm install` in the processor directory first
- `npx` may prompt to install packages, which hangs when stdin is captured
- Use `--debug` flag to stream output and see what's happening

**Copilot runs but produces no output files**
- When running non-interactively (`-p` mode), copilot requires `--allow-all-tools --allow-all-paths` to authorize tool usage
- Without these flags, copilot silently denies tool calls with "Permission denied" and exits 0
- The script handles this automatically, but if you're running copilot manually, include these flags

**Wrong paths or "directory not found"**
- For separated repos, use `--workspace ../my-meeting-notes` to point to your data repo
- Paths in `config.yaml` are relative to the processor directory

**Git push failing**
- Check that `git config user.name` and `user.email` are set
- Verify your `GH_TOKEN` has Contents: write permission
- For relay mode, ensure Actions: write permission for workflow_dispatch

**AI summarization errors**
- Run `npx @github/copilot` to authenticate Copilot locally
- Check that transcripts have actual content (not just a title)
- Try a different model with `--model`

**Webhook not receiving requests**
- Verify daemon is running: `curl http://localhost:9876/`
- Ensure sender (eg MacWhisper) has the correct URL

**workflow_dispatch not triggering**
- Verify `GH_TOKEN` has Actions: write permission
- Check the workflow file exists in your data repo
- Use `--debug` flag to see detailed error messages

---

## More Information

- [AGENTS.md](AGENTS.md) — Instructions for AI coding agents working on this project
- [PRD.md](PRD.md) — Product requirements and implementation details
- [examples/](examples/) — Sample transcripts for testing

---

## Copilot Skills

This repo includes skills for GitHub Copilot CLI that extend meeting notes functionality.

### WorkIQ Notes Skill

Generate meeting notes for meetings you missed by querying Microsoft 365 Copilot (WorkIQ).

**Use case:** You missed a meeting that was recorded in Teams/Stream. Instead of watching the recording, ask Copilot to generate org-mode notes from WorkIQ's understanding of the meeting.

**How it works:**
1. Copilot queries WorkIQ for meeting details (attendees, topics, decisions, actions)
2. Formats the response into your standard org-mode template
3. Writes to your notes directory with proper git workflow

**Example:**
```
You: "Generate notes for the CIP SLT Monthly Planning Review on 2026-02-04"

Copilot:
- Queries WorkIQ for that meeting
- Creates ~/git/meeting-notes/notes/20260204-cip-slt-monthly-planning.org
- Commits and pushes automatically
```

**Setup:**
1. Symlink the skill to your Copilot skills directory:
   ```bash
   ln -sf ~/git/meeting-notes-processor/skills/workiq-notes ~/.copilot/skills/workiq-notes
   ```
2. Ensure WorkIQ MCP server is configured and EULA accepted
3. Configure `data_repo` in `config.yaml` or use default `~/git/meeting-notes`

**Limitations:**
- WorkIQ returns excerpts/snippets, not full transcripts
- Some action item owners may be unclear if not explicitly stated in meeting
- Works best for meetings recorded in Teams/Stream
- Participant lists may be incomplete (only speakers identified in transcript)

**Helper script:** `skills/workiq-notes/write_note.py` handles file writing and git operations. Can be used standalone:
```bash
cat note.org | uv run skills/workiq-notes/write_note.py \
  --date 2026-02-04 --slug my-meeting --title "My Meeting"
```
