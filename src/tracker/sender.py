"""Sender: Telegram Bot API via HTTP (PRD §8.4, §8.6).

Tanpa library tambahan — cukup requests. Yang penting:

- Respons WAJIB dicek: `requests.post` tidak raise untuk HTTP 400/403/429, dan
  Bot API balas HTTP 200 dengan `{"ok": false}` untuk error sebagian. Kode lama
  return True tanpa cek apa pun -> alert hilang tanpa jejak.
- 429 -> tunggu `retry_after` + 1 detik, retry sekali (PRD §8.6).
- 403 (bot di-block) -> stop kirim, jangan retry terus.
- Error lain -> retry 3x backoff 2s/5s/10s.
- Post dengan gambar -> sendPhoto, caption dipotong ke 1024 char; kalau gagal
  (URL CDN kedaluwarsa / bukan gambar) fallback ke sendMessage.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta, timezone

import requests

log = logging.getLogger("tracker.sender")

API_BASE = "https://api.telegram.org/bot{token}/{method}"

# WITA = UTC+8, dipakai tetap (tanpa dependensi tzdata).
WITA = timezone(timedelta(hours=8))
MONTHS = ("Jan", "Feb", "Mar", "Apr", "Mei", "Jun",
          "Jul", "Agu", "Sep", "Okt", "Nov", "Des")

CREDIT_FOOTER = (
    "———\n"
    "🔧 Bot by @dhimedia\n"
    "💙 Kalau berguna, traktir kopi kreator dong: teer.id/PejuangCryptoID"
)

TRUNCATED = "…[dipotong]"
BACKOFF = (2, 5, 10)


class BotBlocked(Exception):
    """403 dari Bot API: user memblokir bot / bot dikeluarkan dari chat."""


class TokenInvalid(Exception):
    """401 dari Bot API: token salah."""


def format_time_wita(iso_ts: str | None) -> str:
    """'2026-09-20T06:28:44+00:00' -> '20 Sep 2026, 14:28 WITA'."""
    if not iso_ts:
        return "-"
    try:
        dt = datetime.fromisoformat(iso_ts.replace("Z", "+00:00"))
    except ValueError:
        return iso_ts
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    local = dt.astimezone(WITA)
    return f"{local.day} {MONTHS[local.month - 1]} {local.year}, {local:%H:%M} WITA"


def truncate(text: str, limit: int) -> str:
    if limit and len(text) > limit:
        return text[: max(0, limit - len(TRUNCATED))].rstrip() + TRUNCATED
    return text


class Sender:
    def __init__(self, token: str, chat_id: str, settings: dict, dry_run: bool = False):
        self.token = token
        self.chat_id = chat_id
        self.dry_run = dry_run
        self.configure(settings)

    def configure(self, settings: dict) -> None:
        """Terapkan setting pengiriman. Dipanggil ulang tiap cycle supaya
        perubahan channels.json (footer, media, batas panjang) ikut hot-reload —
        kalau tidak, Sender yang dibuat saat startup akan memakai setting lama."""
        self.settings = settings
        self.max_len = int(settings.get("max_message_length", 3800))
        self.max_caption = int(settings.get("max_caption_length", 1024))
        self.include_media = bool(settings.get("include_media", True))
        self.credit_footer = bool(settings.get("credit_footer", False))
        self.display_ca = bool(settings.get("display_ca", False))

    # ------------------------------------------------------------- transport
    def _api(self, method: str, payload: dict, files: dict | None = None) -> dict:
        """Panggil Bot API dengan retry sesuai PRD §8.6.

        `files` dipakai untuk upload gambar (multipart). Return {"ok": True, "result": ...}
        atau {"ok": False, "error": str, "status_code": int|None}.
        Raise BotBlocked / TokenInvalid untuk kasus yang tidak ada gunanya di-retry.
        """
        url = API_BASE.format(token=self.token, method=method)
        network_tries = 0
        rate_tries = 0

        while True:
            try:
                if files:
                    r = requests.post(url, data=payload, files=files, timeout=40)
                else:
                    r = requests.post(url, json=payload, timeout=20)
            except requests.RequestException as e:
                network_tries += 1
                if network_tries >= 3:
                    return {"ok": False, "error": f"network: {e}", "status_code": None}
                time.sleep(BACKOFF[min(network_tries - 1, len(BACKOFF) - 1)])
                continue

            try:
                body = r.json()
            except ValueError:
                body = {}
            description = body.get("description") or f"HTTP {r.status_code}"

            if r.status_code == 200 and body.get("ok"):
                return {"ok": True, "result": body.get("result")}

            if r.status_code == 429:
                ra_raw = (body.get("parameters") or {}).get("retry_after") \
                    or r.headers.get("Retry-After")
                retry_after = int(ra_raw) if str(ra_raw).isdigit() else 3
                rate_tries += 1
                if rate_tries <= 1:                      # §8.6: retry sekali
                    log.warning("429 dari Bot API, tunggu %ss lalu retry", retry_after + 1)
                    time.sleep(retry_after + 1)
                    continue
                return {"ok": False, "error": f"429 rate limited (retry_after={retry_after})",
                        "status_code": 429}

            if r.status_code == 401:
                raise TokenInvalid(description)

            if r.status_code == 403:
                raise BotBlocked(description)

            # 400 dan kawan-kawan: tidak akan berhasil kalau diulang.
            if r.status_code >= 500 and network_tries < 3:
                network_tries += 1
                time.sleep(BACKOFF[min(network_tries - 1, len(BACKOFF) - 1)])
                continue

            return {"ok": False, "error": description, "status_code": r.status_code}

    def verify(self) -> str:
        """getMe — validasi token saat startup (PRD §14). Return username bot."""
        if self.dry_run:
            return "dry-run"
        res = self._api("getMe", {})
        if not res.get("ok"):
            raise RuntimeError(f"token bot ditolak Telegram: {res.get('error')}")
        return (res.get("result") or {}).get("username", "?")

    # -------------------------------------------------------------- formatting
    def format_post(self, row, label: str) -> str:
        """Susun pesan sesuai PRD §8.4."""
        username = row["username"]
        msg_id = row["msg_id"]
        head = f"📢 {label}\n🕐 {format_time_wita(row['posted_at'])}"
        parts = [head]
        text = (row["text"] or "").strip()
        if text:
            parts.append(text)

        if self.display_ca:
            ca_line = self._ca_line(row["cas"])
            if ca_line:
                parts.append(ca_line)

        parts.append(f"🔗 https://t.me/{username}/{msg_id}")

        message = "\n\n".join(parts)
        if self.credit_footer:
            message = f"{message}\n\n{CREDIT_FOOTER}"
        return truncate(message, self.max_len)

    @staticmethod
    def _ca_line(cas_json: str | None) -> str:
        import json

        try:
            cas = json.loads(cas_json or "{}")
        except (ValueError, TypeError):
            return ""
        found = list(cas.get("solana", [])) + list(cas.get("evm", []))
        if not found:
            return ""
        return "🎯 CA:\n" + "\n".join(found[:5])

    # ----------------------------------------------------------------- sending
    @staticmethod
    def _download_image(url: str) -> bytes | None:
        """Unduh gambar sendiri. Telegram sering gagal ambil URL telesco.pe
        (signed URL) dengan error 'failed to get HTTP URL content'."""
        try:
            r = requests.get(url, timeout=20)
        except requests.RequestException as e:
            log.warning("gagal unduh gambar %s: %s", url[:60], e)
            return None
        ctype = (r.headers.get("Content-Type") or "").lower()
        if r.status_code == 200 and r.content and ctype.startswith("image/"):
            return r.content
        log.warning("URL media bukan image (status=%s ctype=%s)", r.status_code, ctype)
        return None

    def _send_photo(self, media_url: str, caption: str, row) -> dict:
        """Kirim foto: upload hasil unduhan (andal), fallback kirim via URL."""
        data = self._download_image(media_url)
        if data:
            res = self._api(
                "sendPhoto",
                {"chat_id": self.chat_id, "caption": caption},
                files={"photo": ("photo.jpg", data)},
            )
            if res.get("ok"):
                return res
            log.warning("upload foto gagal (%s), coba kirim via URL: %s#%s",
                        res.get("error"), row["username"], row["msg_id"])
        return self._api("sendPhoto", {
            "chat_id": self.chat_id,
            "photo": media_url,
            "caption": caption,
        })

    def send_post(self, row, label: str) -> tuple[str, str | None]:
        """Kirim satu post. Return (status, error). Raise BotBlocked/TokenInvalid."""
        message = self.format_post(row, label)
        media_url = (row["media_url"] or "").strip() or None

        if self.dry_run:
            log.info("[DRY-RUN] %s#%s -> %d char%s",
                     row["username"], row["msg_id"], len(message),
                     " + foto" if (media_url and self.include_media) else "")
            return "ok", None

        if media_url and self.include_media:
            res = self._send_photo(media_url, truncate(message, self.max_caption), row)
            if res.get("ok"):
                return "ok", None
            log.warning("sendPhoto gagal (%s), fallback ke sendMessage: %s#%s",
                        res.get("error"), row["username"], row["msg_id"])

        res = self._api("sendMessage", {
            "chat_id": self.chat_id,
            "text": message,
            "disable_web_page_preview": False,
        })
        if res.get("ok"):
            return "ok", None
        return "failed", str(res.get("error"))

    def send_test(self, text: str) -> tuple[str, str | None]:
        if self.dry_run:
            log.info("[DRY-RUN] test-send: %s", text)
            return "ok", None
        res = self._api("sendMessage", {
            "chat_id": self.chat_id,
            "text": text,
            "disable_web_page_preview": True,
        })
        if res.get("ok"):
            return "ok", None
        return "failed", str(res.get("error"))
