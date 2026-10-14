#!/usr/bin/env python3
import argparse
import json
import logging
import os
import re
import signal
import sys
from datetime import date, datetime, timezone
from pathlib import Path
from equity.models import Settings
from equity.pipeline import Pipeline


def send_progress_message(completed, last_ticker, total=None):
    """Send progress update via WhatsApp using OpenClaw message tool."""
    try:
        # Check if we're in an OpenClaw isolated session with WhatsApp delivery
        delivery_channel = os.environ.get('OPENCLAW_DELIVERY_CHANNEL')
        delivery_to = os.environ.get('OPENCLAW_DELIVERY_TO')
        
        if not delivery_channel or not delivery_to:
            # Not in OpenClaw delivery context, log instead
            if total is not None:
                logging.info(f'Progress: Starting processing of {total} tickers')
            else:
                logging.info(f'Progress: {completed} tickers completed (latest: {last_ticker})')
            return
        
        # Build progress message
        if total is not None:
            message = f"📊 Stock Monitor: Starting processing of {total} tickers"
        else:
            message = f"✅ Progress: {completed} tickers completed (latest: {last_ticker})"
        
        # Output special marker that OpenClaw isolated session will recognize
        print(f'[PROGRESS_MESSAGE] {message}', flush=True)
        
    except Exception as e:
        logging.warning(f'Failed to send progress message: {e}')


def parse_date(s):
    """Parse YYYY-MM-DD into date object (Python 3.6 compatible)."""
    return datetime.strptime(s, '%Y-%m-%d').date()


def main(argv=None):
    p = argparse.ArgumentParser(description='Checkpointed weekly research; deterministic orchestration, no scheduler registration')
    selection = p.add_mutually_exclusive_group(required=True)
    selection.add_argument('--tickers',nargs='+')
    selection.add_argument('--all',action='store_true')
    selection.add_argument('--resume',action='store_true')
    selection.add_argument('--status',action='store_true')
    p.add_argument('--date',required=True,type=parse_date)
    p.add_argument('--config',default='config/settings.json')
    p.add_argument('--version',default='v1')
    p.add_argument('--db')
    p.add_argument('--output')
    p.add_argument('--demo',action='store_true',help='Synthetic fixtures, no external API calls; isolated demo DB/output')
    args = p.parse_args(argv)
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,40}',args.version): p.error('Invalid report version')
    if args.date>datetime.now(timezone.utc).date(): p.error('Report date must not be in the future')
    try:
        settings = Settings.parse_raw(Path(args.config).read_text())
        tickers = Settings(watchlist=args.tickers).watchlist if args.tickers else settings.watchlist
        db = args.db or ('state/demo.sqlite3' if args.demo else 'state/research.sqlite3')
        output = args.output or ('reports/demo' if args.demo else 'reports')
        logging.basicConfig(level=logging.INFO,format='%(asctime)s %(levelname)s %(message)s')
        pipeline = Pipeline(settings,db,output,args.date,args.version,args.demo,progress_callback=send_progress_message)
        if args.status:
            print(json.dumps(pipeline.status(),indent=2)); return 0
        if args.resume:
            tickers = [j['ticker'] for j in pipeline.store.jobs(args.date.isoformat(),args.version)]
            if not tickers: p.error('No persisted jobs for this date/version; use --all or --tickers first')
        old_handlers = {}
        for sig in (signal.SIGINT,signal.SIGTERM):
            old_handlers[sig] = signal.signal(sig,lambda signum,frame:pipeline.stop.set())
        try: result = pipeline.run(tickers)
        finally:
            for sig,handler in old_handlers.items(): signal.signal(sig,handler)
        print(json.dumps(result,indent=2))
        return 0 if result['outcome']=='complete' else 2
    except Exception as exc:
        # Do not expose provider credentials through validation errors or HTTP exceptions.
        import traceback
        traceback.print_exc()  # Temporary: show full trace for debugging
        print(f'Configuration/runtime error: {type(exc).__name__}. Check documented configuration and credentials.',file=sys.stderr)
        return 1


if __name__=='__main__':
    sys.exit(main())
