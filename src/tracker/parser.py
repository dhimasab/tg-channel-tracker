"""Parser HTML preview Telegram -> list[Post] (PRD §7).

Dua koreksi terhadap PRD v1.0 §7.1/§7.2, hasil audit 20 Sep 2026:

1. `background-image:url('...')` dipakai DUA hal: foto post DAN custom emoji di
   dalam badan teks (`<i class="emoji" style="background-image:url('//telegram.org/img/emoji/40/...')">`).
   Regex polos bakal ngambil emoji. Foto asli nempel di atribut `style` tag
   `<a class="tgme_widget_message_photo_wrap">`, jadi ekstraksi di-scope ke sana
   dan emoji ditolak.
2. Preview channel alpha nempelkan sampah UI di dalam div teks
   ("Please open Telegram to view this post", "VIEW IN TELEGRAM"), dan emoji
   fallback nyisa karakter aneh. Keduanya dibersihkan.
"""

from __future__ import annotations

import html as html_mod
import logging
import re

from .models import Post

log = logging.getLogger("tracker.parser")

# --- struktur blok -----------------------------------------------------------
# Split pakai lookahead (bukan findall) supaya tiap blok tetap bawa <div> pembukanya
# sehingga atribut data-post tidak hilang (PRD §20 poin 3).
BLOCK_SPLIT = re.compile(r'(?=<div class="tgme_widget_message[ "])')
POST_ID_RE = re.compile(r'data-post="([^/"]+)/(\d+)"')
TIME_RE = re.compile(r'<time datetime="([^"]+)"')

TEXT_RE = re.compile(
    r'<div class="tgme_widget_message_text[^"]*"[^>]*>(.*?)</div>\s*'
    r'(?:<div class="tgme_widget_message_(?:reply_markup|footer|service))',
    re.S,
)
TEXT_FALLBACK_RE = re.compile(
    r'<div class="tgme_widget_message_text[^"]*"[^>]*>(.*?)</div>', re.S
)

LINK_RE = re.compile(r'href="(https?://[^"]+)"')
# URL protocol-relative, mis. //telegram.org/img/emoji/40/xxx.png
PROTO_REL_RE = re.compile(r"^//")

# Foto post asli: background-image ada di tag <a class="tgme_widget_message_photo_wrap">,
# BUKAN di <div class="tgme_widget_message_photo"> (div itu cuma padding-top).
PHOTO_WRAP_RE = re.compile(
    r'class="tgme_widget_message_photo_wrap[^"]*"[^>]*'
    r'style="[^"]*background-image:url\(\'([^\']+)\'\)"',
    re.S,
)
MEDIA_REJECT = ("telegram.org/img/emoji",)

# Link yang bukan konten post.
LINK_SKIP = ("t.me/", "telegram.org", "cdn-telegram", "telesco.pe")

# Sampah UI yang ikut ke-capture dari div teks.
CHROME_JUNK = (
    "VIEW IN TELEGRAM",
    "Please open Telegram to view this post",
    "Please open Telegram",
)
EMOJI_TAG_RE = re.compile(r'<i class="emoji"[^>]*>.*?</i>', re.S)
BR_RE = re.compile(r"<br\s*/?>", re.I)
TAG_RE = re.compile(r"<[^>]+>")

# --- contract address (disimpan untuk v1.1, tidak ditampilkan di pesan v1) ----
SOL_RE = re.compile(r"\b[1-9A-HJ-NP-Za-km-z]{32,44}\b")
EVM_RE = re.compile(r"\b0x[a-fA-F0-9]{40}\b")
SOL_BLACKLIST = {
    "solana", "telegram", "dexscreener", "pumpfun", "raydium", "birdeye",
    "jupiter", "meteora", "phantom", "backpack", "solscan", "dextools",
}


def extract_cas(text: str | None, links: list[str] | None = None) -> dict:
    """Ambil contract address dari teks DAN dari link di dalam post.

    Channel alpha sering naruh CA cuma di href (dexscreener/pump.fun/birdeye/gmgn),
    badan teksnya cuma nama token + market cap. Terukur: soltrending 114/114 post
    CA-nya cuma ada di link.
    """
    blob = text or ""
    if links:
        blob += " " + " ".join(links)
    evm = list(dict.fromkeys(EVM_RE.findall(blob)))
    sol = [
        m for m in dict.fromkeys(SOL_RE.findall(blob))
        if m.lower() not in SOL_BLACKLIST and "_" not in m
    ]
    return {"solana": sol, "evm": evm}


def clean_text(raw_html: str) -> str:
    """HTML badan post -> teks bersih siap kirim."""
    t = BR_RE.sub("\n", raw_html)
    t = EMOJI_TAG_RE.sub("", t)          # buang <i class="emoji">...</i> + fallback-nya
    t = TAG_RE.sub(" ", t)
    t = html_mod.unescape(t)
    t = re.sub(r"[ \t]+", " ", t)

    lines = []
    for line in t.split("\n"):
        stripped = line.strip()
        if stripped in CHROME_JUNK:       # sampah UI, bukan isi post
            continue
        lines.append(stripped)
    t = "\n".join(lines)
    t = re.sub(r"\n{3,}", "\n\n", t)
    return t.strip()


def extract_media_url(block: str) -> str | None:
    """URL foto post, atau None kalau tidak ada foto asli (emoji tidak dihitung)."""
    m = PHOTO_WRAP_RE.search(block)
    if not m:
        return None
    url = m.group(1).strip()
    if any(reject in url for reject in MEDIA_REJECT):
        return None
    if PROTO_REL_RE.match(url):           # //telegram.org/... -> https://...
        url = "https:" + url
    return url or None


def is_public(page_html: str) -> bool:
    """Validasi halaman (PRD §7.3).

    Channel privat / username salah / tidak ada post -> halaman tetap balas
    HTTP 200, tapi tanpa penanda channel publik.
    """
    return "tgme_channel_info" in page_html or "tgme_widget_message" in page_html


def parse_posts(page_html: str, username: str) -> list[Post]:
    """Pecah HTML jadi list Post, urut naik berdasarkan msg_id.

    `username` adalah identity yang dipakai sebagai key DB (dari channels.json).
    Nilai `data-post` di HTML (username tampilan Telegram, casing-nya bisa beda:
    SOLTRENDING vs soltrending) disimpan di `detected_username` saja — kalau
    dipakai sebagai key, baris posts dan baris channel_state tidak akan ketemu.
    """
    posts: list[Post] = []
    for block in BLOCK_SPLIT.split(page_html):
        m = POST_ID_RE.search(block)
        if not m:
            continue
        detected_username = m.group(1)
        msg_id = int(m.group(2))

        t = TIME_RE.search(block)
        body = TEXT_RE.search(block) or TEXT_FALLBACK_RE.search(block)
        # Post tanpa div teks (service message / poll) tetap disimpan, text=None —
        # jangan di-drop diam-diam.
        text = clean_text(body.group(1)) if body else None

        hrefs = LINK_RE.findall(block)
        links = list(dict.fromkeys(
            h for h in hrefs
            if not any(skip in h for skip in LINK_SKIP)
        ))

        posts.append(
            Post(
                username=username,
                detected_username=detected_username,
                msg_id=msg_id,
                posted_at=t.group(1) if t else None,
                text=text or None,
                links=links,
                media_url=extract_media_url(block),
                cas=extract_cas(text, links),
            )
        )

    posts.sort(key=lambda p: p.msg_id)
    return posts
