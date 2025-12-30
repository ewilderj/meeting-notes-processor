# Agent Instructions

This document contains instructions for AI agents working on this project.

## Repository Architecture

This project supports **two deployment models**:

1. **Same Repository**: Code and data together (default, simpler)
2. **Separated Repositories**: Code and data in separate repos (recommended for production)

When working with separated repositories:
- **Processor repo** contains: scripts, config, workflows
- **Data repo** contains: inbox/, transcripts/, notes/
- Scripts use `WORKSPACE_DIR` environment variable to locate data repo

## Python Environment Management

**Always use `uv` for Python package management and execution.**

### Running Python Scripts

Scripts in this project use **PEP 723 inline script metadata** to declare dependencies. This allows `uv` to automatically detect and install required packages.

```bash
# Same-repository setup
uv run webhook_daemon.py
uv run run_summarization.py

# Separated-repository setup (from processor repo)
WORKSPACE_DIR=../meeting-notes uv run run_summarization.py

# Run as background daemon
uv run webhook_daemon.py &

# With environment variables
GITHUB_TOKEN=xxx uv run webhook_daemon.py
```

### Inline Script Dependencies (PEP 723)

Python scripts declare dependencies using inline metadata at the top of the file:

```python
#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "flask>=3.0.0",
#     "pyyaml>=6.0.0",
# ]
# ///
```

**Benefits:**
- No need for `--with` flags
- Dependencies travel with the script
- Simpler command lines
- Self-documenting

### Installing Packages System-Wide (Not Recommended)
```bash
# Only use if you need system-wide installation
uv pip install --system package-name
```

### Common Commands

**Same-repository setup:**
- **Run the summarization**: `uv run run_summarization.py`
- **Run webhook daemon**: `uv run webhook_daemon.py`
- **Run daemon with git push**: `GH_TOKEN=xxx uv run webhook_daemon.py &`
- **Test webhook**: `curl -X POST http://localhost:9876/webhook -H "Content-Type: application/json" -d '{"title": "Test", "transcript": "Content"}'`

**Separated-repository setup:**
- **Run the summarization**: `WORKSPACE_DIR=../meeting-notes uv run run_summarization.py`
- **Run webhook daemon**: Configure `config.yaml` with paths to data repo, then `uv run webhook_daemon.py`
- **GitHub Actions**: Uses workflow from `.github/workflows/process-transcripts-separated.yml`

## Configuration

### Webhook Daemon (config.yaml)

For **same-repository** setup:
```yaml
directories:
  inbox: inbox
  repository: .
```

For **separated-repository** setup:
```yaml
directories:
  inbox: ../meeting-notes/inbox
  repository: ../meeting-notes

git:
  repository_url: "github.com/ewilderj/meeting-notes.git"
```

### Processing Script (run_summarization.py)

Supports `WORKSPACE_DIR` environment variable:
- If not set: Uses current directory (same-repository mode)
- If set: Uses specified path to data repository

## Development Workflow

1. Make changes to Python files
2. Add inline script metadata for any new dependencies
3. Test with `uv run <script>`
4. For separated repos: Test with `WORKSPACE_DIR=../meeting-notes uv run <script>`
5. Commit and push changes

## GitHub Actions

### Same-Repository
Use existing `.github/workflows/process-transcripts.yml`

### Separated-Repository
Use `.github/workflows/process-transcripts-separated.yml`:
- Workflow lives in **data repo**
- Checks out both data and processor repos
- Runs processor with `WORKSPACE_DIR` pointing to data repo
- Commits results back to data repo

## Notes

- Never use `python3` or `pip3` directly
- Always prefix Python commands with `uv run`
- Use inline script metadata (PEP 723) for dependencies
- `uv` handles virtual environments automatically
- For separated repos, always set `WORKSPACE_DIR` when running processor scripts
