# Workflow Templates

This directory contains GitHub Actions workflow templates for different deployment scenarios.

## Available Templates

### `process-transcripts-data-repo.yml`

**Use in:** Data repository (meeting-notes)

This is the recommended workflow for the separated repository architecture. Place this in `.github/workflows/` of your data repository.

**What it does:**
- Triggers when files are added to `inbox/`
- Checks out both data and processor repositories
- Runs the processor scripts against the data repo
- Commits results back to the data repo

**Setup:**
1. Copy to data repo: `.github/workflows/process-transcripts.yml`
2. Ensure `GH_TOKEN` secret is configured with Contents write permission
3. Update repository name if different from `ewilderj/meeting-notes-processor`

## Deployment Options

### Option A: Data Repo Triggers (Recommended)

Workflow lives in the **data repository** and triggers on inbox c# Workflow Templates

This directory contains GitHub Actions workflow tempe
- ✅ Simpler 
This directory conoce
## Available Templates

### `process-transcripts-data-repo.yml`

**Use in:** Data repositorthe **processor repository*
**nd polls data repo periodically.

- ?This is the recommended workflow for th- ❌
**What it does:**
- Triggers when files are added to `inbp

Not implemented yet - would require schedule trigger and data repo checkout.

## Notes

- Temp- Checare **not active** - they must be copi- Runs the processor scripts against the dataview - Commits results back to the dataest with a sample transcript after setup
