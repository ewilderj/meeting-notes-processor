#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "pyyaml>=6.0",
# ]
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
import json
import time as _time
import uuid
import select
from datetime import datetime
from pathlib import Path
import shutil
import re

# Script directory for finding default prompt
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

# Copilot executable path - override with COPILOT_PATH env var for systemd/non-PATH contexts
COPILOT_PATH = os.environ.get('COPILOT_PATH', 'copilot')


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


def format_calendar_for_prompt(calendar_entries: list[dict], meeting_date: str) -> str:
    """Format calendar entries for inclusion in the summarization prompt."""
    if not calendar_entries:
        return "No calendar entries found for this date."
    
    lines = []
    for i, e in enumerate(calendar_entries, 1):
        time_str = f"{e['start_time']}-{e['end_time']}" if e['start_time'] else "all-day"
        participants = ', '.join(e['participants']) if e['participants'] else 'unknown'
        lines.append(f"{i}. [{time_str}] {e['title']}")
        lines.append(f"   Participants: {participants}")
        if e['meeting_links']:
            lines.append(f"   Meeting link: {e['meeting_links'][0]}")
        lines.append("")
    
    return '\n'.join(lines)


def build_calendar_aware_prompt(base_prompt: str, calendar_text: str, meeting_date: str, notes_context: str = "") -> str:
    """Build a combined prompt that includes calendar context for single-pass processing."""
    
    calendar_instructions = f"""
## CALENDAR CONTEXT FOR {meeting_date}

You have access to the user's calendar for this date. Use this to:
1. IDENTIFY THE CORRECT PARTICIPANTS - transcription often mishears names
2. Match the meeting to a calendar entry if possible
3. Use the calendar to CORRECT speaker misidentification

{calendar_text}

{notes_context}

## CRITICAL: Participant Identification Strategy

The transcript speaker labels are OFTEN WRONG due to transcription errors. Use this logic:

- Generic `[S]` markers only mean "speaker changed here" and do not identify anyone.
- Explicit labels like `[speaker:Edd]` are machine-generated identity hints and are much
  more reliable than ordinary transcript names or raw ASR guesses.
- Calendar participants remain authoritative for the full participant list and for any
  speakers who do not have an explicit `[speaker:Name]` label.

### Step 1: Cross-reference speakers with calendar
- Look at who speaks in the transcript
- Compare with calendar entries for this date/time
- Calendar participant names are AUTHORITATIVE - trust them over transcript labels

### Step 2: Common transcription errors to watch for
- "Kim" is often a mishearing of other names (Thabani, etc.)
- Names may be phonetically similar but wrong
- If transcript says "Kim" but calendar shows "Thabani 1:1" at that time, the speaker is Thabani

### Step 3: Handling 1:1 meetings
- Calendar format for 1:1s: "username / ewilderj 1:1" (e.g., "thabani11 / ewilderj 1:1")
- The username maps to a person (thabani11 = Thabani)
- If the transcript has 2 speakers and one is Edd, this is a 1:1

### Step 3b: Disambiguating between multiple 1:1 candidates
If there are multiple 1:1 meetings on the same day:
- Look at WHO is describing THEIR OWN work/problems (not Edd)
- The person describing their programs, their direct reports, their issues = that's the meeting counterpart
- KEY INSIGHT: If someone says "I've been having issues with X and Y", the speaker is the COMPLAINANT, not X or Y
- If they reference "my team", "my project", "my manager" - use those possessive phrases to identify the speaker
- DURATION HINT: A very long, detailed transcript likely corresponds to a longer calendar slot
- DO NOT just pick the first 1:1 in calendar order - use content clues to disambiguate
- Cross-reference against calendar participants to find the match

### Step 4: Slug naming based on CORRECTED participants
- For 1:1 meetings: ALWAYS use "firstname-edd-1-1" format (e.g., "marion-edd-1-1", "thabani-edd-1-1")
  - This is REQUIRED for any meeting with exactly 2 participants where one is Edd
  - Do NOT use topic-based slugs for 1:1s, even if the topic is interesting
- For small groups (3-4): include key names (e.g., "mia-brian-edd-tpm")  
- For large meetings (5+): use meeting type, NOT names (e.g., "engineering-town-hall", "cip-slt-sync")

### Step 5: Add calendar metadata to output
If you match to a calendar entry, add these properties to the :PROPERTIES: drawer:
- :CALENDAR_MATCH: <exact calendar title>
- :CALENDAR_TIME: <HH:MM-HH:MM from calendar>
- :MEETING_LINK: <video call URL if present>

## END CALENDAR CONTEXT

"""
    
    # Insert calendar instructions before the base prompt
    return calendar_instructions + base_prompt


def add_capture_mode_guidance(base_prompt: str, metadata: dict) -> str:
    """Add capture-specific speaker guidance when a room microphone was used."""
    if metadata.get("capture_mode") != "room":
        return base_prompt
    return """
## ROOM MICROPHONE CAPTURE

This meeting was captured through one physical room microphone while the user
participated from another device. Both the nearby user and remote participants
therefore appear in the same mono acoustic recording.

- Treat `[S]` as a possible speaker-turn boundary, never as a stable identity.
- Diarization may split one person into several apparent speakers. Reconcile
  adjacent and recurring fragments using conversational continuity, vocabulary,
  self-reference, direct address, and calendar participants.
- The louder/nearer voice is often Edd, but volume alone is not enough to assign
  identity. Use calendar and conversational evidence as the authority.
- Do not create duplicate participants merely because the diarizer oversplit a
  voice. Prefer the smallest participant set consistent with the conversation.

## END ROOM MICROPHONE CAPTURE

""" + base_prompt


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
    """Extract date from filename if present (YYYYMMDD-), otherwise from mtime."""
    import re
    filename = os.path.basename(filepath)
    # Check for YYYYMMDD pattern at start of filename
    match = re.match(r'^(\d{8})-', filename)
    if match:
        return match.group(1)
    # Fall back to file modification time
    timestamp = os.path.getmtime(filepath)
    return datetime.fromtimestamp(timestamp).strftime('%Y%m%d')

def parse_transcript_header(filepath: str) -> dict:
    """Extract YAML front matter from transcript if present.
    
    Expected format:
        ---
        meeting_start: 2026-01-27T14:00:00-08:00
        meeting_end: 2026-01-27T15:03:00-08:00
        recording_source: transcriber
        ---
        
        [Transcript content follows...]
    
    Returns a dict of parsed fields, or empty dict if no header present.
    """
    with open(filepath, 'r', encoding='utf-8') as f:
        content = f.read()
    
    if not content.startswith('---'):
        return {}
    
    # Find closing ---
    end_match = re.search(r'\n---\s*\n', content[3:])
    if not end_match:
        return {}
    
    yaml_text = content[3:3 + end_match.start()]
    try:
        import yaml
        metadata = yaml.safe_load(yaml_text) or {}
        # Normalize datetime strings — ensure they're strings for downstream parsing
        for key in ('meeting_start', 'meeting_end'):
            if key in metadata and not isinstance(metadata[key], str):
                metadata[key] = metadata[key].isoformat()
        return metadata
    except Exception:
        return {}


def get_transcript_body(filepath: str) -> str:
    """Return the transcript text with YAML front matter stripped, if present."""
    with open(filepath, 'r', encoding='utf-8') as f:
        content = f.read()
    
    if not content.startswith('---'):
        return content
    
    end_match = re.search(r'\n---\s*\n', content[3:])
    if not end_match:
        return content
    
    # Return everything after the closing ---
    return content[3 + end_match.end():]


# ============================================================================
# Transcript Pre-Processing: Filter & Split
# ============================================================================

# Minimum transcript body length to be worth processing (characters)
MIN_BODY_LENGTH = 200

# Minimum recording duration to be worth processing (seconds)
MIN_DURATION_SECONDS = 60

# Minimum body length to consider checking for multi-meeting (characters)
MULTI_MEETING_MIN_BODY = 5000

# Minimum duration to consider checking for multi-meeting (seconds)
MULTI_MEETING_MIN_DURATION = 3600  # 60 minutes


def _extract_json_object(text: str) -> dict | None:
    """Extract the first valid JSON object from text, handling nested braces."""
    for i, ch in enumerate(text):
        if ch == '{':
            depth = 0
            for j in range(i, len(text)):
                if text[j] == '{':
                    depth += 1
                elif text[j] == '}':
                    depth -= 1
                    if depth == 0:
                        try:
                            return json.loads(text[i:j+1])
                        except json.JSONDecodeError:
                            break  # malformed, try next '{'
    return None


def is_transcript_worth_processing(filepath: str) -> tuple[bool, str]:
    """Check if a transcript has enough content to be worth summarizing.
    
    Returns (True, "") if worth processing, or (False, reason) if it should be skipped.
    Uses simple heuristics — no LLM call needed.
    """
    body = get_transcript_body(filepath).strip()
    
    if len(body) < MIN_BODY_LENGTH:
        return False, f"too short ({len(body)} chars, need {MIN_BODY_LENGTH})"
    
    metadata = parse_transcript_header(filepath)
    if metadata.get('meeting_start') and metadata.get('meeting_end'):
        try:
            start = datetime.fromisoformat(metadata['meeting_start'])
            end = datetime.fromisoformat(metadata['meeting_end'])
            duration = (end - start).total_seconds()
            if duration < MIN_DURATION_SECONDS:
                return False, f"too brief ({int(duration)}s, need {MIN_DURATION_SECONDS}s)"
        except (ValueError, TypeError):
            pass  # Can't parse timestamps, skip duration check
    
    return True, ""


def detect_multi_meeting(filepath: str, calendar_path: str = None,
                         target: str = 'copilot', model: str = None,
                         debug: bool = False) -> list[int] | None:
    """Detect if a transcript contains multiple back-to-back meetings.
    
    Returns a list of character positions where splits should occur,
    or None if the transcript appears to be a single meeting.
    Only called for long transcripts where multi-meeting is plausible.
    """
    body = get_transcript_body(filepath).strip()
    metadata = parse_transcript_header(filepath)
    
    # Check if transcript is long enough to plausibly contain multiple meetings
    duration = None
    if metadata.get('meeting_start') and metadata.get('meeting_end'):
        try:
            start = datetime.fromisoformat(metadata['meeting_start'])
            end = datetime.fromisoformat(metadata['meeting_end'])
            duration = (end - start).total_seconds()
        except (ValueError, TypeError):
            pass
    
    # Build calendar context if available
    calendar_hint = ""
    overlapping_count = 0
    if calendar_path and os.path.exists(calendar_path) and duration and metadata.get('meeting_start'):
        try:
            meeting_start = datetime.fromisoformat(metadata['meeting_start'])
            meeting_end = datetime.fromisoformat(metadata['meeting_end'])
            meeting_date = meeting_start.strftime('%Y-%m-%d')
            calendar_entries = parse_calendar_org(calendar_path)
            day_entries = [e for e in calendar_entries if e['date'] == meeting_date]
            overlapping = [e for e in day_entries
                           if time_overlaps(e, meeting_start, meeting_end)]
            overlapping_count = len(overlapping)
            if overlapping:
                lines = []
                for i, e in enumerate(overlapping, 1):
                    time_str = f"{e['start_time']}-{e['end_time']}" if e['start_time'] else "all-day"
                    participants = ', '.join(e['participants']) if e['participants'] else 'unknown'
                    lines.append(f"  {i}. [{time_str}] {e['title']} (Participants: {participants})")
                calendar_hint = (
                    f"\n\nCALENDAR CONTEXT: The recording window ({meeting_start.strftime('%H:%M')}-"
                    f"{meeting_end.strftime('%H:%M')}) overlaps with {len(overlapping)} calendar entries:\n"
                    + '\n'.join(lines)
                )
        except (ValueError, TypeError):
            pass
    
    # Only check for multi-meeting if transcript is long enough
    # Either by body size + duration, body size + calendar hints,
    # or body size alone if very long (no metadata available)
    should_check = len(body) >= MULTI_MEETING_MIN_BODY and (
        (duration and duration >= MULTI_MEETING_MIN_DURATION) or
        overlapping_count >= 2 or
        (duration is None and len(body) >= MULTI_MEETING_MIN_BODY * 2)
    )
    
    if not should_check:
        return None
    
    print(f"  Multi-meeting check: {len(body)} chars, {int(duration or 0)}s duration, "
          f"{overlapping_count} calendar entries")
    
    # Use LLM to detect meeting boundaries.
    # Reference the file by path instead of inlining transcript content,
    # to avoid hitting Linux's per-argument size limit (MAX_ARG_STRLEN=128KB).
    filepath_abs = os.path.abspath(filepath)
    
    prompt = f"""Read the file at {filepath_abs} and analyze the meeting transcript to determine if it contains MULTIPLE separate meetings recorded back-to-back (e.g., two consecutive 1:1s where recording wasn't stopped between them). The file may begin with a YAML front matter header (between --- markers) containing metadata — focus your analysis on the transcript body that follows it.

Signs of multiple meetings:
- Goodbye/farewell exchanges followed by new greetings ("Bye!" then "Hello, how are you?")
- Complete topic shift with new participants being greeted
- Sign-off language ("talk to you later", "take care") followed by meeting-start language

Signs of a SINGLE meeting (do NOT split):
- Topic changes within the same meeting (normal agenda progression)
- People joining/leaving a group meeting
- Brief pleasantries between agenda items

{calendar_hint}

Respond with ONLY a JSON object, no other text:
{{
  "meeting_count": 1 or 2 or 3...,
  "confidence": 0.0 to 1.0,
  "reasoning": "brief explanation",
  "split_points": [
    {{"text_before": "last ~20 words before the split", "text_after": "first ~20 words after the split"}}
  ]
}}

If this is a single meeting, return meeting_count: 1 with empty split_points.
Be CONSERVATIVE — only split if you see clear meeting-ending AND meeting-starting signals."""

    model_name = model if model else 'claude-haiku-4.5'
    command = [COPILOT_PATH, '-p', prompt, '--allow-all-tools', '--allow-all-paths', '--model', model_name]
    
    try:
        if debug:
            print(f"  Multi-meeting: Running LLM detection...")
        result = subprocess.run(command, capture_output=True, text=True, timeout=120)
        
        if result.returncode != 0:
            print(f"  Multi-meeting: LLM error: {result.stderr[:200]}")
            return None
        
        output = result.stdout.strip()
        detection = _extract_json_object(output)
        if not detection:
            print(f"  Multi-meeting: Could not parse LLM response")
            return None
        
    except subprocess.TimeoutExpired:
        print(f"  Multi-meeting: LLM timed out")
        return None
    
    meeting_count = detection.get('meeting_count', 1)
    confidence = detection.get('confidence', 0)
    reasoning = detection.get('reasoning', '')
    
    if meeting_count <= 1 or confidence < 0.7:
        print(f"  Multi-meeting: Single meeting (confidence: {confidence:.0%}, {reasoning})")
        return None
    
    print(f"  Multi-meeting: Detected {meeting_count} meetings (confidence: {confidence:.0%})")
    print(f"  Reasoning: {reasoning}")
    
    # Find split positions in the body text using the text anchors
    split_positions = []
    for sp in detection.get('split_points', []):
        text_before = sp.get('text_before', '')
        text_after = sp.get('text_after', '')
        
        if text_before:
            # Find the position of text_before in the body
            idx = body.find(text_before)
            if idx >= 0:
                split_positions.append(idx + len(text_before))
                continue
        
        if text_after:
            idx = body.find(text_after)
            if idx >= 0:
                split_positions.append(idx)
                continue
        
        print(f"  Warning: Could not locate split point in transcript")
    
    return split_positions if split_positions else None


def split_transcript(filepath: str, split_positions: list[int], paths: dict) -> list[str]:
    """Split a transcript file at the given positions into separate inbox files.
    
    Returns list of new file paths created in the inbox directory.
    """
    body = get_transcript_body(filepath)
    metadata = parse_transcript_header(filepath)
    basename = os.path.basename(filepath)
    name_stem = os.path.splitext(basename)[0]
    ext = os.path.splitext(basename)[1]
    
    # Parse timestamps for interpolation
    start_time = None
    end_time = None
    if metadata.get('meeting_start') and metadata.get('meeting_end'):
        try:
            start_time = datetime.fromisoformat(metadata['meeting_start'])
            end_time = datetime.fromisoformat(metadata['meeting_end'])
        except (ValueError, TypeError):
            pass
    
    total_len = len(body)
    
    # Build segments
    positions = [0] + sorted(split_positions) + [total_len]
    new_files = []
    
    try:
        for i in range(len(positions) - 1):
            segment_body = body[positions[i]:positions[i+1]].strip()
            if not segment_body:
                continue
            
            # Interpolate timestamps based on character position
            part_metadata = dict(metadata)
            if start_time and end_time:
                total_duration = (end_time - start_time).total_seconds()
                frac_start = positions[i] / total_len
                frac_end = positions[i+1] / total_len
                from datetime import timedelta
                part_start = start_time + timedelta(seconds=total_duration * frac_start)
                part_end = start_time + timedelta(seconds=total_duration * frac_end)
                part_metadata['meeting_start'] = part_start.isoformat()
                part_metadata['meeting_end'] = part_end.isoformat()
            
            # Build YAML front matter
            front_matter_lines = ['---']
            for key in ('meeting_start', 'meeting_end', 'recording_source', 'capture_mode'):
                if key in part_metadata:
                    front_matter_lines.append(f"{key}: {part_metadata[key]}")
            front_matter_lines.append('---\n\n')
            front_matter = '\n'.join(front_matter_lines)
            
            # Write to inbox
            part_name = f"{name_stem}-part{i+1}{ext}"
            part_path = os.path.join(paths['inbox'], part_name)
            with open(part_path, 'w', encoding='utf-8') as f:
                f.write(front_matter + segment_body)
            
            new_files.append(part_path)
            print(f"  Split: created {part_name} ({len(segment_body)} chars)")
    except (OSError, IOError) as e:
        # Clean up any partial files to avoid confusion
        for f in new_files:
            try:
                os.remove(f)
            except OSError:
                pass
        print(f"  Split failed ({e}), keeping original {basename}")
        return []
    
    # Only remove original after all parts written successfully
    os.remove(filepath)
    print(f"  Split: removed original {basename}")
    
    return new_files


def time_overlaps(calendar_entry: dict, meeting_start: datetime, meeting_end: datetime) -> bool:
    """Check if a calendar entry's time window overlaps with the meeting window.
    
    Calendar entries have 'start_time' and 'end_time' as HH:MM strings (or None for all-day).
    Meeting start/end are full datetime objects. We compare only the time-of-day portion.
    
    All-day events always overlap. Provides 5-minute tolerance on boundaries.
    """
    if not calendar_entry.get('start_time') or not calendar_entry.get('end_time'):
        return True  # All-day events always match
    
    # Parse calendar times as hours/minutes on the meeting date
    try:
        cal_start_parts = calendar_entry['start_time'].split(':')
        cal_end_parts = calendar_entry['end_time'].split(':')
        
        cal_start = meeting_start.replace(
            hour=int(cal_start_parts[0]), minute=int(cal_start_parts[1]),
            second=0, microsecond=0)
        cal_end = meeting_start.replace(
            hour=int(cal_end_parts[0]), minute=int(cal_end_parts[1]),
            second=0, microsecond=0)
    except (ValueError, IndexError):
        return True  # Can't parse times, don't filter
    
    # Apply 5-minute tolerance — meetings often run over or start slightly late
    from datetime import timedelta
    tolerance = timedelta(minutes=5)
    cal_start_tolerant = cal_start - tolerance
    cal_end_tolerant = cal_end + tolerance
    
    # Check overlap: two intervals overlap if start1 < end2 and start2 < end1
    return meeting_start < cal_end_tolerant and cal_start_tolerant < meeting_end


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


def gather_recent_notes_context(notes_dir: str, limit: int = 30) -> str:
    """Extract metadata from recent notes to help with participant identification.
    
    Returns a formatted string showing recent meeting patterns:
    - Who attends which types of meetings
    - What topics each person typically discusses
    """
    if not os.path.exists(notes_dir):
        return ""
    
    notes_files = sorted(glob.glob(os.path.join(notes_dir, '*.org')), reverse=True)[:limit]
    
    if not notes_files:
        return ""
    
    person_topics = {}  # person -> list of topics they've discussed
    
    for note_file in notes_files:
        try:
            with open(note_file, 'r', encoding='utf-8') as f:
                content = f.read(2000)  # Just read header portion
            
            # Extract participants
            participants_match = re.search(r':PARTICIPANTS:\s*(.+?)(?:\n|$)', content)
            if not participants_match:
                continue
            
            participants = [p.strip() for p in participants_match.group(1).split(',')]
            # Filter to non-Edd participants in 1:1s
            other_participants = [p for p in participants 
                                  if 'edd' not in p.lower() and p.strip()]
            
            # Extract topic
            topic_match = re.search(r':TOPIC:\s*(.+?)(?:\n|$)', content)
            topic = topic_match.group(1).strip() if topic_match else None
            
            # Extract title (first line after **)
            title_match = re.search(r'^\*\*\s+(.+?)(?:\s+:note)?', content, re.MULTILINE)
            title = title_match.group(1).strip() if title_match else None
            
            # For 1:1s, associate topics with the other person
            if len(other_participants) == 1 and topic:
                person = other_participants[0]
                # Extract first name for matching
                first_name = person.split()[0] if person else None
                if first_name:
                    if first_name not in person_topics:
                        person_topics[first_name] = []
                    person_topics[first_name].append(topic[:100])  # Truncate long topics
        
        except Exception:
            continue
    
    if not person_topics:
        return ""
    
    # Format as context for LLM
    lines = ["## RECENT MEETING HISTORY (for disambiguation)", ""]
    lines.append("Recent 1:1 meetings and their topics - use this to identify who typically discusses what:")
    lines.append("")
    
    for person, topics in sorted(person_topics.items()):
        # Show last 3 topics for each person
        recent_topics = topics[:3]
        lines.append(f"- **{person}**: {'; '.join(recent_topics)}")
    
    lines.append("")
    lines.append("If uncertain which 1:1 a transcript belongs to, match the discussion topics to the person above.")
    lines.append("")
    
    return '\n'.join(lines)


# ============================================================================
# Calendar Parsing Functions
# ============================================================================
# Calendar data is now passed to the LLM in the initial summarization prompt,
# allowing single-pass processing with correct participant identification.
# The build_calendar_prompt and enrich_with_calendar functions are retained
# for potential reprocessing of existing notes without transcripts.
# ============================================================================

def parse_calendar_org(calendar_path: str) -> list[dict]:
    """Parse calendar.org and extract meeting entries."""
    entries = []
    
    with open(calendar_path, 'r', encoding='utf-8') as f:
        content = f.read()
    
    # Pattern: * Title <timestamp>
    entry_pattern = re.compile(
        r'^\* (.+?) <(\d{4}-\d{2}-\d{2}) \w{3}(?: (\d{2}:\d{2})-(\d{2}:\d{2}))?>\s*\n(.*?)(?=^\* |\Z)',
        re.MULTILINE | re.DOTALL
    )
    
    for match in entry_pattern.finditer(content):
        title = match.group(1).strip()
        date_str = match.group(2)
        start_time = match.group(3)
        end_time = match.group(4)
        body = match.group(5).strip()
        
        # Extract PARTICIPANTS from properties
        participants = []
        participants_match = re.search(r':PARTICIPANTS:\s*(.+?)(?:\n|$)', body)
        if participants_match:
            raw_participants = participants_match.group(1)
            for p in raw_participants.split(','):
                p = p.strip()
                name = re.sub(r'\s*<[^>]+>\s*', '', p).strip()
                if name:
                    participants.append(name)
        
        # Extract video call links from body
        meeting_links = []
        link_pattern = re.compile(r'\[\[(https://[^\]]+)\]\[📹[^\]]*\]\]')
        for link_match in link_pattern.finditer(body):
            meeting_links.append(link_match.group(1))
        
        entries.append({
            'title': title,
            'date': date_str,
            'start_time': start_time,
            'end_time': end_time,
            'participants': participants,
            'meeting_links': meeting_links,
        })
    
    return entries


def parse_notes_org_for_calendar(notes_path: str) -> dict:
    """Parse a notes.org file for calendar matching."""
    with open(notes_path, 'r', encoding='utf-8') as f:
        content = f.read()
    
    result = {
        'title': None, 'timestamp': None, 'date': None, 'time': None,
        'participants': [], 'slug': None, 'topic': None, 'content': content
    }
    
    # Extract title (first ** heading)
    title_match = re.search(r'^\*\* (.+?)\s+:note:', content, re.MULTILINE)
    if title_match:
        result['title'] = title_match.group(1).strip()
    
    # Extract timestamp [YYYY-MM-DD Day HH:MM]
    ts_match = re.search(r'\[(\d{4}-\d{2}-\d{2}) (\w{3})(?: (\d{2}:\d{2}))?\]', content)
    if ts_match:
        result['date'] = ts_match.group(1)
        result['time'] = ts_match.group(3)
        result['timestamp'] = ts_match.group(0)
    
    # Extract properties
    for prop in ['PARTICIPANTS', 'SLUG', 'TOPIC']:
        match = re.search(rf':{prop}:\s*(.+?)(?:\n|$)', content)
        if match:
            if prop == 'PARTICIPANTS':
                result['participants'] = [p.strip() for p in match.group(1).split(',')]
            else:
                result[prop.lower()] = match.group(1).strip()
    
    return result


def build_calendar_prompt(notes: dict, calendar_entries: list[dict]) -> str:
    """Build prompt for LLM calendar matching."""
    # Format calendar entries
    lines = []
    for i, e in enumerate(calendar_entries, 1):
        time_str = f"{e['start_time']}-{e['end_time']}" if e['start_time'] else "all-day"
        participants = ', '.join(e['participants']) if e['participants'] else 'unknown'
        lines.append(f"{i}. [{time_str}] {e['title']}")
        lines.append(f"   Participants: {participants}")
        if e['meeting_links']:
            lines.append(f"   Meeting link: {e['meeting_links'][0]}")
        lines.append("")
    calendar_text = '\n'.join(lines) if lines else "No calendar entries for this date."
    
    participants_str = ', '.join(notes['participants']) if notes['participants'] else 'unknown'
    participant_count = len(notes['participants']) if notes['participants'] else 0
    
    return f"""You are helping match a meeting transcript to a calendar entry.

## Calendar entries for {notes['date']}:

{calendar_text}

## Meeting notes metadata:
- Title: {notes['title']}
- Time in notes: {notes['time'] or 'unknown'}
- PARTICIPANTS DETECTED: {participants_str}
- Participant count: {participant_count}
- Topic: {notes['topic'] or 'unknown'}
- Current slug: {notes['slug']}

## CRITICAL: Participant-First Matching Strategy

The PARTICIPANTS field is your PRIMARY matching signal. Follow this decision tree:

### Step 1: Check for participant name matches
Look at each calendar entry's participants and check if the detected participants match:
- Handle name variations: "Mia" = "its-mia" = "Mia Arts", "Thabani" = "thabani11", etc.
- Calendar titles like "cmart12 / ewilderj" mean participants are Chris Martin and Edd
- Calendar format is usually "username / username" for 1:1s

### Step 2: Decide based on participant matching

**IF participants clearly match a calendar entry:**
- This is your match! High confidence (85-95%)
- The topic/content of the meeting is IRRELEVANT - people discuss many topics in 1:1s
- Do NOT change the match based on what was discussed

**IF participants DON'T match any calendar entry but we have a time-proximate entry:**
- The transcript MAY have misidentified participants
- Common transcription errors: hearing "Kim" when speaker said "CIP", mishearing names
- In this case, use the calendar entry to CORRECT the participant identification
- Lower confidence (70-80%) since we're correcting a potential error

**IF {participant_count} participants detected (one being Edd) = 2:**
- This is almost certainly a 1:1 meeting
- STRONGLY PREFER matching to a 1:1 calendar entry (format: "name / ewilderj 1:1")
- Do NOT match to team syncs, SLT meetings, or group meetings just because topics overlap

### Step 3: Time as tiebreaker
- Use time proximity ONLY to choose between multiple entries with similar participant matches
- Meetings often run over, so notes time of 13:08 could match 12:30-13:00 or 13:00-13:30 slots
- Time is NOT a reason to override a clear participant match

### What NOT to do:
- DON'T match based on topic similarity alone - Edd discusses similar TPM topics in many meetings
- DON'T match to "CIP SLT Sync" just because CIP was discussed - check WHO was in the meeting
- DON'T let meeting title keywords override participant matching

## Response format (output ONLY this JSON, no other text):
{{
  "matched": true/false,
  "confidence": 0.0-1.0,
  "calendar_entry_number": N or null,
  "calendar_title": "exact title from calendar" or null,
  "calendar_time": "HH:MM-HH:MM" or null,
  "meeting_link": "URL" or null,
  "suggested_title": "Improved title incorporating calendar info" or null,
  "suggested_slug": "improved-slug-with-participant-names" or null,
  "reasoning": "Brief explanation: which participants matched which calendar entry"
}}

## Slug guidelines:
- For 1:1 meetings: "firstname-edd-1-1" (e.g., "mia-edd-1-1", "thabani-edd-1-1")
- For small groups (3-4): include key names (e.g., "mia-brian-edd-tpm")
- For large meetings (5+): use meeting type, NOT names (e.g., "engineering-town-hall")

## Title guidelines:
- For 1:1s: "Name / Edd 1:1: Topic Summary" - ALWAYS preserve the original topic
- For groups: keep the descriptive title from the notes

Output ONLY the JSON object, nothing else."""


def enrich_with_calendar(org_path: str, transcript_path: str, calendar_path: str, 
                          target: str = 'copilot', model: str = None, debug: bool = False) -> tuple[str, str] | None:
    """Enrich notes with calendar metadata. Returns (old_path, new_path) if renamed, else None."""
    
    # Parse calendar and notes
    calendar_entries = parse_calendar_org(calendar_path)
    notes = parse_notes_org_for_calendar(org_path)
    
    if not notes['date']:
        print("  Calendar: Could not extract date from notes")
        return None
    
    # Filter calendar to matching date
    day_entries = [e for e in calendar_entries if e['date'] == notes['date']]
    
    if not day_entries:
        print(f"  Calendar: No entries for {notes['date']}, skipping enrichment")
        return None
    
    print(f"  Calendar: Found {len(day_entries)} entries for {notes['date']}, matching...")
    
    # Build prompt and run LLM
    prompt = build_calendar_prompt(notes, day_entries)
    
    model_name = model if model else 'claude-sonnet-4.5'
    command = [COPILOT_PATH, '-p', prompt, '--allow-all-tools', '--allow-all-paths', '--model', model_name]
    
    try:
        if debug:
            print(f"  Calendar: Running Copilot for matching...")
        result = subprocess.run(command, capture_output=True, text=True, timeout=60)
        
        if result.returncode != 0:
            print(f"  Calendar: LLM error: {result.stderr[:200]}")
            return None
        
        # Extract JSON from output
        output = result.stdout.strip()
        match_result = _extract_json_object(output)
        if not match_result:
            print("  Calendar: Could not parse LLM response")
            return None
        
    except subprocess.TimeoutExpired:
        print("  Calendar: LLM timed out")
        return None
    
    # Check if we have a confident match
    if not match_result.get('matched') or match_result.get('confidence', 0) < 0.7:
        print(f"  Calendar: No confident match (confidence: {match_result.get('confidence', 0):.0%})")
        return None
    
    print(f"  Calendar: Matched to '{match_result.get('calendar_title')}' ({match_result.get('confidence', 0):.0%})")
    
    # Apply enrichment
    content = notes['content']
    old_slug = notes['slug']
    new_slug = match_result.get('suggested_slug', old_slug)
    changes = []
    
    # Update title
    if match_result.get('suggested_title') and match_result['suggested_title'] != notes['title']:
        old_title_escaped = re.escape(notes['title'])
        content = re.sub(rf'(\*\* ){old_title_escaped}(\s+:note:)', 
                        f'\\1{match_result["suggested_title"]}\\2', content)
        changes.append(f"Title updated")
    
    # Update slug
    if new_slug and new_slug != old_slug:
        content = re.sub(r':SLUG:\s*.+?(?=\n)', f':SLUG: {new_slug}', content)
        changes.append(f"Slug: {old_slug} → {new_slug}")
    
    # Add calendar properties (before :END:)
    for prop, key in [('CALENDAR_MATCH', 'calendar_title'), 
                      ('CALENDAR_TIME', 'calendar_time'),
                      ('MEETING_LINK', 'meeting_link')]:
        if match_result.get(key) and f':{prop}:' not in content:
            content = re.sub(r'(:END:\s*\n)', f':{prop}: {match_result[key]}\n\\1', content)
            changes.append(f"Added {prop}")
    
    # Update timestamp
    if match_result.get('calendar_time') and notes['timestamp']:
        day_match = re.search(r'\d{4}-\d{2}-\d{2} (\w{3})', notes['timestamp'])
        if day_match:
            start_time = match_result['calendar_time'].split('-')[0]
            new_ts = f"[{notes['date']} {day_match.group(1)} {start_time}]"
            if notes['timestamp'] != new_ts:
                content = content.replace(notes['timestamp'], new_ts)
                changes.append("Timestamp updated")
    
    if changes:
        print(f"  Calendar: {', '.join(changes)}")
        with open(org_path, 'w', encoding='utf-8') as f:
            f.write(content)
        
        # Rename files if slug changed
        if new_slug and new_slug != old_slug:
            date_str = notes['date'].replace('-', '')
            notes_dir = os.path.dirname(org_path)
            transcripts_dir = os.path.dirname(transcript_path)
            
            new_org_path = os.path.join(notes_dir, f"{date_str}-{new_slug}.org")
            new_transcript_path = os.path.join(transcripts_dir, f"{date_str}-{new_slug}.txt")
            
            if org_path != new_org_path:
                os.rename(org_path, new_org_path)
                print(f"  Renamed: {os.path.basename(org_path)} → {os.path.basename(new_org_path)}")
            if transcript_path != new_transcript_path and os.path.exists(transcript_path):
                os.rename(transcript_path, new_transcript_path)
                print(f"  Renamed: {os.path.basename(transcript_path)} → {os.path.basename(new_transcript_path)}")
            
            return (org_path, new_org_path)
    
    return None


# ============================================================================
# Main Processing Functions
# ============================================================================

def process_transcript(input_file, paths, target='copilot', model=None, prompt_template=None, debug=False, calendar_path=None):
    """Process a single transcript: summarize with calendar context, extract slug, and organize files."""
    print(f"\nProcessing: {input_file}")
    
    workspace_dir = paths['workspace']
    
    # Parse transcript header for timing metadata
    metadata = parse_transcript_header(input_file)
    
    # Get date from header timestamps, falling back to filename/mtime
    meeting_start = None
    meeting_end = None
    if metadata.get('meeting_start'):
        try:
            meeting_start = datetime.fromisoformat(metadata['meeting_start'])
            date_str = meeting_start.strftime('%Y%m%d')
            if metadata.get('meeting_end'):
                meeting_end = datetime.fromisoformat(metadata['meeting_end'])
            print(f"  Metadata: {metadata.get('recording_source', 'unknown')} recording, "
                  f"{meeting_start.strftime('%H:%M')}"
                  f"{'-' + meeting_end.strftime('%H:%M') if meeting_end else ''}")
        except (ValueError, TypeError):
            date_str = get_date_from_file(input_file)
    else:
        date_str = get_date_from_file(input_file)
    
    meeting_date = f"{date_str[:4]}-{date_str[4:6]}-{date_str[6:8]}"  # YYYY-MM-DD format
    temp_id = uuid.uuid4().hex[:8]
    temp_org_filename = f"temp-{date_str}-{temp_id}.org"
    
    # Get basename for input file (relative to workspace)
    input_basename = os.path.basename(input_file)
    input_relative = os.path.join('inbox', input_basename)
    
    # Build the prompt - include calendar context if available
    final_prompt = prompt_template.format(input_file=input_relative, output_file=temp_org_filename)
    final_prompt = add_capture_mode_guidance(final_prompt, metadata)
    
    if calendar_path and os.path.exists(calendar_path):
        # Parse calendar and filter to matching date
        calendar_entries = parse_calendar_org(calendar_path)
        day_entries = [e for e in calendar_entries if e['date'] == meeting_date]
        
        # If we have precise timestamps, filter by time overlap for deterministic matching
        if day_entries and meeting_start and meeting_end:
            time_filtered = [e for e in day_entries
                             if time_overlaps(e, meeting_start, meeting_end)]
            if time_filtered:
                print(f"  Calendar: Narrowed {len(day_entries)} entries to {len(time_filtered)} by time overlap")
                day_entries = time_filtered
            else:
                print(f"  Calendar: No time overlap matches, keeping all {len(day_entries)} entries for date")
        
        if day_entries:
            print(f"  Calendar: Found {len(day_entries)} entries for {meeting_date}")
            calendar_text = format_calendar_for_prompt(day_entries, meeting_date)
            # Gather recent notes context to help with disambiguation
            notes_context = gather_recent_notes_context(paths['notes'])
            
            # Add time context from transcript metadata if available
            if meeting_start:
                time_hint = f"\n## MEETING TIME FROM TRANSCRIPT\n"
                time_hint += f"The recording started at {meeting_start.strftime('%H:%M')}"
                if meeting_end:
                    time_hint += f" and ended at {meeting_end.strftime('%H:%M')}"
                    duration_mins = int((meeting_end - meeting_start).total_seconds() / 60)
                    time_hint += f" (duration: {duration_mins} minutes)"
                time_hint += ".\n"
                time_hint += "This STRONGLY constrains which calendar entry matches:\n"
                time_hint += "- Prefer entries whose time slot contains or overlaps this window\n"
                time_hint += f"- A {meeting_start.strftime('%H:%M')}-{meeting_end.strftime('%H:%M') if meeting_end else '??:??'} recording almost certainly matches a calendar slot at that time\n"
                calendar_text = time_hint + "\n" + calendar_text
            
            final_prompt = build_calendar_aware_prompt(final_prompt, calendar_text, meeting_date, notes_context)
        else:
            print(f"  Calendar: No entries for {meeting_date}")
    
    # Run summarization
    print(f"  Generating summary...")

    if target == 'copilot':
        model_name = model if model else 'claude-sonnet-4.5'
        command = [
            COPILOT_PATH,
            '-p', final_prompt,
            '--allow-all-tools',
            '--allow-all-paths',
            '--model', model_name
        ]
        try:
            if debug:
                cmd_display = [c if c != final_prompt else '<prompt>' for c in command]
                print(f"  Running: {' '.join(cmd_display)}")
                print(f"  Working directory: {os.path.abspath(workspace_dir)}")
                print(f"  Prompt length: {len(final_prompt)} chars")
            else:
                print(f"  Prompt length: {len(final_prompt)} chars")

            per_file_timeout = 600  # 10 minutes per file
            start_time = _time.monotonic()
            deadline = start_time + per_file_timeout
            process = subprocess.Popen(
                command,
                cwd=workspace_dir,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1
            )
            output_lines = []
            last_progress = start_time
            timed_out = False

            while process.poll() is None:
                now = _time.monotonic()
                if now > deadline:
                    process.kill()
                    process.wait()
                    timed_out = True
                    break
                remaining = min(5.0, deadline - now)
                ready, _, _ = select.select([process.stdout], [], [], remaining)
                if ready:
                    line = process.stdout.readline()
                    if line:
                        output_lines.append(line)
                        now = _time.monotonic()
                        if debug:
                            print(f"  {line}", end='', flush=True)
                        elif now - last_progress >= 30:
                            elapsed = int(now - start_time)
                            print(f"  ... still running ({elapsed}s, {len(output_lines)} lines) [{input_basename}]", flush=True)
                            last_progress = now
                elif now - last_progress >= 30:
                    elapsed = int(now - start_time)
                    print(f"  ... still running ({elapsed}s, {len(output_lines)} lines) [{input_basename}]", flush=True)
                    last_progress = now

            if timed_out:
                elapsed = int(_time.monotonic() - start_time)
                print(f"  Error: Copilot timed out after {elapsed}s [{input_basename}]")
                # Clean up temp file if it exists
                temp_org_path = os.path.join(workspace_dir, temp_org_filename)
                if os.path.exists(temp_org_path):
                    os.remove(temp_org_path)
                return False, None, None

            # Read remaining output after process exits
            for line in process.stdout:
                output_lines.append(line)
                if debug:
                    print(f"  {line}", end='', flush=True)

            process.wait()
            elapsed = int(_time.monotonic() - start_time)
            print(f"  Copilot finished in {elapsed}s (exit code {process.returncode}, {len(output_lines)} lines) [{input_basename}]")
            if process.returncode != 0:
                # Show last few lines of output for diagnosis
                tail = ''.join(output_lines[-10:])
                print(f"  Error in summarization (last output):\n{tail}")
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
            else:
                print(f"  Prompt length: {len(final_prompt)} chars")

            per_file_timeout = 600  # 10 minutes per file
            start_time = _time.monotonic()
            deadline = start_time + per_file_timeout
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
            output_lines = []
            last_progress = start_time
            timed_out = False

            while process.poll() is None:
                now = _time.monotonic()
                if now > deadline:
                    process.kill()
                    process.wait()
                    timed_out = True
                    break
                remaining = min(5.0, deadline - now)
                ready, _, _ = select.select([process.stdout], [], [], remaining)
                if ready:
                    line = process.stdout.readline()
                    if line:
                        output_lines.append(line)
                        now = _time.monotonic()
                        if debug:
                            print(f"  {line}", end='', flush=True)
                        elif now - last_progress >= 30:
                            elapsed = int(now - start_time)
                            print(f"  ... still running ({elapsed}s, {len(output_lines)} lines) [{input_basename}]", flush=True)
                            last_progress = now
                elif now - last_progress >= 30:
                    elapsed = int(now - start_time)
                    print(f"  ... still running ({elapsed}s, {len(output_lines)} lines) [{input_basename}]", flush=True)
                    last_progress = now

            if timed_out:
                elapsed = int(_time.monotonic() - start_time)
                print(f"  Error: Gemini timed out after {elapsed}s [{input_basename}]")
                temp_org_path = os.path.join(workspace_dir, temp_org_filename)
                if os.path.exists(temp_org_path):
                    os.remove(temp_org_path)
                return False, None, None

            # Read remaining output after process exits
            for line in process.stdout:
                output_lines.append(line)
                if debug:
                    print(f"  {line}", end='', flush=True)

            process.wait()
            elapsed = int(_time.monotonic() - start_time)
            print(f"  Gemini finished in {elapsed}s (exit code {process.returncode}, {len(output_lines)} lines) [{input_basename}]")
            if process.returncode != 0:
                tail = ''.join(output_lines[-10:])
                print(f"  Error in summarization (last output):\n{tail}")
                return False, None, None
        except Exception as e:
            print(f"  Error running gemini: {e}")
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
    
    # Move files to their final locations.
    # Move transcript first — it's the source of truth. If the notes move
    # fails, we lose only the regenerable summary, not the original data.
    shutil.move(input_file, transcript_path)
    print(f"  Moved transcript to: {transcript_path}")
    
    shutil.move(temp_org_path, org_path)
    print(f"  Created: {org_path}")
    
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

def process_inbox(paths, target='copilot', model=None, use_git=False, prompt_template=None, debug=False, calendar_path=None):
    """Process all transcript files in the inbox directory.
    
    Pre-processing steps before summarization:
      1. Filter out junk transcripts (too short, too brief)
      2. Detect and split multi-meeting transcripts
    
    Returns:
        tuple: (successful_count, failed_count) or (0, 0) if no files found
    """
    inbox_dir = paths['inbox']
    
    if not os.path.exists(inbox_dir):
        print(f"Error: {inbox_dir} directory not found.")
        return 0, 1  # Count as a failure
    
    # Find all .txt and .md files in inbox
    transcript_files = []
    for ext in ['*.txt', '*.md']:
        transcript_files.extend(glob.glob(os.path.join(inbox_dir, ext)))
    
    if not transcript_files:
        print(f"No transcript files found in {inbox_dir}/")
        return 0, 0  # No files is not a failure, but nothing succeeded
    
    print(f"Found {len(transcript_files)} transcript(s) to process")
    if calendar_path and os.path.exists(calendar_path):
        print(f"Calendar enrichment enabled: {calendar_path}")
    
    # Ensure output directories exist
    os.makedirs(paths['transcripts'], exist_ok=True)
    os.makedirs(paths['notes'], exist_ok=True)
    
    # --- Step 1: Filter junk transcripts ---
    skipped = 0
    filtered_files = []
    for transcript_file in transcript_files:
        worth_it, reason = is_transcript_worth_processing(transcript_file)
        if not worth_it:
            print(f"  Skipping {os.path.basename(transcript_file)}: {reason}")
            skipped += 1
            if use_git:
                # Use git rm so deletion is tracked; if git fails, keep the file
                workspace_abs = os.path.abspath(paths['workspace'])
                rel_path = os.path.relpath(os.path.abspath(transcript_file), workspace_abs)
                rm_result = subprocess.run(['git', 'rm', '-f', rel_path], capture_output=True, text=True, cwd=paths['workspace'])
                if rm_result.returncode == 0:
                    subprocess.run(['git', 'commit', '-m', f'Skip junk transcript: {os.path.basename(transcript_file)} ({reason})'],
                                   capture_output=True, text=True, cwd=paths['workspace'])
                else:
                    # git rm failed — fall back to plain delete
                    os.remove(transcript_file)
            else:
                os.remove(transcript_file)
        else:
            filtered_files.append(transcript_file)
    
    if skipped:
        print(f"  Filtered out {skipped} junk transcript(s)")
    
    # --- Step 2: Detect and split multi-meeting transcripts ---
    final_files = []
    for transcript_file in filtered_files:
        try:
            split_positions = detect_multi_meeting(
                transcript_file, calendar_path=calendar_path,
                target=target, model=model, debug=debug
            )
            if split_positions:
                new_files = split_transcript(transcript_file, split_positions, paths)
                final_files.extend(new_files)
            else:
                final_files.append(transcript_file)
        except Exception as e:
            print(f"  Error in multi-meeting detection for {os.path.basename(transcript_file)}: {e}")
            final_files.append(transcript_file)
    
    if not final_files:
        if skipped:
            print(f"\nAll {skipped} transcript(s) were filtered as junk")
        return 0, 0
    
    if len(final_files) != len(filtered_files):
        print(f"  After splitting: {len(final_files)} file(s) to process")
    
    # --- Step 3: Process each transcript ---
    successful = 0
    failed = 0
    
    for transcript_file in final_files:
        try:
            result = process_transcript(transcript_file, paths, target, model, prompt_template, debug, calendar_path)
            if result[0]:  # Success
                successful += 1
                # Immediately commit this file's changes so progress is preserved
                # even if later files fail or the process times out
                if use_git:
                    print(f"  Committing changes for {os.path.basename(transcript_file)}...")
                    git_commit_changes(
                        [transcript_file],
                        [result[1]],
                        [result[2]],
                        paths['workspace']
                    )
            else:
                failed += 1
        except Exception as e:
            print(f"Error processing {transcript_file}: {e}")
            failed += 1
    
    print(f"\n{'='*60}")
    summary_parts = [f"{successful} successful", f"{failed} failed"]
    if skipped:
        summary_parts.append(f"{skipped} skipped")
    print(f"Processing complete: {', '.join(summary_parts)}")
    print(f"{'='*60}")
    
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
    
    # Calendar enrichment options (Phase 7)
    calendar_group = parser.add_mutually_exclusive_group()
    calendar_group.add_argument('--calendar', action='store_true', default=True,
                               help='Enable calendar enrichment (default: enabled if calendar.org exists).')
    calendar_group.add_argument('--no-calendar', action='store_true',
                               help='Disable calendar enrichment.')
    
    args = parser.parse_args()
    
    # Determine workspace directory: CLI arg > env var > current directory
    workspace_dir = args.workspace or os.getenv('WORKSPACE_DIR', '.')
    paths = get_workspace_paths(workspace_dir)
    
    # Determine calendar path
    calendar_path = None
    if not args.no_calendar:
        potential_calendar = os.path.join(paths['workspace'], 'calendar.org')
        if os.path.exists(potential_calendar):
            calendar_path = potential_calendar
    
    # Load prompt template
    prompt_template = load_prompt_template(args.prompt, workspace_dir)
    
    # Ensure required directories exist
    for dir_path in [paths['inbox'], paths['transcripts'], paths['notes']]:
        if not os.path.exists(dir_path):
            os.makedirs(dir_path)
            print(f"Created {dir_path}/ directory")
    
    # Process all transcripts in inbox
    result = process_inbox(paths, target=args.target, model=args.model, use_git=args.git, 
                          prompt_template=prompt_template, debug=args.debug, calendar_path=calendar_path)
    
    # Exit with appropriate code
    if result is None:
        sys.exit(1)  # Unexpected error
    successful, failed = result
    if failed > 0:
        sys.exit(1)  # Some files failed
    if successful == 0:
        sys.exit(2)  # No files were processed (not necessarily an error, but nothing happened)
    sys.exit(0)  # Success

if __name__ == "__main__":
    run_summarization()
