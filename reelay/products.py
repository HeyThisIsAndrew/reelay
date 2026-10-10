"""Products tagged on a video (TikTok Shop), so videos about the same product
can be found even when their captions and hashtags differ.

TikTok: a shoppable video's public page carries its product links in the
item's `anchors`: each anchor's `extra` is a JSON list of entries, and product
entries (type 33) hold a JSON `extra` with `product_id` and the product's full
`title`; the entry's `keyword` is the short name shown on the video. This is
the same public page the downloader reads; nothing needs signing in. TikTok
can change that page at any time, so anything unexpected yields "no products"
rather than an error.

Other platforms: YouTube's Data API has no product tags, and Instagram's
product tags need the account owner's Meta app (Graph API), so they aren't
read yet.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass

_REHYDRATION = re.compile(r'<script[^>]+id="__UNIVERSAL_DATA_FOR_REHYDRATION__"[^>]*>(.*?)</script>', re.S)
PRODUCT_TYPE = 33


@dataclass(frozen=True)
class Product:
    id: str                 # the platform's product id (stable across videos)
    name: str               # short name shown on the video, e.g. "Blenders Polarized Sunglasses"
    title: str              # the product's full listing title
    platform: str = "tiktok"
    url: str = ""           # the product page
    seller_id: str = ""     # the shop selling it (groups a brand's products)

    def to_record(self) -> dict:
        return asdict(self)

    @property
    def label(self) -> str:
        """How the product is shown in lists: its listing title, kept short."""
        text = self.title or self.name
        return text if len(text) <= 60 else text[:57].rstrip(" ,-–") + "…"


def _json(value):
    if isinstance(value, (dict, list)):
        return value
    if isinstance(value, str) and value.strip()[:1] in "[{":
        try:
            return json.loads(value)
        except ValueError:
            return None
    return None


def products_from_item(item: dict) -> list[Product]:
    """TikTok Shop products in a video's itemStruct (deduplicated, in order)."""
    found: dict[str, Product] = {}
    for anchor in item.get("anchors") or []:
        entries = _json(anchor.get("extra"))
        if not isinstance(entries, list):
            continue
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            inner = _json(entry.get("extra")) or {}
            pid = inner.get("product_id") or (entry.get("id") if entry.get("type") == PRODUCT_TYPE else None)
            if not pid or (entry.get("type") != PRODUCT_TYPE and "product_id" not in inner):
                continue
            pid = str(pid)
            if pid in found:
                continue
            title = str(inner.get("title") or "").strip()
            name = str(entry.get("keyword") or inner.get("elastic_title") or title).strip()
            url = str(inner.get("seo_url") or "").split("?", 1)[0]
            found[pid] = Product(id=pid, name=name, title=title or name,
                                 url=url if url.startswith("https://") else "",
                                 seller_id=str(inner.get("seller_id") or ""))
    return list(found.values())


def products_from_page(html: str) -> list[Product]:
    m = _REHYDRATION.search(html or "")
    data = _json(m.group(1)) if m else None
    try:
        item = data["__DEFAULT_SCOPE__"]["webapp.video-detail"]["itemInfo"]["itemStruct"]
    except (TypeError, KeyError):
        return []
    return products_from_item(item) if isinstance(item, dict) else []


def tiktok_products(url: str, ydl_opts: dict, ydl_factory=None) -> list[Product]:
    """Read a TikTok video's public page and return its tagged products.
    Raises on network trouble (so the caller can try again later)."""
    if ydl_factory is None:
        import yt_dlp
        ydl_factory = yt_dlp.YoutubeDL
    with ydl_factory({**ydl_opts, "quiet": True, "no_warnings": True}) as ydl:
        ie = ydl.get_info_extractor("TikTok")
        ie.set_downloader(ydl)
        html = ie._download_webpage(url, "products", note=False, impersonate=True)
    return products_from_page(html)


def video_products(item) -> list[Product]:
    """The products recorded on a VideoItem (empty when none, or not looked up yet)."""
    known = set(Product.__dataclass_fields__)
    return [Product(**{k: v for k, v in p.items() if k in known})
            for p in (item.products or []) if isinstance(p, dict) and p.get("id")]


_FILLER = {"the", "a", "an", "new", "official", "-", "–", "|", "by"}


def _words(title: str) -> list[str]:
    return [w.strip(",.:;|()[]") for w in (title or "").split() if w.strip(",.:;|()[]")]


def brand_map(products) -> dict[str, str]:
    """product id → brand name, for filtering by brand first.

    Products from the same shop (seller) share a brand; its name is the words
    their titles start with ("Blenders Eyewear Candy Cove…" and "Blenders North
    Park…" → "Blenders"). A product on its own uses the first word of its title.
    Listing titles almost always lead with the brand."""
    products = {p.id: p for p in products}
    groups: dict[str, list] = {}
    for p in products.values():
        groups.setdefault(p.seller_id or f"title:{(_words(p.title)[:1] or [p.id])[0].lower()}", []).append(p)
    result: dict[str, str] = {}
    for members in groups.values():
        titles = [_words(p.title or p.name) for p in members]
        common: list[str] = []
        if len(titles) > 1:
            for column in zip(*titles):
                if len({w.lower() for w in column}) != 1:
                    break
                common.append(column[0])
        name = " ".join(common[:3]) if common else (titles[0][0] if titles and titles[0] else members[0].name)
        while name.split() and name.split()[-1].lower() in _FILLER:
            name = " ".join(name.split()[:-1])
        for p in members:
            result[p.id] = name or p.name
    return result
