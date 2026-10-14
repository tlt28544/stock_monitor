#!/usr/bin/env python3
"""Weekly entry point for five-search monthly news narratives."""
import argparse
import json
import logging
import os
import re
import signal
import sys
from datetime import date, datetime
from pathlib import Path

from equity.models import Settings, REPORT_ZONE
from equity.pipeline import Pipeline

PROJECT_DIR = Path(__file__).resolve().parent


def send_progress_message(completed, last_ticker, total=None):
    message = (f'Stock Monitor: Starting {total} companies' if total is not None else
               f'Stock Monitor: Processed {completed} companies (latest: {last_ticker}); see final status for failures')
    if os.getenv('OPENCLAW_DELIVERY_CHANNEL') and os.getenv('OPENCLAW_DELIVERY_TO'):
        print('[PROGRESS_MESSAGE] ' + message, flush=True)
    else:
        logging.info(message)


def main(argv=None):
    parser = argparse.ArgumentParser(description='Monthly news -> LLM narrative -> PDF; run weekly')
    selection = parser.add_mutually_exclusive_group(required=True)
    selection.add_argument('--tickers', nargs='+')
    selection.add_argument('--all', action='store_true')
    selection.add_argument('--resume', action='store_true')
    selection.add_argument('--status', action='store_true')
    parser.add_argument('--date', type=date.fromisoformat, default=datetime.now(REPORT_ZONE).date())
    parser.add_argument('--config', default=str(PROJECT_DIR / 'config/settings.json'))
    parser.add_argument('--version', default='v2')
    parser.add_argument('--db')
    parser.add_argument('--output')
    parser.add_argument('--demo', action='store_true', help='Offline synthetic fixtures; isolated state/output')
    args = parser.parse_args(argv)
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,40}', args.version):
        parser.error('Invalid report version')
    if args.date > datetime.now(REPORT_ZONE).date():
        parser.error('Report date must not be in the future')
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    try:
        settings = Settings.model_validate_json(Path(args.config).read_text())
        tickers = Settings(watchlist=args.tickers).watchlist if args.tickers else settings.watchlist
        db = args.db or str(PROJECT_DIR / ('state/monthly-demo.sqlite3' if args.demo else 'state/monthly.sqlite3'))
        output = args.output or str(PROJECT_DIR / ('reports/demo' if args.demo else 'reports'))
        pipeline = Pipeline(settings, db, output, args.date, args.version, args.demo,
                            progress_callback=send_progress_message)
        if args.status:
            print(json.dumps(pipeline.status(), indent=2))
            return 0
        if args.resume:
            tickers = [j['ticker'] for j in pipeline.store.jobs(args.date.isoformat(), args.version)]
            if not tickers:
                parser.error('No persisted jobs for this date/version; use --all or --tickers first')
        handlers = {}
        for sig in (signal.SIGINT, signal.SIGTERM):
            handlers[sig] = signal.signal(sig, lambda signum, frame: pipeline.stop.set())
        try:
            result = pipeline.run(tickers)
        finally:
            for sig, handler in handlers.items():
                signal.signal(sig, handler)
        print(json.dumps(result, indent=2))
        return 0 if result['outcome'] == 'complete' else 2
    except Exception as exc:
        message = 'Configuration changed; use a new --version.' if isinstance(exc, ValueError) and str(exc).startswith('Configuration changed') else type(exc).__name__
        print(f'Configuration/runtime error: {message} Check configuration, credentials and run status.', file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
