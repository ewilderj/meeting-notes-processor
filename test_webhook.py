#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "requests>=2.31.0",
# ]
# ///
"""
Test script for webhook daemon

Sends a transcript file to the webhook daemon for processing.

Usage:
    uv run test_webhook.py <transcript_file>
    
Example:
    uv run test_webhook.py examples/q1-planning-sarah.txt
"""

import sys
import os
import requests
import json

def send_to_webhook(filepath, webhook_url="http://localhost:9876/webhook"):
    """Send a transcript file to the webhook daemon."""
    
    # Check if file exists
    if not os.path.exists(filepath):
        print(f"Error: File not found: {filepath}")
        return False
    
    # Read the transcript
    try:
        with open(filepath, 'r', encoding='utf-8') as f:
            transcript = f.read()
    except Exception as e:
        print(f"Error reading file: {e}")
        return False
    
    # Extract title from first line or use filename
    lines = transcript.strip().split('\n')
    if lines:
        title = lines[0].strip()
    else:
        title = os.path.basename(filepath)
    
    # Prepare payload
    payload = {
        'title': title,
        'transcript': transcript
    }
    
    # Send to webhook
    print(f"Sending to webhook: {webhook_url}")
    print(f"Title: {title}")
    print(f"Transcript size: {len(transcript)} bytes")
    print()
    
    try:
        response = requests.post(
            webhook_url,
            json=payload,
            headers={'Content-Type': 'application/json'}
        )
        
        print(f"Response status: {response.status_code}")
        print(f"Response body:")
        print(json.dumps(response.json(), indent=2))
        
        return response.status_code == 200
        
    except requests.exceptions.ConnectionError:
        print("Error: Could not connect to webhook daemon.")
        print("Make sure it's running: uv run webhook_daemon.py")
        return False
    except Exception as e:
        print(f"Error sending request: {e}")
        return False


def main():
    if len(sys.argv) != 2:
        print("Usage: uv run test_webhook.py <transcript_file>")
        print()
        print("Examples:")
        print("  uv run test_webhook.py examples/q1-planning-sarah.txt")
        print("  uv run test_webhook.py examples/dunder-mifflin-sales.txt")
        print("  uv run test_webhook.py examples/mad-men-heinz.txt")
        sys.exit(1)
    
    filepath = sys.argv[1]
    success = send_to_webhook(filepath)
    sys.exit(0 if success else 1)


if __name__ == '__main__':
    main()
