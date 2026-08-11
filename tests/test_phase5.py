#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "pytest>=8.0.0",
#     "flask>=3.0.0",
#     "pyyaml>=6.0.0",
#     "requests>=2.31.0",
# ]
# ///
"""
Tests for Phase 5: Always-On Agent/Daemon functionality.

Covers:
- FR-5.2: Sync on startup and before webhook processing
- FR-5.3: Optional workflow_dispatch triggering
- FR-5.4: Optional hook command execution on new commits
- FR-5.5: Safe failure handling (no partial writes)
- FR-5.6: Auto-clone when data repo is missing

Run with: uv run test_phase5.py
"""

import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from unittest import mock

import pytest
import yaml

# Add parent directory to sys.path to import meetingnotesd
sys.path.insert(0, str(Path(__file__).parent.parent))
import meetingnotesd


@pytest.fixture
def temp_workspace():
    """Create a temporary workspace with a mock git repo."""
    with tempfile.TemporaryDirectory() as tmpdir:
        workspace = Path(tmpdir)
        data_repo = workspace / "data-repo"
        data_repo.mkdir()
        
        # Initialize a git repo
        subprocess.run(["git", "init"], cwd=data_repo, capture_output=True, check=True)
        subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=data_repo, capture_output=True)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=data_repo, capture_output=True)
        
        # Create inbox directory and initial commit
        inbox = data_repo / "inbox"
        inbox.mkdir()
        (inbox / ".gitkeep").touch()
        subprocess.run(["git", "add", "."], cwd=data_repo, capture_output=True, check=True)
        subprocess.run(["git", "commit", "-m", "Initial"], cwd=data_repo, capture_output=True, check=True)
        
        yield {
            "workspace": workspace,
            "data_repo": data_repo,
            "inbox": inbox,
        }


@pytest.fixture
def minimal_config(temp_workspace):
    """Return a minimal config dict for testing."""
    return {
        "server": {"host": "127.0.0.1", "port": 19876},
        "data_repo": str(temp_workspace["data_repo"]),
        "git": {
            "auto_commit": True,
            "auto_push": False,
            "repository_url": "https://github.com/test/repo.git",
            "commit_message_template": "Add: {title}",
            "branch": "main",
            "remote": "origin",
        },
        "sync": {
            "enabled": True,
            "on_startup": True,
            "before_accepting_webhooks": True,
            "ff_only": True,
        },
    }


@pytest.fixture
def repo_agent(minimal_config):
    """Create a RepoAgent instance with the test config."""
    return meetingnotesd.RepoAgent(minimal_config)


class TestRepoBootstrap:
    """FR-5.6: Auto-clone when data repo is missing."""

    def test_ensure_repo_checkout_exists(self, repo_agent, temp_workspace):
        """When repo already exists, ensure_repo_checkout is a no-op."""
        # Repo already exists from fixture
        repo_agent.ensure_repo_checkout()
        # Should not raise, .git should still exist
        assert (temp_workspace["data_repo"] / ".git").exists()

    def test_ensure_repo_checkout_clones_when_missing(self, minimal_config, temp_workspace):
        """When repo dir is missing, clone it from configured URL."""
        import meetingnotesd
        
        # Point to a non-existent directory
        missing_repo = temp_workspace["workspace"] / "new-data-repo"
        minimal_config["data_repo"] = str(missing_repo)
        
        agent = meetingnotesd.RepoAgent(minimal_config)
        
        # Mock subprocess.run to simulate successful clone
        with mock.patch("subprocess.run") as mock_run:
            mock_run.return_value = mock.Mock(returncode=0, stdout="", stderr="")
            
            agent.ensure_repo_checkout()
            
            # Verify git clone was called with correct args
            mock_run.assert_called_once()
            call_args = mock_run.call_args
            cmd = call_args[0][0]
            assert cmd[0] == "git"
            assert cmd[1] == "clone"
            assert "--branch" in cmd
            assert "main" in cmd
            assert "https://github.com/test/repo.git" in cmd

    def test_ensure_repo_checkout_fails_without_url(self, minimal_config, temp_workspace):
        """When repo is missing and no URL configured, raise ValueError."""
        import meetingnotesd
        
        missing_repo = temp_workspace["workspace"] / "new-data-repo"
        minimal_config["data_repo"] = str(missing_repo)
        minimal_config["git"]["repository_url"] = None
        
        agent = meetingnotesd.RepoAgent(minimal_config)
        
        with pytest.raises(ValueError, match="no git.repository_url configured"):
            agent.ensure_repo_checkout()


class TestSyncRepo:
    """FR-5.2: Sync on startup and before webhook processing."""

    def test_sync_repo_when_disabled(self, minimal_config, temp_workspace):
        """When sync is disabled, sync_repo returns early."""
        import meetingnotesd
        
        minimal_config["sync"]["enabled"] = False
        agent = meetingnotesd.RepoAgent(minimal_config)
        
        changed, message = agent.sync_repo()
        assert changed is False
        assert "disabled" in message.lower()

    def test_sync_repo_already_up_to_date(self, repo_agent, temp_workspace):
        """When no new commits, sync reports already up to date."""
        # Create a "remote" by making a bare clone, then set it as origin
        data_repo = temp_workspace["data_repo"]
        bare_repo = temp_workspace["workspace"] / "bare.git"
        subprocess.run(["git", "clone", "--bare", str(data_repo), str(bare_repo)], capture_output=True, check=True)
        subprocess.run(["git", "remote", "add", "origin", str(bare_repo)], cwd=data_repo, capture_output=True)
        
        changed, message = repo_agent.sync_repo()
        assert changed is False
        assert "up to date" in message.lower()

    def test_sync_repo_detects_new_commits(self, minimal_config, temp_workspace):
        """When remote has new commits, sync pulls them and reports changed."""
        import meetingnotesd
        
        data_repo = temp_workspace["data_repo"]
        
        # Create a bare "remote"
        bare_repo = temp_workspace["workspace"] / "bare.git"
        subprocess.run(["git", "clone", "--bare", str(data_repo), str(bare_repo)], capture_output=True, check=True)
        subprocess.run(["git", "remote", "add", "origin", str(bare_repo)], cwd=data_repo, capture_output=True)
        
        # Clone to a second working copy, make a commit, push to bare
        second_clone = temp_workspace["workspace"] / "second"
        subprocess.run(["git", "clone", str(bare_repo), str(second_clone)], capture_output=True, check=True)
        subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=second_clone, capture_output=True)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=second_clone, capture_output=True)
        (second_clone / "newfile.txt").write_text("hello")
        subprocess.run(["git", "add", "newfile.txt"], cwd=second_clone, capture_output=True, check=True)
        subprocess.run(["git", "commit", "-m", "New commit"], cwd=second_clone, capture_output=True, check=True)
        subprocess.run(["git", "push", "origin", "main"], cwd=second_clone, capture_output=True, check=True)
        
        # Now sync our original data_repo
        agent = meetingnotesd.RepoAgent(minimal_config)
        changed, message = agent.sync_repo()
        
        assert changed is True
        assert "new commits" in message.lower()
        # The new file should now exist
        assert (data_repo / "newfile.txt").exists()


class TestHookExecution:
    """FR-5.4: Optional hook command execution on new commits."""

    def test_hook_disabled_by_default(self, repo_agent):
        """When hook is not enabled, _run_hook_on_new_commits returns early."""
        success, message = repo_agent._run_hook_on_new_commits()
        assert success is False
        assert "disabled" in message.lower()

    def test_hook_runs_command_on_success(self, minimal_config, temp_workspace):
        """When hook is enabled, it runs the configured command."""
        import meetingnotesd
        
        # Configure a simple hook command
        minimal_config["hooks"] = {
            "on_new_commits": {
                "enabled": True,
                "command": "echo hook-ran",
                "working_directory": ".",
                "timeout_seconds": 10,
            }
        }
        agent = meetingnotesd.RepoAgent(minimal_config)
        
        with mock.patch("subprocess.run") as mock_run:
            mock_run.return_value = mock.Mock(returncode=0, stdout="hook-ran\n", stderr="")
            
            success, message = agent._run_hook_on_new_commits()
            
            assert success is True
            assert "completed" in message.lower()
            mock_run.assert_called_once()
            call_args = mock_run.call_args
            assert call_args[0][0] == ["echo", "hook-ran"]

    def test_hook_reports_failure(self, minimal_config, temp_workspace):
        """When hook command fails, it reports the failure."""
        import meetingnotesd
        
        minimal_config["hooks"] = {
            "on_new_commits": {
                "enabled": True,
                "command": "false",  # Always fails
                "working_directory": ".",
                "timeout_seconds": 10,
            }
        }
        agent = meetingnotesd.RepoAgent(minimal_config)
        
        with mock.patch("subprocess.run") as mock_run:
            mock_run.return_value = mock.Mock(returncode=1, stdout="", stderr="command failed")
            
            success, message = agent._run_hook_on_new_commits()
            
            assert success is False
            assert "failed" in message.lower()


class TestStandaloneProcessing:
    """Tests for local standalone processing mode."""

    def test_standalone_processing_sets_workspace_and_cwd(self, minimal_config, temp_workspace):
        """Standalone processing runs in the configured cwd with WORKSPACE_DIR pointing at the data repo."""
        import meetingnotesd

        processor_dir = temp_workspace["workspace"] / "processor"
        processor_dir.mkdir()
        marker = temp_workspace["workspace"] / "standalone-marker.txt"

        minimal_config["sync"]["enabled"] = False
        minimal_config["processing"] = {
            "standalone": {
                "enabled": True,
                "command": (
                    f"{sys.executable} -c "
                    "\"import os,pathlib,sys; "
                    "pathlib.Path(sys.argv[1]).write_text(os.environ['WORKSPACE_DIR'] + chr(10) + os.getcwd())\" "
                    f"{marker}"
                ),
                "working_directory": str(processor_dir),
                "timeout_seconds": 10,
                "async": False,
            }
        }

        agent = meetingnotesd.RepoAgent(minimal_config)
        success, message = agent.run_standalone_processing()

        assert success is True
        assert "completed" in message.lower()
        workspace_dir, cwd = marker.read_text().splitlines()
        assert workspace_dir == str(temp_workspace["data_repo"].resolve())
        assert Path(cwd).resolve() == processor_dir.resolve()

    def test_standalone_processing_disabled_returns_early(self, repo_agent):
        """When standalone mode is disabled, processing is not run."""
        success, message = repo_agent.run_standalone_processing()

        assert success is False
        assert "disabled" in message.lower()

    def test_async_standalone_processing_coalesces_concurrent_runs(self, minimal_config, temp_workspace):
        """A second async trigger skips while processing is already active."""
        import meetingnotesd

        minimal_config["sync"]["enabled"] = False
        minimal_config["processing"] = {
            "standalone": {
                "enabled": True,
                "command": "unused",
                "async": True,
            }
        }
        agent = meetingnotesd.RepoAgent(minimal_config)
        calls = []

        def fake_processing():
            calls.append("run")
            time.sleep(0.2)
            return True, "ok"

        agent.run_standalone_processing = fake_processing

        agent.run_standalone_processing_async()
        agent.run_standalone_processing_async()
        agent._processing_thread.join(timeout=2)

        assert calls == ["run"]


class TestWorkflowDispatch:
    """FR-5.3: Optional workflow_dispatch triggering."""

    def test_workflow_dispatch_disabled_by_default(self, repo_agent):
        """When workflow_dispatch is not enabled, returns early."""
        success, message = repo_agent.maybe_dispatch_workflow(reason="test")
        assert success is False
        assert "disabled" in message.lower()

    def test_workflow_dispatch_requires_token(self, minimal_config, temp_workspace):
        """When enabled but no GH_TOKEN, returns error."""
        import meetingnotesd
        
        minimal_config["github"] = {
            "workflow_dispatch": {
                "enabled": True,
                "repo": "owner/repo",
                "workflow": "process.yml",
                "ref": "main",
            }
        }
        agent = meetingnotesd.RepoAgent(minimal_config)
        
        # Ensure no token is set
        with mock.patch.dict(os.environ, {}, clear=True):
            if "GH_TOKEN" in os.environ:
                del os.environ["GH_TOKEN"]
            success, message = agent.maybe_dispatch_workflow(reason="test")
        
        assert success is False
        assert "token" in message.lower()

    def test_workflow_dispatch_makes_api_call(self, minimal_config, temp_workspace):
        """When enabled with token, makes correct API call."""
        import meetingnotesd
        
        minimal_config["github"] = {
            "workflow_dispatch": {
                "enabled": True,
                "repo": "owner/repo",
                "workflow": "process.yml",
                "ref": "main",
                "inputs": {"foo": "bar"},
            }
        }
        agent = meetingnotesd.RepoAgent(minimal_config)
        
        with mock.patch.dict(os.environ, {"GH_TOKEN": "test-token"}):
            with mock.patch("requests.post") as mock_post:
                mock_post.return_value = mock.Mock(status_code=204, text="")
                
                success, message = agent.maybe_dispatch_workflow(reason="test")
                
                assert success is True
                assert "triggered" in message.lower()
                
                # Verify the API call
                mock_post.assert_called_once()
                call_args = mock_post.call_args
                url = call_args[0][0]
                assert "owner/repo" in url
                assert "process.yml" in url
                assert "dispatches" in url
                
                headers = call_args[1]["headers"]
                assert "Bearer test-token" in headers["Authorization"]
                
                payload = call_args[1]["json"]
                assert payload["ref"] == "main"
                assert payload["inputs"]["foo"] == "bar"

    def test_workflow_dispatch_handles_api_failure(self, minimal_config, temp_workspace):
        """When API returns error, reports failure."""
        import meetingnotesd
        
        minimal_config["github"] = {
            "workflow_dispatch": {
                "enabled": True,
                "repo": "owner/repo",
                "workflow": "process.yml",
                "ref": "main",
            }
        }
        agent = meetingnotesd.RepoAgent(minimal_config)
        
        with mock.patch.dict(os.environ, {"GH_TOKEN": "test-token"}):
            with mock.patch("requests.post") as mock_post:
                mock_post.return_value = mock.Mock(status_code=404, text="Not Found")
                
                success, message = agent.maybe_dispatch_workflow(reason="test")
                
                assert success is False
                assert "404" in message


class TestGitCommitAndPush:
    """FR-5.5: Safe failure handling."""

    def test_commit_without_push(self, repo_agent, temp_workspace):
        """When auto_push is False, commit succeeds without pushing."""
        inbox = temp_workspace["inbox"]
        test_file = inbox / "test-transcript.txt"
        test_file.write_text("Test transcript content")
        
        success, message = repo_agent.git_commit(str(test_file), "Test Meeting")
        
        assert success is True
        
        # Verify the file was committed
        result = subprocess.run(
            ["git", "log", "--oneline", "-1"],
            cwd=temp_workspace["data_repo"],
            capture_output=True,
            text=True,
        )
        assert "Add: Test Meeting" in result.stdout

    def test_commit_fails_for_file_outside_repo(self, repo_agent, temp_workspace):
        """When file is outside repo, commit fails gracefully."""
        outside_file = temp_workspace["workspace"] / "outside.txt"
        outside_file.write_text("Outside content")
        
        success, message = repo_agent.git_commit(str(outside_file), "Outside")
        
        assert success is False
        assert "outside" in message.lower() or "not in" in message.lower()


class TestWebhookEndpoint:
    """Integration tests for webhook endpoint with Phase 5 features."""

    @pytest.fixture
    def test_client(self, minimal_config, temp_workspace, monkeypatch):
        """Create a Flask test client with mocked config."""
        # Patch the config loading before importing
        monkeypatch.setattr("meetingnotesd.config", minimal_config)
        
        import meetingnotesd
        
        # Create a new agent with our config
        agent = meetingnotesd.RepoAgent(minimal_config)
        monkeypatch.setattr("meetingnotesd.agent", agent)
        
        meetingnotesd.app.config["TESTING"] = True
        return meetingnotesd.app.test_client()

    def test_health_check_shows_sync_status(self, test_client):
        """Health endpoint reports sync configuration."""
        response = test_client.get("/")
        assert response.status_code == 200
        
        data = response.get_json()
        assert "sync" in data
        assert data["sync"]["enabled"] is True

    def test_webhook_syncs_before_processing(self, test_client, temp_workspace, monkeypatch):
        """Webhook syncs repo before writing file (when enabled)."""
        import meetingnotesd
        
        sync_called = []
        original_sync = meetingnotesd.agent.sync_repo
        
        def mock_sync():
            sync_called.append(True)
            return False, "mocked"
        
        monkeypatch.setattr(meetingnotesd.agent, "sync_repo", mock_sync)
        
        response = test_client.post(
            "/webhook",
            json={"title": "Test", "transcript": "Content here"},
            content_type="application/json",
        )
        
        assert response.status_code == 200
        assert len(sync_called) >= 1  # Sync was called at least once


class TestBackgroundSync:
    """Tests for background sync thread."""

    def test_background_sync_starts_when_configured(self, minimal_config, temp_workspace):
        """Background sync thread starts when poll_interval > 0."""
        import meetingnotesd
        
        minimal_config["sync"]["poll_interval_seconds"] = 60
        agent = meetingnotesd.RepoAgent(minimal_config)
        
        agent.start_background_sync()
        
        assert agent._sync_thread is not None
        assert agent._sync_thread.is_alive()
        
        # Clean up
        agent.stop_background_sync()

    def test_background_sync_does_not_start_when_zero(self, minimal_config, temp_workspace):
        """Background sync thread does not start when poll_interval is 0."""
        import meetingnotesd
        
        minimal_config["sync"]["poll_interval_seconds"] = 0
        agent = meetingnotesd.RepoAgent(minimal_config)
        
        agent.start_background_sync()
        
        assert agent._sync_thread is None


class TestBuildTranscriptHeader:
    """Tests for _build_transcript_header() YAML front matter injection."""

    def test_basic_header_with_no_timing(self):
        """When payload has no timing fields, header uses receipt time as meeting_end."""
        data = {'title': 'Test', 'transcript': 'Hello'}
        result = meetingnotesd._build_transcript_header(data, 'Hello')

        assert result.startswith('---\n')
        assert '\n---\n' in result
        assert 'recording_source: macwhisper' in result
        assert 'received_at:' in result
        assert 'meeting_end:' in result
        assert result.endswith('Hello')

    def test_header_with_explicit_start_end(self):
        """When payload has meeting_start and meeting_end, they're used directly."""
        data = {
            'title': 'Test',
            'transcript': 'Content',
            'meeting_start': '2026-02-05T14:00:00-08:00',
            'meeting_end': '2026-02-05T15:03:00-08:00',
        }
        result = meetingnotesd._build_transcript_header(data, 'Content')

        assert 'meeting_start: 2026-02-05T14:00:00-08:00' in result
        assert 'meeting_end: 2026-02-05T15:03:00-08:00' in result
        assert result.endswith('Content')

    def test_header_with_duration_only(self):
        """When payload has duration but no start/end, estimates from receipt time."""
        data = {
            'title': 'Test',
            'transcript': 'Content',
            'duration': 3600,  # 1 hour
        }
        result = meetingnotesd._build_transcript_header(data, 'Content')

        assert 'meeting_start:' in result
        assert 'meeting_end:' in result

    def test_header_with_start_and_duration(self):
        """When payload has meeting_start and duration, computes meeting_end."""
        data = {
            'title': 'Test',
            'transcript': 'Content',
            'meeting_start': '2026-02-05T14:00:00-08:00',
            'duration': 3600,
        }
        result = meetingnotesd._build_transcript_header(data, 'Content')

        assert 'meeting_start: 2026-02-05T14:00:00-08:00' in result
        assert 'meeting_end: 2026-02-05T15:00:00-08:00' in result

    def test_custom_recording_source(self):
        """When payload specifies recording_source, it's used instead of default."""
        data = {
            'title': 'Test',
            'transcript': 'Content',
            'recording_source': 'transcriber',
        }
        result = meetingnotesd._build_transcript_header(data, 'Content')

        assert 'recording_source: transcriber' in result

    def test_header_is_valid_yaml(self):
        """Generated header should be parseable as YAML."""
        data = {
            'title': 'Test',
            'transcript': 'Content',
            'meeting_start': '2026-02-05T14:00:00-08:00',
            'meeting_end': '2026-02-05T15:03:00-08:00',
        }
        result = meetingnotesd._build_transcript_header(data, 'Content')

        # Extract YAML portion
        import re
        match = re.search(r'^---\n(.*?)\n---\n', result, re.DOTALL)
        assert match is not None
        parsed = yaml.safe_load(match.group(1))
        assert isinstance(parsed, dict)
        assert 'recording_source' in parsed

    def test_alternate_field_names(self):
        """Should accept start_time/end_time as alternate field names."""
        data = {
            'title': 'Test',
            'transcript': 'Content',
            'start_time': '2026-02-05T14:00:00-08:00',
            'end_time': '2026-02-05T15:03:00-08:00',
        }
        result = meetingnotesd._build_transcript_header(data, 'Content')

        assert 'meeting_start: 2026-02-05T14:00:00-08:00' in result
        assert 'meeting_end: 2026-02-05T15:03:00-08:00' in result


class TestWebhookHeaderInjection:
    """Tests for YAML header injection in the webhook endpoint."""

    @pytest.fixture
    def test_client(self, minimal_config, temp_workspace, monkeypatch):
        """Create a Flask test client."""
        monkeypatch.setattr("meetingnotesd.config", minimal_config)
        agent = meetingnotesd.RepoAgent(minimal_config)
        monkeypatch.setattr("meetingnotesd.agent", agent)
        meetingnotesd.app.config["TESTING"] = True
        return meetingnotesd.app.test_client(), agent

    def test_webhook_injects_header_for_plain_transcript(self, test_client, temp_workspace):
        """Plain transcript should have YAML header injected."""
        client, agent = test_client
        response = client.post(
            "/webhook",
            json={"title": "Team Sync", "transcript": "Alice: Hello\nBob: Hi"},
            content_type="application/json",
        )
        assert response.status_code == 200

        # Find the written file
        inbox = Path(agent.inbox_dir)
        files = list(inbox.glob("*.txt"))
        assert len(files) == 1

        content = files[0].read_text()
        assert content.startswith('---\n')
        assert 'recording_source: macwhisper' in content
        assert 'Alice: Hello' in content

    def test_webhook_preserves_existing_header(self, test_client, temp_workspace):
        """Transcript with existing YAML front matter should NOT get double-header."""
        client, agent = test_client
        transcript = "---\nmeeting_start: 2026-02-05T14:00:00-08:00\nrecording_source: transcriber\n---\n\nThe transcript."
        response = client.post(
            "/webhook",
            json={"title": "From Transcriber", "transcript": transcript},
            content_type="application/json",
        )
        assert response.status_code == 200

        # Find the written file
        inbox = Path(agent.inbox_dir)
        files = list(inbox.glob("*.txt"))
        assert len(files) == 1

        content = files[0].read_text()
        # Should have exactly one header (the original), not two
        assert content.count('---\n') == 2  # opening and closing
        assert 'recording_source: transcriber' in content
        assert 'recording_source: macwhisper' not in content


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
