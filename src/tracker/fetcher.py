"""Fetcher: HTTP GET ke https://t.me/s/<username> (PRD §5, §8.6).

Tanpa login, tanpa API key, tanpa akun. Read-only.
"""

from __future__ import annotations

import logging
import random

import requests

from .models import FetchResult

log = logging.getLogger("tracker.fetcher")

# Rotate User-Agent biar pola request ga terlalu seragam (PRD §15).
UA_POOL = [
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36",
]


def fetch_channel(username: str, timeout: int = 20) -> FetchResult:
    """Ambil HTML preview satu channel.

    Return FetchResult. `error` terisi kalau gagal; `retry_after` terisi kalau
    Telegram balas 429 (dipakai watcher buat backoff, §8.6).
    """
    url = f"https://t.me/s/{username}"
    headers = {
        "User-Agent": random.choice(UA_POOL),
        "Accept-Language": "en-US,en;q=0.9",
        "Accept": "text/html,application/xhtml+xml",
    }
    try:
        r = requests.get(url, headers=headers, timeout=timeout)
    except requests.Timeout:
        return FetchResult(error=f"timeout setelah {timeout}s")
    except requests.RequestException as e:
        return FetchResult(error=f"request gagal: {e}")

    if r.status_code == 429:
        ra = r.headers.get("Retry-After")
        return FetchResult(
            error="HTTP 429 rate limited",
            http_status=429,
            retry_after=int(ra) if (ra or "").isdigit() else None,
        )
    if r.status_code != 200:
        return FetchResult(error=f"HTTP {r.status_code}", http_status=r.status_code)

    return FetchResult(html=r.text, http_status=200)


def probe(username: str, timeout: int = 20) -> dict:
    """Cek cepat satu channel: publik/nggak + jumlah post (dipakai `check`)."""
    from . import parser  # import lokal: hindari siklus

    res = fetch_channel(username, timeout)
    if not res.ok:
        return {"username": username, "status": "error", "detail": res.error}
    if not parser.is_public(res.html):
        return {
            "username": username,
            "status": "tidak_publik",
            "detail": "halaman balas 200 tapi tanpa tgme_channel_info — "
                      "channel privat, username salah, atau tidak ada post",
        }
    posts = parser.parse_posts(res.html, username)
    return {"username": username, "status": "publik", "posts": posts}
