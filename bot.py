"""Telegram deals bot: finds AliExpress affiliate deals and posts them to a channel."""
import html
import json
import logging
import os
import sqlite3
import statistics
import sys
import time
from pathlib import Path

import requests
from aliexpress_api import AliexpressApi, models

BASE = Path(__file__).parent
log = logging.getLogger("dealsbot")


def load_env():
    env = BASE / ".env"
    if env.exists():
        for line in env.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())


def to_float(value, default=0.0):
    try:
        return float(str(value).replace("%", "").replace(",", "").strip())
    except (TypeError, ValueError):
        return default


def init_db():
    db = sqlite3.connect(BASE / "deals.db")
    db.execute("CREATE TABLE IF NOT EXISTS prices (pid TEXT, ts REAL, price REAL)")
    db.execute("CREATE INDEX IF NOT EXISTS idx_prices ON prices (pid, ts)")
    db.execute("CREATE TABLE IF NOT EXISTS posted (pid TEXT PRIMARY KEY, ts REAL, price REAL)")
    db.commit()
    return db


def previous_min_price(db, pid, days):
    row = db.execute(
        "SELECT MIN(price) FROM prices WHERE pid=? AND ts>?", (pid, time.time() - days * 86400)
    ).fetchone()
    return row[0]


def evaluate(db, product, market_median, cfg):
    """Return a list of reasons this product is a good deal (empty = skip)."""
    f = cfg["filters"]
    price = to_float(product.target_sale_price)
    if price <= 0 or not (f["min_price"] <= price <= f["max_price"]):
        return []
    if to_float(getattr(product, "evaluate_rate", 0)) < f["min_rating_percent"]:
        return []
    if to_float(getattr(product, "lastest_volume", 0)) < f["min_orders"]:
        return []

    reasons = []
    discount = to_float(getattr(product, "discount", 0))
    if discount >= f["min_discount_percent"]:
        reasons.append(f"הנחה של {discount:.0f}%")

    prev = previous_min_price(db, product.product_id, f["history_days"])
    if prev and price <= prev * (1 - f["min_price_drop_percent"] / 100):
        reasons.append(f"ירידת מחיר (היה {prev:.2f})")

    if market_median and price <= market_median * (1 - f["min_below_market_percent"] / 100):
        reasons.append(f"זול ב-{(1 - price / market_median) * 100:.0f}% מהממוצע בקטגוריה")

    return reasons


def already_posted(db, pid, price, cfg):
    row = db.execute("SELECT ts, price FROM posted WHERE pid=?", (pid,)).fetchone()
    if not row:
        return False
    ts, old_price = row
    cooldown = cfg["repost_after_days"] * 86400
    # allow a re-post only if cooldown passed or the price dropped meaningfully since
    return time.time() - ts < cooldown and price > old_price * 0.9


def format_message(product, reasons, currency):
    price = to_float(product.target_sale_price)
    original = to_float(getattr(product, "target_original_price", 0))
    lines = [f"🔥 <b>{html.escape(product.product_title[:150])}</b>", ""]
    if original > price:
        lines.append(f"💰 <s>{original:.2f}</s> → <b>{price:.2f} {currency}</b>")
    else:
        lines.append(f"💰 <b>{price:.2f} {currency}</b>")
    lines += [f"✅ {html.escape(r)}" for r in reasons]
    rating = to_float(getattr(product, "evaluate_rate", 0))
    orders = int(to_float(getattr(product, "lastest_volume", 0)))
    if rating:
        lines.append(f"⭐ דירוג {rating:.0f}% | 📦 {orders} הזמנות")
    lines += ["", f'🛒 <a href="{html.escape(product.promotion_link)}">לקנייה באליאקספרס</a>']
    return "\n".join(lines)


def send_telegram(token, chat_id, text, image_url):
    base = f"https://api.telegram.org/bot{token}"
    if image_url:
        r = requests.post(
            f"{base}/sendPhoto",
            data={"chat_id": chat_id, "photo": image_url, "caption": text, "parse_mode": "HTML"},
            timeout=30,
        )
        if r.ok:
            return True
        log.warning("sendPhoto failed (%s), falling back to text", r.text[:200])
    r = requests.post(
        f"{base}/sendMessage",
        data={"chat_id": chat_id, "text": text, "parse_mode": "HTML"},
        timeout=30,
    )
    if not r.ok:
        log.error("sendMessage failed: %s", r.text[:300])
    return r.ok


def run_once(api, db, cfg, dry_run):
    token, chat_id = os.environ["TELEGRAM_BOT_TOKEN"], os.environ["TELEGRAM_CHAT_ID"]
    currency = cfg["currency"]
    candidates = []

    for search in cfg["searches"]:
        try:
            resp = api.get_products(
                keywords=search.get("keywords"),
                category_ids=search.get("category_ids"),
                sort="LAST_VOLUME_DESC",
                page_size=50,
                ship_to_country=cfg["ship_to_country"],
                target_currency=currency,
                target_language=cfg["language"],
            )
        except Exception:
            log.exception("search failed: %s", search)
            continue
        products = resp.products or []
        prices = [to_float(p.target_sale_price) for p in products if to_float(p.target_sale_price) > 0]
        median = statistics.median(prices) if len(prices) >= 8 else None
        now = time.time()
        for p in products:
            price = to_float(p.target_sale_price)
            reasons = evaluate(db, p, median, cfg)
            db.execute("INSERT INTO prices VALUES (?,?,?)", (p.product_id, now, price))
            if reasons and not already_posted(db, p.product_id, price, cfg):
                candidates.append((len(reasons), to_float(getattr(p, "discount", 0)), p, reasons))
    db.commit()

    # best deals first: most reasons, then biggest discount
    candidates.sort(key=lambda c: (c[0], c[1]), reverse=True)
    sent, seen = 0, set()
    for _, _, p, reasons in candidates:
        if sent >= cfg["max_posts_per_run"]:
            break
        if p.product_id in seen:
            continue
        seen.add(p.product_id)
        text = format_message(p, reasons, currency)
        if dry_run:
            print(text, "\n" + "-" * 40)
            sent += 1
            continue
        if send_telegram(token, chat_id, text, getattr(p, "product_main_image_url", None)):
            db.execute(
                "INSERT OR REPLACE INTO posted VALUES (?,?,?)",
                (p.product_id, time.time(), to_float(p.target_sale_price)),
            )
            db.commit()
            sent += 1
            time.sleep(3)  # stay well under Telegram's channel rate limits
    log.info("run finished: %d candidates, %d posted", len(candidates), sent)


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    load_env()
    cfg = json.loads((BASE / "config.json").read_text(encoding="utf-8"))
    dry_run = "--dry-run" in sys.argv
    once = "--once" in sys.argv or dry_run

    api = AliexpressApi(
        os.environ["ALI_APP_KEY"],
        os.environ["ALI_APP_SECRET"],
        getattr(models.Language, cfg["language"]),
        getattr(models.Currency, cfg["currency"]),
        os.environ["ALI_TRACKING_ID"],
    )
    db = init_db()
    while True:
        try:
            run_once(api, db, cfg, dry_run)
        except Exception:
            log.exception("run failed")
        if once:
            break
        time.sleep(cfg["interval_minutes"] * 60)


if __name__ == "__main__":
    main()
