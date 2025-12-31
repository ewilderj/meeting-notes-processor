# Product Requirements Document: Meeting Notes Knowledge Base

## Overview

A system for automatically processing meeting transcripts into a searchable, organized knowledge base using org-mode format. The system ingests transcripts from various sources (MacWhisper, Zoom, Teams, etc.), generates meaningful summaries, and maintains a structured archive for long-term reference.

## Problem Statement

Meeting transcripts accumulate rapidly but lack organization and context, making them difficult to search and reference. Manual processing is time-consuming and inconsistent. This system automates the organization and summarization of meeting transcripts, creating a persistent knowledge base that preserves institutional memory.

## Goals

1. **Automation**: Automatically detect, process, and organize new meeting transcripts
2. **Discoverability**: Generate meaningful file names that aid in searching and browsing
3. **Summarization**: Extract key insights from transcripts using AI summarization
4. **Standardization**: Maintain consistent org-mode format for easy integration with knowledge management tools
5. **Preservation**: Create a durable archive of both original transcripts and summaries

## User Stories

- As a meeting participant, I want transcripts automatically processed so I don't have to manually organize them
- As a knowledge worker, I want meaningful file names so I can quickly locate relevant meetings
- As a team member, I want summarized meeting notes so I can review key points without reading full transcripts
- As a researcher, I want org-mode format so I can integrate meeting notes with my existing knowledge management system

## Functional Requirements

### 1. Transcript Ingestion

- **FR-1.1**: System processes all transcript files in the `inbox/` subdirectory when run
- **FR-1.2**: System accepts transcript formats: .txt and .md
- **FR-1.3**: System processes transcripts from MacWhisper, Zoom, Teams, and other sources

### 2. Content Analysis

- **FR-2.1**: System extracts key topics from transcript content to generate meaningful slugs
- **FR-2.2**: System determines meeting date from file timestamp
- **FR-2.3**: System generates AI-powered summaries using either Gemini or Copilot CLI (user selects at runtime)
- **FR-2.4**: `run_summarization.py` serves as the main program orchestrating slug generation and summarization

### 3. File Management

- **FR-3.1**: System moves processed transcripts from `inbox/` to `transcripts/`
- **FR-3.2**: System renames original transcripts to format: `YYYYMMDD-slug.txt`
- **FR-3.3**: System creates summary files in `notes/` directory with format: `YYYYMMDD-slug.org`
- **FR-3.4**: System ensures slug uniqueness to prevent filename collisions

### 4. Output Format

- **FR-4.1**: Summary files use org-mode format (.org extension)
- **FR-4.2**: Summary includes metadata header with date, source file, processing timestamp
- **FR-4.3**: Summary contains structured sections: participants, key topics, action items, decisions
- **FR-4.4**: Summary links back to original transcript file

### 5. Always-On Agent/Daemon

- **FR-5.1**: System can run as a long-lived daemon/service (always-on) with clear start/stop/restart behavior
- **FR-5.2**: Daemon syncs the configured data repository on startup and before processing new inbound work (e.g., `git pull --ff-only`)
- **FR-5.3**: Daemon can optionally trigger a GitHub Actions `workflow_dispatch` in the data repository when new transcripts are added (configurable)
- **FR-5.4**: Daemon can optionally run a user-configured command hook when new data is detected after a sync (configurable)
- **FR-5.5**: Daemon logs sync/dispatch/hook outcomes clearly and fails safely (no partial writes or silent drops)
- **FR-5.6**: If the configured data-repo working directory does not contain a git checkout yet, daemon can bootstrap it by cloning the data repo before syncing/processing

## Directory Structure

```
meeting-notes/
├── inbox/               # Drop zone for new transcripts
├── transcripts/         # Processed original transcripts
│   └── YYYYMMDD-slug.txt
├── notes/              # Generated org-mode summaries
│   └── YYYYMMDD-slug.org
├── run_summarization.py # Main program
├── package.json         # Node.js dependencies (Gemini CLI, Copilot)
└── PRD.md              # This document
```

## File Naming Convention

### Format: `YYYYMMDD-slug.{txt|org}`

- **YYYYMMDD**: Date derived from original file timestamp
- **slug**: 2-5 word descriptor generated from transcript content
  - Lowercase
  - Hyphen-separated
  - Derived from key topics/meeting subject
  - Examples: `quarterly-planning`, `product-roadmap-review`, `team-standup`

### Examples

- Original: `Meeting Recording 2025-12-29.txt` in inbox/
- Processed transcript: `transcripts/20251229-quarterly-planning.txt`
- Summary: `notes/20251229-quarterly-planning.org`

## Technical Requirements

### Dependencies

- **Python 3.x**: Core processing logic
- **@google/gemini-cli**: AI summarization option
- **@github/copilot**: Alternative AI summarization option
- **Node.js**: Required for CLI tools

### LLM Selection

The system supports two LLM backends that can be selected at runtime:
- **Gemini CLI** (`@google/gemini-cli`): Google's Gemini model
- **Copilot CLI** (`@github/copilot`): GitHub Copilot

User selects which LLM to use via command-line argument or configuration, providing flexibility based on availability, preference, or cost considerations.

### Processing Workflow

1. **Discovery**: Scan `inbox/` directory for transcript files (.txt, .md)
2. **Analysis**: 
   - Extract file creation/modification timestamp → YYYYMMDD
   - Analyze content with selected LLM to generate meaningful slug
   - Generate AI summary using selected LLM
3. **Organization**:
   - Move original transcript to `transcripts/YYYYMMDD-slug.txt`
   - Create summary file at `notes/YYYYMMDD-slug.org`
4. **Validation**: Ensure files were created successfully, handle errors
5. **Completion**: Report processing results

### Implementation Phases

#### Phase 1: Core Processing (Complete)
- ✅ Basic summarization in `run_summarization.py`
- ✅ Refactor `run_summarization.py` as main program
- ✅ Implement slug generation from transcript content
- ✅ Add LLM backend selection (Gemini or Copilot)
- ✅ Implement file renaming and organization
- ✅ Create org-mode formatted output
- ✅ Create separate `notes/` directory structure

#### Phase 2: Automation (Complete)
- ✅ Implement batch processing of inbox directory
- ✅ Add error handling and retry logic
- ✅ Create GitHub Actions workflow for automated processing
- ✅ Implement `--git` mode for automated git operations
- ✅ Configure workflow with proper permissions and tokens

#### Phase 3: Webhook Integration (Complete)
- ✅ Create local webhook daemon to receive MacWhisper transcripts
- ✅ Implement webhook endpoint with JSON payload parsing
- ✅ Add automated git commit and push from daemon
- ✅ Handle concurrent processing and git conflicts
- ✅ Add daemon logging and configuration file

#### Phase 4: Repository Separation (Complete)
- ✅ Separate code repository from data repository
- ✅ Configure webhook daemon to work with separate data repo
- ✅ Update GitHub Actions to work across repositories
- ✅ Document deployment and configuration for separated architecture
- ✅ Add remote repository configuration support
- ✅ Fix file path handling with `cwd` parameter in subprocess calls
- ✅ Fix git operations to use relative paths within data repository
- ✅ Unified token naming to `GH_TOKEN` for consistency
- ✅ Created example transcripts and test tooling

#### Phase 5: Always-On Agent/Daemon (Planned)
- ⏳ Run continuously as a long-lived service (daemonization guidance)
- ⏳ Keep the local data repo current via safe `git pull` semantics
- ⏳ Optionally trigger GitHub Actions via `workflow_dispatch` when configured
- ⏳ Optionally run a local command hook when new data arrives
- ⏳ Consider renaming `webhook_daemon.py` to reflect broader responsibilities

#### Phase 6: Enhancement (Future)
- ⏳ Add duplicate detection
- ⏳ Add search and indexing capabilities
- ⏳ Support additional LLM backends
- ⏳ Implement semantic search using vector embeddings
- ⏳ Add web interface for browsing processed notes

## Non-Functional Requirements

- **Performance**: Process transcript within 30 seconds per file
- **Reliability**: Handle errors gracefully without data loss
- **Scalability**: Support batch processing of 100+ transcripts
- **Maintainability**: Clear logging and error messages for troubleshooting
- **Security**: Handle sensitive meeting content securely, no external data leakage
- **Flexibility**: Easy switching between LLM backends based on user preference

## Success Metrics

- 100% of inbox transcripts automatically processed
- Average slug quality rated 4/5 or higher (meaningful and descriptive)
- Summary generation time < 30 seconds per transcript
- Zero data loss incidents
- Org-mode files successfully parse in Emacs/Org tools

## Future Considerations

- Web interface for browsing and searching processed meetings
- Integration with calendar systems to auto-populate meeting metadata
- Multi-language transcript support
- Advanced search using vector embeddings
- Automatic tagging and categorization
- Cross-referencing between related meetings
- Export to other formats (Markdown, HTML, PDF)

## Phase 3: MacWhisper Webhook Integration

### Overview

MacWhisper can send webhooks upon transcription completion. A local webhook daemon receives these webhooks and automatically commits transcripts to the inbox, triggering the existing GitHub Actions workflow for processing.

### Webhook Payload

MacWhisper sends simple JSON:
```json
{
  "title": "Meeting with John",
  "transcript": "Full transcript text..."
}
```

### Architecture: Webhook → Inbox → GitHub Actions

**Workflow:**
1. Local daemon receives webhook from MacWhisper
2. Daemon writes transcript to `inbox/{timestamp}-{sanitized-title}.txt`
3. Daemon commits and pushes to GitHub
4. GitHub Actions detects inbox commit and processes automatically (existing workflow)

**Advantages:**
- Separation of concerns: daemon only handles webhook → file → commit
- GitHub Actions handles all processing (centralized, logged)
- Processing happens in cloud (works when computer is off for subsequent transcripts)
- Git operations are isolated to GitHub Actions for LLM processing
- Daemon remains lightweight and reliable

### Technical Specification

#### Webhook Daemon (`webhook_daemon.py`)

**Responsibilities:**
- Receive HTTP POST from MacWhisper
- Validate payload
- Sanitize title for filename
- Write transcript to inbox with timestamp
- Git add, commit, and push
- Return success/error to MacWhisper

**API Endpoint:**
```
POST http://localhost:8080/webhook
Content-Type: application/json

{
  "title": "string",
  "transcript": "string"
}
```

**Response:**
```json
{
  "status": "success",
  "filename": "20251230-142305-meeting-with-john.txt",
  "message": "Transcript queued for processing"
}
```

**Requirements:**
- Lightweight HTTP server (Flask/FastAPI)
- Minimal dependencies
- Timestamp-based filenames to prevent collisions
- Safe filename sanitization
- Git commit and push after writing file
- Logging for debugging
- Error handling and meaningful responses

**Filename Format:**
```
inbox/{YYYYMMDD-HHMMSS}-{sanitized-title}.txt
```

#### Configuration

```yaml
# config.yaml (optional)
daemon:
  host: 0.0.0.0
  port: 8080
  
git:
  auto_push: true
  commit_message_template: "Add transcript: {title}"
```

**Phase 5 config sketch (proposed additions for always-on behavior):**

```yaml
# config.yaml (proposed additions)

# Keep the local data repo up to date before writing/committing new work.
sync:
  enabled: true
  on_startup: true
  before_accepting_webhooks: true
  checkout_if_missing:
    enabled: true
    repo_url: "https://github.com/OWNER/REPO.git" # data repo clone URL
    branch: "main"
  pull:
    remote: origin
    branch: main
    ff_only: true

# Optionally trigger a GitHub Actions workflow when new transcripts land.
github:
  workflow_dispatch:
    enabled: false
    repo: "OWNER/REPO"              # e.g. "ewilderj/meeting-notes"
    workflow: "process-transcripts.yml" # workflow file name or workflow id
    ref: "main"
    inputs: {}

# Optionally run a command when a sync brings in new commits.
hooks:
  on_new_commits:
    enabled: false
    command: "uv run run_summarization.py --git"
    working_directory: "."          # typically the data repo root
```

### Implementation Tasks (Phase 3)

1. **Basic Webhook Daemon**
   - Create Flask/FastAPI HTTP server
   - Accept POST requests with JSON payloads
   - Validate required fields (title, transcript)
   - Return appropriate HTTP status codes

2. **File Handling**
   - Sanitize title for safe filenames
   - Add timestamp to prevent collisions
   - Write transcript to inbox directory
   - Handle file write errors gracefully

3. **Git Integration**
   - Auto-commit after receiving webhook
   - Auto-push to trigger GitHub Actions
   - Handle git conflicts and push failures
   - Retry logic for transient failures

4. **Reliability Features**
   - Request logging and audit trail
   - Health check endpoint (GET /)
   - Graceful error responses
   - Optional authentication via webhook secret

### Open Questions (Phase 3)

1. Should webhook endpoint require authentication/secret token?
2. How to handle git push failures (queue? retry? notify user)?
3. Should daemon validate transcript content length/format before accepting?
4. Should daemon support webhooks from other sources (Zoom, Teams)?
5. Daemon startup: systemd service, launchd, or manual start?



## Risks and Mitigation

| Risk | Impact | Mitigation |
|------|--------|------------|
| Slug generation produces non-unique names | Medium | Append counter suffix for duplicates |
| AI summarization API failures | High | Implement retry logic and fallback options |
| Large transcript files cause timeout | Medium | Implement chunking for large files |
| Incorrect date extraction | Low | Allow manual date override mechanism |
| Sensitive information in transcripts | High | Document security best practices, consider local LLM options |

## Open Questions

1. What should happen if a transcript lacks clear topics for slug generation?
2. Should there be a review/approval step before moving files, or fully automated?
3. How long should processed files remain in transcripts/ and notes/ before archiving?
4. Should the system support editing/reprocessing of already-processed transcripts?
5. What should be the default LLM if none is specified?
6. Should the system support processing subdirectories within inbox/?

---

## Phase 4: Repository Separation - Implementation Notes

### Status: COMPLETE ✅

The repository separation has been successfully implemented and tested. The system now supports both same-repository and separated-repository architectures, with separated being the recommended approach.

### What Was Built

**Two Repository Architecture:**
1. **Code Repository** (`meeting-notes-processor`) - This repository
   - Processing scripts with `WORKSPACE_DIR` environment variable support
   - Webhook daemon with configurable paths via `config.yaml`
   - GitHub Actions workflow template for data repositories
   - Example transcripts and testing utilities
   
2. **Data Repository** (user-created, e.g., `my-meeting-notes`)
   - `inbox/` - Drop zone for new transcripts
   - `transcripts/` - Processed original transcripts
   - `notes/` - AI-generated org-mode summaries
   - `.github/workflows/` - Optional automation

### Key Technical Achievements

**1. Path Handling with `cwd` Parameter**
- All subprocess calls (Copilot CLI, Gemini CLI, git commands) now use `cwd=WORKSPACE_DIR`
- This allows the processor to run from one directory while operating on files in another
- Solves the "outside repository" git errors that occurred with relative paths

**2. Relative Path Conversion for Git**
- Git operations convert all paths to be relative to `WORKSPACE_DIR` before execution
- Uses `os.path.relpath()` to compute paths from within the data repository
- Git properly detects file moves/renames (shows as R100 in commit history)

**3. Unified Token Management**
- Changed from `GITHUB_TOKEN` to `GH_TOKEN` throughout codebase
- Single token used for both webhook daemon (local) and GitHub Actions (cloud)
- Fine-grained Personal Access Token with:
  - Contents: Read and write
  - Copilot Requests (for Copilot CLI authentication)

**4. Example Transcripts**
Created three realistic example transcripts in `examples/`:
- `q1-planning-sarah.txt` - Business planning meeting
- `dunder-mifflin-sales.txt` - Sales strategy (The Office characters)
- `mad-men-heinz.txt` - Advertising brainstorm (Mad Men characters)

**5. Testing Utility**
- `test_webhook.py` - Sends example transcripts to webhook daemon
- Uses PEP 723 inline script metadata for dependencies
- Simplifies testing and demonstration

### How It Works

#### Local Development with Separated Repos

```bash
# Directory structure
~/projects/
├── meeting-notes-processor/  (code repo)
└── my-meeting-notes/          (data repo)

# Configure processor
cd meeting-notes-processor
# Edit config.yaml to point to ../my-meeting-notes

# Process transcripts
WORKSPACE_DIR=../my-meeting-notes uv run run_summarization.py

# Run webhook daemon
GH_TOKEN=xxx uv run webhook_daemon.py
```

#### GitHub Actions

The data repository contains a workflow that:
1. Checks out both data repo and processor repo
2. Checks if inbox has files (skips if empty)
3. Runs processor with `WORKSPACE_DIR=../meeting-notes`
4. Processor commits results directly to data repo (using `--git` flag)

**Key improvement:** The processor handles git operations internally, eliminating the need for a separate commit step in the workflow.

### Configuration File Changes

**config.yaml in processor repo:**
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
  repository_url: "github.com/USERNAME/my-meeting-notes.git"
  commit_message_template: "Add transcript: {title}"
```

### Workflow Template

Created `workflows-templates/process-transcripts-data-repo.yml` with:
- Early inbox check to skip processing if empty
- Conditional execution of all steps
- Proper git configuration before processing
- `--git` flag for automated commits

### Documentation Updates

- **README.md**: Completely rewritten to prioritize separated repository setup
- **AGENTS.md**: Updated with `WORKSPACE_DIR` usage and `GH_TOKEN` examples
- **workflows-templates/README.md**: Instructions for using workflow templates

### Testing & Validation

Tested scenarios:
✅ Processing with `WORKSPACE_DIR` set (separated repos)
✅ Processing without `WORKSPACE_DIR` (same repo)
✅ Webhook daemon receiving and committing transcripts
✅ Git operations with proper file renames and deletions
✅ GitHub Actions workflow with empty and non-empty inbox
✅ Example transcripts through webhook test script

### Resolved Issues

**Git "outside repository" errors:**
- Fixed by using `cwd` parameter in subprocess calls
- Fixed by converting file paths to relative paths before git operations

**Inbox file deletion in git:**
- Changed from `git rm` to `git add` to stage deletions
- Git automatically detects moved files as renames

**Token confusion:**
- Unified to `GH_TOKEN` for both local and Actions use
- Documented fine-grained PAT requirements clearly

### Deployment Recommendation

**Option A (Implemented):** Data repo triggers processor via GitHub Actions
- Data repo's workflow checks out processor code
- Processor runs with `WORKSPACE_DIR` pointing to data repo
- Processor commits results back to data repo

This approach is preferred because:
- Natural trigger: new files in inbox
- Simple mental model
- All automation lives in data repo
- Processor repo is purely code (no workflows)

### Migration from Same-Repository

For users with existing same-repository setups:
1. Create new data repository with `inbox/`, `transcripts/`, `notes/`
2. Move data files from old repo to new data repo
3. Clone processor repo separately
4. Update `config.yaml` in processor repo
5. Test with `WORKSPACE_DIR` environment variable
6. Set up GitHub Actions workflow in data repo

The processor supports both models simultaneously - no code changes needed.

### Known Limitations

1. **Relative paths in config.yaml**: Must be relative to processor repo directory
2. **Manual token setup**: Users must create fine-grained PAT with correct permissions
3. **Two-repo cloning**: Initial setup requires cloning both repositories
4. **Path coordination**: Local development needs both repos in expected relative positions

### Future Enhancements (Phase 5)

Potential improvements:
- Config validation tool to check paths and permissions
- Setup script to bootstrap data repository structure
- Docker container with both repos configured
- Remote data repository support (not just local paths)
- Multiple data repositories per processor (team vs personal)

---
