"""Checks which candidate deal feeds respond from this machine and how many AliExpress/pet items they hold."""
import urllib.request
import xml.etree.ElementTree as ET

FEEDS = [
    "https://www.reddit.com/r/AliExpressDeals/new/.rss",
    "https://www.reddit.com/r/aliexpress/new/.rss",
    "https://slickdeals.net/newsearch.php?searchin=first&rss=1&q=aliexpress",
    "https://www.hotukdeals.com/rss/tag/aliexpress",
    "https://www.mydealz.de/rss/tag/aliexpress",
    "https://www.pepper.com/rss/tag/aliexpress",
    "https://www.promodescuentos.com/rss/tag/aliexpress",
    "https://www.dealabs.com/rss/tag/aliexpress",
]
PET_WORDS = ("dog", "cat", "pet", "puppy", "kitten", "leash", "litter", "collar", "aquarium", "fish tank")
UA = "Mozilla/5.0 (compatible; deals-feed-check/1.0)"


def local(tag):
    return tag.rsplit("}", 1)[-1]


def titles(xml_bytes):
    root = ET.fromstring(xml_bytes)
    out = []
    for el in root.iter():
        if local(el.tag) in ("item", "entry"):
            for child in el:
                if local(child.tag) == "title" and child.text:
                    out.append(child.text.strip())
                    break
    return out


for url in FEEDS:
    print("=" * 70)
    print(url)
    try:
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=20) as r:
            data = r.read()
            status = r.status
        items = titles(data)
        pets = [t for t in items if any(w in t.lower() for w in PET_WORDS)]
        ali = [t for t in items if "aliexpress" in t.lower()]
        print(f"OK status={status} items={len(items)} mention_aliexpress={len(ali)} pet_related={len(pets)}")
        for t in items[:3]:
            print("  sample:", t[:110])
        for t in pets[:3]:
            print("  pet:", t[:110])
    except Exception as e:
        print("FAILED:", type(e).__name__, str(e)[:150])
