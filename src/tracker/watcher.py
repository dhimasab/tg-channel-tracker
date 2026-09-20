"""Loop utama + orkestrasi (PRD §5, §8).

Alur satu cycle:

  Phase 1 (paralel, ThreadPoolExecutor)
      baca channels.json  ->  GET t.me/s/<username>  ->  parse  ->  INSERT OR IGNORE
      (tidak ada pengiriman di sini)

  Phase 2 (single thread, berurutan)
      ambil semua post dengan notified_at IS NULL  ->  kirim  ->  tandai
      Ini sekaligus jadi recovery §8.5: post yang gagal terkirim otomatis
      ikut terambil di cycle berikutnya.

Dua phase dipisah supaya: (a) kirim pesan tidak berlomba-lomba antar thread,
(b) urutan pesan stabil, (c) urutan "simpan dulu, baru kirim, baru tandai"
tidak menghasilkan kiriman ganda kalau proses mati di tengah (audit P0-3).

Guard run pertama (§8.3): kalau last_msg_id masih 0 (channel baru), post yang ada
disimpan tapi ditandai 'skipped' — tidak dikirim. Jadi nambah channel tidak
memicu banjir 20 notifikasi.
"""

from __future__ import annotations

import atexit
import json
import logging
import os
import random
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from . import fetcher, parser, store
from .config import load_config
from .models import Channel, ChannelResult
from .sender import BotBlocked, TokenInvalid

log = logging.getLogger("tracker.watcher")

FAIL_THRESHOLD = 5        # §8.6: setelah 5 gagal beruntun -> dianggap bermasalah
FAIL_RETRY_EVERY = 10     # §8.6: tetap dicoba tiap 10 cycle
MAX_BACKOFF_INTERVAL = 300


# ---------------------------------------------------------------- worker tunggal
def process_channel(
    ch: Channel, settings: dict, db_path: str, cycle: int, mode: str = "run"
) -> ChannelResult:
    """Kerjakan satu channel. Jalan di dalam worker thread -> koneksi DB sendiri."""
    conn = store.connect(db_path)
    try:
        state = store.get_state(conn, ch.username)

        # Channel yang sudah bermasalah dicoba tiap 10 cycle, bukan tiap cycle.
        if (
            state["consecutive_failures"] >= FAIL_THRESHOLD
            and cycle % FAIL_RETRY_EVERY != 0
        ):
            return ChannelResult(username=ch.username, skipped_cycle=True)

        res = fetcher.fetch_channel(
            ch.username, timeout=int(settings.get("request_timeout_seconds", 20))
        )
        if not res.ok:
            fails = store.record_failure(conn, ch.username, res.error or "unknown")
            if fails == FAIL_THRESHOLD:
                log.warning(
                    "[%s] %d gagal beruntun, dicoba tiap %d cycle sampai pulih",
                    ch.username, fails, FAIL_RETRY_EVERY,
                )
            return ChannelResult(
                username=ch.username, error=res.error,
                http_status=res.http_status, retry_after=res.retry_after,
            )

        if not parser.is_public(res.html):
            err = "bukan channel publik / username salah / tidak ada post"
            store.record_failure(conn, ch.username, err)
            return ChannelResult(username=ch.username, error=err, http_status=200)

        posts = parser.parse_posts(res.html, ch.username)
        if not posts:
            err = "halaman publik tapi tidak ada post yang bisa dibaca"
            store.record_failure(conn, ch.username, err)
            return ChannelResult(username=ch.username, error=err, http_status=200)

        # Identity yang dilacak adalah username di channels.json; `data-post` cuma
        # username tampilan Telegram (mis. SOLTRENDING untuk soltrending). parser
        # sudah menaruh identity yang benar di p.username, dan nilai mentahnya di
        # p.detected_username — kalau beda, kemungkinan channel ganti username.
        detected = sorted({p.detected_username for p in posts if p.detected_username})
        if any(d.lower() != ch.username.lower() for d in detected):
            log.info(
                "[%s] Telegram menampilkan username %s di data-post — disimpan sebagai %s",
                ch.username, ",".join(detected), ch.username,
            )

        last_id = int(state["last_msg_id"] or 0)
        new_posts = [p for p in posts if p.msg_id > last_id]
        inserted_ids = store.insert_posts(conn, new_posts)

        # §8.3 — backfill: jangan kirim post yang sudah ada saat channel didaftarkan.
        # Sengaja pakai SEMUA post pending channel ini (bukan cuma yang baru di-insert):
        # kalau cycle sebelumnya mati setelah insert tapi sebelum update state,
        # last_msg_id masih 0 dan post-post itu belum ditandai.
        skipped_count = 0
        first_run = last_id == 0 and not settings.get("notify_on_first_run", False)
        if mode == "backfill" or first_run:
            todo = store.pending_ids(conn, ch.username)
            skipped_count = store.mark_skipped(conn, ch.username, todo)
            log.info(
                "[%s] %s: %d post disimpan tanpa dikirim",
                ch.username, "backfill" if mode == "backfill" else "run pertama",
                skipped_count,
            )

        newest = max(p.msg_id for p in posts)
        store.record_success(conn, ch.username, newest, len(inserted_ids))
        return ChannelResult(
            username=ch.username,
            inserted=len(inserted_ids) - skipped_count,
            skipped=skipped_count,
            newest_id=newest,
            http_status=200,
        )
    except Exception as e:  # worker tidak boleh mematikan service
        log.exception("[%s] exception di worker", ch.username)
        try:
            store.record_failure(conn, ch.username, f"exception: {e}")
        except Exception:
            pass
        return ChannelResult(username=ch.username, error=f"exception: {e}")
    finally:
        conn.close()


class Watcher:
    """Scheduler + orkestrator."""

    def __init__(self, base_dir: str | Path, config_path: str | Path,
                 db_path: str | Path, sender=None, dry_run: bool = False):
        self.base_dir = Path(base_dir)
        self.config_path = Path(config_path)
        self.db_path = str(db_path)
        self.sender = sender
        self.dry_run = dry_run

    # ---------------------------------------------------------------- phase 1
    def scrape(self, settings: dict, channels: list[Channel], cycle: int,
               mode: str = "run") -> list[ChannelResult]:
        workers = max(1, int(settings.get("max_workers", 5)))
        results: list[ChannelResult] = []
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {
                pool.submit(process_channel, ch, settings, self.db_path, cycle, mode): ch
                for ch in channels
            }
            for fut in as_completed(futures):
                ch = futures[fut]
                try:
                    results.append(fut.result())
                except Exception as e:
                    log.error("[%s] future gagal: %s", ch.username, e)
                    results.append(ChannelResult(username=ch.username, error=str(e)))
        return results

    # ---------------------------------------------------------------- phase 2
    def send_pending(self, settings: dict, channels: list[Channel]) -> dict:
        """Kirim semua post yang belum terkirim (termasuk sisa cycle sebelumnya)."""
        stats = {"sent": 0, "failed": 0, "skipped": 0, "pending_left": 0}
        if self.sender is None:
            return stats

        # Hot reload setting pengiriman (footer, media, batas panjang, CA).
        configure = getattr(self.sender, "configure", None)
        if configure:
            configure(settings)

        limit = int(settings.get("max_send_per_cycle", 20))
        delay = float(settings.get("send_delay_seconds", 1.0))
        mode_by_user = {c.username: c.forward_mode for c in channels}
        label_by_user = {c.username: c.label for c in channels}

        conn = store.connect(self.db_path)
        try:
            rows = store.pending_posts(conn, limit)
            for i, row in enumerate(rows):
                username, msg_id = row["username"], row["msg_id"]
                label = label_by_user.get(username, username)

                if mode_by_user.get(username, "all") == "ca_only":
                    try:
                        cas = json.loads(row["cas"] or "{}")
                    except (ValueError, TypeError):
                        cas = {}
                    if not (cas.get("solana") or cas.get("evm")):
                        store.mark_notified(conn, username, msg_id, "skipped", "tanpa CA")
                        stats["skipped"] += 1
                        continue

                try:
                    status, error = self.sender.send_post(row, label)
                except TokenInvalid as e:
                    log.error("token bot ditolak Telegram, hentikan pengiriman: %s", e)
                    break
                except BotBlocked as e:
                    log.error(
                        "bot di-block (403): %s — pengiriman dihentikan, "
                        "post tetap di antrean sampai bot bisa kirim lagi", e,
                    )
                    break

                store.mark_notified(conn, username, msg_id, status, error)
                if status == "ok":
                    store.bump_sent(conn, username)
                    stats["sent"] += 1
                    log.info("[%s] terkirim #%s (%s)", username, msg_id, label)
                else:
                    stats["failed"] += 1
                    log.warning("[%s] gagal #%s: %s", username, msg_id, error)

                if delay and i < len(rows) - 1:
                    time.sleep(delay)

            stats["pending_left"] = store.pending_count(conn)
        finally:
            conn.close()
        return stats

    # ------------------------------------------------------------------ cycles
    def cycle(self, settings: dict, telegram: dict, channels: list[Channel],
              cycle: int, mode: str = "run") -> dict:
        t0 = time.time()
        results = self.scrape(settings, channels, cycle, mode)
        send_stats = self.send_pending(settings, channels)

        inserted = sum(r.inserted for r in results)
        skipped = sum(r.skipped for r in results)
        errors = [r for r in results if r.error]
        rate_limited = any(r.http_status == 429 for r in results)
        idle = sum(1 for r in results if r.skipped_cycle)

        log.info(
            "cycle #%d | %d channel | %d post baru | %d backfill | "
            "%d terkirim | %d gagal | %d error | %d ditunda | %.1fs",
            cycle, len(channels), inserted, skipped, send_stats["sent"],
            send_stats["failed"], len(errors), idle, time.time() - t0,
        )
        for r in errors:
            log.warning("  [%s] %s", r.username, r.error)

        return {
            "inserted": inserted, "skipped": skipped, "errors": errors,
            "rate_limited": rate_limited, "send": send_stats,
            "duration": time.time() - t0,
        }

    def run_once(self, settings: dict, telegram: dict, channels: list[Channel],
                 mode: str = "run", cycle: int = 1) -> dict:
        return self.cycle(settings, telegram, channels, cycle, mode)

    def run_forever(self, settings: dict, telegram: dict, channels: list[Channel]) -> None:
        base_interval = max(15, int(settings.get("poll_interval_seconds", 45)))
        interval = base_interval
        cycle = 0
        log.info("mulai loop: %d channel | interval %ss | %s",
                 len(channels), interval, "DRY-RUN" if self.dry_run else "LIVE")

        while True:
            cycle += 1
            # Hot reload tiap cycle (PRD §8.1, §13.6): edit channels.json langsung kepakai.
            try:
                settings, telegram, channels = load_config(self.config_path)
            except Exception as e:
                log.error("channels.json tidak bisa dibaca (%s) — pakai config cycle sebelumnya", e)
            else:
                conn = store.connect(self.db_path)
                try:
                    store.sync_channels(conn, channels)
                finally:
                    conn.close()

            try:
                out = self.cycle(settings, telegram, channels, cycle)
            except Exception:
                log.exception("cycle gagal total, lanjut cycle berikutnya")
                out = {"rate_limited": False, "duration": 0}

            if out.get("rate_limited"):
                interval = min(interval * 2 if interval > base_interval else base_interval * 2,
                               MAX_BACKOFF_INTERVAL)
                log.warning("kena HTTP 429 dari Telegram, interval cycle berikutnya %ss", interval)
            else:
                interval = base_interval

            sleep_for = max(5.0, interval - float(out.get("duration", 0)))
            sleep_for += random.uniform(0, 3)          # jitter (PRD §8.1)
            time.sleep(sleep_for)


# ------------------------------------------------------------------ lock file
def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def acquire_lock(path: str | Path) -> None:
    """Lock file .pid — instance kedua langsung exit (PRD §14)."""
    p = Path(path)
    if p.exists():
        try:
            old = int(p.read_text().strip())
        except (ValueError, OSError):
            old = None
        if old and _pid_alive(old):
            raise RuntimeError(
                f"instance lain masih jalan (pid {old}, lock {p}). "
                "Hentikan dulu, atau hapus file itu kalau process-nya sudah mati."
            )
        log.warning("lock file basi (pid %s sudah tidak ada), diambil alih", old)

    p.write_text(str(os.getpid()))
    atexit.register(lambda: p.unlink(missing_ok=True))
