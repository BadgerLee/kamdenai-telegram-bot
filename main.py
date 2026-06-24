import base64
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


def verify_signature(body: bytes, signature: str, delivery: str = "") -> bool:
    if not KAMDENAI_SECRET:
        return True
    raw_secret = base64.b64decode(KAMDENAI_SECRET.removeprefix("whsec_"))
    sig = signature.removeprefix("v1=")

    candidates = {
        "body only":            body,
        "delivery.body":        f"{delivery}.".encode() + body,
        "body.delivery":        body + f".{delivery}".encode(),
    }
    for label, msg in candidates.items():
        computed = hmac.new(raw_secret, msg, hashlib.sha256).hexdigest()
        print(f"DEBUG [{label}] computed: {computed}")

    print(f"DEBUG received: {sig}")
    # still try body-only for now
    expected = hmac.new(raw_secret, body, hashlib.sha256).hexdigest()
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
            f"📋 <b>9:28 AM Plan Scan</b> — {date_key}",
            f"Market: <b>{market}</b> — {market_detail}",
        ]
        if watch:
            lines.append("\n<b>Watch List:</b>")
            for s in watch:
                ticker = s.get("ticker", "?")
                name = s.get("name", "")
                entry = s.get("entry", "?")
                stop = s.get("stop", "?")
                target = s.get("target", "?")
                shares = s.get("shares", "?")
                lines.append(
                    f"  • <b>{ticker}</b> ({name})\n"
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
            f"✅ <b>9:30 AM Confirmed Buys</b> — {date_key}",
            f"Market: <b>{market}</b> — {market_detail}",
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
                    f"\n  <b>{ticker}</b> ({name}) — {label}\n"
                    f"  Entry ${entry} | Stop ${stop} | Target ${target}\n"
                    f"  {shares} shares | Risk ${risk}"
                )
        else:
            lines.append("No confirmed buys.")
        return "\n".join(lines)

    if event == "quick_exit_results.created":
        results = data.get("quickExitResults", [])
        no_buys = data.get("noConfirmedBuys", False)
        lines = [f"💰 <b>10:05 AM Quick Exit</b> — {date_key}"]
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
                    f"\n  {emoji} <b>{ticker}</b> — {status}\n"
                    f"  Entry ${entry} → Exit ${price} | {shares} shares\n"
                    f"  P&amp;L: <b>${profit}</b> ({r_val}R)"
                )
            lines.append(f"\n<b>Total P&amp;L: ${total_profit:.2f}</b>")
        return "\n".join(lines)

    if event == "webhook.test":
        return "✅ KamdenAI webhook connected successfully!"

    import json
    return f"{event}\n{date_key}\n{json.dumps(data, indent=2)}"


async def send_telegram(text: str, use_html: bool = False) -> None:
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": text}
    if use_html:
        payload["parse_mode"] = "HTML"
    async with httpx.AsyncClient() as client:
        resp = await client.post(f"{TELEGRAM_API}/sendMessage", json=payload)
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

    if not verify_signature(body, x_kamdenai_signature, x_kamdenai_delivery):
        raise HTTPException(status_code=401, detail="Invalid signature")

    payload = await request.json()
    text = format_message(x_kamdenai_event, payload)
    use_html = x_kamdenai_event in ("morning_scan.created", "confirmed_buys.created", "quick_exit_results.created", "webhook.test")

    try:
        await send_telegram(text, use_html=use_html)
    except httpx.HTTPStatusError as exc:
        raise HTTPException(status_code=502, detail=f"Telegram error: {exc}") from exc

    return {"ok": True, "event": x_kamdenai_event, "delivery": x_kamdenai_delivery}


@app.get("/health")
async def health():
    return {"status": "ok"}
