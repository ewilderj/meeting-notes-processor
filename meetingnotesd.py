#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "flask>=3.0.0",
#     "pyyaml>=6.0.0",
#     "requests>=2.31.0",
# ]
# ///
"""
Meeting Notes Daemon (meetingnotesd)

Receives webhooks from MacWhisper and writes transcripts to inbox.
Syncs and manages the data repository. Configuration is loaded from config.yaml.

Run with: uv run meetingnotesd.py

Endpoints:
  GET  /         - Health check
  POST /webhook  - Receive transcript from MacWhisper
  POST /calendar - Update calendar.org

Test transcript webhook:
curl -X POST http://localhost:9876/webhook \
  -H "Content-Type: application/json" \
  -d '{"title": "Test Meeting", "transcript": "This is a test transcript."}'

Test calendar webhook (JSON):
curl -X POST http://localhost:9876/calendar \
  -H "Content-Type: application/json" \
  -d '{"calendar": "* Meeting <2026-01-22 Thu 10:00-11:00>"}'

Test calendar webhook (plain text):
curl -X POST http://localhost:9876/calendar \
  -H "Content-Type: text/plain" \
  --data-binary @calendar.org
"""

from flask import Flask, request, jsonify
import os
import re
from datetime import datetime, timedelta
import logging
import yaml
from pathlib import Path
import subprocess
import threading
import time
import random
import shlex
import sys
import argparse

import requests

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

app = Flask(__name__)

# Load configuration
CONFIG_FILE = os.getenv('WEBHOOK_CONFIG', 'config.yaml')


def load_config() -> dict:
    """Load configuration from YAML file."""
    config_path = Path(CONFIG_FILE)
    if not config_path.exists():
        logger.error(f"Configuration file not found: {CONFIG_FILE}")
        raise FileNotFoundError(f"Configuration file not found: {CONFIG_FILE}")

    with open(config_path, 'r') as f:
        return yaml.safe_load(f) or {}


def _get_nested(config: dict, keys: list[str], default=None):
    current = config
    for key in keys:
        if not isinstance(current, dict) or key not in current:
            return default
        current = current[key]
    return current


def _normalize_repo_url(repo_url: str | None) -> str | None:
    if not repo_url:
        return None
    repo_url = repo_url.strip()
    if repo_url.startswith("http://") or repo_url.startswith("https://"):
        return repo_url
    if repo_url.startswith("github.com/"):
        return f"https://{repo_url}"
    return repo_url


class RepoAgent:
    def __init__(self, config: dict):
        self.config = config

        self.host = _get_nested(config, ['server', 'host'], '127.0.0.1')
        self.port = int(_get_nested(config, ['server', 'port'], 9876))

        # Data repo path - inbox is always {repo}/inbox
        self.repo_dir = config.get('data_repo') or _get_nested(config, ['directories', 'repository'], '.')
        self.inbox_dir = str(Path(self.repo_dir) / 'inbox')

        self.git_auto_commit = bool(_get_nested(config, ['git', 'auto_commit'], False))
        self.git_auto_push = bool(_get_nested(config, ['git', 'auto_push'], False))
        self.git_repo_url = _normalize_repo_url(_get_nested(config, ['git', 'repository_url']))
        self.git_commit_template = _get_nested(config, ['git', 'commit_message_template'], 'Add transcript: {title}')

        # Backwards-compatible default: if auto-push is on, keep doing a safe pull before push.
        self.sync_enabled = bool(_get_nested(config, ['sync', 'enabled'], self.git_auto_push))
        self.sync_on_startup = bool(_get_nested(config, ['sync', 'on_startup'], True))
        self.sync_before_accepting_webhooks = bool(_get_nested(config, ['sync', 'before_accepting_webhooks'], True))
        self.sync_poll_interval_seconds = float(_get_nested(config, ['sync', 'poll_interval_seconds'], 0) or 0)
        self.sync_ff_only = bool(_get_nested(config, ['sync', 'ff_only'], True))
        sync_ff_only_override = os.getenv('MEETINGNOTES_SYNC_FF_ONLY')
        if sync_ff_only_override is not None:
            if sync_ff_only_override.lower() not in ('true', 'false'):
                raise ValueError('MEETINGNOTES_SYNC_FF_ONLY must be true or false')
            self.sync_ff_only = sync_ff_only_override.lower() == 'true'
        self.last_sync_error: str | None = None
        self.last_push_error: str | None = None

        # Git remote/branch settings (used for clone, pull, push)
        self.git_remote = _get_nested(config, ['git', 'remote'], 'origin')
        self.git_branch = _get_nested(config, ['git', 'branch'], 'main')

        self.workflow_dispatch_enabled = bool(_get_nested(config, ['github', 'workflow_dispatch', 'enabled'], False))
        self.workflow_dispatch_repo = _get_nested(config, ['github', 'workflow_dispatch', 'repo'])
        self.workflow_dispatch_workflow = _get_nested(config, ['github', 'workflow_dispatch', 'workflow'])
        self.workflow_dispatch_ref = _get_nested(config, ['github', 'workflow_dispatch', 'ref'], 'main')
        self.workflow_dispatch_inputs = _get_nested(config, ['github', 'workflow_dispatch', 'inputs'], {}) or {}

        self.hook_on_new_commits_enabled = bool(_get_nested(config, ['hooks', 'on_new_commits', 'enabled'], False))
        self.hook_on_new_commits_command = _get_nested(config, ['hooks', 'on_new_commits', 'command'])
        self.hook_working_directory = _get_nested(config, ['hooks', 'on_new_commits', 'working_directory'], '.')
        self.hook_timeout_seconds = int(_get_nested(config, ['hooks', 'on_new_commits', 'timeout_seconds'], 600))

        # Standalone mode: process transcripts locally instead of dispatching to GitHub Actions
        self.standalone_enabled = bool(_get_nested(config, ['processing', 'standalone', 'enabled'], False))
        self.standalone_command = _get_nested(config, ['processing', 'standalone', 'command'], 'uv run run_summarization.py --git')
        standalone_args = shlex.split(self.standalone_command or '')
        if (
            len(standalone_args) >= 3
            and Path(standalone_args[0]).name == 'uv'
            and standalone_args[1:3] == ['run', 'run_summarization.py']
        ):
            self.standalone_command = shlex.join([
                sys.executable,
                str(Path(__file__).with_name('run_summarization.py')),
                *standalone_args[3:],
            ])
        self.standalone_working_directory = _get_nested(config, ['processing', 'standalone', 'working_directory'], '.')
        self.standalone_timeout_seconds = int(_get_nested(config, ['processing', 'standalone', 'timeout_seconds'], 1800))
        # Async mode: return immediately after saving file, process in background
        self.standalone_async = bool(_get_nested(config, ['processing', 'standalone', 'async'], False))

        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._sync_thread: threading.Thread | None = None
        self._processing_thread: threading.Thread | None = None
        self._processing_lock = threading.Lock()
        self._processing_pending = False
        self.last_processing_error: str | None = None

    def _token(self) -> str | None:
        return os.environ.get('GH_TOKEN')

    def _repo_path(self) -> Path:
        return Path(self.repo_dir).resolve()

    def _inbox_path(self) -> Path:
        return Path(self.inbox_dir).resolve()

    def _run_git(self, args: list[str], *, timeout: int = 30) -> subprocess.CompletedProcess:
        return subprocess.run(
            ['git', *args],
            cwd=self._repo_path(),
            capture_output=True,
            text=True,
            timeout=timeout,
            env={**os.environ, 'GIT_TERMINAL_PROMPT': '0'},
        )

    def _checked_git(self, args: list[str], *, timeout: int = 30) -> str:
        result = self._run_git(args, timeout=timeout)
        if result.returncode != 0:
            detail = (result.stderr or result.stdout).strip()
            raise RuntimeError(f"git {args[0]} failed: {detail}")
        return result.stdout.strip()

    def ensure_repo_checkout(self) -> None:
        """Clone the data repo if it doesn't exist yet."""
        repo_path = self._repo_path()
        git_dir = repo_path / '.git'
        if git_dir.exists():
            return

        clone_url = self.git_repo_url
        if not clone_url:
            raise ValueError(
                f"Data repo not found at {repo_path} and no git.repository_url configured for auto-clone."
            )

        repo_path.parent.mkdir(parents=True, exist_ok=True)
        logger.info(f"Cloning data repo into {repo_path}...")

        result = subprocess.run(
            ['git', 'clone', '--branch', str(self.git_branch), '--single-branch', clone_url, str(repo_path)],
            cwd=repo_path.parent,
            capture_output=True,
            text=True,
            timeout=120,
        )
        if result.returncode != 0:
            raise RuntimeError(f"git clone failed: {result.stderr.strip()}")

    def sync_repo(self) -> tuple[bool, str]:
        """Sync local data repo. Returns (changed, message)."""
        if not self.sync_enabled:
            return False, "sync disabled"

        try:
            self.ensure_repo_checkout()
            before = self._checked_git(['rev-parse', 'HEAD'])
            if self.sync_ff_only:
                self._checked_git([
                    'pull', '--ff-only', '--no-rebase',
                    str(self.git_remote), str(self.git_branch),
                ], timeout=60)
            else:
                self._merge_remote()
            after = self._checked_git(['rev-parse', 'HEAD'])
        except (RuntimeError, OSError, subprocess.SubprocessError) as error:
            self.last_sync_error = str(error)
            logger.error(f"Repository sync failed: {error}")
            return False, self.last_sync_error

        self.last_sync_error = None
        changed = before != after
        return changed, ("pulled new commits" if changed else "already up to date")

    def _merge_remote(self) -> None:
        """Preview opt-in merges without touching dirty or conflicting work."""
        branch = self._checked_git(['symbolic-ref', '--short', 'HEAD'])
        if branch != str(self.git_branch):
            raise RuntimeError(f"Expected branch {self.git_branch}, found {branch}")
        if self._checked_git(['status', '--porcelain']):
            raise RuntimeError("Cannot sync: data repository has uncommitted changes")
        merge_head = self._run_git(['rev-parse', '--verify', '-q', 'MERGE_HEAD'])
        if merge_head.returncode == 0:
            raise RuntimeError("Cannot sync: data repository has an unfinished merge")
        self._checked_git(['fetch', '--no-tags', str(self.git_remote), str(self.git_branch)], timeout=60)
        remote_sha = self._checked_git(['rev-parse', 'FETCH_HEAD'])
        self._checked_git(['merge-tree', '--write-tree', 'HEAD', remote_sha], timeout=60)
        try:
            self._checked_git([
                'merge', '--no-edit', '--ff', '--no-autostash', remote_sha,
            ], timeout=60)
        except (RuntimeError, OSError, subprocess.SubprocessError):
            if self._run_git(['rev-parse', '--verify', '-q', 'MERGE_HEAD']).returncode == 0:
                self._checked_git(['merge', '--abort'])
            raise

    def _run_hook_on_new_commits(self) -> tuple[bool, str]:
        if not self.hook_on_new_commits_enabled:
            return False, "hook disabled"
        if not self.hook_on_new_commits_command:
            return False, "hook enabled but no command configured"

        working_dir = (self._repo_path() / self.hook_working_directory).resolve()
        try:
            working_dir.mkdir(parents=True, exist_ok=True)
        except Exception:
            # If it's a file or otherwise invalid, let subprocess surface the error.
            pass

        args = shlex.split(self.hook_on_new_commits_command)
        logger.info(f"Running hook: {args} (cwd={working_dir})")
        result = subprocess.run(
            args,
            cwd=working_dir,
            capture_output=True,
            text=True,
            timeout=self.hook_timeout_seconds,
        )
        if result.returncode != 0:
            stderr = (result.stderr or '').strip()
            return False, f"hook failed: {stderr or 'non-zero exit'}"
        return True, "hook completed"

    def run_standalone_processing(self) -> tuple[bool, str]:
        """Run local summarization in standalone mode."""
        if not self.standalone_enabled:
            return False, "standalone processing disabled"
        if not self.standalone_command:
            return False, "standalone enabled but no command configured"

        # Resolve working directory (relative to script location or absolute)
        if os.path.isabs(self.standalone_working_directory):
            working_dir = Path(self.standalone_working_directory)
        else:
            # Relative to the directory containing this script (processor repo)
            script_dir = Path(__file__).parent.resolve()
            working_dir = (script_dir / self.standalone_working_directory).resolve()

        try:
            working_dir.mkdir(parents=True, exist_ok=True)
        except Exception:
            pass

        # Set WORKSPACE_DIR to point to the data repo
        env = os.environ.copy()
        env['WORKSPACE_DIR'] = str(self._repo_path())

        args = shlex.split(self.standalone_command)
        logger.info(f"Running standalone processing: {args} (cwd={working_dir}, WORKSPACE_DIR={env['WORKSPACE_DIR']})")

        import time as _time
        try:
            start_time = _time.monotonic()
            process = subprocess.Popen(
                args,
                cwd=working_dir,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
                env=env,
            )
            output_lines = []
            last_progress = start_time
            deadline = start_time + self.standalone_timeout_seconds

            import select
            while process.poll() is None:
                # Use select to read with a timeout so we can check deadline
                ready, _, _ = select.select([process.stdout], [], [], 5.0)
                if ready:
                    line = process.stdout.readline()
                    if line:
                        output_lines.append(line)
                        logger.info(f"  proc: {line.rstrip()}")
                now = _time.monotonic()
                if now - last_progress >= 30:
                    elapsed = int(now - start_time)
                    logger.info(f"  ... still running ({elapsed}s, {len(output_lines)} lines)")
                    last_progress = now
                if now > deadline:
                    process.kill()
                    process.wait()
                    return False, f"standalone processing timed out after {self.standalone_timeout_seconds}s"

            # Read remaining output after process exits
            for line in process.stdout:
                output_lines.append(line)
                logger.info(f"  proc: {line.rstrip()}")

            elapsed = int(_time.monotonic() - start_time)
        except Exception as e:
            return False, f"standalone processing failed: {e}"

        if process.returncode not in (0, 2):
            detail = ''.join(output_lines[-10:]).strip() or 'non-zero exit'
            if len(detail) > 500:
                detail = detail[:500] + '...'
            return False, f"standalone processing failed (exit {process.returncode}, {elapsed}s): {detail}"

        logger.info(f"Standalone processing completed in {elapsed}s (exit {process.returncode})")
        return True, "standalone processing completed"

    def run_standalone_processing_async(self) -> None:
        """Run standalone processing in background thread, then push results.

        Coalesce triggers, but run another pass for arrivals during processing.
        """
        def _background_task():
            while True:
                with self._processing_lock:
                    self._processing_pending = False
                try:
                    with self._lock:
                        proc_ok, proc_msg = self.run_standalone_processing()
                        if proc_ok:
                            self.last_processing_error = None
                            logger.info(f"Background processing succeeded: {proc_msg}")
                            if self.git_auto_push:
                                push_ok, push_msg = self.git_push()
                                if push_ok:
                                    logger.info(f"Background push succeeded: {push_msg}")
                                else:
                                    logger.error(f"Background push failed: {push_msg}")
                        else:
                            self.last_processing_error = proc_msg
                            logger.error(f"Background processing failed: {proc_msg}")
                except Exception as e:
                    self.last_processing_error = str(e) or type(e).__name__
                    logger.error(f"Background processing exception: {e}")
                with self._processing_lock:
                    if not self._processing_pending:
                        self._processing_thread = None
                        return

        with self._processing_lock:
            self._processing_pending = True
            if self._processing_thread is not None:
                logger.info("Processing already in progress, queued another pass")
                return
            self._processing_thread = threading.Thread(target=_background_task, daemon=True)
            self._processing_thread.start()
        logger.info("Started background processing thread")

    def maybe_dispatch_workflow(self, *, reason: str) -> tuple[bool, str]:
        if not self.workflow_dispatch_enabled:
            return False, "workflow dispatch disabled"
        if not self.workflow_dispatch_repo or not self.workflow_dispatch_workflow:
            return False, "workflow dispatch enabled but repo/workflow not configured"

        token = self._token()
        if not token:
            return False, "GH_TOKEN not set"

        url = f"https://api.github.com/repos/{self.workflow_dispatch_repo}/actions/workflows/{self.workflow_dispatch_workflow}/dispatches"
        headers = {
            'Authorization': f'Bearer {token}',
            'Accept': 'application/vnd.github+json',
            'X-GitHub-Api-Version': '2022-11-28',
        }
        payload = {
            'ref': self.workflow_dispatch_ref,
            'inputs': dict(self.workflow_dispatch_inputs or {}),
        }

        # Note: we do not inject any implicit inputs; workflows may reject unknown inputs.

        try:
            resp = requests.post(url, headers=headers, json=payload, timeout=20)
        except Exception as e:
            return False, f"workflow dispatch failed: {e}"

        if resp.status_code not in (200, 201, 204):
            return False, f"workflow dispatch failed ({resp.status_code}): {resp.text.strip()}"
        return True, "workflow dispatch triggered"

    def git_commit(self, filepath: str, title: str) -> tuple[bool, str]:
        """Commit a file to the data repository (no push)."""
        repo_path = self._repo_path()
        file_abs_path = Path(filepath).resolve()
        try:
            file_rel_path = file_abs_path.relative_to(repo_path)
        except Exception:
            return False, f"File path is outside repository: {file_abs_path}"

        # Stage file
        result = self._run_git(['add', str(file_rel_path)], timeout=10)
        if result.returncode != 0:
            return False, f"Git add failed: {result.stderr.strip()}"
        logger.info(f"Git added: {file_rel_path}")

        # Commit
        commit_message = self.git_commit_template.format(title=title)
        result = self._run_git(['commit', '-m', commit_message], timeout=10)
        if result.returncode != 0:
            return False, f"Git commit failed: {result.stderr.strip()}"
        logger.info(f"Git committed: {commit_message}")

        return True, "Committed to repository"

    def git_push(self) -> tuple[bool, str]:
        """Push commits to remote repository."""
        if not self.git_auto_push:
            return True, "Push disabled in config"

        for attempt in range(3):
            try:
                changed, message = self.sync_repo()
                logger.info(f"Sync before push: {message}")
                if self.sync_enabled and self.last_sync_error:
                    self.last_push_error = f"Push skipped: {message}"
                    return False, self.last_push_error
                if changed and self.hook_on_new_commits_enabled:
                    ok, hook_msg = self._run_hook_on_new_commits()
                    if not ok:
                        logger.warning(hook_msg)
                result = self._run_git([
                    'push', '--porcelain', str(self.git_remote), str(self.git_branch),
                ], timeout=60)
            except (OSError, subprocess.SubprocessError) as error:
                self.last_push_error = f"Git push failed: {error}"
                logger.error(self.last_push_error)
                return False, self.last_push_error
            if result.returncode == 0:
                self.last_push_error = None
                logger.info(f"Git pushed to {self.git_remote}/{self.git_branch}")
                return True, f"Pushed to {self.git_remote}/{self.git_branch}"

            detail = (result.stdout + result.stderr).strip()
            self.last_push_error = f"Git push failed: {detail}"
            raced = '[rejected]' in detail and any(
                reason in detail for reason in ('fetch first', 'non-fast-forward')
            )
            if not raced or not self.sync_enabled or attempt == 2:
                logger.error(self.last_push_error)
                return False, self.last_push_error
            logger.warning("Remote advanced during push; syncing before retry")
            time.sleep(random.uniform(0.5, 1.5) * (2 ** attempt))

    def start_background_sync(self) -> None:
        if not self.sync_enabled or self.sync_poll_interval_seconds <= 0:
            return

        if self._sync_thread and self._sync_thread.is_alive():
            return

        def _loop():
            logger.info(f"Background sync started (interval={self.sync_poll_interval_seconds}s)")
            while not self._stop_event.is_set():
                try:
                    with self._lock:
                        changed, message = self.sync_repo()
                        if changed:
                            logger.info(f"Background sync: {message}")
                            ok, hook_msg = self._run_hook_on_new_commits()
                            if not ok:
                                logger.warning(hook_msg)
                        else:
                            logger.debug(f"Background sync: {message}")
                        if self.git_auto_push and not self.last_sync_error:
                            pushed, push_message = self.git_push()
                            if not pushed:
                                logger.error(f"Background push failed: {push_message}")
                except Exception as e:
                    logger.warning(f"Background sync error: {e}")
                self._stop_event.wait(self.sync_poll_interval_seconds)

        self._sync_thread = threading.Thread(target=_loop, name='repo-sync', daemon=True)
        self._sync_thread.start()

    def stop_background_sync(self) -> None:
        self._stop_event.set()


config = load_config()
agent = RepoAgent(config)


def sanitize_filename(title):
    """
    Sanitize the title to create a safe filename.
    
    - Convert to lowercase
    - Replace spaces with hyphens
    - Remove special characters
    - Limit length
    """
    # Convert to lowercase and replace spaces with hyphens
    sanitized = title.lower().strip()
    sanitized = re.sub(r'\s+', '-', sanitized)
    
    # Remove any character that isn't alphanumeric, hyphen, or underscore
    sanitized = re.sub(r'[^a-z0-9\-_]', '', sanitized)
    
    # Remove multiple consecutive hyphens
    sanitized = re.sub(r'-+', '-', sanitized)
    
    # Remove leading/trailing hyphens
    sanitized = sanitized.strip('-')
    
    # Limit length (leave room for timestamp prefix)
    max_length = 50
    if len(sanitized) > max_length:
        sanitized = sanitized[:max_length].rstrip('-')
    
    # Fallback if empty
    if not sanitized:
        sanitized = 'untitled'
    
    return sanitized


def _build_transcript_header(data: dict, transcript: str) -> str:
    """Build a YAML front matter header and prepend it to the transcript.
    
    Uses timing fields from the webhook payload if available,
    otherwise falls back to the receipt timestamp.
    
    Args:
        data: The webhook JSON payload (may contain meeting_start, meeting_end, duration, recording_source)
        transcript: The raw transcript text
    
    Returns:
        Transcript with YAML front matter prepended
    """
    now = datetime.now().astimezone()
    header = {}

    # Extract timing fields from payload
    meeting_start = data.get('meeting_start') or data.get('start_time')
    meeting_end = data.get('meeting_end') or data.get('end_time')
    duration = data.get('duration')  # seconds
    source = data.get('recording_source', 'macwhisper')

    if meeting_start:
        header['meeting_start'] = meeting_start
    if meeting_end:
        header['meeting_end'] = meeting_end

    # If we have duration but are missing start/end, estimate
    if duration and not meeting_start and not meeting_end:
        end = now
        start = now - timedelta(seconds=int(duration))
        header['meeting_start'] = start.isoformat()
        header['meeting_end'] = end.isoformat()
    elif duration and meeting_start and not meeting_end:
        try:
            start_dt = datetime.fromisoformat(meeting_start)
            header['meeting_end'] = (start_dt + timedelta(seconds=int(duration))).isoformat()
        except (ValueError, TypeError):
            pass

    # If we still have nothing, use receipt time as meeting_end
    if 'meeting_start' not in header and 'meeting_end' not in header:
        header['meeting_end'] = now.isoformat()

    header['recording_source'] = source
    header['received_at'] = now.isoformat()

    # Build YAML front matter
    lines = ['---']
    for key, value in header.items():
        lines.append(f'{key}: {value}')
    lines.append('---')
    lines.append('')

    return '\n'.join(lines) + transcript


def generate_filename(title):
    """
    Generate a unique filename with timestamp and sanitized title.
    Format: YYYYMMDD-HHMMSS-sanitized-title.txt
    """
    timestamp = datetime.now().strftime('%Y%m%d-%H%M%S')
    sanitized_title = sanitize_filename(title)
    return f"{timestamp}-{sanitized_title}.txt"


@app.route('/', methods=['GET'])
def health_check():
    """Health check endpoint."""
    processing_mode = 'standalone' if agent.standalone_enabled else 'relay'
    sync_error = agent.last_sync_error or agent.last_push_error
    return jsonify({
        'status': 'degraded' if sync_error or agent.last_processing_error else 'ok',
        'service': 'meetingnotesd',
        'inbox_dir': agent.inbox_dir,
        'repository': agent.repo_dir,
        'port': agent.port,
        'endpoints': {
            'health': '/',
            'transcript': '/webhook',
            'calendar': '/calendar',
        },
        'processing_mode': processing_mode,
        'sync': {
            'enabled': agent.sync_enabled,
            'poll_interval_seconds': agent.sync_poll_interval_seconds,
            'ff_only': agent.sync_ff_only,
            'last_error': sync_error,
        },
        'standalone': {
            'enabled': agent.standalone_enabled,
            'command': agent.standalone_command if agent.standalone_enabled else None,
            'last_error': agent.last_processing_error,
        },
        'relay': {
            'workflow_dispatch_enabled': agent.workflow_dispatch_enabled,
            'repo': agent.workflow_dispatch_repo if agent.workflow_dispatch_enabled else None,
            'workflow': agent.workflow_dispatch_workflow if agent.workflow_dispatch_enabled else None,
        },
    }), 200


@app.route('/calendar', methods=['POST'])
def calendar():
    """
    Endpoint for receiving calendar data (org-mode format).
    Overwrites calendar.org in the data repository.
    
    Expected payload:
    {
        "calendar": "* Meeting 1 <2026-01-20 Mon 10:00-11:00>\n..."
    }
    
    Or plain text with Content-Type: text/plain
    """
    try:
        # Accept either JSON or plain text
        if request.is_json:
            data = request.get_json()
            if 'calendar' not in data:
                return jsonify({
                    'status': 'error',
                    'message': "Missing required field: 'calendar'"
                }), 400
            calendar_content = data['calendar']
        elif request.content_type and 'text/plain' in request.content_type:
            calendar_content = request.get_data(as_text=True)
        else:
            return jsonify({
                'status': 'error',
                'message': 'Content-Type must be application/json or text/plain'
            }), 400

        if not calendar_content or not calendar_content.strip():
            return jsonify({
                'status': 'error',
                'message': 'Calendar content cannot be empty'
            }), 400

        # Size limit: 1MB should be plenty for a calendar file
        MAX_CALENDAR_SIZE = 1024 * 1024
        content_size = len(calendar_content.encode('utf-8'))
        if content_size > MAX_CALENDAR_SIZE:
            return jsonify({
                'status': 'error',
                'message': f'Calendar too large ({content_size} bytes). Maximum size is {MAX_CALENDAR_SIZE} bytes.'
            }), 413

        with agent._lock:
            # Sync before writing
            if agent.sync_enabled and agent.sync_before_accepting_webhooks:
                try:
                    changed, msg = agent.sync_repo()
                    logger.info(f"Pre-calendar sync: {msg}")
                except Exception as e:
                    logger.warning(f"Pre-calendar sync failed: {e}")
                    return jsonify({
                        'status': 'error',
                        'message': f'Calendar repository sync failed: {e}',
                    }), 503
                if agent.last_sync_error:
                    return jsonify({
                        'status': 'error',
                        'message': msg,
                    }), 503

            # Write calendar.org
            calendar_path = os.path.join(agent.repo_dir, 'calendar.org')
            previous_content = None
            if os.path.exists(calendar_path):
                with open(calendar_path, 'r', encoding='utf-8') as f:
                    previous_content = f.read()
            with open(calendar_path, 'w', encoding='utf-8') as f:
                f.write(calendar_content)

            logger.info(f"Updated calendar: {calendar_path} ({content_size} bytes)")

            response_data = {
                'status': 'success',
                'message': 'Calendar updated',
                'size': content_size,
            }

            # Commit and push if enabled
            if agent.git_auto_commit:
                if agent.sync_enabled:
                    agent.ensure_repo_checkout()

                commit_ok, commit_msg = agent.git_commit(calendar_path, 'Calendar update')
                response_data['git'] = {
                    'committed': commit_ok,
                    'message': commit_msg,
                }

                if not commit_ok:
                    status = agent._run_git(
                        ['status', '--porcelain', '--', 'calendar.org'],
                        timeout=10,
                    )
                    if status.returncode != 0 or status.stdout.strip():
                        if previous_content is None:
                            os.unlink(calendar_path)
                        else:
                            with open(calendar_path, 'w', encoding='utf-8') as f:
                                f.write(previous_content)
                        agent._run_git(
                            ['restore', '--staged', '--', 'calendar.org'],
                            timeout=10,
                        )
                        logger.error(f"Calendar commit failed: {commit_msg}")
                        return jsonify({
                            'status': 'error',
                            'message': commit_msg,
                        }), 500
                    response_data['git']['message'] = 'Calendar already current'

                if agent.git_auto_push:
                    push_ok, push_msg = agent.git_push()
                    response_data['git']['pushed'] = push_ok
                    response_data['git']['push_message'] = push_msg
                    if not push_ok:
                        return jsonify({
                            'status': 'error',
                            'message': push_msg,
                            'git': response_data['git'],
                        }), 502

        return jsonify(response_data), 200

    except Exception as e:
        logger.error(f"Error processing calendar: {str(e)}", exc_info=True)
        return jsonify({
            'status': 'error',
            'message': f'Internal server error: {str(e)}'
        }), 500


@app.route('/webhook', methods=['POST'])
def webhook():
    """
    Main webhook endpoint for receiving transcripts.
    
    Expected payload:
    {
        "title": "Meeting title",
        "transcript": "Full transcript text...",
        "meeting_start": "2026-02-05T14:00:00-08:00",  // optional
        "meeting_end": "2026-02-05T15:03:00-08:00",    // optional
        "duration": 3780,                                // optional, seconds
        "recording_source": "macwhisper"                 // optional
    }
    
    If the transcript already contains YAML front matter (starts with '---'),
    it is saved as-is (e.g. from the Transcriber appliance).
    Otherwise, a YAML header is injected with timing metadata.
    """
    try:
        # Validate content type
        if not request.is_json:
            logger.warning(f"Invalid content type: {request.content_type}")
            return jsonify({
                'status': 'error',
                'message': 'Content-Type must be application/json'
            }), 400

        # Parse JSON payload
        data = request.get_json()

        # Validate required fields
        if 'title' not in data:
            logger.warning("Missing 'title' field in payload")
            return jsonify({
                'status': 'error',
                'message': "Missing required field: 'title'"
            }), 400

        if 'transcript' not in data:
            logger.warning("Missing 'transcript' field in payload")
            return jsonify({
                'status': 'error',
                'message': "Missing required field: 'transcript'"
            }), 400

        title = data['title']
        transcript = data['transcript']

        # Validate that transcript has content
        if not transcript or not transcript.strip():
            logger.warning(f"Empty transcript received for title: {title}")
            return jsonify({
                'status': 'error',
                'message': 'Transcript cannot be empty'
            }), 400

        # Inject YAML front matter if not already present
        if not transcript.lstrip().startswith('---'):
            transcript = _build_transcript_header(data, transcript)

        # Validate transcript size (256KB limit - covers very long meetings)
        MAX_TRANSCRIPT_SIZE = 256 * 1024  # 256 KB
        transcript_size = len(transcript.encode('utf-8'))
        if transcript_size > MAX_TRANSCRIPT_SIZE:
            logger.warning(f"Transcript too large ({transcript_size} bytes) for title: {title}")
            return jsonify({
                'status': 'error',
                'message': f'Transcript too large ({transcript_size} bytes). Maximum size is {MAX_TRANSCRIPT_SIZE} bytes (256KB).'
            }), 413  # 413 Payload Too Large

        with agent._lock:
            # Optional sync before accepting new work
            if agent.sync_enabled and agent.sync_before_accepting_webhooks:
                try:
                    changed, msg = agent.sync_repo()
                    logger.info(f"Pre-webhook sync: {msg}")
                    if changed:
                        ok, hook_msg = agent._run_hook_on_new_commits()
                        if not ok:
                            logger.warning(hook_msg)
                except Exception as e:
                    logger.warning(f"Pre-webhook sync failed: {e}")

            # Generate filename
            filename = generate_filename(title)
            filepath = os.path.join(agent.inbox_dir, filename)

            # Ensure inbox directory exists
            os.makedirs(agent.inbox_dir, exist_ok=True)

            # Write transcript to file
            with open(filepath, 'w', encoding='utf-8') as f:
                f.write(transcript)

            logger.info(f"Successfully wrote transcript to: {filepath}")

            response_data = {
                'status': 'success',
                'filename': filename,
                'message': 'Transcript queued for processing'
            }

            # Commit and push to git if enabled
            if agent.git_auto_commit:
                logger.info("Initiating git commit...")
                # Ensure repo exists before committing
                if agent.sync_enabled:
                    agent.ensure_repo_checkout()

                commit_ok, commit_msg = agent.git_commit(filepath, title)
                response_data['git'] = {
                    'enabled': True,
                    'committed': commit_ok,
                    'message': commit_msg
                }

                if not commit_ok:
                    # File was saved but git failed - still return success with warning
                    response_data['warning'] = 'File saved but git commit failed'
                    logger.warning(f"Git commit failed but file was saved: {commit_msg}")
                else:
                    # Choose processing mode: standalone (local) or relay (workflow dispatch)
                    if agent.standalone_enabled:
                        if agent.standalone_async:
                            # Async mode: return immediately, process in background
                            agent.run_standalone_processing_async()
                            response_data['processing'] = {
                                'mode': 'standalone',
                                'async': True,
                                'message': 'Processing started in background',
                            }
                        else:
                            # Sync mode: wait for processing to complete
                            proc_ok, proc_msg = agent.run_standalone_processing()
                            response_data['processing'] = {
                                'mode': 'standalone',
                                'async': False,
                                'success': proc_ok,
                                'message': proc_msg,
                            }
                            # Push all commits (inbox + processing results) together
                            if proc_ok and agent.git_auto_push:
                                push_ok, push_msg = agent.git_push()
                                response_data['git']['pushed'] = push_ok
                                response_data['git']['push_message'] = push_msg
                                if not push_ok:
                                    logger.warning(f"Push after standalone processing failed: {push_msg}")
                    else:
                        # Relay mode: push immediately so GitHub Actions can access the file
                        if agent.git_auto_push:
                            push_ok, push_msg = agent.git_push()
                            response_data['git']['pushed'] = push_ok
                            response_data['git']['push_message'] = push_msg
                            if not push_ok:
                                logger.warning(f"Push failed: {push_msg}")
                                # Don't dispatch workflow if push failed
                                response_data['processing'] = {
                                    'mode': 'relay',
                                    'workflow_dispatch': {
                                        'enabled': agent.workflow_dispatch_enabled,
                                        'success': False,
                                        'message': 'Skipped: push failed',
                                    }
                                }
                            else:
                                dispatch_ok, dispatch_msg = agent.maybe_dispatch_workflow(reason=f"webhook:{filename}")
                                response_data['processing'] = {
                                    'mode': 'relay',
                                    'workflow_dispatch': {
                                        'enabled': agent.workflow_dispatch_enabled,
                                        'success': dispatch_ok,
                                        'message': dispatch_msg,
                                    }
                                }
                        else:
                            # Push disabled, just dispatch (workflow may not find the file)
                            dispatch_ok, dispatch_msg = agent.maybe_dispatch_workflow(reason=f"webhook:{filename}")
                            response_data['processing'] = {
                                'mode': 'relay',
                                'workflow_dispatch': {
                                    'enabled': agent.workflow_dispatch_enabled,
                                    'success': dispatch_ok,
                                    'message': dispatch_msg,
                                }
                            }
            else:
                response_data['git'] = {
                    'enabled': False,
                    'message': 'Git operations disabled in config'
                }
                logger.info("Git operations disabled, skipping commit")

        return jsonify(response_data), 200

    except Exception as e:
        logger.error(f"Error processing webhook: {str(e)}", exc_info=True)
        return jsonify({
            'status': 'error',
            'message': f'Internal server error: {str(e)}'
        }), 500


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Meeting Notes Daemon (meetingnotesd)')
    parser.add_argument('--sync-once', action='store_true', help='Run repo bootstrap/sync once and exit')
    parser.add_argument('--debug', action='store_true', help='Enable debug logging')
    args = parser.parse_args()

    # Configure logging based on --debug flag
    logging.getLogger().setLevel(logging.DEBUG if args.debug else logging.INFO)

    logger.info(f"Starting meetingnotesd on {agent.host}:{agent.port}")
    logger.info(f"Inbox directory: {agent.inbox_dir}")
    logger.info(f"Repository: {agent.repo_dir}")
    logger.info(f"Health check: http://{agent.host}:{agent.port}/")
    logger.info(f"Transcript webhook: http://{agent.host}:{agent.port}/webhook")
    logger.info(f"Calendar webhook: http://{agent.host}:{agent.port}/calendar")

    # Ensure repo checkout + initial sync
    if agent.sync_enabled and agent.sync_on_startup:
        with agent._lock:
            try:
                changed, msg = agent.sync_repo()
                logger.info(f"Startup sync: {msg}")
                if changed:
                    ok, hook_msg = agent._run_hook_on_new_commits()
                    if not ok:
                        logger.warning(hook_msg)
            except Exception as e:
                logger.warning(f"Startup sync failed: {e}")

    # Ensure inbox directory exists (might be inside a freshly-cloned repo)
    os.makedirs(agent.inbox_dir, exist_ok=True)

    if args.sync_once:
        logger.info("sync-once complete; exiting")
        raise SystemExit(0)

    agent.start_background_sync()
    app.run(host=agent.host, port=agent.port, debug=False)
