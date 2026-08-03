# REBORN Discord Bot

A single Python process that combines a Discord bot with an account-linking web
service for a roleplay community running on Invision Community (IPS) and FiveM.

Members link their Discord and Steam accounts from the forum, the identifiers are
written back into their forum profile, and the game server can then resolve a
player's Steam identifier from their Discord id.

## What it does

- **Autorole** — assigns a candidate role to members when they join the server.
- **Discord linking** — OAuth2 (`identify` scope only) writes the member's Discord
  id into a custom profile field on the forum, then grants the role immediately.
- **Steam linking** — Steam OpenID, with the SteamID64 converted to the
  lowercase-hex `steam:` format that FiveM uses to identify players.
- **Application intake** — accepts a multi-step application form and creates a
  record in a Pages database, authored by the applicant, then posts it to the
  moderation channel for review.
- **Event notifications** — around thirty endpoints the forum calls to announce
  level changes, XP transfers, warnings, department transfers, mentorship
  invitations and election milestones, delivered to channels or by direct
  message.
- **Elections** — posts ballots, edits live turnout figures on an existing
  message, and announces results.
- **Birthday greetings** — a scheduled task that pulls the day's birthdays from
  the forum and posts congratulations.
- **Binding lookup** — a loopback-only endpoint the game server queries to map a
  Discord id to a Steam identifier.

## Security model

Every request originating from the forum carries `member_id`, a timestamp and an
HMAC-SHA256 signature over `member_id:timestamp`, generated with a secret shared
between the forum and this service. Requests are rejected if the signature does
not verify or the timestamp is older than ten minutes, so a leaked link cannot be
replayed and a forged one cannot be constructed.

Signatures are compared with `hmac.compare_digest` to avoid timing attacks. The
internal binding endpoint additionally refuses any request that does not come
from loopback. No secrets are stored in the repository.

## Architecture

```
Forum (PHP / IPS)
      |  HMAC-signed links and form posts
      v
Flask  (worker thread)  ---- bot.loop.create_task() ---->  discord.py (main thread)
      |                                                          |
      |  IPS REST API                                            |  Discord API
      v                                                          v
  Member profiles                                         Guild roles
```

Flask runs in a worker thread while the Discord client owns the main thread.
Crossing between them goes through `bot.loop.create_task()`, which is the only
thread-safe way to schedule coroutines onto a running event loop.

## Setup

```bash
git clone https://github.com/Anton-celld/reborn-discord-bot.git
cd reborn-discord-bot
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
python bot.py
```

`SERVER MEMBERS INTENT` must be enabled in the Discord Developer Portal, and the
OAuth redirect URI registered there must match `DISCORD_REDIRECT` exactly.

### Configuration

| Variable | Purpose |
|---|---|
| `DISCORD_TOKEN` | Bot token |
| `GUILD_ID`, `CANDIDATE_ROLE_ID` | Target guild and role granted on join |
| `HMAC_SECRET` | Shared secret; must match the value used by the forum |
| `DISCORD_CLIENT_ID`, `DISCORD_CLIENT_SECRET`, `DISCORD_REDIRECT` | OAuth application credentials |
| `RETURN_URL`, `CORS_ORIGIN` | Page users return to, and the origin allowed to post applications |
| `IPS_API_URL`, `IPS_API_KEY` | Forum REST API endpoint and key |
| `IPS_FIELD_DISCORD`, `IPS_FIELD_STEAM` | Custom profile field ids |
| `APPLICATIONS_DB_ID` | Pages database receiving applications |

### Deployment

OAuth requires HTTPS on the redirect URI, so the service sits behind nginx with a
certificate. Flask binds to loopback and is never exposed directly.

```nginx
server {
    server_name link.example.com;
    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
    }
}
```

Run it under systemd using `reborn-bot.service.example`. Secrets live in
`/etc/reborn-bot.env` with mode `600`, referenced via `EnvironmentFile` so they
never appear in the unit file itself.

## Troubleshooting

| Symptom | Cause |
|---|---|
| 403 on a linking request | `HMAC_SECRET` differs between the forum and this service |
| 401 or 403 from the IPS API | Bad key, missing endpoint permission, or IP restriction |
| Field not written despite HTTP 200 | Wrong custom field id or `customFields` format |
| `redirect_uri` mismatch | Portal redirect URI differs from `DISCORD_REDIRECT` |
| Role not granted | Bot role sits below the target role in the hierarchy |

## Notes

This runs in production for an active community. The forum-side PHP integration
is not part of this repository.

User-facing message text is in Russian, since the community is Russian-speaking.
Code, comments and documentation are in English.

Known refactoring work: the notification endpoints repeat a common shape and are
being consolidated into a declarative table with a shared signature-checking
decorator.

## License

MIT
