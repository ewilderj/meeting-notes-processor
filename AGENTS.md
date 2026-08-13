# Agent Instructions

This document contains instructions for AI agents working on this project.

## Repository Architecture

This project supports **two deployment models**:

1. **Same Repository**: Code and data together (default, simpler)
2. **Separated Repositories**: Code and data in separate repos (recommended for production)

When working with separated repositories:
- **Processor repo** contains: scripts, config, workflows
- **Data repo** contains: inbox/, transcripts/, notes/
- Use `--workspace` argument to specify data repo location

## Python Environment Management

**Always use `uv` for Python package management and execution.**

### Running Python Scripts

Scripts in this project use **PEP 723 inline script metadata** to declare dependencies. This allows `uv` to automatically detect and install required packages.

```bash
# Same-repository setup
uv run meetingnotesd.py
uv run run_summarization.py

# Separated-repository setup (from processor repo)
uv run run_summarization.py --workspace ../meeting-notes

# Run as background daemon
uv run meetingnotesd.py &

# With environment variables
GH_TOKEN=xxx uv run meetingnotesd.py
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
- **Run webhook daemon**: `uv run meetingnotesd.py`
- **Run daemon with git push**: `GH_TOKEN=xxx uv run meetingnotesd.py &`
- **Test webhook**: `curl -X POST http://localhost:9876/webhook -H "Content-Type: application/json" -d '{"title": "Test", "transcript": "Content"}'`

**Separated-repository setup:**
- **Run the summarization**: `uv run run_summarization.py --workspace ../meeting-notes`
- **Run webhook daemon**: Configure `config.yaml` with `data_repo` path, then `uv run meetingnotesd.py`
- **GitHub Actions**: Uses workflow from `.github/workflows/process-transcripts.yml` (copy from `workflows-templates/process-transcripts-data-repo.yml`)

## Configuration

### Webhook Daemon (config.yaml)

For **same-repository** setup:
```yaml
data_repo: .
```

For **separated-repository** setup:
```yaml
data_repo: ../meeting-notes

git:
  repository_url: "github.com/ewilderj/meeting-notes.git"
```

### Processing Script (run_summarization.py)

Supports `--workspace` argument (or `WORKSPACE_DIR` env var as fallback):
- If not specified: Uses current directory (same-repository mode)
- If specified: Uses that path as data repository

## Deployment

The `Makefile` handles deployment to nuctu (the production server):

```bash
make deploy    # Push to git, pull on nuctu, restart service
make status    # Show service status
make logs      # Tail service logs (journalctl -f)
make restart   # Restart service only
make ssh       # SSH to nuctu
```

The service runs as a system-level systemd unit (`meetingnotes-webhook`) on nuctu.
Restart requires sudo (will prompt for password via interactive SSH).

## Development Workflow

1. Make changes to Python files
2. Add inline script metadata for any new dependencies
3. Test with `uv run <script>`
4. For separated repos: Test with `uv run run_summarization.py --workspace ../meeting-notes`
5. Run tests: `uv run --with pytest --with pyyaml pytest tests/`
6. Commit and push changes
7. Deploy: `make deploy`

## GitHub Actions

### Same-Repository
Use existing `.github/workflows/process-transcripts.yml`

### Separated-Repository
Use `workflows-templates/process-transcripts-data-repo.yml` (copy to `.github/workflows/process-transcripts.yml` in the data repo):
- Workflow lives in **data repo**
- Checks out both data and processor repos
- Runs processor with `--workspace` pointing to data repo
- Commits results back to data repo

## Notes

- Never use `python3` or `pip3` directly
- Always prefix Python commands with `uv run`
- Use inline script metadata (PEP 723) for dependencies
- `uv` handles virtual environments automatically
- For separated repos, use `--workspace` argument for run_summarization.py

## Copilot CLI (Critical)

When invoking `copilot` in non-interactive/programmatic mode (`-p` flag), you **must** use
`--allow-all-tools --allow-all-paths` to authorize all tool usage. Without these flags,
copilot silently denies tool calls with "Permission denied" and exits 0 — producing no output.

```bash
copilot -p "<prompt>" --allow-all-tools --allow-all-paths --model gpt-5.6-terra
```

Do NOT use the more restrictive `--allow-tool write` — it only authorizes file writes,
but copilot also needs read, glob, and shell permissions.

## Project Structure

- `Makefile` - Deployment targets for nuctu (deploy, status, logs, restart)
- `docs/PRD.md` - Product Requirements Document with design decisions and rationale
- `docs/MISC.md` - Miscellaneous notes and ideas
- `service-configs/` - systemd and launchd configurations for running as a system service
- `tests/` - Test suite (run with `uv run --with pytest --with pyyaml pytest tests/`)
- `skills/` - Copilot CLI skills (e.g., workiq-notes)

### Processing Pipeline (run_summarization.py)

The `process_inbox()` function runs a 3-step pipeline:

1. **Filter** — `is_transcript_worth_processing()` rejects junk transcripts using heuristics (body < 200 chars or duration < 60s). No LLM call needed.
2. **Split** — `detect_multi_meeting()` uses a cheap LLM (haiku) to check for back-to-back meetings. If found, `split_transcript()` creates separate part files with interpolated timestamps.
3. **Process** — Each surviving transcript goes through summarization, calendar enrichment, and file organization.

Key constants: `MIN_BODY_LENGTH=200`, `MIN_DURATION_SECONDS=60`, `MULTI_MEETING_MIN_BODY=5000`.

JSON extraction from LLM output uses `_extract_json_object()` (brace-depth counting, not regex).

## Troubleshooting

- Use `--debug` flag with run_summarization.py for verbose output when diagnosing issues
- Exit codes: 0 = success, 1 = failures occurred, 2 = no files to process
- Check nuctu logs: `make logs` or `ssh edd@nuctu 'sudo journalctl -t meetingnotes-webhook --since "1 hour ago" --no-pager'`
- If copilot produces no output despite exit code 0, check that `--allow-all-tools --allow-all-paths` flags are present
- Processing uses per-file timeouts (600s) and per-file git commits — one failure won't block others
- The daemon deduplicates concurrent processing requests (skips if already processing)
