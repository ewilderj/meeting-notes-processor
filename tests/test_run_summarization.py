#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "pytest>=8.0.0",
# ]
# ///
"""
Tests for run_summarization.py

Covers:
- Workspace path computation and CLI argument handling
- Prompt file lookup logic (workspace-first, script-dir fallback)
- Directory path generation

Run with: uv run pytest tests/test_run_summarization.py -v
"""

import os
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from unittest import mock

import pytest

# Add parent directory to path so we can import run_summarization
sys.path.insert(0, str(Path(__file__).parent.parent))

import run_summarization


def test_default_copilot_model_is_terra():
    assert run_summarization.DEFAULT_COPILOT_MODEL == "gpt-5.6-terra"


class TestGetWorkspacePaths:
    """Tests for get_workspace_paths() function."""

    def test_returns_all_required_paths(self):
        """Should return dict with workspace, inbox, transcripts, and notes paths."""
        paths = run_summarization.get_workspace_paths('/some/path')
        
        assert 'workspace' in paths
        assert 'inbox' in paths
        assert 'transcripts' in paths
        assert 'notes' in paths

    def test_paths_are_relative_to_workspace(self):
        """All paths should be under the workspace directory."""
        workspace = '/data/my-notes'
        paths = run_summarization.get_workspace_paths(workspace)
        
        assert paths['workspace'] == workspace
        assert paths['inbox'] == '/data/my-notes/inbox'
        assert paths['transcripts'] == '/data/my-notes/transcripts'
        assert paths['notes'] == '/data/my-notes/notes'

    def test_handles_relative_path(self):
        """Should work with relative paths."""
        paths = run_summarization.get_workspace_paths('../meeting-notes')
        
        assert paths['workspace'] == '../meeting-notes'
        assert paths['inbox'] == '../meeting-notes/inbox'

    def test_handles_current_directory(self):
        """Should work with '.' as workspace."""
        paths = run_summarization.get_workspace_paths('.')
        
        assert paths['workspace'] == '.'
        assert paths['inbox'] == './inbox'


class TestGetDefaultPromptFile:
    """Tests for get_default_prompt_file() function."""

    def test_prefers_workspace_prompt_when_exists(self):
        """Should return workspace prompt.txt if it exists."""
        with tempfile.TemporaryDirectory() as tmpdir:
            # Create prompt.txt in workspace
            workspace_prompt = Path(tmpdir) / 'prompt.txt'
            workspace_prompt.write_text('workspace prompt')
            
            result = run_summarization.get_default_prompt_file(tmpdir)
            
            assert result == str(workspace_prompt)

    def test_falls_back_to_script_dir_prompt(self):
        """Should fall back to script directory prompt.txt if workspace doesn't have one."""
        with tempfile.TemporaryDirectory() as tmpdir:
            # No prompt.txt in workspace
            result = run_summarization.get_default_prompt_file(tmpdir)
            
            # Should return script directory prompt
            expected = os.path.join(run_summarization.SCRIPT_DIR, 'prompt.txt')
            assert result == expected

    def test_workspace_prompt_takes_precedence(self):
        """Even if script dir has prompt.txt, workspace should take precedence."""
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace_prompt = Path(tmpdir) / 'prompt.txt'
            workspace_prompt.write_text('workspace version')
            
            result = run_summarization.get_default_prompt_file(tmpdir)
            
            # Should be workspace, not script dir
            assert tmpdir in result
            assert run_summarization.SCRIPT_DIR not in result


class TestLoadPromptTemplate:
    """Tests for load_prompt_template() function."""

    def test_loads_explicit_prompt_file(self):
        """Should load content from explicitly specified prompt file."""
        with tempfile.NamedTemporaryFile(mode='w', suffix='.txt', delete=False) as f:
            f.write('Test prompt content with {input_file} and {output_file}')
            f.flush()
            
            try:
                result = run_summarization.load_prompt_template(f.name, '.')
                assert 'Test prompt content' in result
                assert '{input_file}' in result
            finally:
                os.unlink(f.name)

    def test_uses_default_when_none_specified(self):
        """Should use get_default_prompt_file() when prompt_file is None."""
        with tempfile.TemporaryDirectory() as tmpdir:
            prompt_path = Path(tmpdir) / 'prompt.txt'
            prompt_path.write_text('Default workspace prompt')

            result = run_summarization.load_prompt_template(None, tmpdir)

            assert result == 'Default workspace prompt'

    def test_exits_on_missing_file(self):
        """Should sys.exit(1) if prompt file doesn't exist."""
        with pytest.raises(SystemExit) as exc_info:
            run_summarization.load_prompt_template('/nonexistent/prompt.txt', '.')

        assert exc_info.value.code == 1


class TestCalendarFreshness:
    def test_uses_configured_calendar_outside_workspace(self):
        with tempfile.TemporaryDirectory() as workspace:
            configured = Path(workspace).parent / 'shared-calendar.org'
            with mock.patch.dict(
                os.environ,
                {'CALENDAR_PATH': str(configured)},
                clear=False,
            ):
                assert (
                    run_summarization.get_calendar_path(workspace)
                    == os.path.abspath(configured)
                )

    def test_defaults_calendar_to_workspace(self):
        with tempfile.TemporaryDirectory() as workspace:
            with mock.patch.dict(os.environ, {}, clear=False):
                os.environ.pop('CALENDAR_PATH', None)
                assert run_summarization.get_calendar_path(workspace) == str(
                    Path(workspace) / 'calendar.org'
                )

    def test_accepts_recent_non_git_calendar(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            calendar = Path(tmpdir) / 'calendar.org'
            calendar.write_text('* Current meeting\n')
            now = calendar.stat().st_mtime + 60

            fresh, age_seconds = run_summarization.calendar_is_fresh(
                str(calendar), now=now
            )

            assert fresh is True
            assert age_seconds == pytest.approx(60)

    def test_rejects_stale_non_git_calendar(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            calendar = Path(tmpdir) / 'calendar.org'
            calendar.write_text('* Old meeting\n')
            now = (
                calendar.stat().st_mtime
                + run_summarization.CALENDAR_MAX_AGE_SECONDS
                + 1
            )

            fresh, age_seconds = run_summarization.calendar_is_fresh(
                str(calendar), now=now
            )

            assert fresh is False
            assert age_seconds > run_summarization.CALENDAR_MAX_AGE_SECONDS

    def test_rejects_calendar_timestamp_far_in_future(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            calendar = Path(tmpdir) / 'calendar.org'
            calendar.write_text('* Future meeting\n')
            now = calendar.stat().st_mtime - 301

            fresh, _ = run_summarization.calendar_is_fresh(
                str(calendar), now=now
            )

            assert fresh is False

    def test_rejects_untracked_calendar_inside_git_repo(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            subprocess.run(['git', 'init', '-q'], cwd=tmpdir, check=True)
            calendar = Path(tmpdir) / 'calendar.org'
            calendar.write_text('* Untracked meeting\n')

            fresh, _ = run_summarization.calendar_is_fresh(
                str(calendar), now=calendar.stat().st_mtime + 60
            )

            assert fresh is False

    def test_rejects_dirty_calendar_inside_git_repo(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            subprocess.run(['git', 'init', '-q'], cwd=tmpdir, check=True)
            calendar = Path(tmpdir) / 'calendar.org'
            calendar.write_text('* Committed meeting\n')
            subprocess.run(['git', 'add', 'calendar.org'], cwd=tmpdir, check=True)
            subprocess.run(
                [
                    'git',
                    '-c',
                    'user.name=Test User',
                    '-c',
                    'user.email=test@example.com',
                    'commit',
                    '-q',
                    '-m',
                    'Add calendar',
                ],
                cwd=tmpdir,
                check=True,
            )
            calendar.write_text('* Uncommitted meeting\n')

            fresh, _ = run_summarization.calendar_is_fresh(
                str(calendar), now=calendar.stat().st_mtime + 60
            )

            assert fresh is False


class TestWorkspaceArgumentParsing:
    """Tests for workspace argument handling in run_summarization()."""

    def test_cli_argument_takes_precedence_over_env(self):
        """--workspace CLI arg should override WORKSPACE_DIR env var."""
        with tempfile.TemporaryDirectory() as cli_workspace:
            with tempfile.TemporaryDirectory() as env_workspace:
                # Create required directories in CLI workspace
                (Path(cli_workspace) / 'inbox').mkdir()
                (Path(cli_workspace) / 'transcripts').mkdir()
                (Path(cli_workspace) / 'notes').mkdir()
                
                # Create prompt.txt to avoid fallback issues
                (Path(cli_workspace) / 'prompt.txt').write_text('test {input_file} {output_file}')
                
                with mock.patch.dict(os.environ, {'WORKSPACE_DIR': env_workspace}):
                    with mock.patch('sys.argv', ['run_summarization.py', '--workspace', cli_workspace]):
                        with mock.patch.object(run_summarization, 'process_inbox', return_value=(1, 0)) as mock_process:
                            with pytest.raises(SystemExit) as exc_info:
                                run_summarization.run_summarization()
                            
                            assert exc_info.value.code == 0  # Success exit
                            # Check that process_inbox was called with CLI workspace
                            call_args = mock_process.call_args
                            paths = call_args[0][0]  # First positional arg is paths
                            assert paths['workspace'] == cli_workspace

    def test_env_var_used_when_no_cli_arg(self):
        """WORKSPACE_DIR env var should be used when --workspace not specified."""
        with tempfile.TemporaryDirectory() as env_workspace:
            # Create required directories
            (Path(env_workspace) / 'inbox').mkdir()
            (Path(env_workspace) / 'transcripts').mkdir()
            (Path(env_workspace) / 'notes').mkdir()
            (Path(env_workspace) / 'prompt.txt').write_text('test {input_file} {output_file}')
            
            with mock.patch.dict(os.environ, {'WORKSPACE_DIR': env_workspace}):
                with mock.patch('sys.argv', ['run_summarization.py']):
                    with mock.patch.object(run_summarization, 'process_inbox', return_value=(1, 0)) as mock_process:
                        with pytest.raises(SystemExit) as exc_info:
                            run_summarization.run_summarization()
                        
                        assert exc_info.value.code == 0  # Success exit
                        call_args = mock_process.call_args
                        paths = call_args[0][0]
                        assert paths['workspace'] == env_workspace

    def test_defaults_to_current_dir(self):
        """Should default to '.' when neither CLI arg nor env var set."""
        # Clear WORKSPACE_DIR if set
        env = os.environ.copy()
        env.pop('WORKSPACE_DIR', None)
        
        with mock.patch.dict(os.environ, env, clear=True):
            with mock.patch('sys.argv', ['run_summarization.py']):
                with mock.patch.object(run_summarization, 'process_inbox', return_value=(1, 0)) as mock_process:
                    # Mock os.path.exists to avoid directory creation issues
                    with mock.patch('os.path.exists', return_value=True):
                        with mock.patch('os.makedirs'):
                            # Mock load_prompt_template to avoid file issues
                            with mock.patch.object(run_summarization, 'load_prompt_template', return_value='test'):
                                with mock.patch.object(
                                    run_summarization,
                                    'calendar_is_fresh',
                                    return_value=(True, 0),
                                ):
                                    with pytest.raises(SystemExit) as exc_info:
                                        run_summarization.run_summarization()
                                
                                assert exc_info.value.code == 0  # Success exit
                                call_args = mock_process.call_args
                                paths = call_args[0][0]
                                assert paths['workspace'] == '.'


class TestProcessInbox:
    """Tests for process_inbox() function."""

    def test_returns_early_if_inbox_missing(self, capsys):
        """Should print error and return if inbox directory doesn't exist."""
        with tempfile.TemporaryDirectory() as tmpdir:
            paths = run_summarization.get_workspace_paths(tmpdir)
            # Don't create inbox directory
            
            result = run_summarization.process_inbox(paths, prompt_template='test')
            
            assert result == (0, 1)  # Missing inbox counts as a failure
            captured = capsys.readouterr()
            assert 'not found' in captured.out.lower() or 'directory' in captured.out.lower()

    def test_returns_early_if_no_transcripts(self, capsys):
        """Should print message and return if inbox is empty."""
        with tempfile.TemporaryDirectory() as tmpdir:
            paths = run_summarization.get_workspace_paths(tmpdir)
            # Create empty inbox
            os.makedirs(paths['inbox'])
            
            result = run_summarization.process_inbox(paths, prompt_template='test')
            
            assert result == (0, 0)  # No files is not a failure, just nothing to process
            captured = capsys.readouterr()
            assert 'no transcript' in captured.out.lower()

    def test_finds_txt_and_md_files(self):
        """Should find both .txt and .md files in inbox."""
        with tempfile.TemporaryDirectory() as tmpdir:
            paths = run_summarization.get_workspace_paths(tmpdir)
            os.makedirs(paths['inbox'])
            os.makedirs(paths['transcripts'])
            os.makedirs(paths['notes'])
            
            # Create test files (body must be >= 200 chars to pass junk filter)
            long_body = 'Discussion about project work and planning. ' * 10
            (Path(paths['inbox']) / 'test1.txt').write_text(long_body)
            (Path(paths['inbox']) / 'test2.md').write_text(long_body)
            (Path(paths['inbox']) / 'ignore.json').write_text('{}')  # Should be ignored
            
            with mock.patch.object(run_summarization, 'process_transcript') as mock_process:
                mock_process.return_value = (False, None, None)  # Simulate failure to avoid file moves
                
                run_summarization.process_inbox(paths, prompt_template='test')
                
                # Should have been called twice (txt and md, not json)
                assert mock_process.call_count == 2


class TestGitCommitChanges:
    """Tests for git_commit_changes() function."""

    def test_converts_paths_to_relative(self):
        """Should convert absolute paths to workspace-relative paths for git."""
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = tmpdir
            
            # Create test files with absolute paths
            inbox_file = os.path.join(workspace, 'inbox', 'test.txt')
            transcript_file = os.path.join(workspace, 'transcripts', 'test.txt')
            org_file = os.path.join(workspace, 'notes', 'test.org')
            
            with mock.patch('subprocess.run') as mock_run:
                mock_run.return_value = mock.Mock(returncode=0, stderr='')
                
                run_summarization.git_commit_changes(
                    [inbox_file],
                    [transcript_file],
                    [org_file],
                    workspace
                )
                
                # Check that git commands were called with relative paths
                calls = mock_run.call_args_list
                for call in calls:
                    cmd = call[0][0]
                    if 'add' in cmd:
                        # Paths in git add should be relative
                        for arg in cmd[2:]:  # Skip 'git' and 'add'
                            assert not arg.startswith('/'), f"Path should be relative: {arg}"


class TestExtractSlugFromOrg:
    """Tests for extract_slug_from_org() function."""

    def test_extracts_slug_from_property_drawer(self):
        """Should extract slug from :SLUG: property."""
        with tempfile.NamedTemporaryFile(mode='w', suffix='.org', delete=False) as f:
            f.write("""** Test Meeting :note:transcribed:
:PROPERTIES:
:PARTICIPANTS: Alice, Bob
:TOPIC: Test
:SLUG: quarterly-planning
:END:

TL;DR: Test meeting.
""")
            f.flush()
            
            try:
                result = run_summarization.extract_slug_from_org(f.name)
                assert result == 'quarterly-planning'
            finally:
                os.unlink(f.name)

    def test_returns_meeting_on_missing_slug(self):
        """Should return 'meeting' if no slug found."""
        with tempfile.NamedTemporaryFile(mode='w', suffix='.org', delete=False) as f:
            f.write("""** Test Meeting
No property drawer here.
""")
            f.flush()
            
            try:
                result = run_summarization.extract_slug_from_org(f.name)
                assert result == 'meeting'
            finally:
                os.unlink(f.name)

    def test_extracts_first_valid_portion_of_slug(self):
        """Regex captures valid chars only, so spaces/special chars truncate slug."""
        with tempfile.NamedTemporaryFile(mode='w', suffix='.org', delete=False) as f:
            # Spaces break the slug - regex only captures [a-z0-9-]+
            f.write("""** Test
:PROPERTIES:
:SLUG: valid-part invalid!
:END:
""")
            f.flush()
            
            try:
                result = run_summarization.extract_slug_from_org(f.name)
                assert result == 'valid-part'  # Regex stops at space
            finally:
                os.unlink(f.name)

    def test_returns_meeting_for_empty_slug(self):
        """Should return 'meeting' if slug value is empty/whitespace."""
        with tempfile.NamedTemporaryFile(mode='w', suffix='.org', delete=False) as f:
            f.write("""** Test
:PROPERTIES:
:SLUG: !!!
:END:
""")
            f.flush()
            
            try:
                result = run_summarization.extract_slug_from_org(f.name)
                assert result == 'meeting'  # No valid chars to match
            finally:
                os.unlink(f.name)


class TestEnsureUniqueFilename:
    """Tests for ensure_unique_filename() function."""

    def test_returns_base_name_if_not_exists(self):
        """Should return simple filename if it doesn't exist."""
        with tempfile.TemporaryDirectory() as tmpdir:
            result = run_summarization.ensure_unique_filename(tmpdir, '20251230-test', 'txt')
            
            assert result == os.path.join(tmpdir, '20251230-test.txt')

    def test_appends_counter_if_exists(self):
        """Should append counter if file already exists."""
        with tempfile.TemporaryDirectory() as tmpdir:
            # Create existing file
            existing = Path(tmpdir) / '20251230-test.txt'
            existing.write_text('existing')
            
            result = run_summarization.ensure_unique_filename(tmpdir, '20251230-test', 'txt')
            
            assert result == os.path.join(tmpdir, '20251230-test-1.txt')

    def test_increments_counter_for_multiple_collisions(self):
        """Should keep incrementing counter until unique name found."""
        with tempfile.TemporaryDirectory() as tmpdir:
            # Create multiple existing files
            (Path(tmpdir) / '20251230-test.txt').write_text('1')
            (Path(tmpdir) / '20251230-test-1.txt').write_text('2')
            (Path(tmpdir) / '20251230-test-2.txt').write_text('3')
            
            result = run_summarization.ensure_unique_filename(tmpdir, '20251230-test', 'txt')
            
            assert result == os.path.join(tmpdir, '20251230-test-3.txt')


class TestBuildCalendarAwarePrompt:
    """Tests for build_calendar_aware_prompt() function."""

    def test_includes_calendar_context_header(self):
        """Should include calendar context section with the meeting date."""
        result = run_summarization.build_calendar_aware_prompt(
            base_prompt='Test prompt',
            calendar_text='1. [15:00-15:30] Edd / Joe',
            meeting_date='2026-01-26',
            notes_context=''
        )
        
        assert 'CALENDAR CONTEXT FOR 2026-01-26' in result
        assert '1. [15:00-15:30] Edd / Joe' in result

    def test_includes_participant_identification_strategy(self):
        """Should include guidance about correcting participant identification."""
        result = run_summarization.build_calendar_aware_prompt(
            base_prompt='Test prompt',
            calendar_text='1. [15:00-15:30] Edd / Joe',
            meeting_date='2026-01-26',
            notes_context=''
        )
        
        # Should include participant identification guidance
        assert 'PARTICIPANT' in result or 'Participant' in result
        assert 'speaker labels' in result.lower() or 'transcript' in result.lower()

    def test_includes_slug_naming_guidance(self):
        """Should include guidance about slug naming for 1:1 meetings."""
        result = run_summarization.build_calendar_aware_prompt(
            base_prompt='Test prompt',
            calendar_text='1. [15:00-15:30] Edd / Joe',
            meeting_date='2026-01-26',
            notes_context=''
        )
        
        # Should include 1:1 slug format guidance
        assert '1-1' in result or 'firstname-edd' in result.lower()

    def test_includes_calendar_metadata_guidance(self):
        """Should include guidance about adding calendar metadata properties."""
        result = run_summarization.build_calendar_aware_prompt(
            base_prompt='Test prompt',
            calendar_text='1. [15:00-15:30] Edd / Joe',
            meeting_date='2026-01-26',
            notes_context=''
        )
        
        # Should mention calendar metadata properties
        assert ':CALENDAR_TITLE:' in result or ':CALENDAR_TIME:' in result

    def test_prepends_to_base_prompt(self):
        """Should prepend calendar instructions before the base prompt."""
        result = run_summarization.build_calendar_aware_prompt(
            base_prompt='Test prompt',
            calendar_text='1. [15:00-15:30] Edd / Joe',
            meeting_date='2026-01-26',
            notes_context=''
        )
        
        # Calendar context should come before base prompt
        assert 'CALENDAR CONTEXT' in result
        assert 'Test prompt' in result
        assert result.index('CALENDAR CONTEXT') < result.index('Test prompt')

    def test_includes_notes_context_when_provided(self):
        """Should include notes context in the prompt when provided."""
        result = run_summarization.build_calendar_aware_prompt(
            base_prompt='Test prompt',
            calendar_text='1. [15:00-15:30] Edd / Joe',
            meeting_date='2026-01-26',
            notes_context='Past meeting with Joe discussed project updates'
        )
        
        assert 'Past meeting with Joe discussed project updates' in result

    def test_distinguishes_generic_and_explicit_speaker_markers(self):
        """Should explain that [S] is generic while [speaker:Edd] is trusted."""
        result = run_summarization.build_calendar_aware_prompt(
            base_prompt='Test prompt',
            calendar_text='1. [15:00-15:30] Edd / Joe',
            meeting_date='2026-01-26',
            notes_context=''
        )

        assert '`[S]`' in result
        assert '`[speaker:Edd]`' in result


class TestCaptureModeGuidance:
    def test_room_capture_reconciles_oversplit_speakers(self):
        result = run_summarization.add_capture_mode_guidance(
            "Base prompt",
            {"capture_mode": "room"},
        )

        assert "ROOM MICROPHONE CAPTURE" in result
        assert "split one person" in result
        assert "volume alone is not enough" in result
        assert "smallest participant set" in result
        assert result.endswith("Base prompt")

    def test_standard_capture_does_not_add_room_guidance(self):
        assert run_summarization.add_capture_mode_guidance(
            "Base prompt",
            {"capture_mode": "standard"},
        ) == "Base prompt"


def test_split_transcript_preserves_room_capture_mode(tmp_path):
    source = tmp_path / "meeting.txt"
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    source.write_text(
        "---\n"
        "meeting_start: 2026-08-13T09:00:00\n"
        "meeting_end: 2026-08-13T10:00:00\n"
        "recording_source: transcriber\n"
        "capture_mode: room\n"
        "---\n\n"
        "First meeting. Second meeting.",
        encoding="utf-8",
    )

    parts = run_summarization.split_transcript(
        str(source),
        [len("First meeting.")],
        {"inbox": str(inbox)},
    )

    assert len(parts) == 2
    for part in parts:
        assert run_summarization.parse_transcript_header(part)["capture_mode"] == "room"


class TestParseTranscriptHeader:
    """Tests for parse_transcript_header() function."""

    def test_returns_empty_dict_when_no_header(self):
        """Should return {} for transcripts without YAML front matter."""
        with tempfile.NamedTemporaryFile(mode='w', suffix='.txt', delete=False) as f:
            f.write("Just a regular transcript.\nNo header here.")
            f.flush()
            try:
                result = run_summarization.parse_transcript_header(f.name)
                assert result == {}
            finally:
                os.unlink(f.name)

    def test_parses_full_header(self):
        """Should parse a complete YAML front matter header."""
        with tempfile.NamedTemporaryFile(mode='w', suffix='.txt', delete=False) as f:
            f.write("---\nmeeting_start: 2026-02-05T14:00:00-08:00\n"
                    "meeting_end: 2026-02-05T15:03:00-08:00\n"
                    "recording_source: transcriber\n---\n\nHello world transcript")
            f.flush()
            try:
                result = run_summarization.parse_transcript_header(f.name)
                assert 'meeting_start' in result
                assert 'meeting_end' in result
                assert result['recording_source'] == 'transcriber'
                assert isinstance(result['meeting_start'], str)
                assert isinstance(result['meeting_end'], str)
            finally:
                os.unlink(f.name)

    def test_parses_partial_header(self):
        """Should parse header with only some fields."""
        with tempfile.NamedTemporaryFile(mode='w', suffix='.txt', delete=False) as f:
            f.write("---\nrecording_source: macwhisper\n---\n\nTranscript text")
            f.flush()
            try:
                result = run_summarization.parse_transcript_header(f.name)
                assert result['recording_source'] == 'macwhisper'
                assert 'meeting_start' not in result
            finally:
                os.unlink(f.name)

    def test_returns_empty_on_malformed_yaml(self):
        """Should return {} if YAML is malformed."""
        with tempfile.NamedTemporaryFile(mode='w', suffix='.txt', delete=False) as f:
            f.write("---\n: : : bad yaml [[\n---\n\nTranscript")
            f.flush()
            try:
                result = run_summarization.parse_transcript_header(f.name)
                assert result == {}
            finally:
                os.unlink(f.name)

    def test_returns_empty_when_no_closing_delimiter(self):
        """Should return {} if closing --- is missing."""
        with tempfile.NamedTemporaryFile(mode='w', suffix='.txt', delete=False) as f:
            f.write("---\nmeeting_start: 2026-02-05T14:00:00\nno closing delimiter here")
            f.flush()
            try:
                result = run_summarization.parse_transcript_header(f.name)
                assert result == {}
            finally:
                os.unlink(f.name)

    def test_normalizes_datetime_objects_to_strings(self):
        """YAML may parse ISO timestamps as datetime objects; should normalize to strings."""
        with tempfile.NamedTemporaryFile(mode='w', suffix='.txt', delete=False) as f:
            f.write("---\nmeeting_start: 2026-02-05 14:00:00\n"
                    "meeting_end: 2026-02-05 15:03:00\n---\n\nText")
            f.flush()
            try:
                result = run_summarization.parse_transcript_header(f.name)
                assert isinstance(result['meeting_start'], str)
                assert isinstance(result['meeting_end'], str)
            finally:
                os.unlink(f.name)


class TestGetTranscriptBody:
    """Tests for get_transcript_body() function."""

    def test_returns_full_content_without_header(self):
        """Should return full content for files without YAML front matter."""
        with tempfile.NamedTemporaryFile(mode='w', suffix='.txt', delete=False) as f:
            f.write("Just transcript text.\nSecond line.")
            f.flush()
            try:
                result = run_summarization.get_transcript_body(f.name)
                assert result == "Just transcript text.\nSecond line."
            finally:
                os.unlink(f.name)

    def test_strips_header_and_returns_body(self):
        """Should strip YAML front matter and return only the transcript body."""
        with tempfile.NamedTemporaryFile(mode='w', suffix='.txt', delete=False) as f:
            f.write("---\nmeeting_start: 2026-02-05T14:00:00\n---\n\nThe actual transcript.")
            f.flush()
            try:
                result = run_summarization.get_transcript_body(f.name)
                assert result == "The actual transcript."
                assert '---' not in result
                assert 'meeting_start' not in result
            finally:
                os.unlink(f.name)

    def test_returns_full_content_when_no_closing_delimiter(self):
        """Should return full content if closing --- is missing."""
        with tempfile.NamedTemporaryFile(mode='w', suffix='.txt', delete=False) as f:
            content = "---\nmeeting_start: 2026-02-05T14:00:00\nJust continues"
            f.write(content)
            f.flush()
            try:
                result = run_summarization.get_transcript_body(f.name)
                assert result == content
            finally:
                os.unlink(f.name)


class TestTimeOverlaps:
    """Tests for time_overlaps() function."""

    def test_overlapping_entries(self):
        """Calendar 14:00-15:00 should overlap with meeting 14:00-15:03."""
        cal = {'start_time': '14:00', 'end_time': '15:00'}
        start = datetime(2026, 2, 5, 14, 0)
        end = datetime(2026, 2, 5, 15, 3)
        assert run_summarization.time_overlaps(cal, start, end) is True

    def test_non_overlapping_entries(self):
        """Calendar 09:00-10:00 should not overlap with meeting 14:00-15:00."""
        cal = {'start_time': '09:00', 'end_time': '10:00'}
        start = datetime(2026, 2, 5, 14, 0)
        end = datetime(2026, 2, 5, 15, 0)
        assert run_summarization.time_overlaps(cal, start, end) is False

    def test_all_day_events_always_match(self):
        """All-day events (no start/end time) should always overlap."""
        cal = {'start_time': None, 'end_time': None}
        start = datetime(2026, 2, 5, 14, 0)
        end = datetime(2026, 2, 5, 15, 0)
        assert run_summarization.time_overlaps(cal, start, end) is True

    def test_tolerance_on_boundaries(self):
        """Should match within 5-minute tolerance."""
        cal = {'start_time': '15:00', 'end_time': '16:00'}
        start = datetime(2026, 2, 5, 14, 0)
        end = datetime(2026, 2, 5, 14, 57)
        assert run_summarization.time_overlaps(cal, start, end) is True

    def test_no_tolerance_for_far_apart(self):
        """Should not match when times are more than 5 min apart."""
        cal = {'start_time': '15:00', 'end_time': '16:00'}
        start = datetime(2026, 2, 5, 13, 0)
        end = datetime(2026, 2, 5, 14, 50)
        assert run_summarization.time_overlaps(cal, start, end) is False

    def test_meeting_contained_within_calendar(self):
        """Meeting fully inside calendar slot should overlap."""
        cal = {'start_time': '13:00', 'end_time': '15:00'}
        start = datetime(2026, 2, 5, 13, 30)
        end = datetime(2026, 2, 5, 14, 30)
        assert run_summarization.time_overlaps(cal, start, end) is True

    def test_calendar_contained_within_meeting(self):
        """Calendar slot fully inside meeting window should overlap."""
        cal = {'start_time': '14:00', 'end_time': '14:30'}
        start = datetime(2026, 2, 5, 13, 0)
        end = datetime(2026, 2, 5, 16, 0)
        assert run_summarization.time_overlaps(cal, start, end) is True


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
