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
import sys
import tempfile
from pathlib import Path
from unittest import mock

import pytest

# Add parent directory to path so we can import run_summarization
sys.path.insert(0, str(Path(__file__).parent.parent))

import run_summarization


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
            # Create a prompt file in workspace
            prompt_path = Path(tmpdir) / 'prompt.txt'
            prompt_path.write_text('Default workspace prompt')
            
            result = run_summarization.load_prompt_template(None, tmpdir)
            
            assert result == 'Default workspace prompt'

    def test_exits_on_missing_file(self):
        """Should sys.exit(1) if prompt file doesn't exist."""
        with pytest.raises(SystemExit) as exc_info:
            run_summarization.load_prompt_template('/nonexistent/prompt.txt', '.')
        
        assert exc_info.value.code == 1


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
                        with mock.patch.object(run_summarization, 'process_inbox') as mock_process:
                            run_summarization.run_summarization()
                            
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
                    with mock.patch.object(run_summarization, 'process_inbox') as mock_process:
                        run_summarization.run_summarization()
                        
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
                with mock.patch.object(run_summarization, 'process_inbox') as mock_process:
                    # Mock os.path.exists to avoid directory creation issues
                    with mock.patch('os.path.exists', return_value=True):
                        with mock.patch('os.makedirs'):
                            # Mock load_prompt_template to avoid file issues
                            with mock.patch.object(run_summarization, 'load_prompt_template', return_value='test'):
                                run_summarization.run_summarization()
                                
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
            
            assert result is None
            captured = capsys.readouterr()
            assert 'not found' in captured.out.lower() or 'directory' in captured.out.lower()

    def test_returns_early_if_no_transcripts(self, capsys):
        """Should print message and return if inbox is empty."""
        with tempfile.TemporaryDirectory() as tmpdir:
            paths = run_summarization.get_workspace_paths(tmpdir)
            # Create empty inbox
            os.makedirs(paths['inbox'])
            
            result = run_summarization.process_inbox(paths, prompt_template='test')
            
            assert result is None
            captured = capsys.readouterr()
            assert 'no transcript' in captured.out.lower()

    def test_finds_txt_and_md_files(self):
        """Should find both .txt and .md files in inbox."""
        with tempfile.TemporaryDirectory() as tmpdir:
            paths = run_summarization.get_workspace_paths(tmpdir)
            os.makedirs(paths['inbox'])
            os.makedirs(paths['transcripts'])
            os.makedirs(paths['notes'])
            
            # Create test files
            (Path(paths['inbox']) / 'test1.txt').write_text('transcript 1')
            (Path(paths['inbox']) / 'test2.md').write_text('transcript 2')
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


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
