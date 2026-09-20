"""Entry point CLI (PRD §9.1).

  python -m tracker run                  # service loop
  python -m tracker run --once           # satu cycle lalu keluar (cocok buat cron)
  python -m tracker run --dry-run        # jalan tanpa kirim apa pun
  python -m tracker backfill             # tarik history, tidak kirim notifikasi
  python -m tracker stats                # ringkasan semua channel
  python -m tracker test-send            # kirim 1 pesan tes ke chat kamu
  python -m tracker check <username>     # cek channel publik/nggak + preview 3 post
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

from . import fetcher, store
from .config import ConfigError, load_config, load_dotenv, resolve_credentials, setup_logging
from .sender import CREDIT_FOOTER, Sender, TokenInvalid, format_time_wita
from .watcher import Watcher, acquire_lock

log = logging.getLogger("tracker")

BASE_DIR = Path(__file__).resolve().parents[2]
CONFIG_PATH = BASE_DIR / "channels.json"
DB_PATH = BASE_DIR / "data" / "tracker.db"
ENV_PATH = BASE_DIR / ".env"
LOCK_PATH = BASE_DIR / ".pid"


# --------------------------------------------------------------------- helpers
def _load(require_token: bool = False, dry_run: bool = False):
    """Muat .env + config. Return (settings, telegram, channels, sender|None)."""
    load_dotenv(ENV_PATH)
    try:
        settings, telegram, channels = load_config(CONFIG_PATH)
    except ConfigError as e:
        print(f"config tidak valid: {e}", file=sys.stderr)
        raise SystemExit(2)

    sender = None
    if require_token:
        token, chat_id = resolve_credentials(telegram)
        if not token or not chat_id:
            if dry_run:
                sender = Sender("dry-run", "0", settings, dry_run=True)
            else:
                print(
                    "token/chat id belum diisi.\n"
                    f"  Buka {ENV_PATH} (contoh ada di .env.example) lalu isi:\n"
                    "    TG_BOT_TOKEN=123456:ABC...   <- dari @BotFather\n"
                    "    TG_CHAT_ID=123456789         <- id chat kamu/grup\n"
                    "  Ambil chat id: kirim pesan ke bot, buka\n"
                    "    https://api.telegram.org/bot<TOKEN>/getUpdates\n"
                    "  lalu ambil result[0].message.chat.id",
                    file=sys.stderr,
                )
                raise SystemExit(2)
        else:
            sender = Sender(token, chat_id, settings, dry_run=dry_run)
    return settings, telegram, channels, sender


def _init_db() -> None:
    store.init(DB_PATH).close()


def _verify_sender(sender: Sender) -> None:
    """Fail fast kalau token salah (PRD §14)."""
    if sender.dry_run:
        return
    try:
        who = sender.verify()
    except (TokenInvalid, RuntimeError) as e:
        print(f"token bot tidak valid: {e}", file=sys.stderr)
        raise SystemExit(2)
    log.info("bot terverifikasi: @%s -> chat %s", who, sender.chat_id)


# -------------------------------------------------------------------- commands
def cmd_run(args) -> int:
    settings, telegram, channels, sender = _load(require_token=True, dry_run=args.dry_run)
    if args.interval:
        settings["poll_interval_seconds"] = args.interval
    if args.once:
        # --once tidak perlu lock supaya aman dipakai bareng cron.
        _init_db()
        watcher = Watcher(BASE_DIR, CONFIG_PATH, DB_PATH, sender, args.dry_run)
        conn = store.connect(DB_PATH)
        try:
            store.sync_channels(conn, channels)
        finally:
            conn.close()
        if sender and not args.dry_run:
            _verify_sender(sender)
        watcher.run_once(settings, telegram, channels)
        return 0

    try:
        acquire_lock(LOCK_PATH)
    except RuntimeError as e:
        print(f"{e}", file=sys.stderr)
        return 2
    _init_db()
    conn = store.connect(DB_PATH)
    try:
        store.sync_channels(conn, channels)
    finally:
        conn.close()
    _verify_sender(sender)
    watcher = Watcher(BASE_DIR, CONFIG_PATH, DB_PATH, sender, args.dry_run)
    watcher.run_forever(settings, telegram, channels)
    return 0


def cmd_backfill(args) -> int:
    """Tarik history channel tanpa kirim apa pun (PRD §8.3)."""
    settings, telegram, channels, _ = _load()
    settings["notify_on_first_run"] = False
    if args.channel:
        channels = [c for c in channels if c.username.lower() == args.channel.lower().lstrip("@")]
        if not channels:
            print(f"channel {args.channel!r} tidak ada / disabled di channels.json", file=sys.stderr)
            return 2

    _init_db()
    watcher = Watcher(BASE_DIR, CONFIG_PATH, DB_PATH, sender=None)
    conn = store.connect(DB_PATH)
    try:
        store.sync_channels(conn, channels)
    finally:
        conn.close()

    out = watcher.run_once(settings, telegram, channels, mode="backfill")
    print(f"\nbackfill selesai: {out['skipped']} post disimpan tanpa dikirim "
          "(ditandai notified_at, tidak ada notifikasi)")
    return 0


def cmd_stats(args) -> int:
    settings, telegram, channels, _ = _load()
    _init_db()
    conn = store.connect(DB_PATH)
    try:
        rows = store.stats_rows(conn)
        if not rows:
            print("belum ada data. Jalankan `backfill` dulu.")
            return 0

        print(f"\n{'CHANNEL':<24}{'POST':>6}{'KIRIM':>7}{'ANTRE':>7}  {'STATUS':<28}POLL TERAKHIR")
        print("-" * 104)
        for r in rows:
            if r["last_error"]:
                status = f"⚠  {r['last_error'][:24]}"
            elif r["consecutive_failures"]:
                status = f"⚠  gagal {r['consecutive_failures']}x"
            else:
                status = "✅ ok"
            poll = (r["last_poll_at"] or "-")[:19].replace("T", " ")
            print(f"{r['username']:<24}{r['total_posts']:>6}{r['total_sent']:>7}"
                  f"{r['pending']:>7}  {status:<28}{poll}")

        print("-" * 104)
        pending = store.pending_count(conn)
        print(f"total post tersimpan : {store.total_posts(conn)}")
        print(f"belum terkirim       : {pending}")
        print(f"channel aktif        : {len(channels)} dari config")

        errs = store.recent_send_errors(conn, 5)
        if errs:
            print("\nkegagalan kirim terakhir:")
            for e in errs:
                print(f"  {e['sent_at'][:19]} {e['username']}#{e['msg_id']} "
                      f"[{e['status']}] {(e['error'] or '')[:60]}")
        print(f"\ndatabase : {DB_PATH}")
    finally:
        conn.close()
    return 0


def cmd_test_send(args) -> int:
    settings, telegram, channels, sender = _load(require_token=True)
    _verify_sender(sender)
    text = args.text or (
        "✅ Test dari TG Channel Tracker\n\n"
        f"channel aktif: {len(channels)}\n"
        f"interval: {settings.get('poll_interval_seconds')}s\n"
        f"include_media: {settings.get('include_media')}\n"
        f"credit_footer: {settings.get('credit_footer')}\n"
        + (
            f"\nContoh footer yang bakal nempel di tiap pesan:\n\n{CREDIT_FOOTER}"
            if settings.get("credit_footer") else ""
        )
    )
    status, error = sender.send_test(text)
    if status == "ok":
        print(f"terkirim ke chat {sender.chat_id}")
        return 0
    print(f"gagal kirim: {error}", file=sys.stderr)
    return 1


def cmd_check(args) -> int:
    username = args.username.lstrip("@")
    _load()
    res = fetcher.probe(username, timeout=20)
    if res["status"] != "publik":
        print(f"@{username}: {res['status']} — {res.get('detail', '')}")
        return 1

    posts = res["posts"]
    print(f"@{username}: publik ✅ ({len(posts)} post terbaca)")
    newest = max(posts, key=lambda p: p.msg_id)
    print(f"post terbaru: #{newest.msg_id} — {format_time_wita(newest.posted_at)}")
    print()
    for p in posts[-3:]:
        text = (p.text or "(tanpa teks)").replace("\n", " ")
        print(f"--- #{p.msg_id} | {format_time_wita(p.posted_at)}")
        print(f"    teks    : {text[:220]}")
        print(f"    media   : {'ada' if p.media_url else 'tidak ada'}")
        if p.media_url:
            print(f"              {p.media_url[:100]}")
        print(f"    link    : {len(p.links)} | CA: {p.ca_count} {p.cas}")
        print()
    return 0


# ------------------------------------------------------------------------ main
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="tracker",
        description="Pantau channel Telegram publik via web preview, forward tiap post baru.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_run = sub.add_parser("run", help="jalankan service (loop)")
    p_run.add_argument("--once", action="store_true", help="satu cycle lalu keluar")
    p_run.add_argument("--dry-run", action="store_true", help="tidak kirim ke Telegram")
    p_run.add_argument("--interval", type=int, help="override poll_interval_seconds")
    p_run.set_defaults(func=cmd_run)

    p_bf = sub.add_parser("backfill", help="tarik history tanpa kirim notifikasi")
    p_bf.add_argument("channel", nargs="?", help="batasi ke satu channel")
    p_bf.set_defaults(func=cmd_backfill)

    p_stats = sub.add_parser("stats", help="ringkasan semua channel")
    p_stats.set_defaults(func=cmd_stats)

    p_test = sub.add_parser("test-send", help="kirim 1 pesan tes ke chat kamu")
    p_test.add_argument("--text", help="isi pesan (opsional)")
    p_test.set_defaults(func=cmd_test_send)

    p_check = sub.add_parser("check", help="cek channel publik/nggak + preview post")
    p_check.add_argument("username")
    p_check.set_defaults(func=cmd_check)

    args = parser.parse_args(argv)
    BASE_DIR.mkdir(parents=True, exist_ok=True)
    setup_logging(BASE_DIR, logging.DEBUG if os.environ.get("TRACKER_DEBUG") else logging.INFO)

    try:
        return args.func(args)
    except KeyboardInterrupt:
        print("\ndihentikan.")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
