#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""
Meeting Notes Processor

Processes transcripts from inbox directory, generates summaries with LLM,
and organizes files into transcripts/ and notes/ directories.

Supports --workspace argument (or WORKSPACE_DIR env var) for running against a separate data repository.
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

# Script directory for finding default prompt
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))


def get_workspace_paths(workspace_dir: str) -> dict:
    """Compute all workspace-relative paths."""
    return {
        'workspace': workspace_dir,
        'inbox': os.path.join(workspace_dir, 'inbox'),
        'transcripts': os.path.join(workspace_dir, 'transcripts'),
        'notes': os.path.join(workspace_dir, 'notes'),
    }


def get_default_prompt_file(workspace_dir: str) -> str:
    """Return the default prompt file path, preferring workspace over script directory."""
    workspace_prompt = os.path.join(workspace_dir, 'prompt.txt')
    if os.path.exists(workspace_prompt):
        return workspace_prompt
    return os.path.join(SCRIPT_DIR, 'prompt.txt')


def load_prompt_template(prompt_file: str | None, workspace_dir: str) -> str:
    """Load the prompt template from a file.
    
    If prompt_file is None, uses get_default_prompt_file() to find the default.
    """
    if prompt_file is None:
        prompt_file = get_default_prompt_file(workspace_dir)
    
    if not os.path.exists(prompt_file):
        print(f"Error: Prompt file not found: {prompt_file}")
        sys.exit(1)
    
    with open(prompt_file, 'r', encoding='utf-8') as f:
        return f.read()


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

def process_transcript(input_file, paths, target='copilot', model=None, prompt_template=None, debug=False):
    """Process a single transcript: summarize, extract slug, and organize files."""
    print(f"\nProcessing: {input_file}")
    
    workspace_dir = paths['workspace']
    
    # Get date from file for temporary naming
    date_str = get_date_from_file(input_file)
    temp_org_filename = f"temp-{date_str}.org"
    
    # Get basename for input file (relative to workspace)
    input_basename = os.path.basename(input_file)
    input_relative = os.path.join('inbox', input_basename)
    
    # Run summarization (files are relative to workspace)
    print(f"  Generating summary...")
    final_prompt = prompt_template.format(input_file=input_relative, output_file=temp_org_filename)

    if target == 'copilot':
        model_name = model if model else 'claude-sonnet-4.5'
        command = [
            'npx', '@github/copilot',
            '-p', final_prompt,
            '--allow-tool', 'write',
            '--model', model_name
        ]
        try:
            if debug:
                print(f"  Running: {' '.join(command[:4])} '<prompt>' {' '.join(command[5:])}")
                print(f"  Working directory: {os.path.abspath(workspace_dir)}")
                print(f"  Prompt length: {len(final_prompt)} chars")
                print(f"  {'='*50}")
                print(f"  COPILOT OUTPUT:")
                print(f"  {'='*50}")
                # Stream output for debugging
                process = subprocess.Popen(
                    command,
                    cwd=workspace_dir,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    bufsize=1
                )
                for line in process.stdout:
                    print(f"  {line}", end='', flush=True)
                process.wait()
                print(f"  {'='*50}")
                print(f"  Exit code: {process.returncode}")
                if process.returncode != 0:
                    return False, None, None
            else:
                result = subprocess.run(command, capture_output=True, text=True, cwd=workspace_dir)
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
            if debug:
                print(f"  Running: {' '.join(command)}")
                print(f"  Working directory: {os.path.abspath(workspace_dir)}")
                print(f"  Prompt length: {len(final_prompt)} chars")
                print(f"  {'='*50}")
                print(f"  GEMINI OUTPUT:")
                print(f"  {'='*50}")
                # Stream output for debugging
                process = subprocess.Popen(
                    command,
                    cwd=workspace_dir,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    bufsize=1
                )
                process.stdin.write(final_prompt)
                process.stdin.close()
                for line in process.stdout:
                    print(f"  {line}", end='', flush=True)
                process.wait()
                print(f"  {'='*50}")
                print(f"  Exit code: {process.returncode}")
                if process.returncode != 0:
                    return False, None, None
            else:
                result = subprocess.run(command, input=final_prompt, capture_output=True, text=True, cwd=workspace_dir)
                if result.returncode != 0:
                    print(f"  Error in summarization: {result.stderr}")
                    return False, None, None
        except Exception as e:
            print(f"  Error running gemini: {e}")
            return False, None, None
            return False, None, None
    
    # Check if org file was created (in workspace)
    temp_org_path = os.path.join(workspace_dir, temp_org_filename)
    if not os.path.exists(temp_org_path):
        print(f"  Error: Expected org file {temp_org_path} was not created")
        return False, None, None
    
    # Extract slug from the generated org file
    print("  Extracting slug from summary...")
    slug = extract_slug_from_org(temp_org_path)
    base_name = f"{date_str}-{slug}"
    print(f"  Using filename base: {base_name}")
    
    # Create final output paths (ensure uniqueness)
    transcript_path = ensure_unique_filename(paths['transcripts'], base_name, 'txt')
    org_path = ensure_unique_filename(paths['notes'], base_name, 'org')
    
    # Move files to their final locations
    shutil.move(temp_org_path, org_path)
    print(f"  Created: {org_path}")
    
    shutil.move(input_file, transcript_path)
    print(f"  Moved transcript to: {transcript_path}")
    
    return True, transcript_path, org_path

def git_commit_changes(inbox_files, transcript_files, org_files, workspace_dir):
    """Perform git operations: remove inbox files, add new files, and commit."""
    try:
        # Convert all file paths to be relative to workspace
        workspace_abs = os.path.abspath(workspace_dir)
        
        def make_relative(filepath):
            """Convert filepath to be relative to workspace."""
            abs_path = os.path.abspath(filepath)
            return os.path.relpath(abs_path, workspace_abs)
        
        # Stage deletions of inbox files (they've already been moved)
        # Use 'git add' to stage the deletions since files are already gone
        inbox_paths = [make_relative(f) for f in inbox_files]
        for rel_path in inbox_paths:
            result = subprocess.run(['git', 'add', rel_path], capture_output=True, text=True, cwd=workspace_dir)
            if result.returncode != 0:
                print(f"  Warning: git add (deletion) failed for {rel_path}: {result.stderr}")
            else:
                print(f"  Git staged deletion: {rel_path}")
        
        # Git add the new transcript and org files
        files_to_add = [make_relative(f) for f in transcript_files + org_files]
        if files_to_add:
            result = subprocess.run(['git', 'add'] + files_to_add, capture_output=True, text=True, cwd=workspace_dir)
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
        result = subprocess.run(['git', 'commit', '-m', commit_msg], capture_output=True, text=True, cwd=workspace_dir)
        if result.returncode != 0:
            print(f"  Error: git commit failed: {result.stderr}")
            return False
        else:
            print(f"  Git committed: {commit_msg}")
            return True
            
    except Exception as e:
        print(f"  Error during git operations: {e}")
        return False

def process_inbox(paths, target='copilot', model=None, use_git=False, prompt_template=None, debug=False):
    """Process all transcript files in the inbox directory."""
    inbox_dir = paths['inbox']
    
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
    os.makedirs(paths['transcripts'], exist_ok=True)
    os.makedirs(paths['notes'], exist_ok=True)
    
    successful = 0
    failed = 0
    processed_inbox_files = []
    processed_transcript_files = []
    processed_org_files = []
    
    for transcript_file in transcript_files:
        try:
            result = process_transcript(transcript_file, paths, target, model, prompt_template, debug)
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
        if git_commit_changes(processed_inbox_files, processed_transcript_files, processed_org_files, paths['workspace']):
            print("Git operations completed successfully")
        else:
            print("Warning: Git operations failed")
    
    return successful, failed

def run_summarization():
    parser = argparse.ArgumentParser(
        description='Process meeting transcripts from inbox directory.',
        epilog='Processes all .txt and .md files in inbox/, generates summaries, and organizes files.'
    )
    parser.add_argument('--workspace', default=None,
                        help='Path to data repository. Default: WORKSPACE_DIR env var, or current directory.')
    parser.add_argument('--target', choices=['copilot', 'gemini'], default='copilot', 
                        help='The CLI tool to use (copilot or gemini). Default is copilot.')
    parser.add_argument('--model', help='The model to use. Defaults to claude-sonnet-4.5 for copilot and gemini-3-flash-preview for gemini.')
    parser.add_argument('--prompt', default=None,
                        help='Path to the prompt template file. Default: prompt.txt in workspace, or script directory as fallback.')
    parser.add_argument('--git', action='store_true',
                        help='Perform git operations: rm processed inbox files, add new files, and commit. For use in automation/CI.')
    parser.add_argument('--debug', action='store_true',
                        help='Stream AI output to terminal for debugging. Useful when processing hangs.')
    
    args = parser.parse_args()
    
    # Determine workspace directory: CLI arg > env var > current directory
    workspace_dir = args.workspace or os.getenv('WORKSPACE_DIR', '.')
    paths = get_workspace_paths(workspace_dir)
    
    # Load prompt template
    prompt_template = load_prompt_template(args.prompt, workspace_dir)
    
    # Ensure required directories exist
    for dir_path in [paths['inbox'], paths['transcripts'], paths['notes']]:
        if not os.path.exists(dir_path):
            os.makedirs(dir_path)
            print(f"Created {dir_path}/ directory")
    
    # Process all transcripts in inbox
    process_inbox(paths, target=args.target, model=args.model, use_git=args.git, prompt_template=prompt_template, debug=args.debug)

if __name__ == "__main__":
    run_summarization()
