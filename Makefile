# Meeting Notes Processor — deploy to nuctu
#
# The daemon (meetingnotesd) runs on nuctu as a system-level systemd service.
# Deploy by pushing to git, pulling on nuctu, and restarting the service.
# Note: restart requires sudo (will prompt for password via interactive SSH).
#
# Usage: make deploy

REMOTE_HOST := edd@nuctu
REMOTE_DIR := ~/git/meeting-notes-processor
SERVICE := meetingnotes-webhook

.PHONY: deploy status logs restart ssh push pull runtime

# ---------------------------------------------------------------------------
# Deployment
# ---------------------------------------------------------------------------

deploy: push pull restart  ## Push to git, pull on nuctu, restart service

push:  ## Push local changes to remote
	@git push

pull:  ## Pull latest code on nuctu
	@echo "=== Pulling on nuctu ==="
	@ssh $(REMOTE_HOST) 'cd $(REMOTE_DIR) && git pull --ff-only && make runtime'

runtime:  ## Install webhook and summarizer dependencies through CFS before starting services
	bash transcriber/server/prepare-runtime.sh "$(CURDIR)/.venv" "$(CURDIR)/service-configs/uv-cfs.toml" meetingnotesd.py run_summarization.py

# ---------------------------------------------------------------------------
# Service management (requires sudo — will prompt for password)
# ---------------------------------------------------------------------------

restart:  ## Restart the service on nuctu (needs sudo)
	@echo "=== Restarting service (sudo required) ==="
	ssh -t $(REMOTE_HOST) 'sudo systemctl restart $(SERVICE) && sleep 2 && sudo systemctl is-active $(SERVICE)'

status:  ## Show service status on nuctu
	ssh -t $(REMOTE_HOST) 'sudo systemctl status $(SERVICE) --no-pager'

logs:  ## Tail service logs on nuctu
	ssh -t $(REMOTE_HOST) 'sudo journalctl -t $(SERVICE) -f --no-pager'

ssh:  ## SSH to nuctu
	@ssh $(REMOTE_HOST)
