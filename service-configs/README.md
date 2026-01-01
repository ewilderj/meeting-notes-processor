# Service Configuration Files

This directory contains configuration files to run the webhook daemon as a proper system service.

## macOS (launchd)

1. **Edit the plist file** to set correct paths:
   ```bash
   # Copy and customize
   cp com.meetingnotes.webhook.plist ~/Library/LaunchAgents/
   
   # Edit to set your actual paths and environment
   nano ~/Library/LaunchAgents/com.meetingnotes.webhook.plist
   ```

2. **Required customizations:**
   - `WorkingDirectory`: Path to your processor repo
   - `GH_TOKEN`: Your GitHub token (for git push)
   - `WEBHOOK_CONFIG`: Path to your config.yaml (optional, defaults to ./config.yaml)

3. **Load the service:**
   ```bash
   launchctl load ~/Library/LaunchAgents/com.meetingnotes.webhook.plist
   ```

4. **Manage the service:**
   ```bash
   # Check status
   launchctl list | grep meetingnotes
   
   # Stop
   launchctl stop com.meetingnotes.webhook
   
   # Start
   launchctl start com.meetingnotes.webhook
   
   # Unload completely
   launchctl unload ~/Library/LaunchAgents/com.meetingnotes.webhook.plist
   ```

5. **View logs:**
   ```bash
   tail -f /tmp/meetingnotes-webhook.log
   tail -f /tmp/meetingnotes-webhook.err
   ```

## Linux (systemd)

1. **Edit the service file** to set correct paths:
   ```bash
   # Copy and customize
   sudo cp meetingnotes-webhook.service /etc/systemd/system/
   
   # Edit to set your actual paths, user, and environment
   sudo nano /etc/systemd/system/meetingnotes-webhook.service
   ```

2. **Required customizations:**
   - `User`: Your username
   - `WorkingDirectory`: Path to your processor repo
   - `Environment="GH_TOKEN=..."`: Your GitHub token
   - `ExecStart`: Ensure `uv` path is correct (use `which uv` to find it)

3. **Enable and start the service:**
   ```bash
   sudo systemctl daemon-reload
   sudo systemctl enable meetingnotes-webhook
   sudo systemctl start meetingnotes-webhook
   ```

4. **Manage the service:**
   ```bash
   # Check status
   sudo systemctl status meetingnotes-webhook
   
   # Stop
   sudo systemctl stop meetingnotes-webhook
   
   # Restart
   sudo systemctl restart meetingnotes-webhook
   
   # Disable autostart
   sudo systemctl disable meetingnotes-webhook
   ```

5. **View logs:**
   ```bash
   # Application logs (by syslog identifier)
   journalctl -t meetingnotes-webhook -f
   
   # Service status/lifecycle logs (by unit name)
   journalctl -u meetingnotes-webhook -f
   ```

## Environment Variables

Both service configs need these environment variables:

| Variable | Required | Description |
|----------|----------|-------------|
| `GH_TOKEN` | Yes (for git push) | GitHub personal access token |
| `WEBHOOK_CONFIG` | No | Path to config.yaml (defaults to ./config.yaml) |

## Node.js Version (nvm users)

Systemd doesn't source `.bashrc`, so nvm-managed Node.js isn't available by default. The service will use the system Node (often too old for Copilot CLI).

**Fix:** Add your nvm node path to the systemd `PATH` environment:

```ini
Environment="PATH=/home/USER/.nvm/versions/node/vXX.X.X/bin:/usr/local/bin:/usr/bin:/bin:/snap/bin"
```

Find your node path with `which node`.

## Git Authentication Setup

The `GH_TOKEN` environment variable is used by the GitHub API (for workflow_dispatch), but **git itself** needs separate configuration to use the token for push/pull operations.

### Problem: SSH vs HTTPS

If your data repo uses SSH (`git@github.com:...`), git will prompt for your SSH key passphrase, which hangs when running as a service.

**Check your remote URL:**
```bash
cd /path/to/your/meeting-notes
git remote -v
```

### Solution: Use HTTPS with Token

1. **Switch to HTTPS and embed token in URL:**
   ```bash
   cd /path/to/your/meeting-notes
   git remote set-url origin https://YOUR_USERNAME:${GH_TOKEN}@github.com/YOUR_USERNAME/meeting-notes.git
   ```
   
   Replace `YOUR_USERNAME` with your GitHub username and ensure `GH_TOKEN` is set.

2. **Or use a credential helper** (token stays in environment only):
   ```bash
   cd /path/to/your/meeting-notes
   git config credential.helper '!f() { echo "username=YOUR_USERNAME"; echo "password=${GH_TOKEN}"; }; f'
   ```

### Preventing Auth Prompts

The systemd service includes these settings to prevent git from hanging on auth failures:

```ini
Environment="GIT_ASKPASS=/bin/true"
Environment="GIT_TERMINAL_PROMPT=0"
```

This makes git fail fast instead of waiting for input that will never come.

## Security Notes

- The plist/service files contain your `GH_TOKEN`. Protect file permissions accordingly.
- On Linux, consider using systemd's `EnvironmentFile=` directive to load secrets from a separate file.
- The webhook listens on `127.0.0.1:9876` by default (localhost only). Change `server.host` in config.yaml if you need external access.
