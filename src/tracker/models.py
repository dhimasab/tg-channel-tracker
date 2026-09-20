"""Objek data yang dipakai lintas modul (PRD §11)."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Channel:
    """Satu channel dari channels.json."""

    username: str
    label: str
    enabled: bool = True
    tags: list[str] = field(default_factory=list)
    forward_mode: str = "all"  # all | ca_only


@dataclass
class Post:
    """Satu postingan hasil parsing HTML preview.

    `username` selalu identity yang kita lacak (dari channels.json).
    `detected_username` = username tampilan yang Telegram tulis di data-post
    (mis. SOLTRENDING untuk config soltrending) — dipakai cuma buat deteksi rename.
    """

    username: str
    msg_id: int
    posted_at: str | None = None
    text: str | None = None
    links: list[str] = field(default_factory=list)
    media_url: str | None = None
    cas: dict = field(default_factory=lambda: {"solana": [], "evm": []})
    detected_username: str | None = None

    @property
    def ca_count(self) -> int:
        return len(self.cas.get("solana", [])) + len(self.cas.get("evm", []))


@dataclass
class FetchResult:
    """Hasil HTTP GET ke t.me/s/<username>."""

    html: str = ""
    error: str | None = None
    http_status: int | None = None
    retry_after: int | None = None

    @property
    def ok(self) -> bool:
        return self.error is None


@dataclass
class ChannelResult:
    """Ringkasan satu channel dalam satu cycle."""

    username: str
    inserted: int = 0
    skipped: int = 0
    newest_id: int = 0
    error: str | None = None
    http_status: int | None = None
    retry_after: int | None = None
    skipped_cycle: bool = False


@dataclass
class SendResult:
    """Hasil pengiriman satu post."""

    username: str
    msg_id: int
    status: str  # ok | failed | skipped
    error: str | None = None
