"""YouTube SEO help: keyword ideas and a title/tags score, free.

Keyword ideas come from YouTube's own search suggestions (what people type
into the YouTube search box), the same public autocomplete the YouTube site
uses. No account or API key is needed, and nothing is sent until someone clicks
"Get SEO Suggestions". The score is computed here from well-known YouTube
guidance (title length, keyword placement, tag budget) and explains every point.

A paid provider can plug in through the same interface (see vidiq_service.py).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

import requests

SUGGEST_URL = "https://suggestqueries.google.com/complete/search"
TITLE_MAX = 100
TAGS_MAX = 500


class SEOError(Exception):
    """Plain-language reason suggestions aren't available right now."""


@dataclass
class Check:
    label: str
    ok: bool
    tip: str


@dataclass
class SEOReport:
    score: int                               # 0–100
    checks: list[Check] = field(default_factory=list)
    keywords: list[str] = field(default_factory=list)   # ideas, best first
    provider: str = "YouTube search suggestions"


def seeds(title: str = "", topic: str = "", keywords: str = "", hashtags: list[str] | None = None) -> list[str]:
    """Search phrases to start from: the topic and keywords first, then hashtags, then the title."""
    out: list[str] = []
    for part in [topic, *re.split(r"[,;\n]", keywords or "")]:
        if part and part.strip():
            out.append(part.strip())
    for tag in hashtags or []:
        words = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", tag).replace("_", " ").strip()
        if words:
            out.append(words.lower())
    if title.strip():
        out.append(" ".join(re.findall(r"[\w']+", title.lower())[:4]))
    seen, unique = set(), []
    for s in out:
        if s.lower() not in seen:
            seen.add(s.lower())
            unique.append(s)
    return unique[:5]


def youtube_suggestions(seed: str, session=None) -> list[str]:
    """What YouTube's search box suggests for a phrase."""
    http = session or requests
    try:
        resp = http.get(SUGGEST_URL, params={"client": "firefox", "ds": "yt", "q": seed, "hl": "en"}, timeout=10)
    except requests.RequestException:
        raise SEOError("Couldn't reach YouTube's search suggestions. Check your internet connection.") from None
    try:
        data = resp.json()
    except ValueError:
        raise SEOError("YouTube's search suggestions sent something unexpected. Try again later.") from None
    if resp.status_code != 200 or not isinstance(data, list) or len(data) < 2:
        raise SEOError("YouTube's search suggestions aren't available right now.")
    return [s for s in data[1] if isinstance(s, str)]


def keyword_ideas(seed_list: list[str], session=None, limit: int = 25) -> list[str]:
    ideas: list[str] = []
    errors = 0
    for seed in seed_list:
        try:
            for s in youtube_suggestions(seed, session):
                if s.lower() not in {i.lower() for i in ideas}:
                    ideas.append(s)
        except SEOError:
            errors += 1
    if errors and errors == len(seed_list):
        raise SEOError("Couldn't get keyword ideas from YouTube right now.")
    return ideas[:limit]


def score(title: str, tags: list[str], description: str, keywords: list[str]) -> SEOReport:
    """How well the copy targets the best keyword (heuristic, explained)."""
    title_l, desc_l = title.lower(), description.lower()
    main = next((k for k in keywords if k.lower() in title_l), keywords[0] if keywords else "")
    checks = [
        Check("Title length", 20 <= len(title) <= 70,
              f"{len(title)} characters. 20–70 shows in full in search; YouTube allows {TITLE_MAX}."),
        Check("Keyword in the title", bool(main) and main.lower() in title_l,
              f"Put a phrase people search for, like “{main}”, in the title." if main else
              "Add a topic or keywords to the ticket so Reelay can suggest search phrases."),
        Check("Keyword near the start", bool(main) and title_l.find(main.lower()) in range(0, 30),
              "Searchers skim: lead with the main phrase in the first few words."),
        Check("Keyword early in the description", bool(main) and main.lower() in desc_l[:150],
              "Mention the main phrase in the first two lines of the description."),
        Check("Tags", 5 <= len(tags) and sum(len(t) + 1 for t in tags) <= TAGS_MAX,
              f"{len(tags)} tags, {sum(len(t) + 1 for t in tags)}/{TAGS_MAX} characters. Aim for 5–15 relevant tags."),
        Check("Not shouting", sum(c.isupper() for c in title) <= max(3, len(title) // 3),
              "Mostly capital letters reads as clickbait."),
    ]
    weights = [15, 30, 15, 15, 15, 10]
    total = sum(w for c, w in zip(checks, weights) if c.ok)
    return SEOReport(score=total, checks=checks, keywords=list(keywords))


class BuiltInSEO:
    """Free: YouTube search suggestions + the local score."""
    name = "YouTube search suggestions"

    def __init__(self, session=None):
        self.session = session

    def available(self) -> tuple[bool, str]:
        return True, ""

    def report(self, title: str, tags: list[str], description: str, seed_list: list[str]) -> SEOReport:
        ideas = keyword_ideas(seed_list, self.session) if seed_list else []
        rep = score(title, tags, description, ideas)
        rep.provider = self.name
        return rep


def provider_from(store=None, session=None):
    """vidIQ when it's configured (see vidiq_service.py), else the free built-in helper."""
    from .vidiq_service import VidIQService
    vidiq = VidIQService(session=session)
    return vidiq if vidiq.available()[0] else BuiltInSEO(session)
