#!/usr/bin/env python3
"""
Entry point for the LinkedIn job automation bot.

Usage:
  python run_bot.py
  python run_bot.py --config path/to/config.yaml
  python run_bot.py --dry-run   # scrape only, don't apply
"""
import argparse
import sys
from pathlib import Path

def main():
    parser = argparse.ArgumentParser(description="LinkedIn Job Automation Bot")
    parser.add_argument("--config", default=None, help="Path to config.yaml")
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Discover jobs and save them but do NOT submit any applications"
    )
    parser.add_argument(
        "--debug", action="store_true",
        help="Save screenshots + dump selectors to diagnose 'no jobs found' issues"
    )
    args = parser.parse_args()

    # Lazy import so missing playwright gives a friendly error
    try:
        from bot.linkedin import run
        from bot.database import init_db
        from bot.utils import load_config
    except ImportError as e:
        print(f"\n❌  Import error: {e}")
        print("   Run:  pip install -r requirements.txt && playwright install chromium\n")
        sys.exit(1)

    cfg = load_config(args.config)
    init_db()

    if args.dry_run:
        print("\n⚠️  Dry-run mode — jobs will be discovered but NOT applied to.\n")
        # Patch apply method to skip submission
        import bot.linkedin as _bot
        _bot._do_easy_apply = lambda page, cfg, job: (
            print(f"  [dry-run] Would easy-apply to {job['title']}") or False
        )

    print("\n🤖  Starting LinkedIn Job Bot …\n")
    counters = run(config=cfg, debug=args.debug)
    print(f"\n✅  Done!  {counters}\n")
    print("   Open the portal:  python portal/app.py  →  http://127.0.0.1:5000\n")


if __name__ == "__main__":
    main()
