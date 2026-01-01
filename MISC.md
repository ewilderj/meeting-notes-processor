# Miscellaneous Notes

This document contains supplementary information that doesn't fit in the main README.

## Extensibility

Adding support for other AI backends (OpenAI API, Anthropic API, local models, etc.) is straightforward. The processor uses a simple plugin pattern in `run_summarization.py`—see the `process_transcript()` function for the `target` parameter implementation.

## Environment Variables

- `WORKSPACE_DIR` - Fallback for `--workspace` argument (for separated repos setup)
- `GH_TOKEN` - GitHub personal access token for:
  - Git push operations (daemon and Actions)
  - Workflow dispatch triggering (relay mode)
  - Copilot CLI authentication in GitHub Actions
- `WEBHOOK_CONFIG` - Path to config file (default: `config.yaml`)

## Single Repository Setup

If you prefer to keep code and data together, just don't use `--workspace`—the script will use the current directory for `inbox/`, `transcripts/`, and `notes/`.

## GitHub Actions Authentication

For GitHub Actions using Copilot CLI:
- The workflow automatically uses the `GH_TOKEN` secret for authentication
- No additional Copilot setup needed in GitHub Actions
- The token provides both git operations and Copilot CLI authentication
- Token must be a fine-grained PAT with:
  - Contents: Read and write
  - Actions: Read and write (for workflow_dispatch)
  - Copilot Requests (if using Copilot CLI in Actions)

## Directory Structure (Detailed)

```
Processor Repository (meeting-notes-processor/):
├── run_summarization.py       # Main processing script
├── meetingnotesd.py           # HTTP webhook receiver + repo agent
├── send_transcript.py         # Send transcripts to daemon
├── config.yaml                # Configuration for daemon
├── package.json               # Node.js dependencies
├── examples/                  # Sample transcripts
│   ├── q1-planning-sarah.txt
│   ├── dunder-mifflin-sales.txt
│   └── mad-men-heinz.txt
├── workflows-templates/       # GitHub Actions templates
│   └── process-transcripts-data-repo.yml
├── AGENTS.md                  # AI agent instructions
├── PRD.md                     # Product requirements
└── MISC.md                    # This file

Data Repository (my-meeting-notes/):
├── inbox/                     # Drop transcripts here (.txt, .md)
├── transcripts/               # Processed originals (YYYYMMDD-slug.txt)
├── notes/                     # Org-mode summaries (YYYYMMDD-slug.org)
└── .github/workflows/         # Optional: automated processing
    └── process-transcripts.yml
```
