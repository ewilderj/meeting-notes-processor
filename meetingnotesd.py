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

Test with:
curl -X POST http://localhost:9876/webhook \
  -H "Content-Type: application/json" \
  -d '{"title": "Test Meeting", "transcript": "This is a test transcript."}'
"""

from flask import Flask, request, jsonify
import os
import re
from datetime import datetime
import logging
import yaml
from pathlib import Path
import subprocess
import threading
import time
import shlex
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
        self.standalone_working_directory = _get_nested(config, ['processing', 'standalone', 'working_directory'], '.')
        self.standalone_timeout_seconds = int(_get_nested(config, ['processing', 'standalone', 'timeout_seconds'], 300))

        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._sync_thread: threading.Thread | None = None

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
        )

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

    def _get_head_sha(self) -> str | None:
        result = self._run_git(['rev-parse', 'HEAD'], timeout=10)
        if result.returncode != 0:
            return None
        return result.stdout.strip() or None

    def sync_repo(self) -> tuple[bool, str]:
        """Sync local data repo. Returns (changed, message)."""
        if not self.sync_enabled:
            return False, "sync disabled"

        self.ensure_repo_checkout()
        before = self._get_head_sha()

        pull_args = ['pull']
        if self.sync_ff_only:
            pull_args.append('--ff-only')
        pull_args.extend([str(self.git_remote), str(self.git_branch)])

        result = self._run_git(pull_args, timeout=60)
        if result.returncode != 0:
            return False, f"git pull failed: {result.stderr.strip()}"

        after = self._get_head_sha()
        changed = bool(before and after and before != after)
        return changed, ("pulled new commits" if changed else "already up to date")

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

        try:
            result = subprocess.run(
                args,
                cwd=working_dir,
                capture_output=True,
                text=True,
                timeout=self.standalone_timeout_seconds,
                env=env,
            )
        except subprocess.TimeoutExpired:
            return False, f"standalone processing timed out after {self.standalone_timeout_seconds}s"
        except Exception as e:
            return False, f"standalone processing failed: {e}"

        if result.returncode != 0:
            stderr = (result.stderr or '').strip()
            stdout = (result.stdout or '').strip()
            # Include some output for debugging
            detail = stderr or stdout or 'non-zero exit'
            if len(detail) > 200:
                detail = detail[:200] + '...'
            return False, f"standalone processing failed: {detail}"

        logger.info("Standalone processing completed successfully")
        return True, "standalone processing completed"

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

        # Pull before push to avoid conflicts. Prefer ff-only semantics.
        changed, message = self.sync_repo()
        logger.info(f"Sync before push: {message}")
        if changed and self.hook_on_new_commits_enabled:
            ok, hook_msg = self._run_hook_on_new_commits()
            if not ok:
                logger.warning(hook_msg)

        # Push (use configured remote/branch)
        result = self._run_git(['push', str(self.git_remote), str(self.git_branch)], timeout=60)
        if result.returncode != 0:
            return False, f"Git push failed: {result.stderr.strip()}"

        logger.info(f"Git pushed to {self.git_remote}/{self.git_branch}")
        return True, f"Pushed to {self.git_remote}/{self.git_branch}"

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
    return jsonify({
        'status': 'ok',
        'service': 'meetingnotesd',
        'inbox_dir': agent.inbox_dir,
        'repository': agent.repo_dir,
        'port': agent.port,
        'processing_mode': processing_mode,
        'sync': {
            'enabled': agent.sync_enabled,
            'poll_interval_seconds': agent.sync_poll_interval_seconds,
        },
        'standalone': {
            'enabled': agent.standalone_enabled,
            'command': agent.standalone_command if agent.standalone_enabled else None,
        },
        'relay': {
            'workflow_dispatch_enabled': agent.workflow_dispatch_enabled,
            'repo': agent.workflow_dispatch_repo if agent.workflow_dispatch_enabled else None,
            'workflow': agent.workflow_dispatch_workflow if agent.workflow_dispatch_enabled else None,
        },
    }), 200


@app.route('/webhook', methods=['POST'])
def webhook():
    """
    Main webhook endpoint for receiving MacWhisper transcripts.
    
    Expected payload:
    {
        "title": "Meeting title",
        "transcript": "Full transcript text..."
    }
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
                        # Standalone mode: process locally, then push everything together
                        proc_ok, proc_msg = agent.run_standalone_processing()
                        response_data['processing'] = {
                            'mode': 'standalone',
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
    logger.info(f"Webhook endpoint: http://{agent.host}:{agent.port}/webhook")

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
