"""
auto_stage_orders.py
Fetches today's 9:28 watch list from the Railway bot and stages IBKR order
instructions via Claude Code. Run via Windows Task Scheduler at 9:28 PM SGT
(= 9:28 AM ET).
"""
import os
import subprocess
import sys

import httpx
from dotenv import load_dotenv

load_dotenv()

RAILWAY_URL = os.environ["RAILWAY_URL"]          # e.g. https://kamdenai-telegram-bot-production.up.railway.app
CALLBACK_SECRET = os.environ["TELEGRAM_CALLBACK_SECRET"]


def build_prompt(watch: list, date_key: str) -> str:
    lines = [
        f"Stage IBKR limit buy order instructions for today's {date_key} watch list.",
        "Create all instructions now — do not confirm any of them. I will confirm the 2 official picks at 9:30.\n",
    ]
    for s in watch:
        lines.append(
            f"Buy {s['shares']} shares of {s['ticker']} at a limit of ${s['entry']}"
        )
    return "\n".join(lines)


def main() -> None:
    print("Fetching latest scan from Railway...")
    resp = httpx.get(f"{RAILWAY_URL}/last-scan/{CALLBACK_SECRET}", timeout=10)
    resp.raise_for_status()
    data = resp.json()

    watch = data.get("watch", [])
    date_key = data.get("date_key", "today")

    if not watch:
        print("No watch signals found — skipping.")
        sys.exit(0)

    print(f"Staging {len(watch)} orders for {date_key}...")
    prompt = build_prompt(watch, date_key)

    result = subprocess.run(
        ["claude", "-p", prompt],
        capture_output=True,
        text=True,
    )

    if result.returncode != 0:
        print(f"Claude error:\n{result.stderr}")
        sys.exit(1)

    print("Done. Claude output:")
    print(result.stdout)


if __name__ == "__main__":
    main()
