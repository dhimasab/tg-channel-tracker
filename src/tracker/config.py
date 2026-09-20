"""Config loader: channels.json + .env (PRD §6.1, §16).

channels.json dibaca ulang tiap cycle supaya bisa diedit tanpa restart (M1, §13.6).
Token bot TIDAK disimpan di sini — diambil dari environment variable
(`telegram.bot_token_env`), jadi file config aman di-commit ke git.
"""

from __future__ import annotations

import json
import logging
import os
import re
import sys
from pathlib import Path

from .models import Channel

log = logging.getLogger("tracker.config")

USERNAME_RE = re.compile(r"^[A-Za-z0-9_]{3,64}$")
VALID_FORWARD_MODES = ("all", "ca_only")

DEFAULT_SETTINGS: dict = {
    "poll_interval_seconds": 45,
    "max_workers": 5,
    "request_timeout_seconds": 20,
    "notify_on_first_run": False,      # §8.3 — default aman: jangan spam saat channel baru
    "include_media": True,
    "max_message_length": 3800,
    "max_caption_length": 1024,        # batas caption Telegram
    "max_send_per_cycle": 20,          # §8.5 — batasi recovery biar ga membanjiri chat
    "send_delay_seconds": 1.0,         # Bot API: ~1 pesan/detik per chat biar aman flood
    "credit_footer": True,
    "display_ca": False,               # v1: CA disimpan, tidak ditampilkan
    "default_forward_mode": "all",
}


class ConfigError(Exception):
    """channels.json tidak valid."""


def load_dotenv(path: str | Path) -> None:
    """Loader .env minimal (tanpa dependency tambahan).

    Format: KEY=VALUE, boleh diawali `export`, boleh dikutip. Tidak menimpa
    environment variable yang sudah ada di proses.
    """
    p = Path(path)
    if not p.exists():
        return
    for raw in p.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export "):].strip()
        key, _, val = line.partition("=")
        key = key.strip()
        val = val.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = val


def setup_logging(base_dir: str | Path, level: int = logging.INFO) -> None:
    """Log ke logs/app.log + stdout (PRD §5 — Logger)."""
    log_dir = Path(base_dir) / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    root = logging.getLogger()
    if root.handlers:  # sudah dikonfigurasi
        return
    fmt = logging.Formatter("%(asctime)s | %(levelname)-7s | %(name)s | %(message)s")
    fh = logging.FileHandler(log_dir / "app.log", encoding="utf-8")
    fh.setFormatter(fmt)
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    root.setLevel(level)
    root.addHandler(fh)
    root.addHandler(sh)


def load_config(path: str | Path) -> tuple[dict, dict, list[Channel]]:
    """Baca channels.json. Return (settings, telegram, channels_aktif).

    Dipanggil tiap cycle -> hot reload. Raise ConfigError kalau JSON rusak
    supaya service log error yang jelas dan tidak mati diam-diam.
    """
    p = Path(path)
    if not p.exists():
        raise ConfigError(f"file config tidak ada: {p}")
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise ConfigError(f"channels.json bukan JSON valid: {e}") from e

    if not isinstance(raw, dict):
        raise ConfigError("channels.json harus berupa object di root")

    settings = dict(DEFAULT_SETTINGS)
    settings.update(raw.get("settings") or {})

    telegram = raw.get("telegram") or {}
    if not isinstance(telegram, dict):
        raise ConfigError("'telegram' harus object")

    raw_channels = raw.get("channels")
    if not isinstance(raw_channels, list) or not raw_channels:
        raise ConfigError("'channels' harus array dan tidak boleh kosong")

    default_mode = settings.get("default_forward_mode", "all")
    channels: list[Channel] = []
    seen: set[str] = set()

    for i, item in enumerate(raw_channels):
        if not isinstance(item, dict):
            raise ConfigError(f"channels[{i}] harus object")
        username = str(item.get("username", "")).strip().lstrip("@")
        if not USERNAME_RE.match(username):
            raise ConfigError(
                f"channels[{i}].username tidak valid: {username!r} "
                "(huruf/angka/underscore, 3-64 karakter, tanpa @)"
            )
        if username.lower() in seen:
            log.warning("channel duplikat di channels.json, dilewati: %s", username)
            continue
        seen.add(username.lower())

        mode = str(item.get("forward_mode") or default_mode)
        if mode not in VALID_FORWARD_MODES:
            raise ConfigError(
                f"channels[{i}].forward_mode harus salah satu dari "
                f"{VALID_FORWARD_MODES}, dapat {mode!r}"
            )
        tags = item.get("tags") or []
        if not isinstance(tags, list):
            raise ConfigError(f"channels[{i}].tags harus array")

        channels.append(
            Channel(
                username=username,
                label=str(item.get("label") or username),
                enabled=bool(item.get("enabled", True)),
                tags=[str(t) for t in tags],
                forward_mode=mode,
            )
        )

    active = [c for c in channels if c.enabled]
    if not active:
        log.warning("semua channel di channels.json sedang enabled=false")
    return settings, telegram, active


def resolve_credentials(telegram: dict) -> tuple[str | None, str | None]:
    """Ambil token & chat id dari env sesuai nama yang dideklarasikan di config."""
    token_env = telegram.get("bot_token_env") or "TG_BOT_TOKEN"
    chat_env = telegram.get("chat_id_env") or "TG_CHAT_ID"
    token = (os.environ.get(token_env) or "").strip()
    chat = (os.environ.get(chat_env) or "").strip()
    return (token or None), (chat or None)
