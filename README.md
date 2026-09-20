# TG Channel Tracker

Pantau channel Telegram **publik** dan forward **setiap post baru** ke satu chat
Telegram kamu. Tanpa Telethon, tanpa userbot, tanpa API key — sumbernya halaman
preview publik `https://t.me/s/<username>`, jadi tidak ada akun yang bisa kena ban.

Implementasi dari `PRD.md`.

## Cara kerja

```
channels.json ──┐ (dibaca ulang tiap cycle = hot reload)
                ▼
   Phase 1 · paralel (ThreadPoolExecutor)
     GET https://t.me/s/<username> → parse HTML → INSERT OR IGNORE (SQLite)
                │  tidak ada pengiriman di sini
                ▼
   Phase 2 · berurutan (single thread)
     post dengan notified_at IS NULL → kirim Bot API → tandai
     (sekaligus jadi recovery: yang gagal otomatis ikut cycle berikutnya)
```

Dua phase dipisah supaya pesan tidak dikirim berlomba-lomba antar thread,
urutannya stabil, dan kalau proses mati di tengah tidak ada post terkirim dua kali.

## Install

```bash
cd /home/ubuntu/TelegramTracker
python3 -m venv .venv
.venv/bin/pip install -e .
cp .env.example .env      # lalu isi token & chat id
```

Bot token dari [@BotFather](https://t.me/BotFather) (`/newbot`). Setelah token
diisi, **tekan Start** di chat bot-nya, lalu ambil chat id:

```bash
curl -s "https://api.telegram.org/bot<TOKEN>/getUpdates" | grep -o '"chat":{"id":[0-9-]*'
```

## Pakai

```bash
.venv/bin/python -m tracker check soltrending   # cek channel publik/nggak + preview 3 post
.venv/bin/python -m tracker backfill           # tarik history, TIDAK kirim notifikasi
.venv/bin/python -m tracker test-send          # pastikan bot bisa kirim ke kamu
.venv/bin/python -m tracker stats              # ringkasan semua channel
.venv/bin/python -m tracker run                # service (loop)
.venv/bin/python -m tracker run --once         # satu cycle lalu keluar (cocok buat cron)
.venv/bin/python -m tracker run --dry-run      # jalan tanpa kirim apa pun
.venv/bin/python -m tracker backfill soltrending   # backfill 1 channel saja
```

Urutan setup yang benar: `check` → `backfill` → `test-send` → `run`.
**Jangan `run` sebelum `backfill`** kalau channel-nya sudah lama ada — guard run
pertama akan menyelamatkan kamu (lihat bawah), tapi `backfill` eksplisit lebih jelas.

## Service (PM2)

```bash
cd /home/ubuntu/TelegramTracker
pm2 start ecosystem.config.js
pm2 logs tg-channel-tracker
pm2 save                       # WAJIB, biar balik sendiri setelah reboot
```

Token tidak ditulis di `ecosystem.config.js` — dibaca dari `.env` oleh `config.py`,
jadi file PM2 aman di-commit.

## Nambah / ubah channel

Edit `channels.json`, langsung kepakai di cycle berikutnya — **tidak perlu restart**.

```json
{ "username": "soltrending", "label": "SOL Trending",
  "tags": ["alpha"], "forward_mode": "all", "enabled": true }
```

`username` = yang muncul di URL `t.me/<username>`, tanpa `@`.
`enabled: false` = dilewati tanpa dihapus dari list.

Cek dulu publik atau nggak: `.venv/bin/python -m tracker check <username>`.

## Setting

| Key | Default | Fungsi |
|-----|---------|--------|
| `poll_interval_seconds` | 45 | jeda antar cycle |
| `max_workers` | 5 | channel yang di-poll paralel |
| `request_timeout_seconds` | 20 | timeout request |
| `notify_on_first_run` | false | `true` = kirim juga 20 post lama saat channel baru didaftarkan |
| `include_media` | true | kirim foto post via `sendPhoto` |
| `max_message_length` | 3800 | batas panjang pesan |
| `max_caption_length` | 1024 | batas caption Telegram (foto) |
| `max_send_per_cycle` | 20 | batas kirim per cycle (biar tidak membanjiri chat) |
| `send_delay_seconds` | 1.0 | jeda antar pesan (±1 pesan/detik per chat itu batas aman Bot API) |
| `credit_footer` | true | footer kredit di tiap pesan |
| `display_ca` | false | `true` = tampilkan contract address yang terdeteksi |
| `default_forward_mode` | all | `all` = semua post, `ca_only` = cuma yang ada CA |

`forward_mode` bisa di-set per channel. Sekarang semua `all` sesuai permintaan.

## Perilaku penting

**Channel baru tidak membanjiri chat (§8.3).** Kalau `last_msg_id` masih 0,
20 post yang ada disimpan tapi ditandai `notified_at` (dianggap sudah terkirim) —
notifikasi mulai dari post berikutnya. Kalau memang mau lihat isi channel dulu,
set `notify_on_first_run: true`.

**Tidak ada post terkirim dua kali.** Dedup di level DB: PK `(username, msg_id)`.
`last_msg_id` selalu nilai tertinggi yang pernah terlihat, jadi post lama yang
muncul kembali (misal karena post terbaru dihapus) tidak dikirim ulang.

**Recovery otomatis.** Post yang gagal terkirim tetap `notified_at IS NULL` dan
ikut terkirim di cycle berikutnya, dibatasi `max_send_per_cycle` per cycle.

**Channel bermasalah tidak dibuang.** 5 gagal beruntun → dicoba lagi tiap 10 cycle.

**Lock file.** Instance kedua langsung exit (`.pid`). Kalau proses mati kasar,
lock basi otomatis diambil alih.

## Yang sudah diverifikasi

Verifikasi offline yang bisa diulang: `tests/verify_offline.py` (22 check, tanpa
network/token). Jalankan dengan `.venv/bin/python tests/verify_offline.py`.

| Uji | Hasil |
|-----|-------|
| Parser 12 channel live | ✅ 233 post terbaca, 0 error, key = username config |
| Timestamp WITA | ✅ `20 Sep 2026, 14:54 WITA` |
| Foto post (bukan emoji) | ✅ `telesco.pe` terdeteksi, emoji ditolak |
| Sampah UI dibuang | ✅ `VIEW IN TELEGRAM` / `Please open Telegram` hilang |
| Backfill 12 channel | ✅ 233 post disimpan, 0 notifikasi |
| Cycle kedua idempotent | ✅ 0 post baru, 0 kirim |
| Guard channel baru | ✅ 20 post disimpan tanpa dikirim |
| Recovery / antrean | ✅ `notified_at IS NULL` terkirim cycle berikutnya |
| Hot reload channels.json | ✅ 12 → 11 channel tanpa restart |
| Lock file | ✅ instance kedua ditolak, pid basi diambil alih |
| Channel tidak publik | ✅ tercatat error, service tetap jalan |
| 429 Bot API | ✅ retry `retry_after`+1s, lalu gagal dengan error tercatat |
| `HTTP 200 + {"ok":false}` | ✅ dianggap gagal (bug prototipe lama) |
| 403 / 401 | ✅ stop kirim / fail fast, bukan retry terus |
| Kirim nyata ke Telegram | ✅ `send_log` status `ok`, 0 gagal |
| Service PM2 + autostart boot | ✅ `pm2 save` sudah, dump memuat tracker |

## Batasan

- **Cuma 20 post terakhir per channel.** Channel yang posting >20 kali dalam satu
  interval kehilangan sebagian post. Solusi: turunkan `poll_interval_seconds`.
  Untuk `soltrending` (±5 post/menit) pakai 30 detik.
- **Bukan real-time.** Jeda maksimal sebesar interval.
- **Bukan native forward.** Bot menyalin isi post, tidak ada label "Forwarded from".
- **Media cuma gambar preview.** Bukan video/file. URL `telesco.pe` itu signed dan
  bisa kedaluwarsa. Telegram sering gagal mengambil URL itu langsung, jadi bot
  **mengunduh gambar lalu upload sendiri** (`sendPhoto` multipart); kalau unduhan
  gagal, baru fallback ke pesan teks.
- **Channel privat/grup tidak bisa.** Cuma channel publik.
- **CA bisa false positive.** Regex Solana 32–44 char base58 kadang nangkep potongan
  link. Di v1 CA disimpan di DB, tidak ditampilkan (`display_ca: false`).
- **Volume.** `soltrending` bisa ±5 post/menit (±7.700/hari). Kalau terlalu ramai,
  set `forward_mode: "ca_only"` untuk channel itu.

## Struktur

```
TelegramTracker/
├── PRD-AUDIT.md          audit PRD vs prototipe lama
├── channels.json         daftar channel + setting (aman di-commit)
├── .env                  TG_BOT_TOKEN, TG_CHAT_ID (git-ignored)
├── .env.example
├── pyproject.toml
├── ecosystem.config.js   PM2
├── src/tracker/
│   ├── __main__.py       CLI (run/backfill/stats/test-send/check)
│   ├── config.py         loader channels.json + .env, logging
│   ├── models.py         dataclass: Channel/Post/FetchResult/...
│   ├── fetcher.py        HTTP GET t.me/s/
│   ├── parser.py         HTML → Post (+ media/emoji/CA cleanup)
│   ├── store.py          SQLite: schema, dedup, antrean, state
│   ├── sender.py         Bot API: format §8.4, sendPhoto (unduh+upload), retry
│   └── watcher.py        loop, backfill guard, recovery, lock
├── tests/verify_offline.py   verifikasi offline (22 check, tanpa token)
├── tools/probe_candidates.py cek publik/aktif kandidat channel
├── data/tracker.db       (git-ignored)
└── logs/app.log          (git-ignored)
```

## Troubleshooting

```bash
tail -f logs/app.log                          # log aplikasi
pm2 logs tg-channel-tracker                   # log proses PM2
.venv/bin/python -m tracker stats             # status + kegagalan kirim terakhir
sqlite3 data/tracker.db "SELECT * FROM send_log ORDER BY id DESC LIMIT 10"
```

| Gejala | Penyebab |
|--------|----------|
| `token/chat id belum diisi` | `.env` belum diisi |
| `tidak_publik` di `check` | channel privat, username salah, atau tidak ada post |
| `chat not found` di `stats` | `TG_CHAT_ID` salah (paste angka mentah, tanpa tanda kutip) |
| `403` di log | kamu memblokir bot, atau bot dikeluarkan dari chat |
| `instance lain masih jalan` | masih ada proses lain; cek `cat .pid`, `ps -p <pid>` |
