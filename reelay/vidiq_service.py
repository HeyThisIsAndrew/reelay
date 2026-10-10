"""vidIQ as an SEO provider (keyword research and scores), when available.

Status: vidIQ doesn't publish a public developer API, so there's no documented
endpoint to call yet. This module is the plug-in point: it has the same
interface as seo.BuiltInSEO, turns on only when Reelay is given vidIQ partner
access (REELAY_VIDIQ_API_URL and REELAY_VIDIQ_API_KEY), and until then Reelay
uses the free built-in helper. When access exists, `_request` maps vidIQ's
keyword and score responses onto seo.SEOReport; nothing else in the app changes.
"""

from __future__ import annotations

import os

import requests

from .seo import SEOError, SEOReport, score


class VidIQService:
    name = "vidIQ"

    def __init__(self, session=None, api_url: str | None = None, api_key: str | None = None):
        self.http = session or requests
        self.api_url = (api_url if api_url is not None else os.environ.get("REELAY_VIDIQ_API_URL", "")).rstrip("/")
        self.api_key = api_key if api_key is not None else os.environ.get("REELAY_VIDIQ_API_KEY", "")

    def available(self) -> tuple[bool, str]:
        if not (self.api_url and self.api_key):
            return False, ("vidIQ doesn't offer a public API yet, so Reelay uses free YouTube search suggestions. "
                           "This switches on if Reelay gets vidIQ partner access.")
        return True, ""

    def _request(self, path: str, params: dict) -> dict:
        ok, why = self.available()
        if not ok:
            raise SEOError(why)
        try:
            resp = self.http.get(f"{self.api_url}{path}", params=params,
                                 headers={"Authorization": f"Bearer {self.api_key}"}, timeout=20)
        except requests.RequestException:
            raise SEOError("Couldn't reach vidIQ.") from None
        if resp.status_code != 200:
            raise SEOError(f"vidIQ answered {resp.status_code}.")
        return resp.json()

    def report(self, title: str, tags: list[str], description: str, seed_list: list[str]) -> SEOReport:
        """Keyword ideas from vidIQ (partner API shape to be confirmed), scored like the built-in helper."""
        data = self._request("/keywords", {"q": ", ".join(seed_list)})
        ideas = [k.get("keyword") for k in data.get("keywords", []) if isinstance(k, dict) and k.get("keyword")]
        rep = score(title, tags, description, ideas)
        rep.provider = self.name
        return rep
