# 🫏 TeleDonkey

A small Telegram bot that lets you push download links (ed2k, magnet, .torrent)
to a remote [mldonkey](https://github.com/ygrek/mldonkey) instance and check on
your downloads — without exposing the web UI.

It runs as its own Docker container, completely independent from the mldonkey
container. It talks to mldonkey over its **telnet console** (port `4002` on the
host in this setup) and to Telegram over normal long-polling (no inbound ports,
no webhook needed).

## Commands

- Send any `ed2k://`, `magnet:` or `https://…/file.torrent` link → added to downloads.
- `/downloads` (`/dl`) — list active downloads with progress bars.
- `/cancel <num>` / `/pause <num>` / `/resume <num>` — control a download by its number.
- `/bw` — bandwidth stats.
- `/raw <cmd>` — run any raw mldonkey console command (e.g. `/raw vd`).
- `/help` — show help.

## Setup

### 1. Create the Telegram bot

1. Open Telegram, talk to [@BotFather](https://t.me/BotFather), send `/newbot`,
   follow the prompts, and copy the **token**.
2. Talk to [@userinfobot](https://t.me/userinfobot) to get your numeric **user ID**.

### 2. Configure

```bash
cp .env.example .env
# edit .env: TELEGRAM_BOT_TOKEN, ALLOWED_USER_IDS, MLDONKEY_HOST, ...
```

### 3. Allow the bot to reach the mldonkey console

mldonkey only accepts console connections from IPs in its `allowed_ips` list.
The bot connects to the **host** (`192.168.1.15`), so the connection arrives at
mldonkey from the Docker gateway / host. Make sure that source is allowed.

Easiest: in the mldonkey web UI (`http://192.168.1.15:4080`) → *Options* →
search `allowed_ips`, or edit `appdata/mldonkey/downloads.ini`:

```ini
allowed_ips = ["127.0.0.1"; "10.0.0.0/8"; "172.16.0.0/12"; "192.168.0.0/16"]
```

Then restart the mldonkey container. (If a console password is set, put it in
`MLDONKEY_PASSWORD` in `.env`.)

### 4. Run

```bash
docker compose up -d --build
docker compose logs -f
```

In Telegram, send `/help` to your bot, then paste a link.

## Notes

- The `/downloads` view parses mldonkey's `vd` output heuristically. If the
  layout from your mldonkey version doesn't render nicely, use `/raw vd` and
  the formatting can be tuned in `formatting.py`.
- Keep `ALLOWED_USER_IDS` set — otherwise anyone who finds the bot can drive
  your downloader.
