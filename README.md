# Meeting Notes Processor

Automatically transform meeting transcripts into organized, searchable org-mode notes using AI. Drop a transcript in, get structured summaries with action items, participants, and smart categorization.

## What This Does

Have meeting transcripts piling up? This tool:
- **Summarizes** transcripts using AI (Claude, Gemini, etc.)
- **Extracts** action items, decisions, and open questions
- **Organizes** with meaningful filenames based on content, not timestamps
- **Formats** as org-mode files compatible with Emacs, Obsidian, and other tools
- **Automates** the entire pipeline via GitHub Actions or webhook

Perfect for processing transcripts from MacWhisper, Zoom, Teams, Google Meet, or any text-based recording tool.

## Architecture

**Recommended Setup: Separated Repositories**

Keep your code and meeting data in separate repositories for clean version history, independent access controls, and better organization:

- **Code repository** (this repo): Processing scripts, configuration, workflows
- **Data repository** (yours): Meeting transcripts and notes (inbox/, transcripts/, notes/)

This separation means:
- ✅ Your meeting data stays in its own private repo
- ✅ Code updates don't clutter your notes history  
- ✅ You can grant different access levels to each repo
- ✅ Faster clone/sync of the processor code

**Alternative: Single Repository**
For personal use, you can also run everything in one repo with code and data together. See "Single Repository Setup" below.

## Quick Start (Separated Repositories)

### 1. Create Your Data Repository

Create a new repository for your meeting notes:

```bash
mkdir my-meeting-notes
cd my-meeting-notes
git init

# Create directory structure
mkdir -p inbox transcripts notes
touch inbox/.gitkeep transcripts/.gitkeep notes/.gitkeep

git add .
git commit -m "Initial structure"
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

### 3. Configure the Processor

Edit `config.yaml` to point to your data repository:

```yaml
# config.yaml
directories:
  inbox: ../my-meeting-notes/inbox
  repository: ../my-meeting-notes

git:
  repository_url: "github.com/YOUR_USERNAME/my-meeting-notes.git"
```

### 4. Process Transcripts

Add a transcript to your data repo's inbox:

```bash
cp transcript.txt ../my-meeting-notes/inbox/
```

Process it:

```bash
WORKSPACE_DIR=../my-meeting-notes uv run run_summarization.py
```

The result appears in `../my-meeting-notes/notes/` and `../my-meeting-notes/transcripts/` with a meaningful filename like `20251230-q1-planning-discussion.org`

### 5. Automate with GitHub Actions (Optional)

Copy the workflow template to your data repository:

```bash
mkdir -p ../my-meeting-notes/.github/workflows
cp workflows-templates/process-transcripts-data-repo.yml \
   ../my-meeting-notes/.github/workflows/process-transcripts.yml
```

Edit the workflow to use your processor repo URL, then configure secrets and permissions:

**GitHub Actions Setup:**
1. In your data repository settings, go to Settings → Secrets and variables → Actions
2. Add a new repository secret named `GH_TOKEN`
3. Create a fine-grained Personal Access Token with:
   - **Repository access**: Your data repository
   - **Permissions**: 
     - Contents (Read and write)
     - Copilot Requests (if using Copilot CLI)

**For GitHub Copilot CLI in Actions:**
- The workflow automatically uses the `GH_TOKEN` secret for authentication
- No additional Copilot setup needed in GitHub Actions
- The token provides both git operations and Copilot CLI authentication

**For local development with Copilot:**
- Run `npx @github/copilot auth` to authenticate
- Requires active Copilot subscription or GitHub Enterprise access

Now when you push transcripts to `inbox/`, they're automatically processed!

## Webhook Integration (MacWhisper, etc.)

For real-time processing as transcripts arrive, run the webhook daemon:

```bash
uv run webhook_daemon.py
```

Configure your transcription tool to POST to `http://localhost:9876/webhook`:

```json
{
  "title": "Team Standup",
  "transcript": "Full transcript text..."
}
```

The daemon writes to `inbox/` and optionally commits/pushes to trigger automation.

**Test it:**
```bash
uv run test_webhook.py examples/q1-planning-sarah.txt
```

## Single Repository Setup

If you prefer to keep everything in one place:

1. **Clone this repository**
   ```bash
   git clone https://github.com/ewilderj/meeting-notes-processor.git my-meeting-notes
   cd my-meeting-notes
   npm install
   ```

2. **Process transcripts directly**
   ```bash
   cp transcript.txt inbox/
   uv run run_summarization.py
   ```

No `WORKSPACE_DIR` needed—everything runs in the current directory.

## Command Reference

### Process Transcripts

```bash
# Separated repositories
WORKSPACE_DIR=../my-meeting-notes uv run run_summarization.py

# Single repository
uv run run_summarization.py

# Options
--target copilot      # Use GitHub Copilot (default, requires @github/copilot CLI)
--target gemini       # Use Google Gemini (requires @google/gemini-cli)
--model MODEL_NAME    # Specify custom model (e.g., claude-sonnet-4.5)
--git                 # Commit and push results automatically (for CI/CD)
```

### Webhook Daemon

```bash
# Start daemon (separated repos, configured via config.yaml)
uv run webhook_daemon.py

# With GitHub push enabled
GH_TOKEN=xxx uv run webhook_daemon.py

# Test endpoint
curl -X POST http://localhost:9876/webhook \
  -H "Content-Type: application/json" \
  -d '{"title": "Meeting Title", "transcript": "Transcript text..."}'

# Or use the test script
uv run test_webhook.py examples/q1-planning-sarah.txt
```

## Directory Structure

```
Processor Repository (meeting-notes-processor/):
├── run_summarization.py       # Main processing script
├── webhook_daemon.py           # HTTP webhook receiver
├── test_webhook.py             # Webhook testing tool
├── config.yaml                 # Configuration for separated repos
├── examples/                   # Sample transcripts
│   ├── q1-planning-sarah.txt
│   ├── dunder-mifflin-sales.txt
│   └── mad-men-heinz.txt
└── workflows-templates/        # GitHub Actions templates
    └── process-transcripts-data-repo.yml

Data Repository (my-meeting-notes/):
├── inbox/                      # Drop transcripts here (.txt, .md)
├── transcripts/                # Processed originals (YYYYMMDD-slug.txt)
├── notes/                      # Org-mode summaries (YYYYMMDD-slug.org)
└── .github/workflows/          # Optional: automated processing
    └── process-transcripts.yml
```

## Output Format

Each transcript generates two files with content-based filenames:

**`transcripts/20251230-q1-planning-discussion.txt`** - Original transcript

**`notes/20251230-q1-planning-discussion.org`** - Org-mode summary with:
- **TL;DR** - One-sentence summary
- **Actions** - Checkbox list of agreed-upon tasks with owners
- **Open Questions** - Unresolved items
- **Summary** - Detailed discussion overview
- **Metadata** - Participants, topic, slug in property drawer

Example:
```org
** Meeting with Sarah :note:transcribed:
[2025-12-30 Mon 14:00]
:PROPERTIES:
:PARTICIPANTS: Sarah, Edd
:TOPIC: Q1 Planning
:SLUG: q1-planning-discussion
:END:

TL;DR: Discussed Q1 priorities including product roadmap, hiring, and CI/CD.

*** Actions
- [ ] Edd: Draft product roadmap by Friday
- [ ] Sarah: Interview engineering candidates
```

## Requirements

- **Python 3.11+** with `uv` package manager ([install uv](https://docs.astral.sh/uv/))
- **Node.js 22+** with npm
- **AI Backend**: One of:
  - GitHub Copilot CLI (`npm install -g @github/copilot`) - Requires GitHub Copilot subscription
  - Google Gemini CLI (`npm install -g @google/gemini-cli`) - Requires Google AI API key

**For GitHub Copilot (local development):**
- Authenticate with `npx @github/copilot auth`
- Requires active Copilot subscription or GitHub Enterprise access

**For GitHub Actions:**
- Copilot CLI authenticates automatically using the `GH_TOKEN` secret
- Token must be a fine-grained PAT with Contents: write and Copilot Requests permissions

## Configuration

### config.yaml (Separated Repositories)

```yaml
server:
  host: 127.0.0.1
  port: 9876

directories:
  inbox: ../my-meeting-notes/inbox
  repository: ../my-meeting-notes

git:
  auto_commit: true
  auto_push: true
  repository_url: "github.com/YOUR_USERNAME/my-meeting-notes.git"
  commit_message_template: "Add transcript: {title}"
```

### Environment Variables

- `WORKSPACE_DIR` - Path to data repository (for separated setup)
- `GH_TOKEN` - GitHub personal access token with fine-grained PAT permissions:
  - Contents: write (for git operations)
  - Copilot Requests (if using Copilot CLI)
  - Used by both webhook daemon (local) and GitHub Actions (cloud)

## Troubleshooting

**"Permission denied" when reading transcripts**
- Ensure `WORKSPACE_DIR` points to the correct location
- Check file permissions in inbox directory

**Git operations failing**
- Verify `git config user.name` and `user.email` are set
- For separated repos, ensure `WORKSPACE_DIR` is set correctly
- Check that paths in `config.yaml` are relative to processor directory

**Webhook not receiving requests**
- Confirm daemon is running: `curl http://localhost:9876/`
- Check firewall settings
- Verify MacWhisper/sender is configured with correct URL

**AI summarization errors**
- Ensure Copilot or Gemini CLI is installed and authenticated
- Check that transcripts contain actual content (not just titles)
- Verify model name if using `--model` flag

## More Information

- [AGENTS.md](AGENTS.md) - Developer documentation and AI agent instructions
- [PRD.md](PRD.md) - Detailed product requirements and implementation phases
- [examples/](examples/) - Sample transcripts for testing
