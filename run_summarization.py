#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""
Meeting Notes Processor

Processes transcripts from inbox directory, generates summaries with LLM,
and organizes files into transcripts/ and notes/ directories.

Supports WORKSPACE_DIR environment variable for running against a separate data repository.
"""

import subprocess
import os
import argparse
import sys
import glob
from datetime import datetime
from pathlib import Path
import shutil
import re

# Support configurable workspace directory for separated repo architecture
WORKSPACE_DIR = os.getenv('WORKSPACE_DIR', '.')
INBOX_DIR = os.path.join(WORKSPACE_DIR, 'inbox')
TRANSCRIPTS_DIR = os.path.join(WORKSPACE_DIR, 'transcripts')
NOTES_DIR = os.path.join(WORKSPACE_DIR, 'notes')

PROMPT_TEMPLATE = """Summarize the transcript in the input file. Include:

- A single sentence TL;DR up top
- A list of agreed-upon actions, with who will do them
- Any open questions left unresolved
- A brief summary of the discussion
- org-mode property drawer for :PARTICIPANTS:, :TOPIC:, and :SLUG:
- the tags :note:transcribed:

The :SLUG: property should be a 2-5 word hyphenated slug that describes the
main topic (e.g., "quarterly-planning", "ai-coaching-discussion"). Use only
lowercase letters, numbers, and hyphens. This will be used for the filename.

Format this in org format, as used with org-mode in emacs.

The user's name is Edd, which you may hear as Ed. His full name is
Edd Wilder-James.

Do not vary from the org-mode formatting. You can use emoji but do not
create invalid org files. Use the hyphen - for bulleted lists, not
asterisk. use `- [ ]` to denote actions. Here's an example format:

<example>
** Meeting with April :note:transcribed:
[2025-12-02 Tue 22:32]
:PROPERTIES:
:PARTICIPANTS: April, Edd
:TOPIC: A test meeting.
:SLUG: test-meeting-april
:END:

TL;DR: Test notes about what happened.

*** Actions

- [ ] do the first thing
- [ ] do the second thing

*** Open questions

- first open question
- next open question

*** Summary

Brief summary of the discussion.
</example>

For the meeting timestamp just use the first timestamp in the meeting
transcription if there is a date and time specified. If there is no date,
use the timestamp of the input file.
Do not include citations to the transcript anywhere.
Ensure each line is wrapped to max 80 columns.
Input file is {input_file}.
Write the resulting org content to {output_file}"""

def extract_slug_from_org(org_file_path):
    """Extract the slug from the org file's property drawer."""
    try:
        with open(org_file_path, 'r', encoding='utf-8') as f:
            content = f.read()
        
        # Look for :SLUG: property in the property drawer
        match = re.search(r':SLUG:\s+([a-z0-9-]+)', content, re.IGNORECASE)
        if match:
            slug = match.group(1).lower().strip()
            # Ensure it's valid and reasonable length
            if slug and len(slug) <= 50 and re.match(r'^[a-z0-9-]+$', slug):
                return slug
        
        # Fallback to 'meeting' if no valid slug found
        print("  Warning: No valid slug found in org file, using 'meeting'")
        return 'meeting'
    except Exception as e:
        print(f"  Error extracting slug: {e}")
        return 'meeting'

def get_date_from_file(filepath):
    """Extract date from file modification time."""
    timestamp = os.path.getmtime(filepath)
    return datetime.fromtimestamp(timestamp).strftime('%Y%m%d')

def ensure_unique_filename(directory, base_name, extension):
    """Ensure filename is unique by appending counter if necessary."""
    filepath = os.path.join(directory, f"{base_name}.{extension}")
    if not os.path.exists(filepath):
        return filepath
    
    counter = 1
    while True:
        filepath = os.path.join(directory, f"{base_name}-{counter}.{extension}")
        if not os.path.exists(filepath):
            return filepath
        counter += 1

def process_transcript(input_file, target='copilot', model=None):
    """Process a single transcript: summarize, extract slug, and organize files."""
    print(f"\nProcessing: {input_file}")
    
    # Get date from file for temporary naming
    date_str = get_date_from_file(input_file)
    temp_org_filename = f"temp-{date_str}.org"
    
    # Get basename for input file (relative to WORKSPACE_DIR)
    input_basename = os.path.basename(input_file)
    input_relative = os.path.join('inbox', input_basename)
    
    # Run summarization (files are relative to WORKSPACE_DIR)
    print(f"  Generating summary...")
    final_prompt = PROMPT_TEMPLATE.format(input_file=input_relative, output_file=temp_org_filename)

    if target == 'copilot':
        model_name = model if model else 'claude-sonnet-4.5'
        command = [
            'npx', '@github/copilot',
            '-p', final_prompt,
            '--allow-tool', 'write',
            '--model', model_name
        ]
        try:
            result = subprocess.run(command, capture_output=True, text=True, cwd=WORKSPACE_DIR)
            if result.returncode != 0:
                print(f"  Error in summarization: {result.stderr}")
                return False, None, None
        except Exception as e:
            print(f"  Error running copilot: {e}")
            return False, None, None

    elif target == 'gemini':
        model_name = model if model else 'gemini-3-flash-preview'
        command = [
            'npx', '@google/gemini-cli',
            '--approval-mode', 'auto_edit',
            '--model', model_name
        ]
        try:
            result = subprocess.run(command, input=final_prompt, capture_output=True, text=True, cwd=WORKSPACE_DIR)
            if result.returncode != 0:
                print(f"  Error in summarization: {result.stderr}")
                return False, None, None
        except Exception as e:
            print(f"  Error running gemini: {e}")
            return False, None, None
    
    # Check if org file was created (in WORKSPACE_DIR)
    temp_org_path = os.path.join(WORKSPACE_DIR, temp_org_filename)
    if not os.path.exists(temp_org_path):
        print(f"  Error: Expected org file {temp_org_path} was not created")
        return False, None, None
    
    # Extract slug from the generated org file
    print("  Extracting slug from summary...")
    slug = extract_slug_from_org(temp_org_path)
    base_name = f"{date_str}-{slug}"
    print(f"  Using filename base: {base_name}")
    
    # Create final output paths (ensure uniqueness)
    transcript_path = ensure_unique_filename(TRANSCRIPTS_DIR, base_name, 'txt')
    org_path = ensure_unique_filename(NOTES_DIR, base_name, 'org')
    
    # Move files to their final locations
    shutil.move(temp_org_path, org_path)
    print(f"  Created: {org_path}")
    
    shutil.move(input_file, transcript_path)
    print(f"  Moved transcript to: {transcript_path}")
    
    return True, transcript_path, org_path

def git_commit_changes(inbox_files, transcript_files, org_files):
    """Perform git operations: remove inbox files, add new files, and commit."""
    try:
        # Convert all file paths to be relative to WORKSPACE_DIR
        workspace_abs = os.path.abspath(WORKSPACE_DIR)
        
        def make_relative(filepath):
            """Convert filepath to be relative to WORKSPACE_DIR."""
            abs_path = os.path.abspath(filepath)
            return os.path.relpath(abs_path, workspace_abs)
        
        # Stage deletions of inbox files (they've already been moved)
        # Use 'git add' to stage the deletions since files are already gone
        inbox_paths = [make_relative(f) for f in inbox_files]
        for rel_path in inbox_paths:
            result = subprocess.run(['git', 'add', rel_path], capture_output=True, text=True, cwd=WORKSPACE_DIR)
            if result.returncode != 0:
                print(f"  Warning: git add (deletion) failed for {rel_path}: {result.stderr}")
            else:
                print(f"  Git staged deletion: {rel_path}")
        
        # Git add the new transcript and org files
        files_to_add = [make_relative(f) for f in transcript_files + org_files]
        if files_to_add:
            result = subprocess.run(['git', 'add'] + files_to_add, capture_output=True, text=True, cwd=WORKSPACE_DIR)
            if result.returncode != 0:
                print(f"  Error: git add failed: {result.stderr}")
                return False
            else:
                for f in files_to_add:
                    print(f"  Git added: {f}")
        
        # Create commit message
        if len(transcript_files) == 1:
            # Single file - use its basename in message
            basename = os.path.basename(transcript_files[0])
            commit_msg = f"Process transcript: {basename}"
        else:
            # Multiple files
            commit_msg = f"Process {len(transcript_files)} transcripts"
        
        # Commit the changes
        result = subprocess.run(['git', 'commit', '-m', commit_msg], capture_output=True, text=True, cwd=WORKSPACE_DIR)
        if result.returncode != 0:
            print(f"  Error: git commit failed: {result.stderr}")
            return False
        else:
            print(f"  Git committed: {commit_msg}")
            return True
            
    except Exception as e:
        print(f"  Error during git operations: {e}")
        return False

def process_inbox(target='copilot', model=None, use_git=False):
    """Process all transcript files in the inbox directory."""
    inbox_dir = INBOX_DIR
    
    if not os.path.exists(inbox_dir):
        print(f"Error: {inbox_dir} directory not found.")
        return
    
    # Find all .txt and .md files in inbox
    transcript_files = []
    for ext in ['*.txt', '*.md']:
        transcript_files.extend(glob.glob(os.path.join(inbox_dir, ext)))
    
    if not transcript_files:
        print(f"No transcript files found in {inbox_dir}/")
        return
    
    print(f"Found {len(transcript_files)} transcript(s) to process")
    
    # Ensure output directories exist
    os.makedirs(TRANSCRIPTS_DIR, exist_ok=True)
    os.makedirs(NOTES_DIR, exist_ok=True)
    
    successful = 0
    failed = 0
    processed_inbox_files = []
    processed_transcript_files = []
    processed_org_files = []
    
    for transcript_file in transcript_files:
        try:
            result = process_transcript(transcript_file, target, model)
            if result[0]:  # Success
                successful += 1
                processed_inbox_files.append(transcript_file)
                processed_transcript_files.append(result[1])
                processed_org_files.append(result[2])
            else:
                failed += 1
        except Exception as e:
            print(f"Error processing {transcript_file}: {e}")
            failed += 1
    
    print(f"\n{'='*60}")
    print(f"Processing complete: {successful} successful, {failed} failed")
    print(f"{'='*60}")
    
    # Perform git operations if requested and there were successful processes
    if use_git and successful > 0:
        print(f"\nPerforming git operations...")
        if git_commit_changes(processed_inbox_files, processed_transcript_files, processed_org_files):
            print("Git operations completed successfully")
        else:
            print("Warning: Git operations failed")
    
    return successful, failed

def run_summarization():
    parser = argparse.ArgumentParser(
        description='Process meeting transcripts from inbox directory.',
        epilog='Processes all .txt and .md files in inbox/, generates summaries, and organizes files.'
    )
    parser.add_argument('--target', choices=['copilot', 'gemini'], default='copilot', 
                        help='The CLI tool to use (copilot or gemini). Default is copilot.')
    parser.add_argument('--model', help='The model to use. Defaults to claude-sonnet-4.5 for copilot and gemini-3-flash-preview for gemini.')
    parser.add_argument('--git', action='store_true',
                        help='Perform git operations: rm processed inbox files, add new files, and commit. For use in automation/CI.')
    
    args = parser.parse_args()
    
    # Ensure required directories exist
    for directory in ['inbox', 'transcripts', 'notes']:
        if not os.path.exists(directory):
            os.makedirs(directory)
            print(f"Created {directory}/ directory")
    
    # Process all transcripts in inbox
    process_inbox(target=args.target, model=args.model, use_git=args.git)

if __name__ == "__main__":
    run_summarization()
