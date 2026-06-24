import hashlib
import hmac
import os

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, Header, HTTPException, Request

load_dotenv()

TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]
KAMDENAI_SECRET = os.environ.get("KAMDENAI_SECRET", "")

TELEGRAM_API = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"

app = FastAPI()


def verify_signature(body: bytes, signature: str) -> bool:
    if not KAMDENAI_SECRET:
        return True  # skip verification if no secret configured
    expected = hmac.new(KAMDENAI_SECRET.encode(), body, hashlib.sha256).hexdigest()
    # handle both raw hex and "sha256=<hex>" prefix formats
    sig = signature.removeprefix("sha256=")
    return hmac.compare_digest(expected, sig)


def format_message(event: str, payload: dict) -> str:
    data = payload.get("data", payload)
    date_key = payload.get("dateKey", "")

    if event == "morning_scan.created":
        scan = data.get("scan", {})
        market = scan.get("marketStatus", "?")
        market_detail = scan.get("marketDetail", "")
        watch = scan.get("watchSignals", [])
        lines = [
            f"📋 9:28 AM Plan Scan — {date_key}",
            f"Market: {market} — {market_detail}",
        ]
        if watch:
            lines.append("\nWatch List:")
            for s in watch:
                ticker = s.get("ticker", "?")
                name = s.get("name", "")
                entry = s.get("entry", "?")
                stop = s.get("stop", "?")
                target = s.get("target", "?")
                shares = s.get("shares", "?")
                lines.append(
                    f"  • {ticker} ({name})\n"
                    f"    Entry ${entry} | Stop ${stop} | Target ${target} | {shares} shares"
                )
        else:
            lines.append("No watch signals today.")
        return "\n".join(lines)

    if event == "confirmed_buys.created":
        buys = data.get("confirmedBuys", [])
        market = data.get("marketStatus", "?")
        market_detail = data.get("marketDetail", "")
        lines = [
            f"✅ 9:30 AM Confirmed Buys — {date_key}",
            f"Market: {market} — {market_detail}",
        ]
        if buys:
            for b in buys:
                ticker = b.get("ticker", "?")
                name = b.get("name", "")
                entry = b.get("entry", "?")
                stop = b.get("stop", "?")
                target = b.get("target", "?")
                shares = b.get("shares", "?")
                risk = b.get("riskDollars", "?")
                label = b.get("openingConfirmation", {}).get("statusLabel", "")
                lines.append(
                    f"\n  {ticker} ({name}) — {label}\n"
                    f"  Entry ${entry} | Stop ${stop} | Target ${target}\n"
                    f"  {shares} shares | Risk ${risk}"
                )
        else:
            lines.append("No confirmed buys.")
        return "\n".join(lines)

    if event == "quick_exit_results.created":
        results = data.get("quickExitResults", [])
        confirmed_count = data.get("confirmedBuyCount", 0)
        no_buys = data.get("noConfirmedBuys", False)
        lines = [f"💰 10:05 AM Quick Exit — {date_key}"]
        if no_buys:
            lines.append("No confirmed buys today.")
        else:
            total_profit = 0
            for r in results:
                ticker = r.get("ticker", "?")
                entry = r.get("entry", "?")
                shares = r.get("shares", "?")
                qe = r.get("result", {}).get("quickExit", {})
                price = qe.get("price", "?")
                profit = qe.get("profitDollars", 0)
                r_val = qe.get("r", "?")
                status = qe.get("statusLabel", "?")
                total_profit += profit or 0
                emoji = "🟢" if (profit or 0) > 0 else "🔴"
                lines.append(
                    f"\n  {emoji} {ticker} — {status}\n"
                    f"  Entry ${entry} → Exit ${price} | {shares} shares\n"
                    f"  P&L: ${profit} ({r_val}R)"
                )
            lines.append(f"\nTotal P&L: ${total_profit:.2f}")
        return "\n".join(lines)

    # fallback — plain text to avoid Markdown parse errors
    import json
    return f"{event}\n{date_key}\n{json.dumps(data, indent=2)}"


async def send_telegram(text: str) -> None:
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            f"{TELEGRAM_API}/sendMessage",
            json={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": text,
            },
        )
    if not resp.is_success:
        print(f"Telegram error {resp.status_code}: {resp.text}")
    resp.raise_for_status()


@app.post("/kamdenai/webhook")
async def kamdenai_webhook(
    request: Request,
    x_kamdenai_event: str = Header(...),
    x_kamdenai_delivery: str = Header(...),
    x_kamdenai_signature: str = Header(""),
):
    body = await request.body()

    if not verify_signature(body, x_kamdenai_signature):
        raise HTTPException(status_code=401, detail="Invalid signature")

    payload = await request.json()
    text = format_message(x_kamdenai_event, payload)

    try:
        await send_telegram(text)
    except httpx.HTTPStatusError as exc:
        raise HTTPException(status_code=502, detail=f"Telegram error: {exc}") from exc

    return {"ok": True, "event": x_kamdenai_event, "delivery": x_kamdenai_delivery}


@app.get("/health")
async def health():
    return {"status": "ok"}
