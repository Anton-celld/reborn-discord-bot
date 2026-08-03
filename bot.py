"""
REBORN — combined Discord bot and account-linking service.

Responsibilities:
  * assign the "Candidate" role to members on join (autorole)
  * expose a Flask web layer for account linking:
        /link/discord  -> Discord OAuth  -> writes discord_id to the IPS profile
        /link/steam    -> Steam OpenID   -> writes a FiveM steam:hex id to the profile
  * grant the role immediately after a successful Discord link

Architecture: Flask runs in a worker thread, the Discord client owns the main
thread. Flask hands work to the bot through bot.loop.create_task(), which is the
safe bridge between the two.

Requirements:
  * SERVER MEMBERS INTENT must be enabled in the Discord Developer Portal
    (needed for on_member_join).
  * The web layer must be reachable over HTTPS from the internet, since OAuth
    rejects plain-HTTP redirect URIs. Put nginx and a certificate in front of it.
"""

import os
import time
import hmac
import hashlib
import logging
import threading
from urllib.parse import urlencode

import requests
import discord
from flask import Flask, request, redirect, abort
from flask_cors import CORS
from dotenv import load_dotenv

load_dotenv()
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("reborn")

# --- Core settings -----------------------------------------------------------
TOKEN = os.getenv("DISCORD_TOKEN")
GUILD_ID = int(os.getenv("GUILD_ID", "0"))
CANDIDATE_ROLE_ID = int(os.getenv("CANDIDATE_ROLE_ID", "0"))

# --- OAuth / account linking -------------------------------------------------
HMAC_SECRET = os.getenv("HMAC_SECRET", "").encode()
RETURN_URL = os.getenv("RETURN_URL", "https://example.com/apply")
DISCORD_CLIENT_ID = os.getenv("DISCORD_CLIENT_ID")
DISCORD_CLIENT_SECRET = os.getenv("DISCORD_CLIENT_SECRET")
DISCORD_REDIRECT = os.getenv("DISCORD_REDIRECT")
WEB_PORT = int(os.getenv("WEB_PORT", "8000"))

# --- IPS REST API ------------------------------------------------------------
# The key travels as a query parameter: this IPS deployment does not accept
# Authorization headers.
IPS_API_URL = os.getenv("IPS_API_URL")
IPS_API_KEY = os.getenv("IPS_API_KEY")
IPS_FIELD_DISCORD = os.getenv("IPS_FIELD_DISCORD")
IPS_FIELD_STEAM = os.getenv("IPS_FIELD_STEAM")
APPLICATIONS_DB_ID = int(os.getenv("APPLICATIONS_DB_ID", "2"))

DISCORD_API = "https://discord.com/api/v10"
STEAM_OPENID = "https://steamcommunity.com/openid/login"
TOKEN_TTL = 600

# User-visible text stored on created records. Localise as needed.
APPLICATION_TITLE = "Application from {nick}"


# =============================== Discord bot =================================
intents = discord.Intents.default()
intents.members = True
bot = discord.Client(intents=intents)


@bot.event
async def on_ready():
    log.info(f"Signed in as {bot.user}")
    guild = bot.get_guild(GUILD_ID)
    if not guild or not guild.get_role(CANDIDATE_ROLE_ID):
        log.warning("Check GUILD_ID and CANDIDATE_ROLE_ID.")
        return
    if guild.me.top_role <= guild.get_role(CANDIDATE_ROLE_ID):
        log.warning("Bot role ranks below the candidate role; grants will fail.")
    else:
        log.info("Autorole ready.")


@bot.event
async def on_member_join(member: discord.Member):
    if member.guild.id != GUILD_ID:
        return
    role = member.guild.get_role(CANDIDATE_ROLE_ID)
    if not role:
        return
    try:
        await member.add_roles(role, reason="Automatic candidate role on join")
        log.info(f"Granted {role.name} to {member}")
    except discord.Forbidden:
        log.error("Missing permission to grant role (Manage Roles / hierarchy).")


async def give_candidate_by_id(discord_user_id: int):
    """Grant the candidate role by Discord user id, called after linking."""
    guild = bot.get_guild(GUILD_ID)
    if not guild:
        return
    member = guild.get_member(discord_user_id)
    role = guild.get_role(CANDIDATE_ROLE_ID)
    if not member or not role or role in member.roles:
        return
    try:
        await member.add_roles(role, reason="Granted after Discord account link")
        log.info(f"Granted {role.name} to {member} after linking")
    except discord.Forbidden:
        log.error("Could not grant role after linking (permissions / hierarchy).")


# =============================== Web layer ===================================
web = Flask(__name__)
CORS(web, resources={r"/submit": {"origins": os.getenv("CORS_ORIGIN", "")}})


def verify_token(member_id, ts, sig):
    """Validate an HMAC-signed, time-limited link issued by the forum."""
    try:
        if abs(time.time() - int(ts)) > TOKEN_TTL:
            return False
    except (ValueError, TypeError):
        return False
    expected = hmac.new(
        HMAC_SECRET, f"{member_id}:{ts}".encode(), hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(expected, sig or "")


def write_to_ips(member_id, field_id, value):
    """Write a single custom profile field for an IPS member."""
    url = f"{IPS_API_URL}/core/members/{member_id}?key={IPS_API_KEY}"
    try:
        r = requests.post(
            url, data={f"customFields[{field_id}]": value}, timeout=10
        )
        if r.status_code == 200:
            log.info(f"IPS: wrote field {field_id} for member {member_id}")
            return True
        log.error(f"IPS API {r.status_code}: {r.text[:160]}")
    except requests.RequestException as e:
        log.error(f"IPS API request failed: {e}")
    return False


def create_application(member_id, nick, fields: dict):
    """
    Create an application record in the Pages database.

    fields maps custom field ids to values. The record is authored by the
    applicant so that permissions and notifications behave as expected.
    """
    url = f"{IPS_API_URL}/cms/records/{APPLICATIONS_DB_ID}?key={IPS_API_KEY}"
    title = APPLICATION_TITLE.format(nick=nick)
    data = {
        "title": title,
        "content": f"<p>{title}</p>",  # IPS requires an HTML content body
        "author": str(member_id),
        "hidden": 0,
    }
    for fid, val in fields.items():
        data[f"field_{fid}"] = val
    try:
        r = requests.post(url, data=data, timeout=15)
        if r.status_code == 200:
            record_id = r.json().get("id", "?")
            log.info(f"Application created: record {record_id} by member {member_id}")
            return True
        log.error(f"IPS cms/records {r.status_code}: {r.text[:300]}")
    except requests.RequestException as e:
        log.error(f"IPS cms/records request failed: {e}")
    return False


def back(status):
    """Redirect the user back to the application page with a status flag."""
    return redirect(f"{RETURN_URL}?{urlencode({'link': status})}")


@web.route("/health")
def health():
    return "ok"


@web.route("/submit", methods=["POST"])
def submit_application():
    """
    Accept an application submitted from the multi-step form on the forum.

    Fields arrive form-urlencoded together with member_id, ts and sig, which are
    verified against the shared HMAC secret before anything is written.
    """
    form = request.form
    member_id = form.get("member_id", "")
    ts = form.get("ts", "")
    sig = form.get("sig", "")
    if not verify_token(member_id, ts, sig):
        return {"ok": False, "error": "bad_signature"}, 403

    nick = (form.get("nick") or f"#{member_id}").strip()

    # Form input names mapped onto Pages database field ids.
    fields = {
        6: form.get("name", ""),
        7: form.get("surname", ""),
        8: form.get("gender", ""),
        9: form.get("birth", ""),
        10: form.get("social", ""),
        11: form.get("qualities", ""),
        12: form.get("about", ""),
        13: form.get("dept", ""),
        14: form.get("dept_why", ""),
        15: form.get("license", ""),
        16: "pending",
    }
    ok = create_application(member_id, nick, fields)
    return ({"ok": True}, 200) if ok else ({"ok": False, "error": "ips_failed"}, 502)


@web.route("/api/binding")
def api_binding():
    """
    Internal lookup used by the game server: resolve a Steam identifier from a
    Discord id. Bound to loopback so it is never exposed to the internet.
    """
    if request.remote_addr not in ("127.0.0.1", "::1"):
        abort(403)
    discord_id = request.args.get("discord_id", "").strip()
    if not discord_id:
        return {"error": "missing discord_id"}, 400

    page = 1
    while True:
        url = f"{IPS_API_URL}/core/members?page={page}&perPage=100&key={IPS_API_KEY}"
        try:
            r = requests.get(url, timeout=30)
        except requests.RequestException as e:
            log.error(f"IPS API request failed during binding lookup: {e}")
            return {"error": "ips_error"}, 503

        if r.status_code != 200:
            log.error(f"IPS binding lookup returned {r.status_code}")
            return {"error": "ips_error"}, 503

        payload = r.json()
        members = payload.get("results", [])
        if not members:
            break

        for member in members:
            member_discord = None
            member_steam = None
            for group in member.get("customFields", {}).values():
                for fid, field in group.get("fields", {}).items():
                    if fid == str(IPS_FIELD_DISCORD):
                        member_discord = str(field.get("value") or "").strip()
                    if fid == str(IPS_FIELD_STEAM):
                        member_steam = str(field.get("value") or "").strip()

            if member_discord == discord_id:
                name = member.get("name", "")
                log.info(f"Binding resolved: discord {discord_id} -> {member_steam}")
                return {"steam": member_steam or "", "name": name}

        total = payload.get("totalResults", 0)
        per_page = payload.get("perPage", 100)
        if page * per_page >= total:
            break
        page += 1

    return {"steam": "", "name": ""}


# --- Discord linking ---------------------------------------------------------
@web.route("/link/discord")
def link_discord():
    member_id = request.args.get("member_id", "")
    ts = request.args.get("ts", "")
    sig = request.args.get("sig", "")
    if not verify_token(member_id, ts, sig):
        abort(403, "Invalid signature")
    params = {
        "client_id": DISCORD_CLIENT_ID,
        "redirect_uri": DISCORD_REDIRECT,
        "response_type": "code",
        "scope": "identify",
        "state": f"{member_id}|{ts}|{sig}",
    }
    return redirect(f"{DISCORD_API}/oauth2/authorize?{urlencode(params)}")


@web.route("/callback/discord")
def callback_discord():
    code = request.args.get("code", "")
    state = request.args.get("state", "")
    try:
        member_id, ts, sig = state.split("|")
    except ValueError:
        abort(400)
    if not verify_token(member_id, ts, sig):
        abort(403)

    token_response = requests.post(
        f"{DISCORD_API}/oauth2/token",
        data={
            "client_id": DISCORD_CLIENT_ID,
            "client_secret": DISCORD_CLIENT_SECRET,
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": DISCORD_REDIRECT,
        },
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        timeout=10,
    )
    if token_response.status_code != 200:
        return back("error")

    access_token = token_response.json().get("access_token")
    identity = requests.get(
        f"{DISCORD_API}/users/@me",
        headers={"Authorization": f"Bearer {access_token}"},
        timeout=10,
    )
    if identity.status_code != 200:
        return back("error")

    discord_id = identity.json().get("id")
    ok = write_to_ips(member_id, IPS_FIELD_DISCORD, discord_id)
    if ok and discord_id:
        try:
            bot.loop.create_task(give_candidate_by_id(int(discord_id)))
        except Exception as e:
            log.warning(f"Could not schedule role grant: {e}")
    return back("discord_ok" if ok else "error")


# --- Steam linking -----------------------------------------------------------
@web.route("/link/steam")
def link_steam():
    member_id = request.args.get("member_id", "")
    ts = request.args.get("ts", "")
    sig = request.args.get("sig", "")
    if not verify_token(member_id, ts, sig):
        abort(403, "Invalid signature")
    realm = request.url_root.rstrip("/")
    return_to = f"{realm}/callback/steam?member_id={member_id}&ts={ts}&sig={sig}"
    params = {
        "openid.ns": "http://specs.openid.net/auth/2.0",
        "openid.mode": "checkid_setup",
        "openid.return_to": return_to,
        "openid.realm": realm,
        "openid.identity": "http://specs.openid.net/auth/2.0/identifier_select",
        "openid.claimed_id": "http://specs.openid.net/auth/2.0/identifier_select",
    }
    return redirect(f"{STEAM_OPENID}?{urlencode(params)}")


@web.route("/callback/steam")
def callback_steam():
    member_id = request.args.get("member_id", "")
    ts = request.args.get("ts", "")
    sig = request.args.get("sig", "")
    if not verify_token(member_id, ts, sig):
        abort(403)

    params = dict(request.args)
    params["openid.mode"] = "check_authentication"
    verification = requests.post(STEAM_OPENID, data=params, timeout=10)
    if "is_valid:true" not in verification.text:
        return back("error")

    claimed = request.args.get("openid.claimed_id", "")
    steam64 = claimed.rsplit("/", 1)[-1] if claimed else ""
    if not steam64.isdigit():
        return back("error")

    # FiveM identifies players by a lowercase hexadecimal SteamID64.
    fivem_id = "steam:" + format(int(steam64), "x")
    ok = write_to_ips(member_id, IPS_FIELD_STEAM, fivem_id)
    return back("steam_ok" if ok else "error")


def run_web():
    """Flask development server, adequate for this traffic level."""
    web.run(host="127.0.0.1", port=WEB_PORT)


if __name__ == "__main__":
    if not TOKEN:
        raise SystemExit("DISCORD_TOKEN is not set")
    threading.Thread(target=run_web, daemon=True).start()
    log.info(f"Web layer listening on 127.0.0.1:{WEB_PORT}")
    bot.run(TOKEN)
