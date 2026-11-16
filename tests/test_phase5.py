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
import shlex
import subprocess
import sys
import tempfile
import threading
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

    @pytest.fixture
    def diverged_repos(self, minimal_config, temp_workspace, monkeypatch):
        monkeypatch.delenv("MEETINGNOTES_SYNC_FF_ONLY", raising=False)
        local = temp_workspace["data_repo"]
        bare = temp_workspace["workspace"] / "remote.git"
        writer = temp_workspace["workspace"] / "writer"

        def git(repo, *args):
            git_dir = ["--git-dir", str(repo)] if repo.suffix == ".git" else []
            return subprocess.check_output(
                ["git", *git_dir, "-c", "core.hooksPath=/dev/null", *args],
                cwd=repo, stderr=subprocess.PIPE, text=True,
            ).strip()

        git(local, "clone", "--bare", str(local), str(bare))
        git(local, "remote", "add", "origin", str(bare))
        git(local, "clone", str(bare), str(writer))
        git(writer, "config", "user.email", "test@test.com")
        git(writer, "config", "user.name", "Test")
        (local / "note.org").write_text("Local notes\n")
        git(local, "add", "note.org")
        git(local, "commit", "-m", "Local notes")
        (writer / "calendar.org").write_text("Remote calendar\n")
        git(writer, "add", "calendar.org")
        git(writer, "commit", "-m", "Remote calendar")
        git(writer, "push", "origin", "main")
        minimal_config["sync"]["ff_only"] = False
        minimal_config["git"]["auto_push"] = True
        return meetingnotesd.RepoAgent(minimal_config), local, writer, bare, git

    def test_merge_sync_preserves_both_writers(self, diverged_repos):
        agent, local, writer, _, git = diverged_repos
        local_head = git(local, "rev-parse", "HEAD")
        remote_head = git(writer, "rev-parse", "HEAD")
        git(local, "config", "pull.rebase", "true")
        git(local, "config", "merge.ff", "only")

        success, message = agent.git_push()

        assert success, message
        assert git(local, "rev-list", "--parents", "-1", "HEAD").split()[1:] == [
            local_head, remote_head,
        ]
        assert (local / "note.org").read_text() == "Local notes\n"
        assert (local / "calendar.org").read_text() == "Remote calendar\n"
        assert git(local, "status", "--porcelain") == ""
        assert git(local, "rev-parse", "HEAD") == git(local, "rev-parse", "origin/main")

    def test_conflict_preview_leaves_repository_untouched(self, diverged_repos):
        agent, local, writer, _, git = diverged_repos
        (writer / "note.org").write_text("Conflicting remote notes\n")
        git(writer, "add", "note.org")
        git(writer, "commit", "-m", "Conflicting notes")
        git(writer, "push", "origin", "main")
        before = git(local, "rev-parse", "HEAD")

        success, message = agent.git_push()

        assert not success
        assert "merge-tree failed" in message
        assert git(local, "rev-parse", "HEAD") == before
        assert git(local, "status", "--porcelain") == ""
        assert not (local / ".git" / "MERGE_HEAD").exists()
        assert (local / "note.org").read_text() == "Local notes\n"

    def test_merge_sync_refuses_uncommitted_work(self, diverged_repos):
        agent, local, _, _, git = diverged_repos
        (local / "pending.txt").write_text("Do not discard\n")
        before = git(local, "rev-parse", "HEAD")

        success, message = agent.git_push()

        assert not success
        assert "uncommitted changes" in message
        assert git(local, "rev-parse", "HEAD") == before
        assert (local / "pending.txt").read_text() == "Do not discard\n"

    def test_ff_only_still_rejects_divergence_and_skips_push(self, diverged_repos):
        agent, local, _, _, git = diverged_repos
        agent.sync_ff_only = True
        before = git(local, "rev-parse", "HEAD")
        with mock.patch.object(agent, "_run_git", wraps=agent._run_git) as run:
            success, message = agent.git_push()

        assert not success
        assert "Push skipped" in message
        assert not any(call.args[0][0] == "push" for call in run.call_args_list)
        assert git(local, "rev-parse", "HEAD") == before

    def test_push_retries_remote_writer_race(self, diverged_repos, monkeypatch):
        agent, local, writer, bare, git = diverged_repos
        run_git = agent._run_git
        pushes = []

        def racing_git(args, **kwargs):
            if args[0] == "push":
                pushes.append(args)
                if len(pushes) == 1:
                    (writer / "calendar.org").write_text("New remote calendar\n")
                    git(writer, "add", "calendar.org")
                    git(writer, "commit", "-m", "Concurrent calendar update")
                    git(writer, "push", "origin", "main")
            return run_git(args, **kwargs)

        monkeypatch.setattr(agent, "_run_git", racing_git)
        monkeypatch.setattr(meetingnotesd.time, "sleep", lambda _: None)
        success, message = agent.git_push()

        assert success, message
        assert len(pushes) == 2
        assert (local / "calendar.org").read_text() == "New remote calendar\n"
        assert git(bare, "rev-parse", "main") == git(local, "rev-parse", "HEAD")

    def test_push_retry_budget_is_bounded(self, diverged_repos, monkeypatch):
        agent, local, writer, _, git = diverged_repos
        run_git = agent._run_git
        pushes = []

        def racing_git(args, **kwargs):
            if args[0] == "push":
                pushes.append(args)
                (writer / "calendar.org").write_text(f"Calendar version {len(pushes)}\n")
                git(writer, "add", "calendar.org")
                git(writer, "commit", "-m", "Concurrent calendar update")
                git(writer, "push", "origin", "main")
            return run_git(args, **kwargs)

        monkeypatch.setattr(agent, "_run_git", racing_git)
        monkeypatch.setattr(meetingnotesd.time, "sleep", lambda _: None)
        success, message = agent.git_push()

        assert not success
        assert "rejected" in message
        assert len(pushes) == 3
        assert git(local, "status", "--porcelain") == ""

    def test_merge_sync_refuses_wrong_branch(self, diverged_repos):
        agent, local, _, _, git = diverged_repos
        git(local, "checkout", "-b", "other-work")
        before = git(local, "rev-parse", "HEAD")

        success, message = agent.git_push()

        assert not success
        assert "Expected branch main, found other-work" in message
        assert git(local, "rev-parse", "HEAD") == before

    def test_sync_timeout_is_reported(self, repo_agent):
        with mock.patch.object(
            repo_agent, "_run_git",
            side_effect=subprocess.TimeoutExpired(["git", "pull"], 60),
        ):
            changed, message = repo_agent.sync_repo()
        assert not changed
        assert "timed out" in message
        assert repo_agent.last_sync_error == message

    def test_auth_failure_is_not_retried(self, repo_agent):
        repo_agent.sync_enabled = False
        repo_agent.git_auto_push = True
        with mock.patch.object(repo_agent, "_run_git") as run:
            run.return_value = subprocess.CompletedProcess(
                ["git", "push"], 128, "", "Permission denied (publickey)"
            )
            success, message = repo_agent.git_push()
        assert not success
        assert "Permission denied" in message
        run.assert_called_once()

    def test_sync_policy_can_be_overridden_for_launchd(self, minimal_config, monkeypatch):
        monkeypatch.setenv("MEETINGNOTES_SYNC_FF_ONLY", "false")
        assert not meetingnotesd.RepoAgent(minimal_config).sync_ff_only
        monkeypatch.setenv("MEETINGNOTES_SYNC_FF_ONLY", "invalid")
        with pytest.raises(ValueError, match="must be true or false"):
            meetingnotesd.RepoAgent(minimal_config)


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

    @pytest.mark.parametrize("uv_path", ["uv", "/opt/homebrew/bin/uv"])
    def test_legacy_summarizer_command_uses_prepared_interpreter(self, minimal_config, monkeypatch, uv_path):
        monkeypatch.setattr(sys, "executable", "/prepared runtime/bin/python")
        minimal_config["processing"] = {
            "standalone": {"command": f"{uv_path} run run_summarization.py --git --debug"}
        }
        agent = meetingnotesd.RepoAgent(minimal_config)
        assert shlex.split(agent.standalone_command) == [
            sys.executable,
            str(Path(meetingnotesd.__file__).with_name("run_summarization.py")),
            "--git",
            "--debug",
        ]

    def test_custom_processing_command_is_preserved(self, minimal_config):
        minimal_config["processing"] = {
            "standalone": {"command": "/custom/processor --flag"}
        }
        agent = meetingnotesd.RepoAgent(minimal_config)
        assert agent.standalone_command == "/custom/processor --flag"

    @pytest.mark.parametrize("exit_code,expected_success", [(0, True), (2, True), (1, False)])
    def test_processor_exit_codes(self, minimal_config, temp_workspace, exit_code, expected_success):
        minimal_config["sync"]["enabled"] = False
        minimal_config["processing"] = {
            "standalone": {
                "enabled": True,
                "command": f'{sys.executable} -c "import sys; sys.exit({exit_code})"',
                "working_directory": str(temp_workspace["workspace"]),
                "timeout_seconds": 10,
            }
        }
        agent = meetingnotesd.RepoAgent(minimal_config)

        success, _ = agent.run_standalone_processing()

        assert success is expected_success

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

    @pytest.mark.parametrize("first_pass_fails", [False, True])
    def test_async_standalone_processing_coalesces_concurrent_runs(
        self, minimal_config, temp_workspace, first_pass_fails
    ):
        """Arrivals during a batch coalesce into one additional pass."""
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
        started = threading.Event()
        release = threading.Event()

        def fake_processing():
            calls.append("run")
            if len(calls) == 1:
                started.set()
                assert release.wait(timeout=2)
                if first_pass_fails:
                    raise RuntimeError("First batch failed")
            return True, "ok"

        agent.run_standalone_processing = fake_processing

        agent.run_standalone_processing_async()
        assert started.wait(timeout=2)
        thread = agent._processing_thread
        try:
            for _ in range(3):
                agent.run_standalone_processing_async()
        finally:
            release.set()
        thread.join(timeout=2)

        assert not thread.is_alive()
        assert calls == ["run", "run"]
        assert agent._processing_thread is None
        assert agent.last_processing_error is None


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

    def test_health_check_reports_sync_failure(self, test_client, monkeypatch):
        monkeypatch.setattr(meetingnotesd.agent, "last_push_error", "SSH authentication failed")
        data = test_client.get("/").get_json()
        assert data["status"] == "degraded"
        assert data["sync"]["last_error"] == "SSH authentication failed"

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

    def test_calendar_accepts_unchanged_content(self, test_client, monkeypatch):
        import meetingnotesd

        monkeypatch.setattr(meetingnotesd.agent, "sync_enabled", False)
        calendar = "* Current meeting\n"

        first = test_client.post(
            "/calendar",
            data=calendar,
            content_type="text/plain",
        )
        second = test_client.post(
            "/calendar",
            data=calendar,
            content_type="text/plain",
        )

        assert first.status_code == 200
        assert second.status_code == 200
        assert second.get_json()["git"] == {
            "committed": False,
            "message": "Calendar already current",
        }

    def test_calendar_does_not_overwrite_after_merge_sync_failure(
        self, test_client, temp_workspace, monkeypatch
    ):
        calendar = temp_workspace["data_repo"] / "calendar.org"
        calendar.write_text("Preserve current calendar\n")

        def failed_sync():
            meetingnotesd.agent.last_sync_error = "git merge-tree failed: conflict"
            return False, meetingnotesd.agent.last_sync_error

        monkeypatch.setattr(meetingnotesd.agent, "sync_repo", failed_sync)
        response = test_client.post(
            "/calendar", data="Replacement\n", content_type="text/plain"
        )
        assert response.status_code == 503
        assert calendar.read_text() == "Preserve current calendar\n"

    def test_calendar_commit_failure_restores_previous_content(
        self, test_client, temp_workspace, monkeypatch
    ):
        import meetingnotesd

        monkeypatch.setattr(meetingnotesd.agent, "sync_enabled", False)
        calendar_path = temp_workspace["data_repo"] / "calendar.org"
        calendar_path.write_text("* Previous meeting\n")
        subprocess.run(
            ["git", "add", "calendar.org"],
            cwd=temp_workspace["data_repo"],
            check=True,
        )
        subprocess.run(
            ["git", "commit", "-m", "Add calendar"],
            cwd=temp_workspace["data_repo"],
            capture_output=True,
            check=True,
        )
        monkeypatch.setattr(
            meetingnotesd.agent,
            "git_commit",
            lambda *_: (False, "Git commit failed: test failure"),
        )

        response = test_client.post(
            "/calendar",
            data="* Replacement meeting\n",
            content_type="text/plain",
        )

        assert response.status_code == 500
        assert calendar_path.read_text() == "* Previous meeting\n"
        status = subprocess.run(
            ["git", "status", "--porcelain", "--", "calendar.org"],
            cwd=temp_workspace["data_repo"],
            capture_output=True,
            text=True,
            check=True,
        )
        assert status.stdout == ""


class TestBackgroundSync:
    """Tests for background sync thread."""

    def test_background_sync_retries_unpublished_commits(self, minimal_config, monkeypatch):
        minimal_config["sync"]["poll_interval_seconds"] = 60
        minimal_config["git"]["auto_push"] = True
        agent = meetingnotesd.RepoAgent(minimal_config)
        monkeypatch.setattr(agent, "sync_repo", lambda: (False, "already up to date"))

        def push():
            agent.stop_background_sync()
            return True, "Pushed pending commits"

        pushed = mock.Mock(side_effect=push)
        monkeypatch.setattr(agent, "git_push", pushed)
        agent.start_background_sync()
        agent._sync_thread.join(timeout=2)

        assert not agent._sync_thread.is_alive()
        pushed.assert_called_once()

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
