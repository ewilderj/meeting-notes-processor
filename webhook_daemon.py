#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "flask>=3.0.0",
#     "pyyaml>=6.0.0",
# ]
# ///
"""
MacWhisper Webhook Daemon

Receives webhooks from MacWhisper and writes transcripts to inbox.
Configuration is loaded from config.yaml.

Run with: uv run --with flask --with pyyaml python webhook_daemon.py

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

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

app = Flask(__name__)

# Load configuration
CONFIG_FILE = os.getenv('WEBHOOK_CONFIG', 'config.yaml')

def load_config():
    """Load configuration from YAML file."""
    config_path = Path(CONFIG_FILE)
    if not config_path.exists():
        logger.error(f"Configuration file not found: {CONFIG_FILE}")
        raise FileNotFoundError(f"Configuration file not found: {CONFIG_FILE}")
    
    with open(config_path, 'r') as f:
        return yaml.safe_load(f)

config = load_config()

# Extract configuration values
HOST = config['server']['host']
PORT = config['server']['port']
INBOX_DIR = config['directories']['inbox']
REPO_DIR = config['directories']['repository']
GIT_AUTO_COMMIT = config['git']['auto_commit']
GIT_AUTO_PUSH = config['git']['auto_push']
GIT_REPO_URL = config['git']['repository_url']
GIT_COMMIT_TEMPLATE = config['git']['commit_message_template']


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


def git_commit_and_push(filepath, title):
    """
    Commit and push the file to git repository.
    
    Args:
        filepath: Path to the file to commit
        title: Title for commit message
    
    Returns:
        tuple: (success: bool, message: str)
    """
    try:
        # Change to repository directory
        repo_path = Path(REPO_DIR).resolve()
        github_token = os.environ.get('GH_TOKEN')
        
        # Convert filepath to absolute path, then make it relative to the repo
        file_abs_path = Path(filepath).resolve()
        file_rel_path = file_abs_path.relative_to(repo_path)
        
        # Git add the file using the relative path from within the repo
        result = subprocess.run(
            ['git', 'add', str(file_rel_path)],
            cwd=repo_path,
            capture_output=True,
            text=True,
            timeout=10
        )
        
        if result.returncode != 0:
            error_msg = f"Git add failed: {result.stderr}"
            logger.error(error_msg)
            return False, error_msg
        
        logger.info(f"Git added: {file_rel_path}")
        
        # Git commit
        commit_message = GIT_COMMIT_TEMPLATE.format(title=title)
        result = subprocess.run(
            ['git', 'commit', '-m', commit_message],
            cwd=repo_path,
            capture_output=True,
            text=True,
            timeout=10
        )
        
        if result.returncode != 0:
            error_msg = f"Git commit failed: {result.stderr}"
            logger.error(error_msg)
            return False, error_msg
        
        logger.info(f"Git committed: {commit_message}")
        
        # Git push if enabled
        if GIT_AUTO_PUSH:
            # Pull first to avoid conflicts
            logger.info("Pulling latest changes before push...")
            
            remote_url = f"https://{github_token}@{GIT_REPO_URL}" if github_token else 'origin'
            
            result = subprocess.run(
                ['git', 'pull', '--rebase', remote_url, 'main'],
                cwd=repo_path,
                capture_output=True,
                text=True,
                timeout=30
            )
            
            if result.returncode != 0:
                error_msg = f"Git pull failed: {result.stderr}"
                logger.error(error_msg)
                return False, error_msg
            
            logger.info("Git pull successful")
            
            # Now push
            result = subprocess.run(
                ['git', 'push', remote_url, 'main'],
                cwd=repo_path,
                capture_output=True,
                text=True,
                timeout=30
            )
            
            if result.returncode != 0:
                error_msg = f"Git push failed: {result.stderr}"
                logger.error(error_msg)
                return False, error_msg
            
            logger.info("Git pushed to origin/main")
            return True, "Committed and pushed to repository"
        else:
            return True, "Committed to repository (push disabled)"
            
    except subprocess.TimeoutExpired:
        error_msg = "Git operation timed out"
        logger.error(error_msg)
        return False, error_msg
    except Exception as e:
        error_msg = f"Git operation failed: {str(e)}"
        logger.error(error_msg, exc_info=True)
        return False, error_msg


@app.route('/', methods=['GET'])
def health_check():
    """Health check endpoint."""
    return jsonify({
        'status': 'ok',
        'service': 'MacWhisper Webhook Daemon',
        'inbox_dir': INBOX_DIR,
        'repository': REPO_DIR,
        'port': PORT
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
        
        # Generate filename
        filename = generate_filename(title)
        filepath = os.path.join(INBOX_DIR, filename)
        
        # Ensure inbox directory exists
        os.makedirs(INBOX_DIR, exist_ok=True)
        
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
        if GIT_AUTO_COMMIT:
            logger.info("Initiating git commit...")
            success, git_message = git_commit_and_push(filepath, title)
            response_data['git'] = {
                'enabled': True,
                'success': success,
                'message': git_message
            }
            
            if not success:
                # File was saved but git failed - still return success with warning
                response_data['warning'] = 'File saved but git operation failed'
                logger.warning(f"Git operation failed but file was saved: {git_message}")
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
    logger.info(f"Starting MacWhisper Webhook Daemon on {HOST}:{PORT}")
    logger.info(f"Inbox directory: {INBOX_DIR}")
    logger.info(f"Health check: http://{HOST}:{PORT}/")
    logger.info(f"Webhook endpoint: http://{HOST}:{PORT}/webhook")
    
    # Ensure inbox directory exists
    os.makedirs(INBOX_DIR, exist_ok=True)
    
    # Run Flask app
    app.run(host=HOST, port=PORT, debug=False)
