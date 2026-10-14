#!/bin/bash
# Load environment variables from .env file and run orchestrator

# Read .env file and export each variable
set -a
source /home/admin/.openclaw/.env
set +a

# Run orchestrator with all arguments passed through
exec .venv/bin/python orchestrator.py "$@"
