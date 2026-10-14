#!/bin/bash
set -e

# Weekly stock monitor runner
REPO_DIR="/home/admin/.openclaw/workspace/repos/stock_monitor"
cd "$REPO_DIR"

# Load environment variables
set -a
source "$REPO_DIR/.env"
set +a

# Pass delivery context from cron job environment to Python
export OPENCLAW_DELIVERY_CHANNEL="${OPENCLAW_DELIVERY_CHANNEL:-}"
export OPENCLAW_DELIVERY_TO="${OPENCLAW_DELIVERY_TO:-}"

# Use provided date or compute Saturday date in Asia/Shanghai
if [ -n "$1" ]; then
    REPORT_DATE="$1"
else
    REPORT_DATE=$(TZ=Asia/Shanghai date -d "last Saturday" +%Y-%m-%d 2>/dev/null || date +%Y-%m-%d)
fi

echo "Running stock monitor for date: $REPORT_DATE"

# Run the pipeline and capture progress messages
"$REPO_DIR/.venv/bin/python" "$REPO_DIR/orchestrator.py" \
    --all \
    --date "$REPORT_DATE" \
    2>&1 | while IFS= read -r line; do
        echo "$line"
        # Check for progress marker and send WhatsApp message if in OpenClaw context
        if [[ "$line" =~ ^\[PROGRESS_MESSAGE\]\  ]]; then
            msg="${line#\[PROGRESS_MESSAGE\] }"
            if [ -n "$OPENCLAW_DELIVERY_CHANNEL" ] && [ -n "$OPENCLAW_DELIVERY_TO" ]; then
                # This will be picked up by the isolated session handler
                echo "📱 Sending progress: $msg" >&2
            fi
        fi
    done

exit_code=$?

if [ $exit_code -eq 0 ]; then
    echo "✓ Stock monitor completed successfully"
elif [ $exit_code -eq 2 ]; then
    echo "⚠ Stock monitor completed with warnings (partial success)"
    # Read and report status
    if [ -f "$REPO_DIR/reports/$REPORT_DATE/status.json" ]; then
        cat "$REPO_DIR/reports/$REPORT_DATE/status.json"
    fi
else
    echo "✗ Stock monitor failed with exit code $exit_code"
fi

exit $exit_code
