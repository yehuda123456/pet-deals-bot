"""Posts deals from queue.txt to a Telegram channel and moves them to posted.txt."""
import html
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

BASE = Path(__file__).parent
QUEUE = BASE / "queue.txt"
POSTED = BASE / "posted.txt"
FIELDS = ("title", "price", "was", "link", "image", "note")


def is_deal_block(block):
    return any(l.strip() and not l.strip().startswith("#") for l in block.splitlines())


def parse_block(block):
    deal = {}
    for line in block.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or ":" not in line:
            continue
        key, value = line.split(":", 1)
        key = key.strip().lower()
        if key in FIELDS:
            deal[key] = value.strip()
    return deal


def validate(deal):
    if not deal.get("title") or not deal.get("link"):
        return "missing title or link"
    url = urllib.parse.urlparse(deal["link"])
    if url.scheme not in ("http", "https") or "aliexpress" not in url.netloc.lower():
        return "link must be an AliExpress link"
    return None


def number(text):
    m = re.search(r"\d+(?:[.,]\d+)?", text or "")
    return float(m.group().replace(",", ".")) if m else None


def format_message(deal):
    esc = html.escape
    lines = [f"🔥 <b>{esc(deal['title'][:150])}</b>", ""]
    price, was = deal.get("price"), deal.get("was")
    if price and was:
        p, w = number(price), number(was)
        off = f"  (-{round((1 - p / w) * 100)}%)" if p and w and w > p else ""
        lines.append(f"💰 <s>{esc(was)}</s> → <b>{esc(price)}</b>{off}")
    elif price:
        lines.append(f"💰 <b>{esc(price)}</b>")
    if deal.get("note"):
        lines.append(f"📝 {esc(deal['note'])}")
    lines += ["", f'🛒 <a href="{esc(deal["link"], quote=True)}">Get it on AliExpress</a>']
    return "\n".join(lines)


def telegram(method, params):
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    data = urllib.parse.urlencode(params).encode()
    try:
        with urllib.request.urlopen(f"https://api.telegram.org/bot{token}/{method}", data, timeout=30) as r:
            return True, r.read().decode()
    except urllib.error.HTTPError as e:
        return False, e.read().decode(errors="replace")
    except urllib.error.URLError as e:
        return False, str(e)


def send(deal):
    chat = os.environ["TELEGRAM_CHAT_ID"]
    text = format_message(deal)
    if deal.get("image"):
        ok, body = telegram("sendPhoto", {"chat_id": chat, "photo": deal["image"], "caption": text, "parse_mode": "HTML"})
        if ok:
            return True, body
        print("sendPhoto failed, sending as text:", body[:200])
    return telegram("sendMessage", {"chat_id": chat, "text": text, "parse_mode": "HTML"})


def main():
    dry = "--dry-run" in sys.argv
    limit = int(sys.argv[sys.argv.index("--max") + 1]) if "--max" in sys.argv else 1

    text = QUEUE.read_text(encoding="utf-8-sig") if QUEUE.exists() else ""
    posted_text = POSTED.read_text(encoding="utf-8-sig") if POSTED.exists() else ""
    posted_links = {l.split("|")[1].strip() for l in posted_text.splitlines() if l.count("|") >= 2}

    kept, sent, failed = [], 0, False
    for block in re.split(r"\n\s*\n", text.strip("\n")) if text.strip() else []:
        if not is_deal_block(block):
            kept.append(block)
            continue
        deal = parse_block(block)
        problem = validate(deal)
        if problem:
            print(f"SKIPPED ({problem}): {block.splitlines()[0][:80]}")
            kept.append(block)
            continue
        if deal["link"] in posted_links:
            print("Already posted before, removing from queue:", deal["link"])
            continue
        if sent >= limit or failed:
            kept.append(block)
            continue
        if sent:
            time.sleep(3)
        if dry:
            print(format_message(deal), "\n" + "-" * 40)
            ok = True
        else:
            ok, body = send(deal)
            if not ok:
                print("Telegram error:", body[:300])
        if ok:
            sent += 1
            posted_links.add(deal["link"])
            if not dry:
                stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")
                with POSTED.open("a", encoding="utf-8") as f:
                    f.write(f"{stamp} | {deal['link']} | {deal['title'][:80]}\n")
        else:
            failed = True
            kept.append(block)

    if not dry:
        QUEUE.write_text("\n\n".join(kept) + "\n", encoding="utf-8")
    print(f"Done: {sent} posted, {len(kept)} blocks left in queue.")
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    main()
