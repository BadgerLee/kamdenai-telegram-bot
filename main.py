import hashlib
import hmac
import json
import os
import secrets
import time

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, Header, HTTPException, Request

load_dotenv()

TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]
KAMDENAI_SECRET = os.environ.get("KAMDENAI_SECRET", "")
TELEGRAM_CALLBACK_SECRET = os.environ.get("TELEGRAM_CALLBACK_SECRET", "")

TELEGRAM_API = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"

app = FastAPI()

# In-memory pending orders. Wiped on Railway restart — fine for a same-day approval flow.
PENDING_ORDERS: dict[str, dict] = {}

# Positions we have opened today (ticker -> {shares, entry}). Closed at 10:05.
OPEN_POSITIONS: dict[str, dict] = {}


def verify_signature(body: bytes, signature: str, delivery: str = "", timestamp: str = "") -> bool:
    if not KAMDENAI_SECRET:
        return True
    sig = signature.removeprefix("v1=")
    signed_content = f"{timestamp}.".encode() + body
    expected = hmac.new(KAMDENAI_SECRET.encode(), signed_content, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, sig)


async def telegram(method: str, payload: dict) -> dict:
    async with httpx.AsyncClient() as client:
        resp = await client.post(f"{TELEGRAM_API}/{method}", json=payload)
    if not resp.is_success:
        print(f"Telegram {method} error {resp.status_code}: {resp.text}")
    resp.raise_for_status()
    return resp.json()


async def send_message(text: str, reply_markup: dict | None = None) -> dict:
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "HTML"}
    if reply_markup:
        payload["reply_markup"] = reply_markup
    return await telegram("sendMessage", payload)


async def edit_message(chat_id: int, message_id: int, text: str) -> None:
    await telegram(
        "editMessageText",
        {"chat_id": chat_id, "message_id": message_id, "text": text, "parse_mode": "HTML"},
    )


async def answer_callback(callback_query_id: str, text: str = "") -> None:
    await telegram("answerCallbackQuery", {"callback_query_id": callback_query_id, "text": text})


def place_order_stub(order: dict) -> str:
    """Open a position. Stub until IBKR is wired."""
    OPEN_POSITIONS[order["ticker"]] = {"shares": order["shares"], "entry": order["entry"]}
    return (
        f"[PAPER STUB] Would BUY {order['shares']} {order['ticker']} @ ${order['entry']}\n"
        f"Stop ${order['stop']} | Target ${order['target']}"
    )


def close_position_stub(ticker: str, shares: int) -> str:
    """Close a position. Stub until IBKR is wired."""
    return f"[PAPER STUB] Would SELL {shares} {ticker} @ market"


async def handle_morning_scan(data: dict, date_key: str) -> None:
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
            lines.append(
                f"  • <b>{s.get('ticker','?')}</b> ({s.get('name','')})\n"
                f"    Entry ${s.get('entry','?')} | Stop ${s.get('stop','?')}"
                f" | Target ${s.get('target','?')} | {s.get('shares','?')} shares"
            )
    else:
        lines.append("No watch signals today.")
    await send_message("\n".join(lines))


async def handle_confirmed_buys(data: dict, date_key: str) -> None:
    buys = data.get("confirmedBuys", [])
    market = data.get("marketStatus", "?")
    market_detail = data.get("marketDetail", "")

    await send_message(
        f"✅ <b>9:30 AM Confirmed Buys</b> — {date_key}\n"
        f"Market: <b>{market}</b> — {market_detail}\n"
        f"{len(buys)} confirmed buy(s) below."
    )

    if not buys:
        await send_message("No confirmed buys.")
        return

    for b in buys:
        order_id = secrets.token_urlsafe(6)
        order = {
            "ticker": b.get("ticker", "?"),
            "name": b.get("name", ""),
            "entry": b.get("entry", 0),
            "stop": b.get("stop", 0),
            "target": b.get("target", 0),
            "shares": b.get("shares", 0),
            "risk": b.get("riskDollars", 0),
            "created_at": time.time(),
        }
        PENDING_ORDERS[order_id] = order
        label = b.get("openingConfirmation", {}).get("statusLabel", "")

        text = (
            f"<b>{order['ticker']}</b> ({order['name']}) — {label}\n"
            f"Entry ${order['entry']} | Stop ${order['stop']} | Target ${order['target']}\n"
            f"{order['shares']} shares | Risk ${order['risk']}"
        )
        keyboard = {
            "inline_keyboard": [[
                {"text": f"✅ Buy {order['ticker']}", "callback_data": f"buy:{order_id}"},
                {"text": "❌ Skip",                  "callback_data": f"skip:{order_id}"},
            ]]
        }
        await send_message(text, reply_markup=keyboard)


async def handle_quick_exit(data: dict, date_key: str) -> None:
    results = data.get("quickExitResults", [])
    no_buys = data.get("noConfirmedBuys", False)
    lines = [f"💰 <b>10:05 AM Quick Exit</b> — {date_key}"]
    if no_buys:
        lines.append("No confirmed buys today.")
    else:
        total_profit = 0
        for r in results:
            qe = r.get("result", {}).get("quickExit", {})
            profit = qe.get("profitDollars", 0) or 0
            total_profit += profit
            emoji = "🟢" if profit > 0 else "🔴"
            lines.append(
                f"\n  {emoji} <b>{r.get('ticker','?')}</b> — {qe.get('statusLabel','?')}\n"
                f"  Entry ${r.get('entry','?')} → Exit ${qe.get('price','?')}"
                f" | {r.get('shares','?')} shares\n"
                f"  P&amp;L: <b>${profit}</b> ({qe.get('r','?')}R)"
            )
        lines.append(f"\n<b>Total P&amp;L: ${total_profit:.2f}</b>")
    await send_message("\n".join(lines))

    # Auto-close any positions we opened this morning.
    if OPEN_POSITIONS:
        close_lines = ["🔔 <b>Closing open positions:</b>"]
        for ticker, pos in list(OPEN_POSITIONS.items()):
            status = close_position_stub(ticker, pos["shares"])
            close_lines.append(f"<code>{status}</code>")
            del OPEN_POSITIONS[ticker]
        await send_message("\n".join(close_lines))


@app.post("/kamdenai/webhook")
async def kamdenai_webhook(
    request: Request,
    x_kamdenai_event: str = Header(...),
    x_kamdenai_delivery: str = Header(...),
    x_kamdenai_signature: str = Header(""),
    x_kamdenai_timestamp: str = Header(""),
):
    body = await request.body()
    if not verify_signature(body, x_kamdenai_signature, x_kamdenai_delivery, x_kamdenai_timestamp):
        raise HTTPException(status_code=401, detail="Invalid signature")

    payload = await request.json()
    event = x_kamdenai_event
    data = payload.get("data", payload)
    date_key = payload.get("dateKey", "")

    try:
        if event == "morning_scan.created":
            await handle_morning_scan(data, date_key)
        elif event == "confirmed_buys.created":
            await handle_confirmed_buys(data, date_key)
        elif event == "quick_exit_results.created":
            await handle_quick_exit(data, date_key)
        elif event == "webhook.test":
            await send_message("✅ KamdenAI webhook connected successfully!")
        else:
            await send_message(f"{event}\n{date_key}\n{json.dumps(data, indent=2)[:3500]}")
    except httpx.HTTPStatusError as exc:
        raise HTTPException(status_code=502, detail=f"Telegram error: {exc}") from exc

    return {"ok": True, "event": event, "delivery": x_kamdenai_delivery}


@app.post("/telegram/webhook/{secret}")
async def telegram_webhook(secret: str, request: Request):
    if TELEGRAM_CALLBACK_SECRET and secret != TELEGRAM_CALLBACK_SECRET:
        raise HTTPException(status_code=401, detail="bad secret")

    update = await request.json()
    cq = update.get("callback_query")
    if not cq:
        return {"ok": True}

    data = cq.get("data", "")
    chat_id = cq["message"]["chat"]["id"]
    message_id = cq["message"]["message_id"]
    original_text = cq["message"].get("text", "")
    cq_id = cq["id"]

    action, _, order_id = data.partition(":")
    order = PENDING_ORDERS.pop(order_id, None)

    if not order:
        await answer_callback(cq_id, "Order expired or already actioned.")
        return {"ok": True}

    if action == "skip":
        await edit_message(chat_id, message_id, original_text + "\n\n❌ <b>Skipped</b>")
        await answer_callback(cq_id, "Skipped")
        return {"ok": True}

    if action == "buy":
        status = place_order_stub(order)
        await edit_message(chat_id, message_id, original_text + f"\n\n✅ <b>Approved</b>\n<code>{status}</code>")
        await answer_callback(cq_id, "Order sent")
        return {"ok": True}

    await answer_callback(cq_id, "Unknown action")
    return {"ok": True}


@app.post("/dev/replay/{secret}")
async def dev_replay(secret: str, request: Request):
    """Replay any KamdenAI-shaped payload without signature checks.
    Guarded by TELEGRAM_CALLBACK_SECRET so only you can call it.
    """
    if not TELEGRAM_CALLBACK_SECRET or secret != TELEGRAM_CALLBACK_SECRET:
        raise HTTPException(status_code=401, detail="bad secret")

    payload = await request.json()
    event = payload.get("event", "confirmed_buys.created")
    data = payload.get("data", {})
    date_key = payload.get("dateKey", "test")

    if event == "morning_scan.created":
        await handle_morning_scan(data, date_key)
    elif event == "confirmed_buys.created":
        await handle_confirmed_buys(data, date_key)
    elif event == "quick_exit_results.created":
        await handle_quick_exit(data, date_key)
    else:
        await send_message(f"Replayed: {event}")
    return {"ok": True}


@app.get("/health")
async def health():
    return {"status": "ok"}
