# REBORN Discord Bot

A Python service that keeps a community website and its Discord server in sync: role assignment, nickname formatting, and push notifications for in-app events.

Built as a companion service for [REBORN](https://rebornrp.online), a roleplay community platform running on PHP/MySQL.

## What it does

- **Role synchronisation** — mirrors department, command and trainee roles from the website's database onto Discord members. The bot only touches a fixed allowlist of managed roles, so manually assigned roles are never removed.
- **Nickname sync** — formats Discord nicknames as `Name · Level` and updates them when a member's level changes on the website.
- **Push notifications** — delivers level-up alerts, birthday rewards and site notifications to members via direct message.
- **Dry-run mode** — every sync operation can be previewed before it is applied (`--apply` to commit changes).

## Architecture

```
Website (PHP/MySQL)  ──HMAC-signed HTTP──>  Flask API  ──>  discord.py client  ──>  Discord
```

- **Flask** exposes the HTTP endpoints the website calls.
- **discord.py** maintains the gateway connection and performs guild operations.
- Every endpoint verifies an **HMAC signature** derived from a shared secret, so requests cannot be forged by anyone who discovers the URL.
- Deployed as a **systemd unit** on a Linux VPS, with automatic restart on failure.

## Setup

```bash
git clone https://github.com/Anton-celld/reborn-discord-bot.git
cd reborn-discord-bot
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env    # fill in your own values
python bot.py
```

### Configuration

| Variable | Description |
|---|---|
| `DISCORD_TOKEN` | Bot token from the Discord developer portal |
| `GUILD_ID` | Target Discord server ID |
| `HMAC_SECRET` | Shared secret used to sign requests from the website |
| `DB_HOST` / `DB_NAME` / `DB_USER` / `DB_PASSWORD` | MySQL connection details |

No secrets are stored in the repository. `.env` is gitignored.

### Running as a service

```ini
[Unit]
Description=REBORN Discord bot
After=network.target

[Service]
WorkingDirectory=/opt/reborn-bot
ExecStart=/opt/reborn-bot/venv/bin/python bot.py
Restart=always

[Install]
WantedBy=multi-user.target
```

## Notes

This is a working service, not a demo — it runs in production for an active community. The code is published as a portfolio piece; the website side of the integration is not open source.

## License

MIT
