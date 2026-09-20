#!/usr/bin/env python3
"""Probe batch kandidat channel sebelum dimasukkan ke channels.json.

Pakai modul project sendiri (fetcher + parser) supaya hasilnya konsisten dengan
apa yang nanti benar-benar dibaca service — termasuk soal media/emoji dan CA.

Yang dilaporkan per channel:
  publik/tidak, jumlah post terbaca, waktu post terakhir (+ umur),
  berapa post yang punya CA, dan estimasi laju post (post/jam).

Jalankan:
  .venv/bin/python tools/probe_candidates.py
  .venv/bin/python tools/probe_candidates.py --json
  .venv/bin/python tools/probe_candidates.py --extra nama_channel_1,nama_channel_2
"""

from __future__ import annotations

import argparse
import json
import sys
import random
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from tracker import fetcher, parser  # noqa: E402
from tracker.sender import format_time_wita  # noqa: E402

# Kandidat: alpha/new-pair, on-chain, news, exchange, DeFi project.
# Tidak semuanya bakal publik — justru itu yang mau disaring.
CANDIDATES = [
    # alpha / new pairs / meme radar
    "soltrending", "solana_newpairs", "pumpfun_alerts", "dexscreener_alerts",
    "solanafloor", "birdeye_so", "dexscreener", "pumpfun", "solana_alerts",
    "memecoin_alerts", "newpair_alerts", "solanabeach_alerts",
    # on-chain / whale
    "whale_alert_io", "whale_alert", "spotonchain", "lookonchain",
    "arkham_intel", "bubblemaps", "unusual_whales", "wallet_alert",
    # news
    "cointelegraph", "BWEnews", "wublockchainenglish", "watcherguru",
    "TreeNewsFeed", "tier10k", "CoinDesk", "cryptopanic", "thedefiant",
    "DLNews", "CoinBureau", "AltcoinDaily", "CryptoBanter",
    "bitcoinmagazine", "newsbtc", "cryptobriefing", "cointelegraph_news",
    # exchange / cex
    "binance_announcements", "binance_futures", "okx_announcements",
    "bybit_announcements", "upbit_announcements", "coinbase_assets",
    # event
    "coinfestasia",
]


def probe(username: str) -> dict:
    res = fetcher.fetch_channel(username, timeout=20)
    out = {"username": username, "ok": False, "reason": res.error,
           "http": res.http_status}
    if not res.ok:
        return out
    if not parser.is_public(res.html):
        out["reason"] = "tidak publik / username salah / tanpa post"
        return out

    posts = parser.parse_posts(res.html, username)
    if not posts:
        out["reason"] = "publik tapi 0 post terbaca"
        return out

    def ts(p):
        try:
            d = datetime.fromisoformat((p.posted_at or "").replace("Z", "+00:00"))
            return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
        except ValueError:
            return None

    dated = [p for p in posts if ts(p)]
    newest = max(dated, key=ts) if dated else None
    oldest = min(dated, key=ts) if dated else None
    now = datetime.now(timezone.utc)

    span_h = ((ts(newest) - ts(oldest)).total_seconds() / 3600) if (dated and len(dated) > 1) else None
    rate = ((len(dated) - 1) / span_h) if span_h and span_h > 0 else None

    with_ca = [p for p in posts if p.ca_count > 0]
    out.update({
        "ok": True,
        "posts": len(posts),
        "newest_iso": newest.posted_at if newest else None,
        "newest_wita": format_time_wita(newest.posted_at) if newest else "-",
        "age_h": round((now - ts(newest)).total_seconds() / 3600, 1) if newest else None,
        "with_ca": len(with_ca),
        "ca_total": sum(p.ca_count for p in posts),
        "with_photo": sum(1 for p in posts if p.media_url),
        "rate_per_h": round(rate, 1) if rate else None,
        "span_h": round(span_h, 2) if span_h else None,
    })
    return out


def verdict(r: dict) -> str:
    if not r.get("ok"):
        return "TIDAK PUBLIK"
    age = r.get("age_h")
    if age is None:
        return "TANPA WAKTU"
    if age <= 24:
        return "AKTIF"
    if age <= 24 * 7:
        return "SEMI-AKTIF"
    if age <= 24 * 30:
        return "JARANG"
    return "DORMAN"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true", help="keluarkan JSON")
    ap.add_argument("--extra", help="username tambahan, dipisah koma")
    ap.add_argument("--workers", type=int, default=4)
    args = ap.parse_args()

    targets = list(dict.fromkeys(CANDIDATES))
    if args.extra:
        targets += [u.strip().lstrip("@") for u in args.extra.split(",") if u.strip()]

    results: list[dict] = []
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futs = {pool.submit(probe, u): u for u in targets}
        for fut in as_completed(futs):
            try:
                results.append(fut.result())
            except Exception as e:                       # pragma: no cover
                results.append({"username": futs[fut], "ok": False, "reason": str(e)})
            time.sleep(random.uniform(0.05, 0.2))

    order = {"AKTIF": 0, "SEMI-AKTIF": 1, "JARANG": 2, "DORMAN": 3, "TANPA WAKTU": 4,
             "TIDAK PUBLIK": 5}
    for r in results:
        r["verdict"] = verdict(r)
    results.sort(key=lambda r: (order.get(r["verdict"], 9),
                                -(r.get("with_ca") or 0),
                                -(r.get("rate_per_h") or 0)))

    if args.json:
        print(json.dumps(results, indent=2, ensure_ascii=False))
        return 0

    print(f"{'CHANNEL':<24}{'STATUS':<13}{'POST':>5}{'CA':>5}{'FOTO':>6}"
          f"{'POST/JAM':>10}  {'POST TERAKHIR':<26}UMUR")
    print("-" * 104)
    for r in results:
        if r["verdict"] == "TIDAK PUBLIK":
            print(f"{r['username']:<24}{'TIDAK PUBLIK':<13}{'-':>5}{'-':>5}{'-':>6}{'-':>10}"
                  f"  {str(r.get('reason'))[:40]}")
            continue
        rate = f"{r['rate_per_h']:.1f}" if r.get("rate_per_h") else "-"
        age = f"{r['age_h']:.1f} jam" if (r.get("age_h") or 0) < 48 \
            else f"{r['age_h'] / 24:.1f} hari"
        print(f"{r['username']:<24}{r['verdict']:<13}{r['posts']:>5}{r['with_ca']:>5}"
              f"{r['with_photo']:>6}{rate:>10}  {r['newest_wita']:<26}{age}")

    aktif = [r for r in results if r["verdict"] in ("AKTIF", "SEMI-AKTIF")]
    print(f"\nkandidat publik & aktif: {len(aktif)} dari {len(results)} diuji")
    print("yang punya CA:", ", ".join(r["username"] for r in aktif if r.get("with_ca")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
