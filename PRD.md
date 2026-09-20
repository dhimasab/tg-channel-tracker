# PRD — Telegram Channel Tracker Bot

**Versi:** 1.0
**Tanggal:** 20 September 2026
**Owner:** Dhimas Aura Bhagastama (@PejuangCryptoID)
**Status:** Draft untuk implementasi

---

## 1. Ringkasan

Sebuah service yang memantau daftar **channel Telegram publik** dan mengirimkan
setiap postingan baru ke chat pribadi pemilik lewat **Telegram Bot**.

Service berjalan tanpa akun Telegram (tanpa Telethon, tanpa userbot). Sumber
data diambil dari halaman web preview publik Telegram: `https://t.me/s/<username>`.

**Kenapa tanpa akun:** userbot MTProto bisa kena ban, perlu warm-up, dan perlu
proxy. Preview publik tidak butuh login sama sekali, jadi bebas risiko ban.

---

## 2. Masalah yang diselesaikan

Pemilik memantau belasan channel crypto (alpha channel, news, on-chain alert).
Sekarang harus buka satu-satu secara manual. Postingan penting sering lewat
karena timing-nya tidak terduga.

Yang dibutuhkan: satu kanal masuk saja — semua postingan dari channel yang
diikuti, masuk otomatis ke satu chat Telegram.

---

## 3. Goals

1. Deteksi postingan baru dari daftar channel publik dengan jeda maksimal 60 detik.
2. Kirim postingan tersebut ke chat Telegram pemilik lewat bot.
3. Tidak ada postingan yang terkirim dua kali (dedup).
4. Daftar channel bisa diedit tanpa restart service.
5. Jalan sebagai service background yang auto-restart.

## 4. Non-Goals (versi 1)

- Channel privat / grup tertutup → tidak didukung, tidak bisa secara teknis
- Real-time push (harus polling; jeda 30–60 detik wajar)
- Parsing contract address & enrichment harga
- Multi-user / bot publik untuk orang lain
- Web dashboard
- Filter kata kunci / sentiment analysis

---

## 5. Arsitektur

```
┌──────────────────┐
│ channels.json    │  daftar channel + setting
└────────┬─────────┘
         │ dibaca tiap cycle
         ▼
┌──────────────────────────────┐
│ Poller (ThreadPool, N worker)│
│  GET https://t.me/s/<user>   │
└────────┬─────────────────────┘
         │ HTML
         ▼
┌──────────────────────────────┐
│ Parser                       │
│  split per <div tgme_widget…>│
│  → msg_id, datetime, text,   │
│    links, media_url          │
└────────┬─────────────────────┘
         │ list of post
         ▼
┌──────────────────────────────┐
│ Deduplikator                 │
│  bandingkan vs last_msg_id   │
│  INSERT OR IGNORE ke SQLite  │
└────────┬─────────────────────┘
         │ post yang benar-benar baru
         ▼
┌──────────────────────────────┐
│ Sender                       │
│  Bot API sendMessage/sendPhoto│
│  → chat pemilik              │
└──────────────────────────────┘
```

**Komponen:**

| Komponen | Tanggung jawab |
|----------|----------------|
| Config loader | Baca `channels.json` tiap cycle (hot reload) |
| Poller | HTTP GET paralel ke semua channel aktif |
| Parser | Ekstrak field dari HTML preview |
| State store | SQLite: post, state per channel, log kirim |
| Sender | Kirim ke Bot API, handle rate limit |
| Scheduler | Loop utama, jaga interval & backoff |
| Logger | Log ke file + stdout |

---

## 6. Data Model

### 6.1 `channels.json` — daftar channel & konfigurasi

Dipilih JSON supaya bisa diedit manual tanpa tool DB, dan bisa di-commit ke git.

```json
{
  "settings": {
    "poll_interval_seconds": 45,
    "max_workers": 5,
    "request_timeout_seconds": 20,
    "notify_on_first_run": false,
    "include_media": true,
    "max_message_length": 3800,
    "credit_footer": false
  },
  "telegram": {
    "bot_token_env": "TG_BOT_TOKEN",
    "chat_id_env": "TG_CHAT_ID"
  },
  "channels": [
    {
      "username": "solana_newpairs",
      "label": "Solana NewPairs",
      "enabled": true,
      "tags": ["alpha", "solana"]
    },
    {
      "username": "cointelegraph",
      "label": "Cointelegraph",
      "enabled": true,
      "tags": ["news"]
    }
  ]
}
```

**Catatan field:**

- `username` — yang muncul di URL `t.me/<username>`, tanpa `@`
- `label` — nama tampilan di pesan notifikasi
- `enabled` — `false` = dilewati tanpa dihapus dari list
- `notify_on_first_run` — lihat §8.3, ini penting biar ga spam di run pertama
- `include_media` — kirim gambar preview kalau postingan punya gambar
- `credit_footer` — kalau `true`, tambahkan footer kredit di tiap pesan

**Token bot TIDAK disimpan di file ini.** Ambil dari environment variable
(`bot_token_env`). File config aman di-commit ke git.

### 6.2 SQLite — `data/tracker.db`

```sql
-- Daftar channel (mirror dari channels.json, buat historis & query)
CREATE TABLE IF NOT EXISTS channels (
    username   TEXT PRIMARY KEY,
    label      TEXT,
    enabled    INTEGER DEFAULT 1,
    added_at   TEXT NOT NULL
);

-- Semua postingan yang pernah terdeteksi
CREATE TABLE IF NOT EXISTS posts (
    username    TEXT    NOT NULL,
    msg_id      INTEGER NOT NULL,
    posted_at   TEXT,              -- ISO8601 UTC, dari <time datetime>
    text        TEXT,              -- isi postingan (plain text)
    links       TEXT,              -- JSON array of URL
    media_url   TEXT,              -- URL gambar preview, NULL kalau ga ada
    seen_at     TEXT NOT NULL,     -- kapan kita deteksi
    notified_at TEXT,              -- NULL = belum terkirim ke Telegram
    PRIMARY KEY (username, msg_id)
);

-- State per channel: posisi terakhir + kesehatan
CREATE TABLE IF NOT EXISTS channel_state (
    username             TEXT PRIMARY KEY,
    last_msg_id          INTEGER NOT NULL DEFAULT 0,
    last_poll_at         TEXT,
    last_success_at      TEXT,
    last_error           TEXT,
    consecutive_failures INTEGER NOT NULL DEFAULT 0,
    total_posts          INTEGER NOT NULL DEFAULT 0,
    total_sent           INTEGER NOT NULL DEFAULT 0
);

-- Audit pengiriman
CREATE TABLE IF NOT EXISTS send_log (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT,
    msg_id   INTEGER,
    sent_at  TEXT,
    status   TEXT,      -- 'ok' | 'failed' | 'skipped'
    error    TEXT
);

CREATE INDEX IF NOT EXISTS idx_posts_pending ON posts(notified_at);
CREATE INDEX IF NOT EXISTS idx_posts_posted  ON posts(posted_at);
```

**Aturan kunci:**

- Primary key `(username, msg_id)` bikin dedup otomatis. `INSERT OR IGNORE`
  yang duplikat tinggal di-skip, tidak perlu cek manual.
- `last_msg_id` adalah satu-satunya sumber kebenaran untuk "post baru".
  **Jangan pakai timestamp** — dua post bisa punya detik yang sama, sementara
  `msg_id` selalu naik dan unik per channel.
- `notified_at` NULL = belum dikirim. Ini safety net: kalau bot mati pas mau
  kirim, post-nya di-queue dan dikirim di cycle berikutnya lewat recovery job.

---

## 7. Parsing HTML

### 7.1 Struktur yang dipakai

Telegram preview HTML punya pola stabil:

```
<div class="tgme_widget_message" data-post="solana_newpairs/185570">
  <div class="tgme_widget_message_bubble">
    ...
    <div class="tgme_widget_message_text">ISI POSTINGAN</div>
    <a class="tgme_widget_message_photo_wrap" href="...">
      <div class="tgme_widget_message_photo" style="background-image:url('...')">
    </a>
    <time datetime="2026-09-20T06:28:44+00:00">
```

### 7.2 Rumus ekstraksi

```python
import re, html

# 1. Pecah HTML jadi blok per postingan
blocks = re.split(r'(?=<div class="tgme_widget_message[ "])', page_html)

for b in blocks:
    # 2. ID postingan
    m = re.search(r'data-post="([^/"]+)/(\d+)"', b)
    if not m:
        continue
    username, msg_id = m.group(1), int(m.group(2))

    # 3. Timestamp
    t = re.search(r'<time datetime="([^"]+)"', b)
    posted_at = t.group(1) if t else None

    # 4. Teks
    body = re.search(
        r'<div class="tgme_widget_message_text[^"]*"[^>]*>(.*?)</div>\s*'
        r'(?:<div class="tgme_widget_message_(?:reply_markup|footer|service))',
        b, re.S)
    text = ""
    if body:
        text = re.sub(r"<br\s*/?>", "\n", body.group(1))
        text = re.sub(r"<[^>]+>", " ", text)
        text = html.unescape(re.sub(r"[ \t]+", " ", text)).strip()

    # 5. Link di dalam postingan
    hrefs = re.findall(r'href="(https?://[^"]+)"', b)
    links = [h for h in dict.fromkeys(hrefs)
             if not any(s in h for s in
                        ("t.me/", "telegram.org", "cdn-telegram", "telesco.pe"))]

    # 6. Gambar (kalau ada)
    photo = re.search(
        r'background-image:url\(\'([^\']+)\'\)', b)
    media_url = photo.group(1) if photo else None
```

### 7.3 Validasi halaman

Sebelum parsing, cek apakah channel-nya publik:

```python
if "tgme_channel_info" not in page_html:
    # Channel privat, username salah, atau tidak ada post
    raise ChannelUnavailable(username)
```

---

## 8. Perilaku Sistem

### 8.1 Loop utama

```
selamanya:
    config  = baca channels.json          # hot reload
    channels = filter(config.channels, enabled == true)

    for channel in channels (paralel, max_workers):
        html    = GET t.me/s/<username>
        posts   = parse(html)
        baru    = [p for p in posts if p.msg_id > state[username].last_msg_id]
        simpan(baru)                       # INSERT OR IGNORE
        untuk tiap post baru:
            kirim_ke_telegram(post)
        update state[username].last_msg_id = max(msg_id di posts)

    tunggu(poll_interval - durasi_cycle + jitter)
```

**Jitter:** tambahkan `random.uniform(0, 3)` detik. Tujuannya biar pola request
ga terlalu teratur, mengurangi risiko kena rate limit.

### 8.2 Deteksi post baru

```
post_baru  ⟺  msg_id > channel_state.last_msg_id
```

`last_msg_id` di-update ke nilai **tertinggi yang pernah terlihat**, bukan ke
`max(post_baru)`. Kenapa: kalau channel menghapus post terbarunya, nilai
tertinggi tetap kepegang, jadi post lama yang muncul lagi tidak dikirim ulang.

### 8.3 Run pertama (backfill) — WAJIB

Kalau `last_msg_id` masih 0, artinya channel baru didaftarkan. Yang terjadi:

- Kalau `notify_on_first_run: false` (**default, direkomendasikan**)
  → simpan 20 post yang ada, set `notified_at` supaya dianggap sudah dikirim,
    **tidak ada notifikasi**. Notifikasi mulai dari post berikutnya.
- Kalau `notify_on_first_run: true`
  → 20 post itu langsung dikirim semua. Cuma cocok kalau kamu memang mau
    lihat isi channel-nya dulu.

Tanpa mekanisme ini, tiap kali nambah channel kamu kena spam 20 pesan.

### 8.4 Format pesan notifikasi

**Postingan teks:**

```
📢 Solana NewPairs
🕐 20 Sep 2026, 14:28 WITA

Bread (Bread)
CA: 5RYLnfAg3WvpkjdbAgXMeHrcejicWLx8WU2HBWiQQh81
🏦 Market Cap: $2.35K
💸 Liquidity: $2.35K

🔗 https://t.me/solana_newpairs/185570
```

**Postingan dengan gambar** (`include_media: true`):

Kirim via `sendPhoto`, `media_url` jadi `photo`, caption-nya pesan di atas
(dipotong ke 1024 karakter — batas caption Telegram).

**Aturan:**

- Kalau `text` lebih panjang dari `max_message_length` (default 3800):
  potong, tambahkan `…[dipotong]`.
- `disable_web_page_preview: false` supaya link tampil dengan preview.
- Kalau `credit_footer: true`, tambahkan di akhir:
  ```
  ———
  🔧 Bot by @dhimedia
  💙 Kalau berguna, traktir kopi kreator dong: teer.id/PejuangCryptoID
  ```

### 8.5 Recovery pengiriman yang gagal

Kirim pesan bisa gagal (network, 429, bot di-block). Karena `notified_at`
disimpan di DB, post yang gagal otomatis ter-queue.

Tiap awal cycle, jalankan recovery:

```sql
SELECT username, msg_id FROM posts
WHERE notified_at IS NULL
ORDER BY posted_at ASC
LIMIT 20
```

Kirim ulang, update `notified_at` kalau sukses. Batasi jumlahnya biar ga
membanjiri chat setelah bot lama mati.

### 8.6 Rate limit & error handling

**Telegram Bot API:**

- 429 → baca header `retry_after`, tunggu selama itu + 1 detik, retry sekali
- Bot di-block (403) → log error, hentikan pengiriman, jangan retry terus
- Error lain → retry 3x dengan backoff 2s / 5s / 10s

**Scraping:**

- HTTP 429 dari Telegram → naikkan interval cycle berikutnya (backoff), maksimal 5 menit
- HTTP 404 / tidak ada `tgme_channel_info` → catat di `last_error`, jangan spam log
- Timeout → masuk hitungan `consecutive_failures`
- `consecutive_failures >= 5` → tandai channel sebagai bermasalah, log peringatan,
  tetap dicoba tiap 10 cycle (jangan buang, mungkin cuma gangguan sementara)

---

## 9. Antarmuka

### 9.1 Command line

```bash
python -m tracker run                  # jalan sebagai service (loop)
python -m tracker run --once           # satu cycle, lalu keluar (buat cron)
python -m tracker backfill             # tarik history, tidak kirim notifikasi
python -m tracker stats                # ringkasan: channel, jumlah post, error
python -m tracker test-send            # kirim 1 pesan tes ke chat kamu
python -m tracker check <username>     # cek channel publik/nggak + preview 3 post
```

`stats` minimal menampilkan: nama channel, jumlah post, jumlah terkirim,
poll terakhir, status/error terakhir.

### 9.2 Command bot (opsional, v2)

Kalau mau kontrol dari Telegram:

```
/list          — daftar channel aktif + jumlah post
/add <user>    — tambah channel
/remove <user> — hapus channel
/mute <user>   — nonaktifkan sementara
/stats         — status service
```

Butuh handler `getUpdates` / webhook. Jangan masuk scope v1.

---

## 10. Tech Stack

| Bagian | Pilihan | Alasan |
|--------|---------|--------|
| Bahasa | Python 3.11+ | Ekosistem scraping paling matang |
| HTTP | `requests` | Cukup, simpel |
| Parser | `re` + `html` (stdlib) | Tidak perlu BeautifulSoup, HTML-nya stabil |
| Storage | SQLite (`sqlite3` stdlib) | Zero-config, satu file, SQL penuh |
| Config | JSON | Mudah diedit & di-commit |
| Kirim pesan | Telegram Bot API via HTTP | Tanpa dependency library |
| Paralel | `concurrent.futures.ThreadPoolExecutor` | I/O bound, cukup |
| Service | PM2 atau systemd | Auto-restart |

**Dependencies:** `requests` saja. Sisanya stdlib.

---

## 11. Struktur Project

```
tg-channel-tracker/
├── README.md
├── requirements.txt
├── .env.example              # TG_BOT_TOKEN, TG_CHAT_ID
├── .gitignore                # .env, data/, logs/, __pycache__
├── channels.json             # daftar channel + setting
├── src/
│   └── tracker/
│       ├── __init__.py
│       ├── __main__.py       # entry point CLI
│       ├── config.py         # load & validasi channels.json
│       ├── fetcher.py        # HTTP GET ke t.me/s/
│       ├── parser.py         # HTML → objek Post
│       ├── store.py          # SQLite: schema, insert, query, state
│       ├── sender.py         # Bot API: sendMessage/sendPhoto + retry
│       ├── watcher.py        # loop utama, orkestrasi
│       └── models.py         # dataclass Post, Channel
├── data/                     # tracker.db (git-ignored)
├── logs/                     # app.log (git-ignored)
└── ecosystem.config.js       # PM2
```

---

## 12. Milestones

| # | Deliverable | Kriteria selesai |
|---|-------------|------------------|
| M1 | Skeleton + config loader | `channels.json` kebaca, validasi jalan |
| M2 | Fetcher + parser | Bisa cetak `msg_id`, `text`, `posted_at` dari 1 channel |
| M3 | Storage | Schema kebuat, `INSERT OR IGNORE` dedup jalan |
| M4 | New-post detection | Cycle kedua tidak menghasilkan post "baru" palsu |
| M5 | Sender | Pesan nyampe ke chat pribadi, format sesuai §8.4 |
| M6 | Backfill safety | Channel baru didaftarkan → tidak ada banjir notifikasi |
| M7 | Paralel + multi-channel | 10+ channel, 1 cycle selesai < 5 detik |
| M8 | Resilience | 429 → backoff; channel privat → tidak crash; recovery gagal kirim |
| M9 | Service | Auto-start saat boot, auto-restart saat crash |
| M10 | Dokumentasi | README cukup buat setup dari nol |

---

## 13. Acceptance Criteria

1. Daftarkan 3 channel publik. Dalam 2 menit, semua post terbaru masuk ke chat.
2. Post ke-21 di channel (yang membuat post ke-1 keluar dari preview) tidak
   menghasilkan duplikat.
3. Hapus satu post dari channel, post lama yang muncul kembali tidak dikirim ulang.
4. Nambah channel baru → 20 post lamanya **tidak** dikirim, cuma yang baru.
5. Matikan service 5 menit sementara channel aktif posting. Nyalakan lagi →
   semua post yang terlewat terkirim (maksimal 20 per channel, dibatasi preview).
6. Ubah `channels.json` (tambah channel) saat service jalan → kepakai di cycle
   berikutnya tanpa restart.
7. Channel privat di daftar → dicatat sebagai error, service tetap jalan.
8. Service crash → restart otomatis dalam 10 detik.
9. Setelah 24 jam jalan: tidak ada post yang terkirim dua kali.
10. `python -m tracker stats` menampilkan kondisi semua channel dengan benar.

---

## 14. Edge Cases & Penanganan

| Kasus | Perilaku yang diharapkan |
|-------|--------------------------|
| Channel privat / username salah | Catat error, skip, service tetap jalan |
| Channel posting >20x dalam 1 interval | Sebagian post terlewat — turunkan interval, atau catat sebagai limitasi |
| Post diedit setelah terkirim | v1: tidak ada notifikasi edit. v2: bandingkan hash teks |
| Post dihapus | Tidak ada aksi. `last_msg_id` tidak mundur |
| Postingan cuma gambar, tanpa teks | Tetap kirim kalau `include_media: true`, caption = label + link |
| Teks > 4096 karakter | Potong di `max_message_length`, tandai terpotong |
| Bot di-block user | Log 403, stop kirim, jangan retry terus |
| Token bot salah | Fail fast saat startup, pesan error jelas |
| `chat_id` salah | Error 400 dari API, tampilkan chat_id yang dipakai |
| Disk penuh | SQLite raise error, service log & exit dengan kode non-zero (biar PM2 alert) |
| Dua instance jalan bersamaan | Lock file `.pid` di startup, instance kedua langsung exit |
| Channel ganti username | Channel lama jadi error; tambahkan username baru manual |
| Telegram ganti struktur HTML | Parser tes masuk M2; kalau semua channel error serentak, itu sinyalnya |

---

## 15. Risiko & Mitigasi

| Risiko | Dampak | Mitigasi |
|--------|--------|----------|
| Telegram hapus / ubah preview publik | Service mati total | Alternatif: RSSHub, atau userbot sebagai fallback |
| Preview cuma 20 post | Kehilangan post di channel super rame | Interval ≤45s; untuk channel paling rame, ≤30s |
| Rate limit 429 | Request gagal | Interval ≥30s + jitter + backoff eksponensial |
| IP datacenter di-block | Semua request gagal | Rotate User-Agent; kalau perlu, proxy |
| False positive deteksi "baru" | Spam notifikasi | Dedup via PK `(username, msg_id)` di level DB, bukan di memori |

---

## 16. Konfigurasi & Deployment

### `.env`

```bash
TG_BOT_TOKEN=123456789:AAA...
TG_CHAT_ID=123456789
```

### Setup bot

1. Chat `@BotFather` → `/newbot` → simpan token
2. Buka chat bot-nya, tekan **Start** (wajib, biar bot boleh kirim ke kamu)
3. Ambil `chat_id`: buka `https://api.telegram.org/bot<TOKEN>/getUpdates`
   setelah kirim pesan ke bot; ambil `result[0].message.chat.id`

### Jalan

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env          # isi token & chat id
python -m tracker check solana_newpairs   # tes dulu 1 channel
python -m tracker test-send               # pastikan bot bisa kirim
python -m tracker run                     # jalan
```

### Service

```bash
pm2 start ecosystem.config.js
pm2 save && pm2 startup
```

---

## 17. Limitasi yang Diakui

1. **Bukan real-time sempurna.** Jeda maksimal sebesar `poll_interval_seconds`.
2. **Cuma 20 post terakhir per channel** yang bisa diakses dari preview.
3. **Cuma channel publik.** Grup dan channel privat tidak bisa.
4. **Bukan native forward.** Bot meng-copy isi postingan; tidak ada label
   "Forwarded from", karena bot bukan anggota channel tersebut.
5. **Media terbatas.** Cuma gambar preview yang bisa diambil, bukan video/file.

---

## 18. Roadmap Setelah v1

- **v1.1** — Ekstraksi contract address (Solana base58 & EVM `0x`) dari teks **dan**
  link, plus blacklist kata umum untuk mengurangi false positive
- **v1.2** — Enrichment harga via Dexscreener API (harga, liquidity, market cap,
  usia pair)
- **v1.3** — Command bot (`/list`, `/add`, `/remove`, `/stats`)
- **v1.4** — Scoring channel: catat harga saat post masuk, bandingkan dengan
  harga sekarang, ranking channel dari performa
- **v1.5** — Deteksi edit & hapus post
- **v2.0** — Filter berbasis kata kunci dan threshold market cap

---

## 19. Referensi Implementasi

Implementasi kerja yang sudah terverifikasi ada di:
`/home/ubuntu/tg_channel_tracker/`

File yang bisa jadi acuan:
- `watcher.py` — poller, parser, dedup, sender, recovery
- `channels.json` — contoh config lengkap
- `probe.py` — cek channel publik/nggak + hitung jumlah CA
- `README.md` — dokumentasi operasional
- `ecosystem.config.js` — config PM2

**Pelajaran penting dari implementasi itu:**

1. `sqlite3` connection **tidak thread-safe**. Kalau pakai ThreadPoolExecutor,
   bikin koneksi baru **di dalam** worker function. Kalau diwarisin dari parent,
   error: `SQLite objects created in a thread can only be used in that same thread`.
   Aktifkan juga `PRAGMA journal_mode=WAL`.
2. Channel alpha sering menaruh CA di `href`, bukan di body text. Body-nya cuma
   nama token + market cap. Scan link juga — di test, hasilnya naik dari 40 ke
   119 CA.
3. Split HTML pakai **lookahead** (`re.split(r'(?=...)')`), bukan `re.findall`.
   Dengan lookahead, tiap blok tetap punya pembuka `<div>`-nya sendiri sehingga
   atribut `data-post` tidak hilang.
4. Banyak channel crypto populer **bukan publik** — tidak muncul di `t.me/s/`.
   Selalu probe dulu.
5. `random.uniform(0, 3)` jitter di akhir cycle membantu menghindari pola
   request yang terlalu teratur.

---

## 20. Lampiran — Channel Hasil Verifikasi (20 Sep 2026)

**Publik & aktif:**

```
solana_newpairs          — alpha Solana, CA di body text
pumpfun_alerts           — alpha, CA di body text
soltrending              — alpha, CA di link
dexscreener_alerts       — alpha, CA di link
cointelegraph            — news
bwenews                  — news
wublockchainenglish      — news
watcherguru              — news
whale_alert_io           — on-chain
spotonchain              — on-chain
binance_announcements    — exchange
coinfestasia             — event
```

**Tidak publik (tidak bisa di-scrape):** `coingecko`, `decryptmedia`,
`beincrypto`, `theblockofficial`, `cryptoslate`, `blockworks`, `gmgnai`,
`dexscreener`, `pepeboost`
