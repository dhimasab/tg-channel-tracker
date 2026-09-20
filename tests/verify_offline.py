#!/usr/bin/env python3
"""Ad-hoc verification untuk /home/ubuntu/TelegramTracker.

Bukan test suite resmi milik project — ini script verifikasi sementara yang
menguji perilaku yang baru diubah, offline (tanpa network, tanpa token).

Cakupan:
  parser   : clean_text, media_url (emoji vs foto), is_public, parse_posts, CA
  sender   : format WITA, truncate, format_post §8.4, pemetaan error Bot API
  store    : dedup, siklus notified_at/pending, mark_skipped, stats
  config   : validasi + default + normalisasi username
  watcher  : guard run pertama, deteksi post baru, phase-2 send, lock file
"""

from __future__ import annotations

import sys
import tempfile
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from tracker import fetcher, parser, store, watcher          # noqa: E402
from tracker.config import ConfigError, DEFAULT_SETTINGS, load_config   # noqa: E402
from tracker.models import Channel                            # noqa: E402
from tracker.sender import (BotBlocked, Sender, TokenInvalid,  # noqa: E402
                            format_time_wita, truncate)

RESULTS: list[tuple[bool, str, str]] = []


def check(name: str, fn):
    try:
        fn()
        RESULTS.append((True, name, ""))
    except Exception as e:
        RESULTS.append((False, name, f"{type(e).__name__}: {e}"))
        traceback.print_exc()


def eq(got, want, label=""):
    assert got == want, f"{label} dapat {got!r}, harusnya {want!r}"


def truthy(cond, label=""):
    assert cond, f"{label} harusnya truthy, dapat {cond!r}"


# --------------------------------------------------------------------- fixture
POST = """<div class="tgme_widget_message" data-post="TESTCHAN/{mid}">
  <div class="tgme_widget_message_bubble">
    {body}
    {photo}
    <time datetime="{ts}"></time>
  </div>
</div>"""


def block(mid, ts, body="", photo=False, emoji=False):
    if emoji:
        body += ('<i class="emoji" style="background-image:url('
                 "'//telegram.org/img/emoji/40/F09F9189.png')\"><b>\U0001f449</b></i>")
    if photo:
        body += ('<a class="tgme_widget_message_photo_wrap 123 456" '
                 'href="https://t.me/testchan/%d" style="width:800px;'
                 "background-image:url('https://cdn5.telesco.pe/file/REALPHOTO.jpg')\">"
                 '<div class="tgme_widget_message_photo" style="padding-top:56.25%%">'
                 "</div></a>" % mid)
    inner = f'<div class="tgme_widget_message_text js-message_text" dir="auto">{body}</div>' if body else ""
    return POST.format(mid=mid, ts=ts, body=inner, photo="",
                       ).replace("<time", '<div class="tgme_widget_message_footer"></div><time')


CA = "So11111111111111111111111111111111111111112"

PAGE = (
    '<div class="tgme_channel_info">TestChannel</div>'
    + block(100, "2026-09-20T06:28:44+00:00",
            body=f"Alpha <b>launch</b> {CA}<br/><br/>Please open Telegram to view this post"
                 "<br/>VIEW IN TELEGRAM<br/>"
                 '<a href="https://dexscreener.com/solana/ABC">Dex</a>'
                 '<a href="https://t.me/testchan/100">lihat</a>',
            photo=True, emoji=True)
    + block(101, "2026-09-20T06:29:00+00:00", body="Hanya emoji", emoji=True)
    # post tanpa div teks sama sekali (service message) -> text None
    + POST.format(mid=102, ts="2026-09-20T06:30:00+00:00", body="", photo="")
    + block(103, "2026-09-20T06:31:00+00:00", body=f"New pair {CA}")
)

# ------------------------------------------------------------------- parser
def t_parser_public():
    truthy(parser.is_public(PAGE), "halaman dengan tgme_channel_info")
    eq(parser.is_public("<html>landing page</html>"), False, "halaman privat")


def t_parser_posts():
    posts = parser.parse_posts(PAGE, "testchan")
    eq([p.msg_id for p in posts], [100, 101, 102, 103], "urut msg_id")
    eq(posts[0].posted_at, "2026-09-20T06:28:44+00:00", "posted_at")
    eq(posts[2].text, None, "post tanpa div teks disimpan, bukan dibuang")
    # fixture memakai data-post="TESTCHAN" (huruf besar) — identity harus tetap
    # yang diminta pemanggil, nilai mentah disimpan di detected_username
    eq({p.username for p in posts}, {"testchan"}, "identity = argumen, bukan data-post")
    eq(posts[0].detected_username, "TESTCHAN", "username tampilan tercatat")


def t_parser_clean_text():
    posts = parser.parse_posts(PAGE, "testchan")
    txt = posts[0].text
    truthy(txt and CA in txt, "CA di badan teks")
    for junk in ("VIEW IN TELEGRAM", "Please open Telegram", "\U0001f449", "<b>", "<br"):
        truthy(junk not in txt, f"junk {junk!r} sudah dibuang")
    # link t.me/ disaring, link konten dibiarkan
    eq(posts[0].links, ["https://dexscreener.com/solana/ABC"], "filter link")
    truthy(posts[0].ca_count >= 1, "CA terdeteksi dari teks")


def t_parser_media():
    posts = parser.parse_posts(PAGE, "testchan")
    eq(posts[0].media_url, "https://cdn5.telesco.pe/file/REALPHOTO.jpg", "foto asli")
    eq(posts[1].media_url, None, "emoji TIDAK dianggap foto (bug PRD 7.2)")
    eq(posts[3].media_url, None, "tanpa foto")


def t_parser_protocol_relative():
    html = ('<div class="tgme_widget_message" data-post="c/1">'
            '<a class="tgme_widget_message_photo_wrap" style="background-image:'
            "url('//cdn1.telesco.pe/file/x.jpg')\"></a></div>")
    eq(parser.extract_media_url(html), "https://cdn1.telesco.pe/file/x.jpg", "// -> https")


# ------------------------------------------------------------------- sender
def t_time_wita():
    eq(format_time_wita("2026-09-20T06:28:44+00:00"), "20 Sep 2026, 14:28 WITA", "WITA")
    eq(format_time_wita(None), "-", "timestamp kosong")


def t_truncate():
    assert truncate("x" * 50, 20).endswith("…[dipotong]"), "penanda potong"
    eq(len(truncate("x" * 50, 20)), 20, "panjang hasil")
    eq(truncate("pendek", 100), "pendek", "tidak dipotong kalau pendek")


def _row(**kw):
    base = dict(username="testchan", msg_id=100, posted_at="2026-09-20T06:28:44+00:00",
                text="isi post", links="[]", media_url=None, cas='{"solana":[],"evm":[]}')
    base.update(kw)
    return base


def t_format_post():
    s = Sender("t", "1", DEFAULT_SETTINGS)
    msg = s.format_post(_row(), "Test Chan")
    truthy(msg.startswith("📢 Test Chan\n🕐 20 Sep 2026, 14:28 WITA"), "header §8.4")
    truthy("🔗 https://t.me/testchan/100" in msg, "link post")
    truthy("teer.id/PejuangCryptoID" in msg, "credit footer")
    truthy("———" in msg, "separator footer")
    truthy("<a " not in msg and "<b>" not in msg, "footer teks polos, bukan hyperlink")


def t_format_post_no_footer():
    st = dict(DEFAULT_SETTINGS, credit_footer=False)
    msg = Sender("t", "1", st).format_post(_row(text=None), "L")
    truthy("teer.id" not in msg, "footer mati")
    truthy("(tanpa teks)" not in msg and "None" not in msg, "text None tidak bocor")


def t_format_post_display_ca():
    st = dict(DEFAULT_SETTINGS, display_ca=True)
    msg = Sender("t", "1", st).format_post(_row(cas=f'{{"solana":["{CA}"],"evm":[]}}'), "L")
    truthy(f"🎯 CA:\n{CA}" in msg, "CA ditampilkan saat display_ca")


def t_api_429_retry():
    import tracker.sender as S
    calls = {"n": 0}

    class R:
        def __init__(self, code, body, headers=None):
            self.status_code, self._b, self.headers = code, body, headers or {}
        def json(self):
            return self._b

    real_post, real_sleep = S.requests.post, S.time.sleep
    S.time.sleep = lambda *_: None
    try:
        def fake_post(url, json=None, timeout=None):
            calls["n"] += 1
            if calls["n"] == 1:
                return R(429, {"ok": False, "description": "Too Many Requests",
                               "parameters": {"retry_after": 1}})
            return R(200, {"ok": True, "result": {"message_id": 7}})
        S.requests.post = fake_post
        res = Sender("t", "1", DEFAULT_SETTINGS)._api("sendMessage", {})
        truthy(res["ok"], "retry setelah 429 berhasil")
        eq(calls["n"], 2, "jumlah percobaan")
    finally:
        S.requests.post, S.time.sleep = real_post, real_sleep


def t_api_429_give_up():
    import tracker.sender as S

    class R:
        status_code, headers = 429, {}
        def json(self):
            return {"ok": False, "description": "Too Many Requests",
                    "parameters": {"retry_after": 1}}

    real_post, real_sleep = S.requests.post, S.time.sleep
    S.time.sleep = lambda *_: None
    try:
        S.requests.post = lambda *a, **k: R()
        res = Sender("t", "1", DEFAULT_SETTINGS)._api("sendMessage", {})
        eq(res["ok"], False, "menyerah setelah retry 429")
        truthy("429" in res["error"], "error 429 tercatat")
    finally:
        S.requests.post, S.time.sleep = real_post, real_sleep


def t_api_ok_false_http200():
    """HTTP 200 tapi {"ok": false} -> HARUS dianggap gagal (bug prototipe lama)."""
    import tracker.sender as S

    class R:
        status_code, headers = 200, {}
        def json(self):
            return {"ok": False, "description": "chat not found"}

    real = S.requests.post
    try:
        S.requests.post = lambda *a, **k: R()
        res = Sender("t", "1", DEFAULT_SETTINGS)._api("sendMessage", {})
        eq(res["ok"], False, "ok:false ditangkap")
        eq(res["error"], "chat not found", "deskripsi error")
    finally:
        S.requests.post = real


def t_api_403_and_401():
    import tracker.sender as S

    class R:
        def __init__(self, code, desc):
            self.status_code, self.headers = code, {}
            self._d = desc
        def json(self):
            return {"ok": False, "description": self._d}

    real = S.requests.post
    try:
        S.requests.post = lambda *a, **k: R(403, "bot was blocked by the user")
        try:
            Sender("t", "1", DEFAULT_SETTINGS)._api("sendMessage", {})
            raise AssertionError("403 harusnya raise BotBlocked")
        except BotBlocked:
            pass
        S.requests.post = lambda *a, **k: R(401, "Unauthorized")
        try:
            Sender("t", "1", DEFAULT_SETTINGS)._api("getMe", {})
            raise AssertionError("401 harusnya raise TokenInvalid")
        except TokenInvalid:
            pass
    finally:
        S.requests.post = real


def t_verify_and_dry_run():
    s = Sender("t", "1", DEFAULT_SETTINGS, dry_run=True)
    truthy(s.verify() != "", "dry-run verify tidak memanggil network")
    eq(s.send_post(_row(text="x"), "L"), ("ok", None), "dry-run tidak mengirim")


# -------------------------------------------------------------------- store
def t_store_dedup_and_pending():
    with tempfile.TemporaryDirectory(prefix="hermes-verify-") as d:
        db = Path(d) / "v.db"
        conn = store.connect(db)
        store.init_schema(conn)
        store.sync_channels(conn, [Channel(username="testchan", label="Test Chan")])

        posts = parser.parse_posts(PAGE, "testchan")
        ids = store.insert_posts(conn, posts)
        eq(ids, [100, 101, 102, 103], "insert pertama")
        eq(store.insert_posts(conn, posts), [], "insert kedua = 0 (dedup PK)")
        eq(store.total_posts(conn), 4, "total post")
        eq(store.pending_count(conn), 4, "semua pending sebelum ditandai")

        eq(store.mark_skipped(conn, "testchan", store.pending_ids(conn, "testchan")), 4,
           "mark_skipped")
        eq(store.pending_count(conn), 0, "pending habis setelah skipped")

        store.mark_notified(conn, "testchan", 103, "ok")
        eq(store.pending_count(conn), 0, "notified tetap 0 pending")
        store.bump_sent(conn, "testchan")
        row = conn.execute("SELECT total_sent FROM channel_state").fetchone()
        eq(row["total_sent"], 1, "total_sent naik")

        # last_msg_id tidak pernah mundur
        store.record_success(conn, "testchan", 103, 4)
        store.record_success(conn, "testchan", 50, 0)
        eq(store.get_state(conn, "testchan")["last_msg_id"], 103, "last_msg_id monoton")

        # record_failure menaikkan counter, tidak menyentuh last_msg_id
        eq(store.record_failure(conn, "testchan", "HTTP 500"), 1, "failure #1")
        eq(store.record_failure(conn, "testchan", "HTTP 500"), 2, "failure #2")
        eq(store.get_state(conn, "testchan")["last_msg_id"], 103, "last_msg_id utuh")
        store.record_success(conn, "testchan", 103, 0)
        eq(store.get_state(conn, "testchan")["consecutive_failures"], 0, "reset setelah sukses")
        conn.close()


# ------------------------------------------------------------------- config
def t_config_validation():
    with tempfile.TemporaryDirectory(prefix="hermes-verify-") as d:
        p = Path(d) / "c.json"

        p.write_text("{ bukan json }")
        try:
            load_config(p)
            raise AssertionError("JSON rusak harusnya ConfigError")
        except ConfigError:
            pass

        p.write_text('{"channels": [{"username": "ok_chan"}]}')
        s, tg, ch = load_config(p)
        eq(ch[0].label, "ok_chan", "label default = username")
        eq(ch[0].forward_mode, "all", "forward_mode default")
        eq(s["poll_interval_seconds"], DEFAULT_SETTINGS["poll_interval_seconds"], "default merge")
        eq(tg, {}, "telegram kosong aman")

        p.write_text('{"channels": [{"username": "bad name!"}]}')
        try:
            load_config(p)
            raise AssertionError("username invalid harusnya ConfigError")
        except ConfigError:
            pass

        p.write_text('{"channels": [{"username": "ok", "forward_mode": "kadang"}]}')
        try:
            load_config(p)
            raise AssertionError("forward_mode invalid harusnya ConfigError")
        except ConfigError:
            pass

        p.write_text('{"channels": []}')
        try:
            load_config(p)
            raise AssertionError("channels kosong harusnya ConfigError")
        except ConfigError:
            pass

        p.write_text('{"channels": [{"username": "@Dengan_At"}, {"username": "OFF", "enabled": false}]}')
        _s, _tg, ch = load_config(p)
        eq([c.username for c in ch], ["Dengan_At"], "@ dilepas + enabled=false disaring")


# ------------------------------------------------------------------ watcher
def t_watcher_first_run_guard_and_new_post():
    """Inti acceptance §13.4 & §8.2: channel baru tidak banjir, post baru terkirim."""
    with tempfile.TemporaryDirectory(prefix="hermes-verify-") as d:
        db = Path(d) / "w.db"
        conn = store.connect(db)
        store.init_schema(conn)
        store.sync_channels(conn, [Channel(username="testchan", label="Test Chan")])
        conn.close()

        ch = Channel(username="testchan", label="Test Chan")
        settings = dict(DEFAULT_SETTINGS, send_delay_seconds=0, max_send_per_cycle=20)
        sent_labels: list[str] = []

        class FakeSender:
            dry_run = False
            def send_post(self, row, label):
                sent_labels.append(label)
                return "ok", None

        # fetch_channel di-monkeypatch: offline, halaman terkendali
        page = {"html": PAGE}
        real_fetch = fetcher.fetch_channel
        fetcher.fetch_channel = lambda *a, **k: fetcher.FetchResult(html=page["html"], http_status=200)
        try:
            w = watcher.Watcher(d, Path(d) / "c.json", db, FakeSender())

            # cycle 1: channel baru -> semua disimpan, TIDAK dikirim
            r1 = w.run_once(settings, {}, [ch], mode="run", cycle=1)
            eq(r1["skipped"], 4, "guard run pertama menandai 4 post")
            eq(r1["send"]["sent"], 0, "tidak ada yang dikirim saat run pertama")
            eq(sent_labels, [], "sender tidak dipanggil")

            # cycle 2: tidak ada post baru -> tidak ada kiriman (idempotent)
            r2 = w.run_once(settings, {}, [ch], mode="run", cycle=2)
            eq(r2["inserted"], 0, "cycle kedua 0 post baru")
            eq(r2["send"]["sent"], 0, "cycle kedua 0 kiriman")

            # cycle 3: channel dapat post baru -> HARUS terkirim
            page["html"] = PAGE + block(104, "2026-09-20T06:32:00+00:00",
                                        body=f"Fresh {CA}")
            r3 = w.run_once(settings, {}, [ch], mode="run", cycle=3)
            eq(r3["inserted"], 1, "1 post baru terdeteksi")
            eq(r3["skipped"], 0, "post baru TIDAK ditandai skipped")
            eq(r3["send"]["sent"], 1, "post baru terkirim")
            eq(sent_labels, ["Test Chan"], "label dari config (bukan username Telegram)")

            # cycle 4: idempotensi setelah kirim
            r4 = w.run_once(settings, {}, [ch], mode="run", cycle=4)
            eq(r4["send"]["sent"], 0, "tidak ada duplikat")
        finally:
            fetcher.fetch_channel = real_fetch


def t_watcher_not_public_and_fail_tracking():
    with tempfile.TemporaryDirectory(prefix="hermes-verify-") as d:
        db = Path(d) / "w2.db"
        store.init_schema(store.connect(db))
        ch = Channel(username="privat", label="Privat")
        real = fetcher.fetch_channel
        fetcher.fetch_channel = lambda *a, **k: fetcher.FetchResult(
            html="<html>landing</html>", http_status=200)
        try:
            r = watcher.process_channel(ch, dict(DEFAULT_SETTINGS), db, 1, "run")
            truthy(r.error and "publik" in r.error, "channel privat tercatat error")
            conn = store.connect(db)
            eq(store.get_state(conn, "privat")["consecutive_failures"], 1, "failure terhitung")
            conn.close()
        finally:
            fetcher.fetch_channel = real


def t_watcher_backoff_status():
    with tempfile.TemporaryDirectory(prefix="hermes-verify-") as d:
        db = Path(d) / "w3.db"
        store.init_schema(store.connect(db))
        ch = Channel(username="rl", label="RL")
        real = fetcher.fetch_channel
        fetcher.fetch_channel = lambda *a, **k: fetcher.FetchResult(
            error="HTTP 429 rate limited", http_status=429, retry_after=30)
        try:
            w = watcher.Watcher(d, Path(d) / "c.json", db)
            out = w.run_once(dict(DEFAULT_SETTINGS), {}, [ch])
            truthy(out["rate_limited"], "429 diteruskan ke scheduler (backoff)")
            eq(out["send"]["sent"], 0, "tidak ada kiriman saat 429")
        finally:
            fetcher.fetch_channel = real


def t_watcher_failing_channel_skipped():
    """§8.6: setelah 5 gagal, channel dicoba tiap 10 cycle."""
    with tempfile.TemporaryDirectory(prefix="hermes-verify-") as d:
        db = Path(d) / "w4.db"
        conn = store.connect(db)
        store.init_schema(conn)
        store.sync_channels(conn, [Channel(username="dead", label="Dead")])
        for _ in range(watcher.FAIL_THRESHOLD):
            store.record_failure(conn, "dead", "HTTP 404")
        conn.close()

        called = {"n": 0}
        real = fetcher.fetch_channel

        def counting(*a, **k):
            called["n"] += 1
            return fetcher.FetchResult(html=PAGE, http_status=200)

        fetcher.fetch_channel = counting
        try:
            r = watcher.process_channel(Channel(username="dead", label="Dead"),
                                        dict(DEFAULT_SETTINGS), db, 3, "run")
            truthy(r.skipped_cycle, "cycle 3 -> ditunda")
            eq(called["n"], 0, "tidak ada request saat ditunda")
            r = watcher.process_channel(Channel(username="dead", label="Dead"),
                                        dict(DEFAULT_SETTINGS), db, 10, "run")
            eq(called["n"], 1, "dicoba lagi di cycle ke-10")
        finally:
            fetcher.fetch_channel = real


def t_lock_file():
    with tempfile.TemporaryDirectory(prefix="hermes-verify-") as d:
        lock = Path(d) / ".pid"
        watcher.acquire_lock(lock)
        eq(lock.read_text().strip(), str(__import__("os").getpid()), "pid tertulis")
        try:
            watcher.acquire_lock(lock)
            raise AssertionError("instance kedua harusnya ditolak")
        except RuntimeError:
            pass
        lock.write_text("999999999")          # pid basi (tidak ada)
        watcher.acquire_lock(lock)            # harus diambil alih, tidak raise
        lock.unlink()


# ---------------------------------------------------------------------- main
TESTS = [
    ("parser: deteksi channel publik", t_parser_public),
    ("parser: parse blok post + urutan msg_id", t_parser_posts),
    ("parser: bersihkan junk UI & emoji", t_parser_clean_text),
    ("parser: media_url (foto asli vs emoji)", t_parser_media),
    ("parser: URL protocol-relative", t_parser_protocol_relative),
    ("sender: format waktu WITA", t_time_wita),
    ("sender: truncate + penanda", t_truncate),
    ("sender: format pesan §8.4 + footer", t_format_post),
    ("sender: footer off & text None", t_format_post_no_footer),
    ("sender: display_ca", t_format_post_display_ca),
    ("sender: 429 retry sekali lalu sukses", t_api_429_retry),
    ("sender: 429 menyerah + error tercatat", t_api_429_give_up),
    ("sender: HTTP 200 ok:false = gagal", t_api_ok_false_http200),
    ("sender: 403 -> BotBlocked, 401 -> TokenInvalid", t_api_403_and_401),
    ("sender: dry-run tidak menyentuh network", t_verify_and_dry_run),
    ("store: dedup PK + siklus pending/notified", t_store_dedup_and_pending),
    ("config: validasi + default + normalisasi", t_config_validation),
    ("watcher: guard run pertama + post baru terkirim", t_watcher_first_run_guard_and_new_post),
    ("watcher: channel privat -> error, tidak crash", t_watcher_not_public_and_fail_tracking),
    ("watcher: 429 -> sinyal backoff", t_watcher_backoff_status),
    ("watcher: channel bermasalah ditunda 10 cycle", t_watcher_failing_channel_skipped),
    ("watcher: lock file + pid basi", t_lock_file),
]

for name, fn in TESTS:
    check(name, fn)

ok = sum(1 for r in RESULTS if r[0])
print()
for passed, name, err in RESULTS:
    print(f"  {'PASS' if passed else 'FAIL'}  {name}" + (f"\n        {err}" if err else ""))
print(f"\n{ok}/{len(RESULTS)} check lolos")
sys.exit(0 if ok == len(RESULTS) else 1)
