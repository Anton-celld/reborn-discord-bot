"""
REBORN — combined Discord bot and account-linking service.

Responsibilities:
  * assign the "Candidate" role to members on join (autorole)
  * expose a Flask web layer for account linking:
        /link/discord  -> Discord OAuth  -> writes discord_id to the IPS profile
        /link/steam    -> Steam OpenID   -> writes a FiveM steam:hex id to the profile
  * deliver notifications from the forum into Discord: level changes, XP
    transfers, warnings, elections, mentorship and department events
  * synchronise roles and nicknames between the forum and the guild
  * accept application submissions and create records in a Pages database

Architecture: Flask runs in a worker thread, the Discord client owns the main
thread. Flask hands work to the bot through bot.loop.create_task(), which is the
only thread-safe way to schedule coroutines onto a running event loop.

Requirements:
  * SERVER MEMBERS INTENT must be enabled in the Discord Developer Portal.
  * The web layer must be reachable over HTTPS, since OAuth rejects plain-HTTP
    redirect URIs. Put nginx and a certificate in front of it.

User-facing message text is intentionally kept in Russian: this service runs for
a Russian-speaking community.
"""

import os
import time
import random
import hmac
import hashlib
import asyncio
import logging
import threading
from urllib.parse import urlencode
from datetime import datetime, timezone, timedelta

import requests
import discord
from discord.ext import tasks
try:
    from zoneinfo import ZoneInfo
except ImportError:
    ZoneInfo = None
from discord.ext import tasks
from flask import Flask, request, redirect, abort
from flask_cors import CORS
from dotenv import load_dotenv

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger("reborn")

# --- Core settings -----------------------------------------------------
TOKEN             = os.getenv("DISCORD_TOKEN")
GUILD_ID          = int(os.getenv("GUILD_ID", "0"))
CANDIDATE_ROLE_ID = int(os.getenv("CANDIDATE_ROLE_ID", "0"))

# Channel for XP transfer notifications
XP_LOG_CHANNEL_ID = int(os.getenv("XP_LOG_CHANNEL_ID", "0"))

# Birthday greetings
BIRTHDAY_CHANNEL_ID = int(os.getenv("BIRTHDAY_CHANNEL_ID", "0"))
BIRTHDAY_URL        = os.getenv("BIRTHDAY_URL", "")
BIRTHDAY_KEY        = os.getenv("BIRTHDAY_KEY", "")

# Channel and data source for birthday greetings
BIRTHDAY_CHANNEL_ID = int(os.getenv("BIRTHDAY_CHANNEL_ID", "0"))
BIRTHDAYS_URL       = os.getenv("BIRTHDAYS_URL", "")
BIRTHDAYS_KEY       = os.getenv("BIRTHDAYS_KEY", "")

# Applications channel and moderator role pinged on new submissions
APPLICATIONS_CHANNEL_ID = int(os.getenv("APPLICATIONS_CHANNEL_ID", "0"))
MODERATOR_ROLE_ID       = int(os.getenv("MODERATOR_ROLE_ID", "0"))
ADMIN_ROLE_ID           = int(os.getenv("ADMIN_ROLE_ID", "0"))
APPLICATIONS_LIST_URL   = os.getenv("APPLICATIONS_LIST_URL", "")

# Channel for level-change notifications
LEVEL_CHANNEL_ID = int(os.getenv("LEVEL_CHANNEL_ID", "0"))
COMPLAINT_CHANNEL_ID = int(os.getenv("COMPLAINT_CHANNEL_ID", "0"))

# OAuth / account linking
HMAC_SECRET           = os.getenv("HMAC_SECRET", "").encode()
RETURN_URL            = os.getenv("RETURN_URL", "")
DISCORD_CLIENT_ID     = os.getenv("DISCORD_CLIENT_ID")
DISCORD_CLIENT_SECRET = os.getenv("DISCORD_CLIENT_SECRET")
DISCORD_REDIRECT      = os.getenv("DISCORD_REDIRECT")
WEB_PORT              = int(os.getenv("WEB_PORT", "8000"))

# IPS REST API. The key travels in the query string: this deployment does not
# accept Authorization headers.
IPS_API_URL       = os.getenv("IPS_API_URL")
IPS_API_KEY       = os.getenv("IPS_API_KEY")
IPS_FIELD_DISCORD = os.getenv("IPS_FIELD_DISCORD")
IPS_FIELD_STEAM   = os.getenv("IPS_FIELD_STEAM")

DISCORD_API  = "https://discord.com/api/v10"
STEAM_OPENID = "https://steamcommunity.com/openid/login"
TOKEN_TTL    = 600

# =============================== Discord bot ===========================
intents = discord.Intents.default()
intents.members = True
bot = discord.Client(intents=intents)


@bot.event
async def on_ready():
    log.info(f"Бот запущен как {bot.user}")
    guild = bot.get_guild(GUILD_ID)
    if guild and guild.get_role(CANDIDATE_ROLE_ID):
        if guild.me.top_role <= guild.get_role(CANDIDATE_ROLE_ID):
            log.warning("Роль бота НИЖЕ «Кандидата» — подними её выше, иначе выдача упадёт.")
        else:
            log.info("autorole готов.")
    else:
        log.warning("Проверь GUILD_ID и CANDIDATE_ROLE_ID.")
    # start the background birthday check
    if not birthday_check.is_running():
        birthday_check.start()
        log.info("Планировщик поздравлений с ДР запущен (00:01 МСК).")

    # start the birthday check unless it is already running
    if not birthday_check.is_running():
        birthday_check.start()
        log.info("Проверка ДР запущена (0:01 МСК).")


@bot.event
async def on_member_join(member: discord.Member):
    if member.guild.id != GUILD_ID:
        return
    role = member.guild.get_role(CANDIDATE_ROLE_ID)
    if role:
        try:
            await member.add_roles(role, reason="Автовыдача «Кандидат» при входе")
            log.info(f"Выдал «{role.name}» {member}")
        except discord.Forbidden:
            log.error("Нет прав выдать роль (Manage Roles / иерархия).")


async def set_member_role(discord_user_id: int, role_id: int, grant: bool):
    """Выдать (grant=True) или забрать (grant=False) роль по Discord ID."""
    guild = bot.get_guild(GUILD_ID)
    if not guild:
        return
    member = guild.get_member(discord_user_id)
    role = guild.get_role(role_id)
    if not member or not role:
        log.warning(f"set_member_role: участник {discord_user_id} или роль {role_id} не найдены")
        return
    try:
        if grant and role not in member.roles:
            await member.add_roles(role, reason="Флаг выдан из админки")
            log.info(f"Роль {role.name} выдана {member}")
        elif not grant and role in member.roles:
            await member.remove_roles(role, reason="Флаг снят из админки")
            log.info(f"Роль {role.name} снята с {member}")
    except discord.Forbidden:
        log.error(f"Нет прав менять роль {role_id} (Manage Roles / иерархия).")
    except Exception as e:
        log.error(f"set_member_role ошибка: {e}")


async def give_candidate_by_id(discord_user_id: int):
    """Выдать «Кандидат» по Discord ID (вызывается после привязки)."""
    guild = bot.get_guild(GUILD_ID)
    if not guild:
        return
    member = guild.get_member(discord_user_id)
    role = guild.get_role(CANDIDATE_ROLE_ID)
    if member and role and role not in member.roles:
        try:
            await member.add_roles(role, reason="Выдача после привязки Discord")
            log.info(f"После привязки выдал «{role.name}» {member}")
        except discord.Forbidden:
            log.error("Не смог выдать роль после привязки (права/иерархия).")

async def notify_xp_transfer(from_discord_id, to_discord_id, from_name, to_name, amount, note):
    """Пишет в канал передач XP сообщение с пингами обоих участников."""
    guild = bot.get_guild(GUILD_ID)
    if not guild:
        log.warning("notify_xp_transfer: гильдия не найдена")
        return
    channel = guild.get_channel(XP_LOG_CHANNEL_ID)
    if not channel:
        log.warning(f"notify_xp_transfer: канал {XP_LOG_CHANNEL_ID} не найден")
        return

    # mention by <@id> when the Discord id is known, otherwise fall back to the name
    from_ping = f"<@{from_discord_id}>" if from_discord_id else f"**{from_name}**"
    to_ping   = f"<@{to_discord_id}>"   if to_discord_id   else f"**{to_name}**"

    note_line = f"\nНазначение перевода: {note}" if note else ""
    text = (f"💸 {from_ping} передал **{amount} XP** участнику {to_ping}.{note_line}")
    try:
        await channel.send(text)
        log.info(f"XP-перевод: уведомление отправлено ({from_name} → {to_name}, {amount})")
    except discord.Forbidden:
        log.error("Нет прав писать в канал передач XP (Send Messages).")
    except Exception as e:
        log.error(f"notify_xp_transfer ошибка: {e}")


async def sync_roles(managed, players, apply=False):
    """\u0412\u044b\u0440\u0430\u0432\u043d\u0438\u0432\u0430\u0435\u0442 \u0441\u043b\u0443\u0436\u0435\u0431\u043d\u044b\u0435 \u0440\u043e\u043b\u0438 \u0443\u0447\u0430\u0441\u0442\u043d\u0438\u043a\u043e\u0432.

    managed \u2014 \u043f\u043e\u043b\u043d\u044b\u0439 \u0441\u043f\u0438\u0441\u043e\u043a \u0440\u043e\u043b\u0435\u0439, \u043a\u043e\u0442\u043e\u0440\u044b\u043c\u0438 \u043c\u044b \u0443\u043f\u0440\u0430\u0432\u043b\u044f\u0435\u043c. \u0412\u0441\u0451, \u0447\u0435\u0433\u043e \u043d\u0435\u0442
    \u0432 \u044d\u0442\u043e\u043c \u0441\u043f\u0438\u0441\u043a\u0435, \u043d\u0435 \u0442\u0440\u043e\u0433\u0430\u0435\u043c: \u0434\u0435\u043a\u043e\u0440\u0430\u0442\u0438\u0432\u043d\u044b\u0435 \u0440\u043e\u043b\u0438, \u0431\u0443\u0441\u0442\u044b \u0438 \u043f\u0440\u043e\u0447\u0435\u0435.
    players \u2014 [{discord, name, roles}] \u2014 \u043a\u0430\u043a\u0438\u0435 \u0440\u043e\u043b\u0438 \u041f\u041e\u041b\u041e\u0416\u0415\u041d\u042b \u0438\u0433\u0440\u043e\u043a\u0443.
    apply=False \u2014 \u0442\u043e\u043b\u044c\u043a\u043e \u043f\u043e\u0441\u0447\u0438\u0442\u0430\u0442\u044c \u0440\u0430\u0441\u0445\u043e\u0436\u0434\u0435\u043d\u0438\u044f, \u043d\u0438\u0447\u0435\u0433\u043e \u043d\u0435 \u043c\u0435\u043d\u044f\u0442\u044c.
    """
    guild = bot.get_guild(GUILD_ID)
    if not guild:
        return {"ok": False, "error": "guild_not_found"}

    managed_ids = set()
    for r in managed or []:
        try:
            managed_ids.add(int(r))
        except (ValueError, TypeError):
            continue

    # \u0438\u043c\u0435\u043d\u0430 \u0440\u043e\u043b\u0435\u0439 \u0434\u043b\u044f \u0447\u0438\u0442\u0430\u0435\u043c\u043e\u0433\u043e \u043e\u0442\u0447\u0451\u0442\u0430
    def rname(rid):
        r = guild.get_role(rid)
        return r.name if r else str(rid)

    changes = []
    applied = 0
    for p in players or []:
        did = str(p.get("discord", "")).strip()
        if not did:
            continue
        try:
            member = guild.get_member(int(did))
        except (ValueError, TypeError):
            continue
        if not member:
            continue                      # \u043d\u0435 \u043d\u0430 \u0441\u0435\u0440\u0432\u0435\u0440\u0435 \u2014 \u043f\u0440\u043e\u043f\u0443\u0441\u043a\u0430\u0435\u043c

        want = set()
        for r in p.get("roles") or []:
            try:
                want.add(int(r))
            except (ValueError, TypeError):
                continue
        want &= managed_ids               # \u043d\u0430 \u0432\u0441\u044f\u043a\u0438\u0439: \u0442\u043e\u043b\u044c\u043a\u043e \u0443\u043f\u0440\u0430\u0432\u043b\u044f\u0435\u043c\u044b\u0435

        have = {r.id for r in member.roles} & managed_ids
        to_add = want - have
        to_del = have - want
        if not to_add and not to_del:
            continue

        changes.append({
            "name":   p.get("name", did),
            "add":    [rname(r) for r in to_add],
            "remove": [rname(r) for r in to_del],
        })

        if not apply:
            continue

        try:
            if to_add:
                objs = [guild.get_role(r) for r in to_add]
                objs = [o for o in objs if o]
                if objs:
                    await member.add_roles(*objs, reason="\u0421\u0438\u043d\u0445\u0440\u043e\u043d\u0438\u0437\u0430\u0446\u0438\u044f \u0441 \u0444\u043e\u0440\u0443\u043c\u043e\u043c")
            if to_del:
                objs = [guild.get_role(r) for r in to_del]
                objs = [o for o in objs if o]
                if objs:
                    await member.remove_roles(*objs, reason="\u0421\u0438\u043d\u0445\u0440\u043e\u043d\u0438\u0437\u0430\u0446\u0438\u044f \u0441 \u0444\u043e\u0440\u0443\u043c\u043e\u043c")
            applied += 1
            await asyncio.sleep(0.6)      # \u0431\u0435\u0440\u0435\u0436\u0451\u043c \u043b\u0438\u043c\u0438\u0442\u044b Discord
        except discord.Forbidden:
            log.warning(f"sync_roles: \u043d\u0435\u0442 \u043f\u0440\u0430\u0432 \u043c\u0435\u043d\u044f\u0442\u044c \u0440\u043e\u043b\u0438 \u0443 {did}")
        except Exception as ex:
            log.warning(f"sync_roles: {did}: {ex}")

    log.info(f"sync_roles: \u0440\u0430\u0441\u0445\u043e\u0436\u0434\u0435\u043d\u0438\u0439 {len(changes)}, \u043f\u0440\u0438\u043c\u0435\u043d\u0435\u043d\u043e {applied}, apply={apply}")
    return {"ok": True, "changes": changes, "applied": applied}


async def sync_nicknames(entries):
    """Ставит участникам ник 'Имя · Уровень'. entries=[{discord,name,level}].
    Владельца сервера и тех, кто выше бота, пропускает (Discord не даёт менять их ник)."""
    guild = bot.get_guild(GUILD_ID)
    if not guild:
        return
    changed = 0
    for e in entries:
        did = str(e.get("discord", "")).strip()
        name = str(e.get("name", "")).strip()
        level = e.get("level")
        if not did or not name or level is None:
            continue
        try:
            member = guild.get_member(int(did))
        except (ValueError, TypeError):
            continue
        if not member or member.id == guild.owner_id:
            continue
        suffix = f" · {level}"
        base = name
        if len(base) + len(suffix) > 32:
            base = base[: 32 - len(suffix)].rstrip()
        new_nick = base + suffix
        if member.nick == new_nick:
            continue
        try:
            await member.edit(nick=new_nick, reason="Синхронизация ника с форумом")
            changed += 1
        except discord.Forbidden:
            continue
        except Exception as ex:
            log.warning(f"sync_nicknames: не смог сменить ник {did}: {ex}")
    if changed:
        log.info(f"Ники синхронизированы: {changed}")


async def notify_new_application(nick, dept, rid):
    """Пишет в модер-канал о новой заявке: пинг роли модерации + ссылка на карточку."""
    dept_code = dept
    deptg = dept_gen(dept_code)
    deptp = dept_pre(dept_code)
    dept  = dept_full(dept_code)
    guild = bot.get_guild(GUILD_ID)
    if not guild:
        return
    channel = guild.get_channel(APPLICATIONS_CHANNEL_ID)
    if not channel:
        log.warning(f"notify_new_application: канал {APPLICATIONS_CHANNEL_ID} не найден")
        return
    ping = f"<@&{MODERATOR_ROLE_ID}> " if MODERATOR_ROLE_ID else ""
    # link to the application record by its id
    link = f"{APPLICATIONS_LIST_URL}/_/r{rid}/" if rid and rid != "?" else APPLICATIONS_LIST_URL
    dept_line = f"\nДепартамент: **{dept}**" if dept else ""
    text = (f"{ping}🔔 **Новая заявка** от **{nick}**{dept_line}\n"
            f"Открыть и рассмотреть: {link}")
    try:
        await channel.send(text)
        log.info(f"Заявка: уведомил модерацию ({nick}, запись {rid})")
    except discord.Forbidden:
        log.error("Нет прав писать в канал заявок (Send Messages).")
    except Exception as e:
        log.error(f"notify_new_application ошибка: {e}")


async def notify_complaint(kind, text, target_discord="", target_name="", target_id=0):
    guild = bot.get_guild(GUILD_ID)
    if not guild:
        return
    channel = guild.get_channel(COMPLAINT_CHANNEL_ID)
    if not channel:
        log.warning("notify_complaint: \u043a\u0430\u043d\u0430\u043b \u043d\u0435 \u043d\u0430\u0439\u0434\u0435\u043d")
        return
    ping = f"<@&{MODERATOR_ROLE_ID}> " if MODERATOR_ROLE_ID else ""
    who = ""
    if target_discord:
        who = f"\n\u041d\u0430 \u0438\u0433\u0440\u043e\u043a\u0430: <@{target_discord}>"
    elif target_name:
        who = f"\n\u041d\u0430 \u0438\u0433\u0440\u043e\u043a\u0430: **{target_name}**"
    body = text if text else "\u2014"
    link = os.getenv("PLAYERS_PAGE_URL", "")
    msg = (f"{ping}\U0001f6a8 **\u041d\u043e\u0432\u0430\u044f \u0436\u0430\u043b\u043e\u0431\u0430** ({kind}){who}\n"
           f">>> {body}\n\n"
           f"\U0001f517 \u041f\u0430\u043d\u0435\u043b\u044c \u0418\u0433\u0440\u043e\u043a\u0438: {link}")
    try:
        await channel.send(msg)
        log.info("\u0416\u0430\u043b\u043e\u0431\u0430: \u0443\u0432\u0435\u0434\u043e\u043c\u0438\u043b")
    except discord.Forbidden:
        log.error("\u041d\u0435\u0442 \u043f\u0440\u0430\u0432 \u0432 \u043a\u0430\u043d\u0430\u043b \u0436\u0430\u043b\u043e\u0431.")
    except Exception as e:
        log.error(f"notify_complaint error: {e}")


async def notify_expel(name, discord, count):
    guild = bot.get_guild(GUILD_ID)
    if not guild:
        return
    channel = guild.get_channel(COMPLAINT_CHANNEL_ID)
    if not channel:
        log.warning("notify_expel: no channel")
        return
    ping = f"<@&{MODERATOR_ROLE_ID}> " if MODERATOR_ROLE_ID else ""
    who = f"<@{discord}>" if discord else (f"**{name}**" if name else "\u0438\u0433\u0440\u043e\u043a")
    msg = (f"{ping}\u26a0\ufe0f **\u041d\u0410 \u0418\u0421\u041a\u041b\u042e\u0427\u0415\u041d\u0418\u0415**\n"
           f"{who} \u043d\u0430\u0431\u0440\u0430\u043b **{count}/3** \u0431\u0430\u043b\u043b\u043e\u0432 \u043f\u0440\u0435\u0434\u0443\u043f\u0440\u0435\u0436\u0434\u0435\u043d\u0438\u044f.\n"
           f"\u0422\u0440\u0435\u0431\u0443\u0435\u0442\u0441\u044f \u0440\u0435\u0448\u0435\u043d\u0438\u0435 \u043a\u043e\u043c\u0430\u043d\u0434\u043e\u0432\u0430\u043d\u0438\u044f.")
    try:
        await channel.send(msg)
        log.info("expel notified")
    except Exception as e:
        log.error(f"notify_expel error: {e}")


async def notify_warning(target, admin, reason, term, count, discord=""):
    guild = bot.get_guild(GUILD_ID)
    if not guild:
        return
    channel = guild.get_channel(COMPLAINT_CHANNEL_ID)
    if not channel:
        return
    who = f"<@{discord}>" if discord else f"**{target}**"
    ping = f"<@&{ADMIN_ROLE_ID}> " if ADMIN_ROLE_ID else ""
    msg = (f"{ping}\u26a0\ufe0f \u0412\u044b\u0434\u0430\u043d\u043e \u043f\u0440\u0435\u0434\u0443\u043f\u0440\u0435\u0436\u0434\u0435\u043d\u0438\u0435 (**{count}/3**)\n"
           f"\u0418\u0433\u0440\u043e\u043a: {who}\n"
           f"\u0412\u044b\u0434\u0430\u043b: {admin}\n"
           f"\u0421\u0440\u043e\u043a: {term}\n"
           f"\u041f\u0440\u0438\u0447\u0438\u043d\u0430: {reason}")
    try:
        await channel.send(msg)
        log.info("warning notified")
    except Exception as e:
        log.error(f"notify_warning error: {e}")


async def notify_warning_removed(target, admin, reason, discord=""):
    guild = bot.get_guild(GUILD_ID)
    if not guild:
        return
    channel = guild.get_channel(COMPLAINT_CHANNEL_ID)
    if not channel:
        return
    who = f"<@{discord}>" if discord else f"**{target}**"
    ping = f"<@&{MODERATOR_ROLE_ID}> " if MODERATOR_ROLE_ID else ""
    msg = (f"{ping}\u2705 \u0421\u043d\u044f\u0442 \u0431\u0430\u043b\u043b \u043f\u0440\u0435\u0434\u0443\u043f\u0440\u0435\u0436\u0434\u0435\u043d\u0438\u044f\n"
           f"\u0418\u0433\u0440\u043e\u043a: {who}\n"
           f"\u0421\u043d\u044f\u043b: {admin}\n"
           f"\u0411\u044b\u043b \u0437\u0430: {reason}")
    try:
        await channel.send(msg)
        log.info("warning removal notified")
    except Exception as e:
        log.error(f"notify_warning_removed error: {e}")


async def notify_warning_expired(target, reason, active, discord=""):
    guild = bot.get_guild(GUILD_ID)
    if not guild:
        return
    channel = guild.get_channel(COMPLAINT_CHANNEL_ID)
    if not channel:
        return
    who = f"<@{discord}>" if discord else f"**{target}**"
    msg = (f"\u23f3 \u0418\u0441\u0442\u0451\u043a \u0431\u0430\u043b\u043b \u043f\u0440\u0435\u0434\u0443\u043f\u0440\u0435\u0436\u0434\u0435\u043d\u0438\u044f\n"
           f"\u0418\u0433\u0440\u043e\u043a: {who}\n"
           f"\u0411\u044b\u043b \u0437\u0430: {reason}\n"
           f"\u041e\u0441\u0442\u0430\u043b\u043e\u0441\u044c \u0430\u043a\u0442\u0438\u0432\u043d\u044b\u0445: **{active}/3**")
    try:
        await channel.send(msg)
        log.info("warning expiry notified")
    except Exception as e:
        log.error(f"notify_warning_expired error: {e}")


COMMAND_CHANNEL_ID = int(os.getenv("COMMAND_CHANNEL_ID", "0"))

async def notify_command(target, dept, discord="", dept_role=""):
    dept_code = dept
    deptg = dept_gen(dept_code)
    deptp = dept_pre(dept_code)
    dept  = dept_full(dept_code)
    guild = bot.get_guild(GUILD_ID)
    if not guild:
        return
    channel = guild.get_channel(COMMAND_CHANNEL_ID)
    if not channel:
        return
    who = f"<@{discord}>" if discord else f"**{target}**"
    ping = f"\n<@&{dept_role}>" if dept_role else ""
    post = "\u0441\u0443\u043f\u0435\u0440\u0432\u0430\u0439\u0437\u0435\u0440\u0430" if dept == "\u0413\u0414" else "\u043a\u043e\u043c\u0430\u043d\u0434\u0438\u0440\u0430"
    msg = (f"\u2b50 {who} \u043d\u0430\u0437\u043d\u0430\u0447\u0435\u043d \u043d\u0430 \u0434\u043e\u043b\u0436\u043d\u043e\u0441\u0442\u044c "
           f"**{post} {deptg}** \u0441\u0440\u043e\u043a\u043e\u043c \u043d\u0430 **3 \u043c\u0435\u0441\u044f\u0446\u0430**.\n"
           f"\u0416\u0435\u043b\u0430\u0435\u043c \u0443\u0441\u043f\u0435\u0445\u043e\u0432 \u043d\u0430 \u043f\u043e\u0441\u0442\u0443! \U0001f91d{ping}")
    try:
        await channel.send(msg)
        log.info("command assignment notified")
    except Exception as e:
        log.error(f"notify_command error: {e}")
    # internal push to the moderation channel, pinging admins and moderators
    modch = guild.get_channel(COMPLAINT_CHANNEL_ID)
    if modch:
        aping = (f"<@&{ADMIN_ROLE_ID}> " if ADMIN_ROLE_ID else "") + (f"<@&{MODERATOR_ROLE_ID}> " if MODERATOR_ROLE_ID else "")
        postw = "супервайзером" if dept == "ГД" else "командиром"
        mmsg = (f"{aping}ℹ️ {who} назначен **{postw} {deptg}**.")
        try:
            await modch.send(mmsg)
            log.info("command assignment notified (mod)")
        except Exception as e:
            log.error(f"notify_command mod error: {e}")


async def notify_command_extend(target, dept, discord="", dept_role=""):
    dept_code = dept
    deptg = dept_gen(dept_code)
    deptp = dept_pre(dept_code)
    dept  = dept_full(dept_code)
    guild = bot.get_guild(GUILD_ID)
    if not guild:
        return
    who = f"<@{discord}>" if discord else f"**{target}**"
    ping = f"\n<@&{dept_role}>" if dept_role else ""
    post = "\u0441\u0443\u043f\u0435\u0440\u0432\u0430\u0439\u0437\u0435\u0440\u0430" if dept == "\u0413\u0414" else "\u043a\u043e\u043c\u0430\u043d\u0434\u0438\u0440\u0430"
    postw = "\u0441\u0443\u043f\u0435\u0440\u0432\u0430\u0439\u0437\u0435\u0440\u043e\u043c" if dept == "\u0413\u0414" else "\u043a\u043e\u043c\u0430\u043d\u0434\u0438\u0440\u043e\u043c"
    ch = guild.get_channel(COMMAND_CHANNEL_ID)
    if ch:
        msg = (f"\U0001f504 {who} \u043f\u0435\u0440\u0435\u0438\u0437\u0431\u0440\u0430\u043d \u043d\u0430 \u0434\u043e\u043b\u0436\u043d\u043e\u0441\u0442\u044c "
               f"**{post} {deptg}** \u043d\u0430 \u043d\u043e\u0432\u044b\u0439 \u0441\u0440\u043e\u043a **3 \u043c\u0435\u0441\u044f\u0446\u0430**.\n"
               f"\u0422\u0430\u043a \u0434\u0435\u0440\u0436\u0430\u0442\u044c! \U0001f91d{ping}")
        try:
            await ch.send(msg)
            log.info("command extend notified")
        except Exception as e:
            log.error(f"notify_command_extend error: {e}")
    modch = guild.get_channel(COMPLAINT_CHANNEL_ID)
    if modch:
        aping = (f"<@&{ADMIN_ROLE_ID}> " if ADMIN_ROLE_ID else "") + (f"<@&{MODERATOR_ROLE_ID}> " if MODERATOR_ROLE_ID else "")
        mmsg = (f"{aping}\u2139\ufe0f \u0423 {who} \u043f\u043e\u043b\u043d\u043e\u043c\u043e\u0447\u0438\u044f **{postw} {deptg}** \u043f\u0440\u043e\u0434\u043b\u0435\u043d\u044b \u043d\u0430 **3 \u043c\u0435\u0441\u044f\u0446\u0430**.")
        try:
            await modch.send(mmsg)
            log.info("command extend notified (mod)")
        except Exception as e:
            log.error(f"notify_command_extend mod error: {e}")


async def notify_command_expiry(target, dept, days, discord=""):
    dept_code = dept
    deptg = dept_gen(dept_code)
    deptp = dept_pre(dept_code)
    dept  = dept_full(dept_code)
    guild = bot.get_guild(GUILD_ID)
    if not guild:
        return
    channel = guild.get_channel(COMPLAINT_CHANNEL_ID)
    if not channel:
        return
    who = f"<@{discord}>" if discord else f"**{target}**"
    ping = ((f"<@&{ADMIN_ROLE_ID}> " if ADMIN_ROLE_ID else "") + (f"<@&{MODERATOR_ROLE_ID}> " if MODERATOR_ROLE_ID else ""))
    post = "\u0441\u0443\u043f\u0435\u0440\u0432\u0430\u0439\u0437\u0435\u0440\u0430" if dept == "\u0413\u0414" else "\u043a\u043e\u043c\u0430\u043d\u0434\u0438\u0440\u0430"
    if int(days) <= 0:
        msg = (f"{ping}\u23f0 \u0423 {who} \u0438\u0441\u0442\u0435\u043a\u043b\u0438 \u043f\u043e\u043b\u043d\u043e\u043c\u043e\u0447\u0438\u044f "
               f"**{post} {deptg}**.\n"
               f"\u041d\u0443\u0436\u043d\u043e \u043f\u0440\u043e\u0434\u043b\u0438\u0442\u044c \u0438\u043b\u0438 \u0441\u043d\u044f\u0442\u044c \u043a\u043e\u043c\u0430\u043d\u0434\u043e\u0432\u0430\u043d\u0438\u0435.")
    else:
        d = int(days)
        if d == 1: word = "\u0434\u0435\u043d\u044c"
        elif d in (2,3,4): word = "\u0434\u043d\u044f"
        else: word = "\u0434\u043d\u0435\u0439"
        msg = (f"{ping}\u26a0\ufe0f \u0423 {who} \u0447\u0435\u0440\u0435\u0437 **{d} {word}** "
               f"\u0437\u0430\u043a\u0430\u043d\u0447\u0438\u0432\u0430\u044e\u0442\u0441\u044f \u043f\u043e\u043b\u043d\u043e\u043c\u043e\u0447\u0438\u044f **{post} {deptg}**.")
    try:
        await channel.send(msg)
        log.info(f"command expiry notified ({days}d)")
    except Exception as e:
        log.error(f"notify_command_expiry error: {e}")


SMART_LABELS = [
    ("smart_s", "S", "\u041a\u043e\u043d\u043a\u0440\u0435\u0442\u043d\u0430\u044f"),
    ("smart_m", "M", "\u0418\u0437\u043c\u0435\u0440\u0438\u043c\u0430\u044f"),
    ("smart_a", "A", "\u0414\u043e\u0441\u0442\u0438\u0436\u0438\u043c\u0430\u044f"),
    ("smart_r", "R", "\u0410\u043a\u0442\u0443\u0430\u043b\u044c\u043d\u0430\u044f"),
    ("smart_t", "T", "\u041e\u0433\u0440\u0430\u043d\u0438\u0447\u0435\u043d\u043d\u0430\u044f \u0432\u043e \u0432\u0440\u0435\u043c\u0435\u043d\u0438"),
]


def _dm_chunks(lines, limit=1900):
    """\u0421\u043e\u0431\u0440\u0430\u0442\u044c \u0441\u0442\u0440\u043e\u043a\u0438 \u0432 \u043a\u0443\u0441\u043a\u0438, \u0432\u043b\u0435\u0437\u0430\u044e\u0449\u0438\u0435 \u0432 \u043b\u0438\u043c\u0438\u0442 \u0441\u043e\u043e\u0431\u0449\u0435\u043d\u0438\u044f Discord."""
    out, buf = [], ""
    for ln in lines:
        if len(ln) > limit:                      # \u043e\u0434\u043d\u0430 \u0441\u0442\u0440\u043e\u043a\u0430 \u0434\u043b\u0438\u043d\u043d\u0435\u0435 \u043b\u0438\u043c\u0438\u0442\u0430 \u2014 \u0440\u0435\u0436\u0435\u043c \u0436\u0451\u0441\u0442\u043a\u043e
            if buf:
                out.append(buf); buf = ""
            for i in range(0, len(ln), limit):
                out.append(ln[i:i + limit])
            continue
        if len(buf) + len(ln) + 1 > limit:
            out.append(buf); buf = ln
        else:
            buf = (buf + "\n" + ln) if buf else ln
    if buf:
        out.append(buf)
    return out


def _safe(text):
    """\u041e\u0431\u0435\u0437\u0432\u0440\u0435\u0434\u0438\u0442\u044c \u043c\u0430\u0441\u0441\u043e\u0432\u044b\u0435 \u0443\u043f\u043e\u043c\u0438\u043d\u0430\u043d\u0438\u044f \u0432 \u043f\u043e\u043b\u044c\u0437\u043e\u0432\u0430\u0442\u0435\u043b\u044c\u0441\u043a\u043e\u043c \u0442\u0435\u043a\u0441\u0442\u0435."""
    return str(text).replace("@everyone", "@\u200beveryone").replace("@here", "@\u200bhere")


async def notify_election_apply_status(discord_id, dept, status, reason="", goals=None, pitch=""):
    """\u041b\u0438\u0447\u043d\u043e\u0435 \u0441\u043e\u043e\u0431\u0449\u0435\u043d\u0438\u0435 \u043a\u0430\u043d\u0434\u0438\u0434\u0430\u0442\u0443 \u043e \u0441\u0443\u0434\u044c\u0431\u0435 \u0435\u0433\u043e \u0437\u0430\u044f\u0432\u043a\u0438 (+ \u043a\u043e\u043f\u0438\u044f \u0430\u043d\u043a\u0435\u0442\u044b \u043f\u0440\u0438 \u043f\u043e\u0434\u0430\u0447\u0435)."""
    dept_code = dept
    deptg = dept_gen(dept_code)
    deptp = dept_pre(dept_code)
    dept  = dept_full(dept_code)
    if not discord_id:
        return
    try:
        user = await bot.fetch_user(int(discord_id))
    except Exception as e:
        log.warning(f"apply_status: user {discord_id} not found: {e}")
        return
    if status == "registered":
        msg = f"\U0001f4e9 \u0412\u0430\u0448\u0430 \u0437\u0430\u044f\u0432\u043a\u0430 \u043d\u0430 \u043f\u043e\u0441\u0442 \u043a\u043e\u043c\u0430\u043d\u0434\u0438\u0440\u0430 **{deptg}** \u0437\u0430\u0440\u0435\u0433\u0438\u0441\u0442\u0440\u0438\u0440\u043e\u0432\u0430\u043d\u0430 \u0438 \u043e\u0436\u0438\u0434\u0430\u0435\u0442 \u0440\u0435\u0448\u0435\u043d\u0438\u044f \u0430\u0434\u043c\u0438\u043d\u0438\u0441\u0442\u0440\u0430\u0446\u0438\u0438."
    elif status == "approved":
        msg = f"\u2705 \u0412\u0430\u0448\u0430 \u0437\u0430\u044f\u0432\u043a\u0430 \u043d\u0430 \u043f\u043e\u0441\u0442 \u043a\u043e\u043c\u0430\u043d\u0434\u0438\u0440\u0430 **{deptg}** \u043e\u0434\u043e\u0431\u0440\u0435\u043d\u0430. \u0412\u044b \u0434\u043e\u043f\u0443\u0449\u0435\u043d\u044b \u043a \u0432\u044b\u0431\u043e\u0440\u0430\u043c."
    elif status == "withdrawn":
        fee = str(reason).strip()
        msg = f"\U0001f6d1 \u0412\u044b \u043e\u0442\u043e\u0437\u0432\u0430\u043b\u0438 \u0437\u0430\u044f\u0432\u043a\u0443 \u043d\u0430 \u043f\u043e\u0441\u0442 \u043a\u043e\u043c\u0430\u043d\u0434\u0438\u0440\u0430 **{deptg}**."
        if fee and fee != "0":
            msg += f"\n\u0421\u043f\u0438\u0441\u0430\u043d\u043e {fee} XP \u0437\u0430 \u043e\u0442\u0437\u044b\u0432 \u043f\u043e\u0441\u043b\u0435 \u0437\u0430\u043a\u0440\u044b\u0442\u0438\u044f \u043f\u0440\u0438\u0451\u043c\u0430."
    elif status == "rejected":
        msg = f"\u274c \u0412\u0430\u0448\u0430 \u0437\u0430\u044f\u0432\u043a\u0430 \u043d\u0430 \u043f\u043e\u0441\u0442 \u043a\u043e\u043c\u0430\u043d\u0434\u0438\u0440\u0430 **{deptg}** \u043e\u0442\u043a\u043b\u043e\u043d\u0435\u043d\u0430."
        if reason:
            msg += f"\n\u041f\u0440\u0438\u0447\u0438\u043d\u0430: {reason}"
    elif status == "disqualified":
        msg = f"\u26d4 \u0412\u044b \u0441\u043d\u044f\u0442\u044b \u0441 \u0432\u044b\u0431\u043e\u0440\u043e\u0432 \u043a\u043e\u043c\u0430\u043d\u0434\u0438\u0440\u0430 **{deptg}**."
        if reason:
            msg += f"\n\u041f\u0440\u0438\u0447\u0438\u043d\u0430: {reason}"
    else:
        return

    nomention = discord.AllowedMentions.none()
    try:
        await user.send(msg, allowed_mentions=nomention)
    except Exception as e:
        log.warning(f"apply status DM failed for {discord_id}: {e}")
        return

    # \u043a\u043e\u043f\u0438\u044f \u0430\u043d\u043a\u0435\u0442\u044b \u2014 \u0442\u043e\u043b\u044c\u043a\u043e \u043f\u0440\u0438 \u0440\u0435\u0433\u0438\u0441\u0442\u0440\u0430\u0446\u0438\u0438 \u0438 \u0442\u043e\u043b\u044c\u043a\u043e \u0435\u0441\u043b\u0438 \u0435\u0451 \u043f\u0440\u0438\u0441\u043b\u0430\u043b\u0438
    if status != "registered" or not goals:
        log.info(f"apply status DM sent: {discord_id} {status}")
        return

    lines = ["\u041a\u043e\u043f\u0438\u044f \u0432\u0430\u0448\u0435\u0439 \u0430\u043d\u043a\u0435\u0442\u044b:", ""]
    for i, g in enumerate(goals, 1):
        lines.append(f"**\u0426\u0435\u043b\u044c {i}**")
        for key, letter, name in SMART_LABELS:
            val = _safe(g.get(key, "")).strip()
            if val:
                lines.append(f"**{letter} \u2014 {name}:** {val}")
        lines.append("")
    if pitch:
        lines.append("**\u041f\u043e\u0447\u0435\u043c\u0443 \u0438\u0434\u0443:**")
        lines.append(_safe(pitch))

    for part in _dm_chunks(lines):
        try:
            await user.send(">>> " + part, allowed_mentions=nomention)
        except Exception as e:
            log.warning(f"apply copy DM failed for {discord_id}: {e}")
            break
    log.info(f"apply status DM sent with copy: {discord_id}")


async def notify_election_deadline(kind, dept, deadline="", pending=0, approved=0, votes=0, url=""):
    """\u041d\u0430\u043f\u043e\u043c\u0438\u043d\u0430\u043d\u0438\u0435 \u0430\u0434\u043c\u0438\u043d\u0438\u0441\u0442\u0440\u0430\u0446\u0438\u0438: \u0441\u0440\u043e\u043a \u0432\u044b\u0448\u0435\u043b, \u044d\u0442\u0430\u043f \u043f\u043e\u0440\u0430 \u043f\u0435\u0440\u0435\u043a\u043b\u044e\u0447\u0438\u0442\u044c \u0440\u0443\u043a\u0430\u043c\u0438."""
    dept_code = dept
    deptg = dept_gen(dept_code)
    deptp = dept_pre(dept_code)
    dept  = dept_full(dept_code)
    guild = bot.get_guild(GUILD_ID)
    if not guild:
        return
    ch = guild.get_channel(COMPLAINT_CHANNEL_ID)
    if not ch:
        return
    ping = f"<@&{ADMIN_ROLE_ID}> " if ADMIN_ROLE_ID else ""
    if kind == "apply":
        lines = [f"{ping}\u23f0 **\u0421\u0440\u043e\u043a \u043f\u0440\u0438\u0451\u043c\u0430 \u0437\u0430\u044f\u0432\u043e\u043a \u0438\u0441\u0442\u0451\u043a \u2014 {dept}**",
                 f"\u041f\u0440\u0438\u0451\u043c \u0431\u044b\u043b \u0434\u043e: {deadline}",
                 f"\u041d\u0435 \u0440\u0430\u0441\u0441\u043c\u043e\u0442\u0440\u0435\u043d\u043e \u0437\u0430\u044f\u0432\u043e\u043a: **{pending}**, \u0434\u043e\u043f\u0443\u0449\u0435\u043d\u043e: **{approved}**",
                 "\u0417\u0430\u043a\u0440\u043e\u0439\u0442\u0435 \u043f\u0440\u0438\u0451\u043c \u0438 \u043e\u0442\u043a\u0440\u043e\u0439\u0442\u0435 \u0433\u043e\u043b\u043e\u0441\u043e\u0432\u0430\u043d\u0438\u0435 \u0432 \u043f\u0430\u043d\u0435\u043b\u0438."]
    elif kind in ("vote", "runoff"):
        head = "\u0432\u0442\u043e\u0440\u043e\u0433\u043e \u0442\u0443\u0440\u0430" if kind == "runoff" else "\u0433\u043e\u043b\u043e\u0441\u043e\u0432\u0430\u043d\u0438\u044f"
        lines = [f"{ping}\u23f0 **\u0421\u0440\u043e\u043a {head} \u0438\u0441\u0442\u0451\u043a \u2014 {dept}**",
                 f"\u0413\u043e\u043b\u043e\u0441\u043e\u0432\u0430\u043d\u0438\u0435 \u0431\u044b\u043b\u043e \u0434\u043e: {deadline}",
                 f"\u041e\u0442\u0434\u0430\u043d\u043e \u0433\u043e\u043b\u043e\u0441\u043e\u0432: **{votes}**",
                 "\u0417\u0430\u043a\u0440\u043e\u0439\u0442\u0435 \u0442\u0443\u0440 \u0438 \u043f\u043e\u0434\u0432\u0435\u0434\u0438\u0442\u0435 \u0438\u0442\u043e\u0433\u0438 \u0432 \u043f\u0430\u043d\u0435\u043b\u0438."]
    else:
        return
    if url:
        lines.append(url)
    try:
        await ch.send("\n".join(lines))
        log.info(f"election deadline reminder: {kind} {dept}")
    except Exception as e:
        log.error(f"notify_election_deadline error: {e}")


DEPT_FULL = {
    "\u0414\u041f\u0421":  "\u0414\u043e\u0440\u043e\u0436\u043d\u043e-\u043f\u0430\u0442\u0440\u0443\u043b\u044c\u043d\u0430\u044f \u0441\u043b\u0443\u0436\u0431\u0430",
    "\u041f\u041f\u0421":  "\u041f\u0430\u0442\u0440\u0443\u043b\u044c\u043d\u043e-\u043f\u043e\u0441\u0442\u043e\u0432\u0430\u044f \u0441\u043b\u0443\u0436\u0431\u0430",
    "\u0421\u041c\u041f":  "\u0421\u043a\u043e\u0440\u0430\u044f \u043c\u0435\u0434\u0438\u0446\u0438\u043d\u0441\u043a\u0430\u044f \u043f\u043e\u043c\u043e\u0449\u044c",
    "\u0421\u0421\u041f\u041e": "\u0421\u043b\u0443\u0436\u0431\u0430 \u0441\u043f\u0430\u0441\u0435\u043d\u0438\u044f \u0438 \u043f\u043e\u0436\u0430\u0440\u043d\u043e\u0439 \u043e\u0445\u0440\u0430\u043d\u044b",
    "\u0421\u041a":   "\u0421\u043b\u0435\u0434\u0441\u0442\u0432\u0435\u043d\u043d\u044b\u0439 \u043a\u043e\u043c\u0438\u0442\u0435\u0442",
    "\u0415\u0414\u0421":  "\u0415\u0434\u0438\u043d\u0430\u044f \u0434\u0438\u0441\u043f\u0435\u0442\u0447\u0435\u0440\u0441\u043a\u0430\u044f \u0441\u043b\u0443\u0436\u0431\u0430",
    "\u0413\u0414":   "\u0413\u0440\u0430\u0436\u0434\u0430\u043d\u0441\u043a\u0438\u0439 \u0434\u0435\u043f\u0430\u0440\u0442\u0430\u043c\u0435\u043d\u0442",
}


def dept_full(code):
    """\u041a\u043e\u0440\u043e\u0442\u043a\u0438\u0439 \u043a\u043e\u0434 \u0434\u0435\u043f\u0430 -> \u043f\u043e\u043b\u043d\u043e\u0435 \u043d\u0430\u0437\u0432\u0430\u043d\u0438\u0435. \u041d\u0435\u0438\u0437\u0432\u0435\u0441\u0442\u043d\u043e\u0435 \u043e\u0442\u0434\u0430\u0451\u043c \u043a\u0430\u043a \u0435\u0441\u0442\u044c."""
    c = str(code or "").strip()
    return DEPT_FULL.get(c, c)


DEPT_GEN = {
    "\u0414\u041f\u0421":  "\u0414\u043e\u0440\u043e\u0436\u043d\u043e-\u043f\u0430\u0442\u0440\u0443\u043b\u044c\u043d\u043e\u0439 \u0441\u043b\u0443\u0436\u0431\u044b",
    "\u041f\u041f\u0421":  "\u041f\u0430\u0442\u0440\u0443\u043b\u044c\u043d\u043e-\u043f\u043e\u0441\u0442\u043e\u0432\u043e\u0439 \u0441\u043b\u0443\u0436\u0431\u044b",
    "\u0421\u041c\u041f":  "\u0421\u043a\u043e\u0440\u043e\u0439 \u043c\u0435\u0434\u0438\u0446\u0438\u043d\u0441\u043a\u043e\u0439 \u043f\u043e\u043c\u043e\u0449\u0438",
    "\u0421\u0421\u041f\u041e": "\u0421\u043b\u0443\u0436\u0431\u044b \u0441\u043f\u0430\u0441\u0435\u043d\u0438\u044f \u0438 \u043f\u043e\u0436\u0430\u0440\u043d\u043e\u0439 \u043e\u0445\u0440\u0430\u043d\u044b",
    "\u0421\u041a":   "\u0421\u043b\u0435\u0434\u0441\u0442\u0432\u0435\u043d\u043d\u043e\u0433\u043e \u043a\u043e\u043c\u0438\u0442\u0435\u0442\u0430",
    "\u0415\u0414\u0421":  "\u0415\u0434\u0438\u043d\u043e\u0439 \u0434\u0438\u0441\u043f\u0435\u0442\u0447\u0435\u0440\u0441\u043a\u043e\u0439 \u0441\u043b\u0443\u0436\u0431\u044b",
    "\u0413\u0414":   "\u0413\u0440\u0430\u0436\u0434\u0430\u043d\u0441\u043a\u043e\u0433\u043e \u0434\u0435\u043f\u0430\u0440\u0442\u0430\u043c\u0435\u043d\u0442\u0430",
}

DEPT_PRE = {
    "\u0414\u041f\u0421":  "\u0414\u043e\u0440\u043e\u0436\u043d\u043e-\u043f\u0430\u0442\u0440\u0443\u043b\u044c\u043d\u043e\u0439 \u0441\u043b\u0443\u0436\u0431\u0435",
    "\u041f\u041f\u0421":  "\u041f\u0430\u0442\u0440\u0443\u043b\u044c\u043d\u043e-\u043f\u043e\u0441\u0442\u043e\u0432\u043e\u0439 \u0441\u043b\u0443\u0436\u0431\u0435",
    "\u0421\u041c\u041f":  "\u0421\u043a\u043e\u0440\u043e\u0439 \u043c\u0435\u0434\u0438\u0446\u0438\u043d\u0441\u043a\u043e\u0439 \u043f\u043e\u043c\u043e\u0449\u0438",
    "\u0421\u0421\u041f\u041e": "\u0421\u043b\u0443\u0436\u0431\u0435 \u0441\u043f\u0430\u0441\u0435\u043d\u0438\u044f \u0438 \u043f\u043e\u0436\u0430\u0440\u043d\u043e\u0439 \u043e\u0445\u0440\u0430\u043d\u044b",
    "\u0421\u041a":   "\u0421\u043b\u0435\u0434\u0441\u0442\u0432\u0435\u043d\u043d\u043e\u043c \u043a\u043e\u043c\u0438\u0442\u0435\u0442\u0435",
    "\u0415\u0414\u0421":  "\u0415\u0434\u0438\u043d\u043e\u0439 \u0434\u0438\u0441\u043f\u0435\u0442\u0447\u0435\u0440\u0441\u043a\u043e\u0439 \u0441\u043b\u0443\u0436\u0431\u0435",
    "\u0413\u0414":   "\u0413\u0440\u0430\u0436\u0434\u0430\u043d\u0441\u043a\u043e\u043c \u0434\u0435\u043f\u0430\u0440\u0442\u0430\u043c\u0435\u043d\u0442\u0435",
}


def dept_gen(code):
    """\u0420\u043e\u0434\u0438\u0442\u0435\u043b\u044c\u043d\u044b\u0439 \u043f\u0430\u0434\u0435\u0436: \u043a\u043e\u043c\u0430\u043d\u0434\u0438\u0440 \u0427\u0415\u0413\u041e."""
    c = str(code or "").strip()
    return DEPT_GEN.get(c, DEPT_FULL.get(c, c))


def dept_pre(code):
    """\u041f\u0440\u0435\u0434\u043b\u043e\u0436\u043d\u044b\u0439 \u043f\u0430\u0434\u0435\u0436: \u043d\u0430\u0441\u0442\u0430\u0432\u043d\u0438\u0447\u0435\u0441\u0442\u0432\u043e \u0412 \u0427\u0401\u041c."""
    c = str(code or "").strip()
    return DEPT_PRE.get(c, DEPT_FULL.get(c, c))


MENTOR_CHANNEL_ID = int(os.getenv("MENTOR_CHANNEL_ID", "0")) or COMMAND_CHANNEL_ID


REQUEST_CHANNEL_ID = int(os.getenv("REQUEST_CHANNEL_ID", "0"))


async def notify_dept_request(kind, player, player_disc="", target_dept="", from_dept="", cmd_role=""):
    """\u041f\u043e\u0441\u0442\u0443\u043f\u0438\u043b \u0437\u0430\u043f\u0440\u043e\u0441 \u043d\u0430 \u043f\u0435\u0440\u0435\u0432\u043e\u0434 \u0438\u043b\u0438 \u043e\u0431\u044a\u0435\u0434\u0438\u043d\u0435\u043d\u0438\u0435 \u2014 \u0437\u043e\u0432\u0451\u043c \u043a\u043e\u043c\u0430\u043d\u0434\u043e\u0432\u0430\u043d\u0438\u0435 \u0446\u0435\u043b\u0435\u0432\u043e\u0433\u043e \u0434\u0435\u043f\u0430."""
    guild = bot.get_guild(GUILD_ID)
    if not guild:
        return
    ch = guild.get_channel(REQUEST_CHANNEL_ID)
    if not ch:
        log.warning(f"notify_dept_request: \u043a\u0430\u043d\u0430\u043b {REQUEST_CHANNEL_ID} \u043d\u0435 \u043d\u0430\u0439\u0434\u0435\u043d")
        return
    who = f"<@{player_disc}>" if player_disc else f"**{player}**"
    ping = f"<@&{cmd_role}>" if cmd_role else "\u041a\u043e\u043c\u0430\u043d\u0434\u043e\u0432\u0430\u043d\u0438\u0435"
    word = "\u043f\u0435\u0440\u0435\u0432\u043e\u0434" if kind == "transfer" else "\u043e\u0431\u044a\u0435\u0434\u0438\u043d\u0435\u043d\u0438\u0435"
    dep = dept_full(target_dept)
    msg = f"{ping}, \u043d\u043e\u0432\u044b\u0439 \u0437\u0430\u043f\u0440\u043e\u0441 \u043d\u0430 {word} \u0432 {dep} \u043e\u0442 {who}"
    try:
        await ch.send(msg,
                      allowed_mentions=discord.AllowedMentions(roles=True, users=True, everyone=False))
        log.info(f"dept request notified: {player} -> {target_dept}")
    except Exception as e:
        log.error(f"notify_dept_request error: {e}")


async def notify_dept_request_decided(kind, player, player_disc="", target_dept="",
                                      by="", by_disc="", approved=True):
    """\u0420\u0435\u0448\u0435\u043d\u0438\u0435 \u043f\u043e \u0437\u0430\u043f\u0440\u043e\u0441\u0443 \u043d\u0430 \u043f\u0435\u0440\u0435\u0432\u043e\u0434/\u043e\u0431\u044a\u0435\u0434\u0438\u043d\u0435\u043d\u0438\u0435."""
    guild = bot.get_guild(GUILD_ID)
    if not guild:
        return
    ch = guild.get_channel(REQUEST_CHANNEL_ID)
    if not ch:
        return
    who = f"<@{player_disc}>" if player_disc else f"**{player}**"
    byw = f"<@{by_disc}>" if by_disc else f"**{by}**"
    word = "\u043f\u0435\u0440\u0435\u0432\u043e\u0434" if kind == "transfer" else "\u043e\u0431\u044a\u0435\u0434\u0438\u043d\u0435\u043d\u0438\u0435"
    dep = dept_full(target_dept)
    verdict = "\u043f\u0440\u0438\u043d\u044f\u0442" if approved else "\u043e\u0442\u043a\u043b\u043e\u043d\u0451\u043d"
    msg = f"{who}, \u0432\u0430\u0448 \u0437\u0430\u043f\u0440\u043e\u0441 \u043d\u0430 {word} \u0432 {dep} {verdict}. \u0418\u043d\u0438\u0446\u0438\u0430\u0442\u043e\u0440: {byw}"
    try:
        await ch.send(msg,
                      allowed_mentions=discord.AllowedMentions(roles=False, users=True, everyone=False))
        log.info(f"dept request decided: {player} {target_dept} approved={approved}")
    except Exception as e:
        log.error(f"notify_dept_request_decided error: {e}")


async def notify_mentor_graduated(graduates):
    """\u0412\u044b\u043f\u0443\u0441\u043a \u0441\u0442\u0430\u0436\u0451\u0440\u043e\u0432 \u0438\u0437 \u043e\u0431\u0443\u0447\u0435\u043d\u0438\u044f. graduates = [{trainee, traineeDisc, mentor, mentorDisc, dept}]"""
    guild = bot.get_guild(GUILD_ID)
    if not guild:
        return
    ch = guild.get_channel(MENTOR_CHANNEL_ID)
    if not ch:
        return
    for g in graduates or []:
        t = f"<@{g.get('traineeDisc')}>" if g.get("traineeDisc") else f"**{g.get('trainee','')}**"
        dept = dept_full(g.get("dept", ""))
        lines = [f"\U0001f393 {t}, \u043f\u043e\u0437\u0434\u0440\u0430\u0432\u043b\u044f\u0435\u043c \u0441 \u0432\u044b\u043f\u0443\u0441\u043a\u043e\u043c \u0438\u0437 \u043e\u0431\u0443\u0447\u0435\u043d\u0438\u044f"
                 + (f" \u2014 **{dept}**!" if dept else "!"),
                 "\u0421\u0442\u0430\u0436\u0438\u0440\u043e\u0432\u043a\u0430 \u043f\u043e\u0437\u0430\u0434\u0438 \u2014 \u0442\u0435\u043f\u0435\u0440\u044c \u043f\u043e\u043b\u043d\u043e\u0446\u0435\u043d\u043d\u044b\u0439 \u0441\u043e\u0442\u0440\u0443\u0434\u043d\u0438\u043a."]
        try:
            await ch.send("\n".join(lines),
                          allowed_mentions=discord.AllowedMentions(users=True, roles=False, everyone=False))
        except Exception as e:
            log.error(f"notify_mentor_graduated error: {e}")
    log.info(f"graduations notified: {len(graduates or [])}")


async def notify_mentor_invite(mentor, mentor_disc="", trainee="", trainee_disc="", dept=""):
    """\u041c\u0435\u043d\u0442\u043e\u0440 \u043f\u043e\u0437\u0432\u0430\u043b \u0441\u0442\u0430\u0436\u0451\u0440\u0430 \u043f\u043e\u0434 \u043a\u0440\u044b\u043b\u043e."""
    dept_code = dept
    deptg = dept_gen(dept_code)
    deptp = dept_pre(dept_code)
    dept  = dept_full(dept_code)
    guild = bot.get_guild(GUILD_ID)
    if not guild:
        return
    ch = guild.get_channel(MENTOR_CHANNEL_ID)
    if not ch:
        return
    m = f"<@{mentor_disc}>" if mentor_disc else f"**{mentor}**"
    t = f"<@{trainee_disc}>" if trainee_disc else f"**{trainee}**"
    msg = (f"\U0001f91d {m} \u043e\u0442\u043f\u0440\u0430\u0432\u0438\u043b \u043f\u0440\u0438\u0433\u043b\u0430\u0448\u0435\u043d\u0438\u0435 \u043d\u0430 \u043d\u0430\u0441\u0442\u0430\u0432\u043d\u0438\u0447\u0435\u0441\u0442\u0432\u043e"
           + (f" \u0432 **{deptp}**" if dept else "") + f" \u2014 {t}")
    msg += "\n\u041f\u0440\u0438\u043d\u044f\u0442\u044c \u0438\u043b\u0438 \u043e\u0442\u043a\u043b\u043e\u043d\u0438\u0442\u044c \u043c\u043e\u0436\u043d\u043e \u0432\u043e \u0432\u043a\u043b\u0430\u0434\u043a\u0435 \xab\u041e\u0431\u0443\u0447\u0435\u043d\u0438\u0435\xbb \u043d\u0430 \u0441\u0430\u0439\u0442\u0435."
    try:
        await ch.send(msg, allowed_mentions=discord.AllowedMentions(users=True, roles=False, everyone=False))
        log.info(f"mentor invite notified: {mentor} -> {trainee}")
    except Exception as e:
        log.error(f"notify_mentor_invite error: {e}")


async def notify_mentor_accepted(mentor, mentor_disc="", trainee="", trainee_disc="", dept=""):
    """\u0421\u0442\u0430\u0436\u0451\u0440 \u043f\u0440\u0438\u043d\u044f\u043b \u043f\u0440\u0438\u0433\u043b\u0430\u0448\u0435\u043d\u0438\u0435."""
    dept_code = dept
    deptg = dept_gen(dept_code)
    deptp = dept_pre(dept_code)
    dept  = dept_full(dept_code)
    guild = bot.get_guild(GUILD_ID)
    if not guild:
        return
    ch = guild.get_channel(MENTOR_CHANNEL_ID)
    if not ch:
        return
    m = f"<@{mentor_disc}>" if mentor_disc else f"**{mentor}**"
    t = f"<@{trainee_disc}>" if trainee_disc else f"**{trainee}**"
    msg = (f"\u2705 {t} \u043f\u0440\u0438\u043d\u044f\u043b \u043f\u0440\u0438\u0433\u043b\u0430\u0448\u0435\u043d\u0438\u0435 \u043d\u0430 \u043d\u0430\u0441\u0442\u0430\u0432\u043d\u0438\u0447\u0435\u0441\u0442\u0432\u043e \u043e\u0442 {m}"
           + (f" \u2014 **{dept}**" if dept else ""))
    msg += "\n\u0423\u0434\u0430\u0447\u0438 \u0432 \u043e\u0431\u0443\u0447\u0435\u043d\u0438\u0438!"
    try:
        await ch.send(msg, allowed_mentions=discord.AllowedMentions(users=True, roles=False, everyone=False))
        log.info(f"mentor accepted notified: {trainee} <- {mentor}")
    except Exception as e:
        log.error(f"notify_mentor_accepted error: {e}")


async def notify_election_turnout_full(dept, voted=0, eligible=0, runoff=False, deadline="", url=""):
    """\u041f\u0440\u043e\u0433\u043e\u043b\u043e\u0441\u043e\u0432\u0430\u043b\u0438 \u0432\u0441\u0435 \u2014 \u0442\u0443\u0440 \u043c\u043e\u0436\u043d\u043e \u0437\u0430\u043a\u0440\u044b\u0432\u0430\u0442\u044c, \u043d\u0435 \u0434\u043e\u0436\u0438\u0434\u0430\u044f\u0441\u044c \u0441\u0440\u043e\u043a\u0430."""
    dept_code = dept
    deptg = dept_gen(dept_code)
    deptp = dept_pre(dept_code)
    dept  = dept_full(dept_code)
    guild = bot.get_guild(GUILD_ID)
    if not guild:
        return
    ch = guild.get_channel(COMPLAINT_CHANNEL_ID)
    if not ch:
        return
    ping = f"<@&{ADMIN_ROLE_ID}> " if ADMIN_ROLE_ID else ""
    head = "\u0432\u0442\u043e\u0440\u043e\u0433\u043e \u0442\u0443\u0440\u0430" if runoff else "\u0433\u043e\u043b\u043e\u0441\u043e\u0432\u0430\u043d\u0438\u044f"
    lines = [f"{ping}\u2705 **\u042f\u0432\u043a\u0430 100% \u2014 \u043a\u043e\u043c\u0430\u043d\u0434\u0438\u0440 {deptg}**",
             f"\u041f\u0440\u043e\u0433\u043e\u043b\u043e\u0441\u043e\u0432\u0430\u043b\u0438 \u0432\u0441\u0435: **{voted} \u0438\u0437 {eligible}**",
             f"\u0416\u0434\u0430\u0442\u044c \u043e\u043a\u043e\u043d\u0447\u0430\u043d\u0438\u044f {head} \u0431\u043e\u043b\u044c\u0448\u0435 \u043d\u0435\u0437\u0430\u0447\u0435\u043c \u2014 \u0442\u0443\u0440 \u043c\u043e\u0436\u043d\u043e \u0437\u0430\u043a\u0440\u044b\u0442\u044c \u0434\u043e\u0441\u0440\u043e\u0447\u043d\u043e."]
    if deadline:
        lines.append(f"\u0421\u0440\u043e\u043a \u0441\u0442\u043e\u044f\u043b \u0434\u043e: {deadline}")
    if url:
        lines.append(url)
    try:
        await ch.send("\n".join(lines))
        log.info(f"turnout full notified: {dept}")
    except Exception as e:
        log.error(f"notify_election_turnout_full error: {e}")


async def edit_vote_turnout(msg_id, voted=0, eligible=0, percent=0):
    """\u041e\u0431\u043d\u043e\u0432\u0438\u0442\u044c \u0441\u0442\u0440\u043e\u043a\u0443 \u044f\u0432\u043a\u0438 \u0432 \u043e\u0431\u044a\u044f\u0432\u043b\u0435\u043d\u0438\u0438 \u043e \u0433\u043e\u043b\u043e\u0441\u043e\u0432\u0430\u043d\u0438\u0438."""
    if not msg_id:
        return
    guild = bot.get_guild(GUILD_ID)
    if not guild:
        return
    ch = guild.get_channel(COMMAND_CHANNEL_ID)
    if not ch:
        return
    try:
        msg = await ch.fetch_message(int(msg_id))
    except Exception as e:
        log.warning(f"edit_vote_turnout: message {msg_id} not found: {e}")
        return
    line = (f"\u042f\u0432\u043a\u0430: **{voted} \u0438\u0437 {eligible}** ({percent}%)" if eligible
            else f"\u042f\u0432\u043a\u0430: **{voted}**")
    keep = [l for l in msg.content.split("\n") if not l.startswith("\u042f\u0432\u043a\u0430:")]
    try:
        await msg.edit(content="\n".join(keep).rstrip() + "\n" + line)
    except Exception as e:
        log.warning(f"edit_vote_turnout failed for {msg_id}: {e}")


async def notify_election_early_close(dept, deadline="", votes=0, eligible=0, percent=0, by="", runoff=False):
    """\u0413\u043e\u043b\u043e\u0441\u043e\u0432\u0430\u043d\u0438\u0435 \u0437\u0430\u043a\u0440\u044b\u0442\u043e \u0440\u0430\u043d\u044c\u0448\u0435 \u0441\u0440\u043e\u043a\u0430 \u2014 \u0441\u043e\u043e\u0431\u0449\u0430\u0435\u043c \u0430\u0434\u043c\u0438\u043d\u0438\u0441\u0442\u0440\u0430\u0446\u0438\u0438."""
    dept_code = dept
    deptg = dept_gen(dept_code)
    deptp = dept_pre(dept_code)
    dept  = dept_full(dept_code)
    guild = bot.get_guild(GUILD_ID)
    if not guild:
        return
    ch = guild.get_channel(COMPLAINT_CHANNEL_ID)
    if not ch:
        return
    ping = f"<@&{ADMIN_ROLE_ID}> " if ADMIN_ROLE_ID else ""
    head = "\u0412\u0442\u043e\u0440\u043e\u0439 \u0442\u0443\u0440" if runoff else "\u0413\u043e\u043b\u043e\u0441\u043e\u0432\u0430\u043d\u0438\u0435"
    lines = [f"{ping}\u26a1 **{head} \u0437\u0430\u0432\u0435\u0440\u0448\u0435\u043d\u043e \u0434\u043e\u0441\u0440\u043e\u0447\u043d\u043e \u2014 {dept}**"]
    if deadline:
        lines.append(f"\u041f\u043b\u0430\u043d\u0438\u0440\u043e\u0432\u0430\u043b\u043e\u0441\u044c \u0434\u043e: {deadline}")
    lines.append(f"\u0418\u0442\u043e\u0433\u043e\u0432\u0430\u044f \u044f\u0432\u043a\u0430: **{votes} \u0438\u0437 {eligible}** ({percent}%)")
    if by:
        lines.append(f"\u0417\u0430\u043a\u0440\u044b\u043b: {by}")
    try:
        await ch.send("\n".join(lines))
        log.info(f"early close notified: {dept}")
    except Exception as e:
        log.error(f"notify_election_early_close error: {e}")


async def notify_election_apply_extended(dept, deadline="", url="", dept_roles=None):
    """\u0421\u0440\u043e\u043a \u043f\u043e\u0434\u0430\u0447\u0438 \u0437\u0430\u044f\u0432\u043e\u043a \u043f\u0440\u043e\u0434\u043b\u0451\u043d."""
    dept_code = dept
    deptg = dept_gen(dept_code)
    deptp = dept_pre(dept_code)
    dept  = dept_full(dept_code)
    guild = bot.get_guild(GUILD_ID)
    if not guild:
        return
    ch = guild.get_channel(COMMAND_CHANNEL_ID)
    if not ch:
        return
    pings = " ".join(f"<@&{r}>" for r in (dept_roles or []) if r)
    lines = [pings.strip(),
             f"\U0001f552 **\u041f\u0440\u0438\u0451\u043c \u0437\u0430\u044f\u0432\u043e\u043a \u043f\u0440\u043e\u0434\u043b\u0451\u043d \u2014 \u043a\u043e\u043c\u0430\u043d\u0434\u0438\u0440 {deptg}**",
             "\u0415\u0449\u0451 \u0435\u0441\u0442\u044c \u0432\u0440\u0435\u043c\u044f \u0432\u044b\u0434\u0432\u0438\u043d\u0443\u0442\u044c \u0441\u0432\u043e\u044e \u043a\u0430\u043d\u0434\u0438\u0434\u0430\u0442\u0443\u0440\u0443."]
    if deadline:
        lines.append(f"\u041d\u043e\u0432\u044b\u0439 \u0441\u0440\u043e\u043a: **{deadline}**")
    if url:
        lines.append(f"\u041f\u043e\u0434\u0430\u0442\u044c \u0437\u0430\u044f\u0432\u043a\u0443: {url}")
    try:
        await ch.send("\n".join([l for l in lines if l]),
                      allowed_mentions=discord.AllowedMentions(roles=True, users=True, everyone=False))
        log.info(f"apply extended notified: {dept}")
    except Exception as e:
        log.error(f"notify_election_apply_extended error: {e}")


async def notify_election_candidate_removed(dept, candidate, cand_disc="", reason="", voting=False, dept_roles=None):
    """\u041f\u0443\u0431\u043b\u0438\u0447\u043d\u043e \u0441\u043e\u0442\u0440\u0443\u0434\u043d\u0438\u043a\u0430\u043c \u0434\u0435\u043f\u0430: \u043a\u0430\u043d\u0434\u0438\u0434\u0430\u0442 \u0441\u043d\u044f\u0442 \u0441 \u0432\u044b\u0431\u043e\u0440\u043e\u0432."""
    dept_code = dept
    deptg = dept_gen(dept_code)
    deptp = dept_pre(dept_code)
    dept  = dept_full(dept_code)
    guild = bot.get_guild(GUILD_ID)
    if not guild:
        return
    ch = guild.get_channel(COMMAND_CHANNEL_ID)
    if not ch:
        return
    pings = " ".join(f"<@&{r}>" for r in (dept_roles or []) if r)
    who = f"<@{cand_disc}> (**{candidate}**)" if cand_disc else f"**{candidate}**"
    lines = [pings.strip(),
             f"\u26d4 **\u041a\u0430\u043d\u0434\u0438\u0434\u0430\u0442 \u0441\u043d\u044f\u0442 \u0441 \u0432\u044b\u0431\u043e\u0440\u043e\u0432 \u043a\u043e\u043c\u0430\u043d\u0434\u0438\u0440\u0430 {deptg}**",
             f"\u0421\u043d\u044f\u0442: {who}"]
    if reason:
        lines.append(f"\u041f\u0440\u0438\u0447\u0438\u043d\u0430: {reason}")
    if voting:
        lines.append("\u0413\u043e\u043b\u043e\u0441\u043e\u0432\u0430\u043d\u0438\u0435 \u043f\u0440\u043e\u0434\u043e\u043b\u0436\u0430\u0435\u0442\u0441\u044f, \u0433\u043e\u043b\u043e\u0441\u0430 \u0437\u0430 \u043d\u0435\u0433\u043e \u043d\u0435 \u0443\u0447\u0438\u0442\u044b\u0432\u0430\u044e\u0442\u0441\u044f.")
    try:
        await ch.send("\n".join([l for l in lines if l]),
                      allowed_mentions=discord.AllowedMentions(roles=True, users=True, everyone=False))
        log.info(f"candidate removed notified: {dept} {candidate}")
    except Exception as e:
        log.error(f"notify_election_candidate_removed error: {e}")


async def notify_election_vote_open(dept, deadline="", runoff=False, url="", dept_roles=None, candidates=None, eligible=0):
    """\u041e\u0431\u044a\u044f\u0432\u043b\u0435\u043d\u0438\u0435 \u043e \u0441\u0442\u0430\u0440\u0442\u0435 \u0433\u043e\u043b\u043e\u0441\u043e\u0432\u0430\u043d\u0438\u044f \u2014 \u0430\u0434\u0440\u0435\u0441\u043d\u043e \u0440\u043e\u043b\u044f\u043c \u0434\u0435\u043f\u0430\u0440\u0442\u0430\u043c\u0435\u043d\u0442\u0430."""
    dept_code = dept
    deptg = dept_gen(dept_code)
    deptp = dept_pre(dept_code)
    dept  = dept_full(dept_code)
    guild = bot.get_guild(GUILD_ID)
    if not guild:
        return
    ch = guild.get_channel(COMMAND_CHANNEL_ID)
    if not ch:
        return
    pings = " ".join(f"<@&{r}>" for r in (dept_roles or []) if r)
    head = "\u0412\u0442\u043e\u0440\u043e\u0439 \u0442\u0443\u0440 \u0433\u043e\u043b\u043e\u0441\u043e\u0432\u0430\u043d\u0438\u044f" if runoff else "\u041d\u0430\u0447\u0430\u043b\u043e\u0441\u044c \u0433\u043e\u043b\u043e\u0441\u043e\u0432\u0430\u043d\u0438\u0435"
    lines = [f"{pings}".strip(),
             f"\U0001f5f3 **{head}: \u043a\u043e\u043c\u0430\u043d\u0434\u0438\u0440 {deptg}**"]
    if candidates:
        lines.append("")
        lines.append("**\u0414\u043e\u043f\u0443\u0449\u0435\u043d\u043d\u044b\u0435 \u043a\u0430\u043d\u0434\u0438\u0434\u0430\u0442\u044b:**")
        for i, nm in enumerate(candidates, 1):
            lines.append(f"{i}. {nm}")
        lines.append("")
    if url:
        lines.append(f"\u0413\u043e\u043b\u043e\u0441\u043e\u0432\u0430\u0442\u044c: {url}")
    if deadline:
        lines.append(f"\u0413\u043e\u043b\u043e\u0441\u043e\u0432\u0430\u043d\u0438\u0435 \u0434\u043e: **{deadline}**")
    lines.append("\u0413\u043e\u043b\u043e\u0441 \u043e\u0442\u0434\u0430\u0451\u0442\u0441\u044f \u043e\u0434\u0438\u043d \u0440\u0430\u0437 \u0438 \u0438\u0437\u043c\u0435\u043d\u0438\u0442\u044c \u0435\u0433\u043e \u043d\u0435\u043b\u044c\u0437\u044f.")
    lines.append(f"\u042f\u0432\u043a\u0430: **0 \u0438\u0437 {eligible}** (0%)" if eligible else "\u042f\u0432\u043a\u0430: **0**")
    try:
        sent = await ch.send("\n".join([l for l in lines if l]),
                             allowed_mentions=discord.AllowedMentions(roles=True, users=True, everyone=False))
        log.info(f"vote open notified: {dept} runoff={runoff}")
        return sent.id
    except Exception as e:
        log.error(f"notify_election_vote_open error: {e}")
        return None


async def notify_election_result(dept, winner, winner_disc="", votes=0, total=0, round_no=1, dept_roles=None):
    """\u041e\u0444\u0438\u0446\u0438\u0430\u043b\u044c\u043d\u043e\u0435 \u043e\u0431\u044a\u044f\u0432\u043b\u0435\u043d\u0438\u0435 \u043f\u043e\u0431\u0435\u0434\u0438\u0442\u0435\u043b\u044f \u0432\u044b\u0431\u043e\u0440\u043e\u0432."""
    dept_code = dept
    deptg = dept_gen(dept_code)
    deptp = dept_pre(dept_code)
    dept  = dept_full(dept_code)
    guild = bot.get_guild(GUILD_ID)
    if not guild:
        return
    ch = guild.get_channel(COMMAND_CHANNEL_ID)
    if not ch:
        return
    pings = " ".join(f"<@&{r}>" for r in (dept_roles or []) if r)
    who = f"<@{winner_disc}>" if winner_disc else f"**{winner}**"
    if winner_disc and winner:
        who = f"<@{winner_disc}> (**{winner}**)"
    tail = ""
    if total:
        pct = round(votes * 100 / total)
        tail = f"\n\u0413\u043e\u043b\u043e\u0441\u043e\u0432: **{votes}** \u0438\u0437 {total} ({pct}%)"
        if round_no > 1:
            tail += " \u2014 \u043f\u043e \u0438\u0442\u043e\u0433\u0430\u043c \u0432\u0442\u043e\u0440\u043e\u0433\u043e \u0442\u0443\u0440\u0430"
    lines = [pings.strip(),
             f"\U0001f3c6 **\u0412\u044b\u0431\u043e\u0440\u044b \u043a\u043e\u043c\u0430\u043d\u0434\u0438\u0440\u0430 {deptg} \u0437\u0430\u0432\u0435\u0440\u0448\u0435\u043d\u044b**",
             f"\u041f\u043e\u0431\u0435\u0434\u0438\u0442\u0435\u043b\u044c: {who}{tail}",
             "\u041f\u043e\u0437\u0434\u0440\u0430\u0432\u043b\u044f\u0435\u043c!"]
    try:
        await ch.send("\n".join([l for l in lines if l]),
                      allowed_mentions=discord.AllowedMentions(roles=True, users=True, everyone=False))
        log.info(f"election result notified: {dept} -> {winner}")
    except Exception as e:
        log.error(f"notify_election_result error: {e}")


async def edit_application_notice(msg_id, status, by="", reason=""):
    """\u0414\u043e\u043f\u0438\u0441\u0430\u0442\u044c/\u043e\u0431\u043d\u043e\u0432\u0438\u0442\u044c \u0441\u0442\u0440\u043e\u043a\u0443 \u0441\u0442\u0430\u0442\u0443\u0441\u0430 \u0432 \u0441\u043e\u043e\u0431\u0449\u0435\u043d\u0438\u0438 \u043e \u0437\u0430\u044f\u0432\u043a\u0435."""
    if not msg_id:
        return
    guild = bot.get_guild(GUILD_ID)
    if not guild:
        return
    ch = guild.get_channel(COMPLAINT_CHANNEL_ID)
    if not ch:
        return
    try:
        msg = await ch.fetch_message(int(msg_id))
    except Exception as e:
        log.warning(f"edit_application_notice: message {msg_id} not found: {e}")
        return

    if status == "approved":
        line = "\u0421\u0442\u0430\u0442\u0443\u0441: \u2705 **\u041e\u0434\u043e\u0431\u0440\u0435\u043d\u0430**"
        if by:
            line += f" \u2014 {by}"
    elif status == "rejected":
        line = "\u0421\u0442\u0430\u0442\u0443\u0441: \u274c **\u041e\u0442\u043a\u043b\u043e\u043d\u0435\u043d\u0430**"
        if by:
            line += f" \u2014 {by}"
        if reason:
            line += f"\n\u041f\u0440\u0438\u0447\u0438\u043d\u0430: {reason}"
    elif status == "withdrawn":
        line = "\u0421\u0442\u0430\u0442\u0443\u0441: \U0001f6d1 **\u041e\u0442\u043e\u0437\u0432\u0430\u043d\u0430 \u043a\u0430\u043d\u0434\u0438\u0434\u0430\u0442\u043e\u043c**"
        if reason and reason != "0":
            line += f" (\u0448\u0442\u0440\u0430\u0444 {reason} XP)"
    elif status == "disqualified":
        line = "\u0421\u0442\u0430\u0442\u0443\u0441: \u26d4 **\u0421\u043d\u044f\u0442\u0430** (\u043f\u043e\u0431\u0435\u0434\u0430 \u0432 \u0434\u0440\u0443\u0433\u043e\u043c \u0434\u0435\u043f\u0430\u0440\u0442\u0430\u043c\u0435\u043d\u0442\u0435)"
        if by:
            line += f" \u2014 {by}"
    else:
        return

    # \u0432\u044b\u043a\u0438\u0434\u044b\u0432\u0430\u0435\u043c \u043f\u0440\u0435\u0436\u043d\u0438\u0439 \u0441\u0442\u0430\u0442\u0443\u0441, \u0447\u0442\u043e\u0431\u044b \u043d\u0435 \u043a\u043e\u043f\u0438\u043b\u0441\u044f
    keep, skip = [], False
    for ln in msg.content.split("\n"):
        if ln.startswith("\u0421\u0442\u0430\u0442\u0443\u0441:"):
            skip = True
            continue
        if skip and ln.startswith("\u041f\u0440\u0438\u0447\u0438\u043d\u0430:"):
            continue
        skip = False
        keep.append(ln)
    body = "\n".join(keep).rstrip()

    try:
        await msg.edit(content=body + "\n" + line)
        log.info(f"application notice edited: {msg_id} -> {status}")
    except Exception as e:
        log.warning(f"edit_application_notice failed for {msg_id}: {e}")


async def notify_election_new_application(dept, candidate, candidate_disc="", goals=0, reapplied=False, url=""):
    """\u0421\u043e\u043e\u0431\u0449\u0435\u043d\u0438\u0435 \u0430\u0434\u043c\u0438\u043d\u0438\u0441\u0442\u0440\u0430\u0446\u0438\u0438 \u043e \u043f\u043e\u0441\u0442\u0443\u043f\u0438\u0432\u0448\u0435\u0439 \u0437\u0430\u044f\u0432\u043a\u0435."""
    dept_code = dept
    deptg = dept_gen(dept_code)
    deptp = dept_pre(dept_code)
    dept  = dept_full(dept_code)
    guild = bot.get_guild(GUILD_ID)
    if not guild:
        return
    ch = guild.get_channel(COMPLAINT_CHANNEL_ID)
    if not ch:
        return
    who = f"<@{candidate_disc}>" if candidate_disc else f"**{candidate}**"
    if candidate_disc and candidate:
        who = f"<@{candidate_disc}> ({candidate})"
    head = "\u0417\u0430\u044f\u0432\u043a\u0430 \u043f\u0435\u0440\u0435\u043f\u043e\u0434\u0430\u043d\u0430" if reapplied else "\u041d\u043e\u0432\u0430\u044f \u0437\u0430\u044f\u0432\u043a\u0430"
    ping = f"<@&{ADMIN_ROLE_ID}> " if ADMIN_ROLE_ID else ""
    lines = [f"{ping}\U0001f4dd **{head}** \u043d\u0430 \u043f\u043e\u0441\u0442 \u043a\u043e\u043c\u0430\u043d\u0434\u0438\u0440\u0430 **{deptg}**",
             f"\u041a\u0430\u043d\u0434\u0438\u0434\u0430\u0442: {who}"]
    if goals:
        lines.append(f"\u0426\u0435\u043b\u0435\u0439 \u0432 \u043f\u0440\u043e\u0433\u0440\u0430\u043c\u043c\u0435: {goals}")
    if url:
        lines.append(f"\u0420\u0430\u0441\u0441\u043c\u043e\u0442\u0440\u0435\u0442\u044c: {url}")
    try:
        sent = await ch.send("\n".join(lines))
        log.info(f"new application notified: {dept} {candidate}")
        return sent.id
    except Exception as e:
        log.error(f"notify_election_new_application error: {e}")
        return None


async def notify_election_open(dept, apply_deadline="", url="", by=""):
    dept_code = dept
    deptg = dept_gen(dept_code)
    deptp = dept_pre(dept_code)
    dept  = dept_full(dept_code)
    guild = bot.get_guild(GUILD_ID)
    if not guild:
        return
    ch = guild.get_channel(COMMAND_CHANNEL_ID)
    if not ch:
        return
    lines = ["@everyone", f"\U0001f5f3 **\u0412\u044b\u0431\u043e\u0440\u044b \u043a\u043e\u043c\u0430\u043d\u0434\u0438\u0440\u0430 {deptg}** \u2014 \u043e\u0442\u043a\u0440\u044b\u0442 \u043f\u0440\u0438\u0451\u043c \u0437\u0430\u044f\u0432\u043e\u043a!"]
    if url:
        lines.append(f"\u041f\u043e\u0434\u0430\u0442\u044c \u0437\u0430\u044f\u0432\u043a\u0443: {url}")
    if apply_deadline:
        lines.append(f"\u041f\u0440\u0438\u0451\u043c \u0437\u0430\u044f\u0432\u043e\u043a \u0434\u043e: **{apply_deadline}**")
    lines.append("\u0417\u0430\u044f\u0432\u043a\u0430 \u0437\u0430\u043f\u043e\u043b\u043d\u044f\u0435\u0442\u0441\u044f \u043f\u043e \u0441\u0438\u0441\u0442\u0435\u043c\u0435 SMART. \u0413\u043e\u043b\u043e\u0441\u0443\u044e\u0442 \u0441\u043e\u0442\u0440\u0443\u0434\u043d\u0438\u043a\u0438 \u0441\u043b\u0443\u0436\u0431\u044b.")
    msg = "\n".join(lines)
    try:
        await ch.send(msg, allowed_mentions=discord.AllowedMentions(everyone=True, roles=True, users=True))
        log.info(f"election open notified: {dept}")
    except Exception as e:
        log.error(f"notify_election_open error: {e}")


async def notify_noreelect_cleared(target, discord="", by=""):
    guild = bot.get_guild(GUILD_ID)
    if not guild:
        return
    modch = guild.get_channel(COMPLAINT_CHANNEL_ID)
    if not modch:
        return
    who = f"<@{discord}>" if discord else f"**{target}**"
    aping = (f"<@&{ADMIN_ROLE_ID}> " if ADMIN_ROLE_ID else "") + (f"<@&{MODERATOR_ROLE_ID}> " if MODERATOR_ROLE_ID else "")
    bytxt = f" \u0421\u043d\u044f\u043b: {by}." if by else ""
    msg = f"{aping}\u2705 \u0421 \u0438\u0433\u0440\u043e\u043a\u0430 {who} \u0441\u043d\u044f\u0442\u0430 \u043c\u0435\u0442\u043a\u0430 \u00ab\u0431\u0435\u0437 \u043f\u0440\u0430\u0432\u0430 \u043f\u043e\u0432\u0442\u043e\u0440\u043d\u043e\u0433\u043e \u0438\u0437\u0431\u0440\u0430\u043d\u0438\u044f\u00bb.{bytxt}"
    try:
        await modch.send(msg)
        log.info("noreelect cleared notified")
    except Exception as e:
        log.error(f"notify_noreelect_cleared error: {e}")


async def notify_command_removed(target, dept, discord="", dept_role="", reason="", no_reelect=False):
    dept_code = dept
    deptg = dept_gen(dept_code)
    deptp = dept_pre(dept_code)
    dept  = dept_full(dept_code)
    guild = bot.get_guild(GUILD_ID)
    if not guild:
        return
    who = f"<@{discord}>" if discord else f"**{target}**"
    ping = f"\n<@&{dept_role}>" if dept_role else ""
    post = "\u0441\u0443\u043f\u0435\u0440\u0432\u0430\u0439\u0437\u0435\u0440\u0430" if dept == "\u0413\u0414" else "\u043a\u043e\u043c\u0430\u043d\u0434\u0438\u0440\u0430"
    reason = (reason or "").strip()
    # message tail: reason when present, plus the no-re-election flag
    if reason:
        tail = f". \u041e\u0441\u043d\u043e\u0432\u0430\u043d\u0438\u0435: {reason}"
    else:
        tail = " \u043f\u043e \u043e\u043a\u043e\u043d\u0447\u0430\u043d\u0438\u0438 \u043f\u043e\u043b\u043d\u043e\u043c\u043e\u0447\u0438\u0439. \u0411\u043b\u0430\u0433\u043e\u0434\u0430\u0440\u0438\u043c \u0437\u0430 \u0441\u043b\u0443\u0436\u0431\u0443"
    if no_reelect:
        tail += ". \u0411\u0435\u0437 \u043f\u0440\u0430\u0432\u0430 \u043f\u043e\u0432\u0442\u043e\u0440\u043d\u043e\u0433\u043e \u0438\u0437\u0431\u0440\u0430\u043d\u0438\u044f"
    body = f"\u2796 {who} \u0441\u043d\u044f\u0442 \u0441 \u0434\u043e\u043b\u0436\u043d\u043e\u0441\u0442\u0438 **{post} {deptg}**{tail}."
    ch = guild.get_channel(COMMAND_CHANNEL_ID)
    if ch:
        try:
            await ch.send(body + ping)
            log.info("command removal notified")
        except Exception as e:
            log.error(f"notify_command_removed error: {e}")
    modch = guild.get_channel(COMPLAINT_CHANNEL_ID)
    if modch:
        aping = (f"<@&{ADMIN_ROLE_ID}> " if ADMIN_ROLE_ID else "") + (f"<@&{MODERATOR_ROLE_ID}> " if MODERATOR_ROLE_ID else "")
        try:
            await modch.send(aping + body)
            log.info("command removal notified (mod)")
        except Exception as e:
            log.error(f"notify_command_removed mod error: {e}")


async def notify_acting_set(target, dept, discord="", dept_role=""):
    dept_code = dept
    deptg = dept_gen(dept_code)
    deptp = dept_pre(dept_code)
    dept  = dept_full(dept_code)
    guild = bot.get_guild(GUILD_ID)
    if not guild:
        return
    modch = guild.get_channel(COMPLAINT_CHANNEL_ID)
    if not modch:
        return
    who = f"<@{discord}>" if discord else f"**{target}**"
    aping = (f"<@&{ADMIN_ROLE_ID}> " if ADMIN_ROLE_ID else "") + (f"<@&{MODERATOR_ROLE_ID}> " if MODERATOR_ROLE_ID else "")
    mmsg = (f"{aping}\U0001f536 {who} \u043d\u0430\u0437\u043d\u0430\u0447\u0435\u043d **\u0412\u0420\u0418\u041e \u043a\u043e\u043c\u0430\u043d\u0434\u0438\u0440\u0430 {deptg}**.")
    try:
        await modch.send(mmsg)
        log.info("acting set notified")
    except Exception as e:
        log.error(f"notify_acting_set error: {e}")


async def notify_acting_removed(target, dept, discord="", dept_role="", reason="", no_reelect=False):
    dept_code = dept
    deptg = dept_gen(dept_code)
    deptp = dept_pre(dept_code)
    dept  = dept_full(dept_code)
    guild = bot.get_guild(GUILD_ID)
    if not guild:
        return
    modch = guild.get_channel(COMPLAINT_CHANNEL_ID)
    if not modch:
        return
    who = f"<@{discord}>" if discord else f"**{target}**"
    aping = (f"<@&{ADMIN_ROLE_ID}> " if ADMIN_ROLE_ID else "") + (f"<@&{MODERATOR_ROLE_ID}> " if MODERATOR_ROLE_ID else "")
    reason = (reason or "").strip()
    tail = f". \u041e\u0441\u043d\u043e\u0432\u0430\u043d\u0438\u0435: {reason}" if reason else ""
    if no_reelect:
        tail += ". \u0411\u0435\u0437 \u043f\u0440\u0430\u0432\u0430 \u043f\u043e\u0432\u0442\u043e\u0440\u043d\u043e\u0433\u043e \u0438\u0437\u0431\u0440\u0430\u043d\u0438\u044f"
    mmsg = f"{aping}\u2796 {who} \u0441\u043d\u044f\u0442 \u0441 **\u0412\u0420\u0418\u041e \u043a\u043e\u043c\u0430\u043d\u0434\u0438\u0440\u0430 {deptg}**{tail}."
    try:
        await modch.send(mmsg)
        log.info("acting removal notified")
    except Exception as e:
        log.error(f"notify_acting_removed error: {e}")
async def notify_level_changes(changes):
    """Постит в канал уровней сообщения о повышении/понижении. changes = [{discord, old, new}]."""
    guild = bot.get_guild(GUILD_ID)
    if not guild:
        return
    channel = guild.get_channel(LEVEL_CHANNEL_ID)
    if not channel:
        log.warning(f"notify_level_changes: канал {LEVEL_CHANNEL_ID} не найден")
        return
    for ch in changes:
        did = str(ch.get("discord", "")).strip()
        old = ch.get("old"); new = ch.get("new")
        if not did or old is None or new is None:
            continue
        who = f"<@{did}>"
        if new > old:
            text = f"📈 {who}, твой уровень повысился: **{old} → {new}**. Так держать!"
        else:
            text = f"📉 {who}, твой уровень понизился: **{old} → {new}**. Опыт выгорает — набирай активность."
        try:
            await channel.send(text)
        except discord.Forbidden:
            log.error("Нет прав писать в канал уровней (Send Messages).")
            return
        except Exception as e:
            log.error(f"notify_level_changes send ошибка: {e}")
    log.info(f"Уровни: отправлено {len(changes)} уведомлений")


@tasks.loop(minutes=1)
async def birthday_check():
    """Каждую минуту проверяет: не наступила ли 0:01 по МСК. Если да — поздравляет."""
    msk = timezone(timedelta(hours=3))
    now = datetime.now(msk)
    if now.hour != 0 or now.minute != 1:
        return  # 0:01 МСК — время поздравлений

    guild = bot.get_guild(GUILD_ID)
    if not guild:
        return
    channel = guild.get_channel(BIRTHDAY_CHANNEL_ID)
    if not channel:
        log.warning(f"birthday_check: канал {BIRTHDAY_CHANNEL_ID} не найден")
        return

    # fetch today's birthdays from the site
    try:
        r = requests.get(f"{BIRTHDAYS_URL}?key={BIRTHDAYS_KEY}", timeout=20)
        if r.status_code != 200:
            log.error(f"birthday_check: сайт вернул {r.status_code}")
            return
        people = r.json()
    except Exception as e:
        log.error(f"birthday_check: ошибка запроса — {e}")
        return

    if not people:
        return  # сегодня именинников нет

    for p in people:
        did  = p.get("discord_id", "")
        name = p.get("name", "")
        who  = f"<@{did}>" if did else f"**{name}**"
        greetings = [
            f"С днём рождения, {who}! Пусть год будет ярким, а дороги — свободными 🎂",
            f"Сегодня твой день, {who}. Желаем спокойных смен и хорошего настроения по обе стороны экрана 🎉",
            f"С днём рождения, {who}! Пусть всё задуманное сложится, а рядом будут те, кто важен 🕯️",
            f"Поздравляем, {who}! Желаем сил, тепла и поводов для радости — сегодня и весь год 🎂",
            f"С днём рождения, {who}! Пусть будет много светлых моментов и ни одного скучного дня 🎉",
            f"Сегодня праздник у нашего человека. С днём рождения, {who} — и пусть год будет твоим 🥂",
        ]
        text = random.choice(greetings)
        try:
            await channel.send(text)
            log.info(f"ДР: поздравил {name}")
        except discord.Forbidden:
            log.error("Нет прав писать в канал ДР (Send Messages).")
        except Exception as e:
            log.error(f"birthday_check send ошибка: {e}")


# =============================== Web layer (Flask) =====================
web = Flask(__name__)
CORS(web, resources={r"/submit": {"origins": os.getenv("CORS_ORIGIN", "")}})


def verify_token(member_id, ts, sig):
    try:
        if abs(time.time() - int(ts)) > TOKEN_TTL:
            return False
    except (ValueError, TypeError):
        return False
    expected = hmac.new(HMAC_SECRET, f"{member_id}:{ts}".encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, sig or "")


def write_to_ips(member_id, field_id, value):
    url = f"{IPS_API_URL}/core/members/{member_id}?key={IPS_API_KEY}"
    try:
        r = requests.post(url, data={f"customFields[{field_id}]": value}, timeout=20)
        if r.status_code == 200:
            log.info(f"IPS: записал поле {field_id} для member {member_id}")
            return True
        log.error(f"IPS API {r.status_code}: {r.text[:160]}")
    except requests.RequestException as e:
        log.error(f"IPS API ошибка: {e}")
    return False


# --- Application record creation in the Pages database ---
APPLICATIONS_DB_ID = int(os.getenv("APPLICATIONS_DB_ID", "2"))

def create_application(member_id, nick, fields: dict):
    """
    Создаёт запись в базе заявок.
    fields — словарь {field_id: значение}, напр. {6:'Иван', 7:'Петров', ...}
    Заголовок ставим 'Заявка от [ник]', автор — сам заявитель.
    """
    url = f"{IPS_API_URL}/cms/records/{APPLICATIONS_DB_ID}?key={IPS_API_KEY}"
    # NOTE: fields are sent as fields[ID]=value, not field_ID.
    # Title maps to field 3 and content to field 4, otherwise IPS returns TITLE_CONTENT_REQUIRED.
    data = {
        "author":          str(member_id),
        "hidden":          0,
        "fields[3]":       f"Заявка от {nick}",          # Заголовок
        "fields[4]":       f"<p>Заявка от {nick}</p>",   # Контент (HTML)
    }
    # remaining custom form fields
    for fid, val in fields.items():
        data[f"fields[{fid}]"] = val
    try:
        r = requests.post(url, data=data, timeout=30)
        if r.status_code in (200, 201):
            rid = r.json().get("id", "?")
            log.info(f"Заявка создана: запись {rid} от member {member_id}")
            # notify the moderation channel through the bridge into the bot
            try:
                dept = fields.get(13, "")   # желаемый департамент
                bot.loop.create_task(notify_new_application(nick, dept, rid))
            except Exception as e:
                log.warning(f"Не смог запланировать пуш заявки: {e}")
            return True
        log.error(f"IPS cms/records {r.status_code}: {r.text[:300]}")
    except requests.RequestException as e:
        log.error(f"IPS cms/records ошибка: {e}")
    return False


def back(status):
    return redirect(f"{RETURN_URL}?{urlencode({'link': status})}")


@web.route("/health")
def health():
    return "ok"


@web.route("/submit", methods=["POST"])
def submit_application():
    """
    Принимает заявку из анкеты (форма со степпера) и пишет в базу.
    Поля приходят form-urlencoded + member_id/ts/sig для проверки HMAC.
    """
    f = request.form
    m, ts, sig = f.get("member_id",""), f.get("ts",""), f.get("sig","")
    if not verify_token(m, ts, sig):
        return {"ok": False, "error": "bad_signature"}, 403

    # title nickname: use what the form sent, otherwise fall back to the member id
    nick = (f.get("nick") or f"#{m}").strip()

    # The birth date field in IPS is a Date type and expects a UNIX timestamp,
    # while the form submits a YYYY-MM-DD string, so it is converted here.
    birth_raw = f.get("birth", "").strip()
    birth_ts = ""
    if birth_raw:
        try:
            birth_ts = str(int(datetime.strptime(birth_raw, "%Y-%m-%d")
                               .replace(tzinfo=timezone.utc).timestamp()))
        except ValueError:
            birth_ts = ""  # некорректная дата — оставим пустой

    # form input names mapped onto database field ids
    fields = {
        6:  f.get("name", ""),       # Имя
        7:  f.get("surname", ""),    # Фамилия
        8:  f.get("gender", ""),     # Пол
        9:  birth_ts,                # Дата рождения (timestamp)
        10: f.get("social", ""),     # Соцсеть
        11: f.get("qualities", ""),  # Твои качества
        12: f.get("about", ""),      # О себе
        13: f.get("dept", ""),       # Желаемый департамент
        14: f.get("dept_why", ""),   # Чем заниматься / почему
        15: f.get("license", ""),    # Лицензия
        16: "pending",               # Статус — всегда "на рассмотрении"
    }
    ok = create_application(m, nick, fields)
    return ({"ok": True}, 200) if ok else ({"ok": False, "error": "ips_failed"}, 502)


@web.route("/notify/transfer", methods=["POST"])
def notify_transfer_route():
    """
    Принимает от сайта уведомление о передаче XP и постит в канал.
    Защита: HMAC-подпись (как у остальных endpoint'ов).
    Тело (form-urlencoded или JSON):
      from_discord, to_discord, from_name, to_name, amount, note, ts, sig
    Подпись: HMAC(secret, f"{from_discord}:{to_discord}:{amount}:{ts}")
    """
    d = request.form if request.form else (request.get_json(silent=True) or {})
    from_discord = str(d.get("from_discord", "")).strip()
    to_discord   = str(d.get("to_discord", "")).strip()
    from_name    = str(d.get("from_name", "")).strip()
    to_name      = str(d.get("to_name", "")).strip()
    amount       = str(d.get("amount", "")).strip()
    note         = str(d.get("note", "")).strip()
    ts           = str(d.get("ts", "")).strip()
    sig          = str(d.get("sig", "")).strip()

    # timestamp check guarding against replay, five minute window
    try:
        if abs(time.time() - int(ts)) > 300:
            return {"ok": False, "error": "expired"}, 403
    except (ValueError, TypeError):
        return {"ok": False, "error": "bad_ts"}, 403

    # signature check
    base = f"{from_discord}:{to_discord}:{amount}:{ts}"
    expected = hmac.new(HMAC_SECRET, base.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, sig):
        return {"ok": False, "error": "bad_signature"}, 403

    # hand off to the bot through the bridge, as in give_candidate_by_id
    try:
        amt = int(amount)
    except ValueError:
        amt = 0
    try:
        from_did = int(from_discord) if from_discord else 0
        to_did   = int(to_discord) if to_discord else 0
        bot.loop.create_task(
            notify_xp_transfer(from_did, to_did, from_name, to_name, amt, note)
        )
    except Exception as e:
        log.warning(f"Не смог запланировать уведомление о передаче: {e}")
        return {"ok": False, "error": "schedule_failed"}, 500

    return {"ok": True}, 200


@web.route("/notify/levels", methods=["POST"])
def notify_levels_route():
    """
    Принимает от сайта (cron) список изменений уровня и постит в канал.
    Защита: HMAC-подпись. Заголовки X-Ts, X-Sig; тело — JSON {changes:[{discord,old,new}]}.
    Подпись: HMAC(secret, f"{ts}:{raw_body}").
    """
    raw = request.get_data(as_text=True)
    ts  = request.headers.get("X-Ts", "")
    sig = request.headers.get("X-Sig", "")

    try:
        if abs(time.time() - int(ts)) > 600:
            return {"ok": False, "error": "expired"}, 403
    except (ValueError, TypeError):
        return {"ok": False, "error": "bad_ts"}, 403

    expected = hmac.new(HMAC_SECRET, f"{ts}:{raw}".encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, sig):
        return {"ok": False, "error": "bad_signature"}, 403

    data = request.get_json(silent=True) or {}
    changes = data.get("changes", [])
    if not isinstance(changes, list) or not changes:
        return {"ok": True, "note": "no_changes"}, 200

    try:
        bot.loop.create_task(notify_level_changes(changes))
    except Exception as e:
        log.warning(f"Не смог запланировать уведомления уровней: {e}")
        return {"ok": False, "error": "schedule_failed"}, 500

    return {"ok": True}, 200


@web.route("/sync/roles", methods=["POST"])
def sync_roles_route():
    raw = request.get_data(as_text=True)
    ts  = request.headers.get("X-Ts", "")
    sig = request.headers.get("X-Sig", "")
    try:
        if abs(time.time() - int(ts)) > 600:
            return {"ok": False, "error": "expired"}, 403
    except (ValueError, TypeError):
        return {"ok": False, "error": "bad_ts"}, 403
    expected = hmac.new(HMAC_SECRET, f"{ts}:{raw}".encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, sig):
        return {"ok": False, "error": "bad_signature"}, 403
    d = request.get_json(silent=True) or {}
    try:
        fut = asyncio.run_coroutine_threadsafe(
            sync_roles(d.get("managed") or [],
                       d.get("players") or [],
                       bool(d.get("apply", False))),
            bot.loop)
        res = fut.result(timeout=300)
    except Exception as e:
        log.warning(f"sync_roles route: {e}")
        return {"ok": False, "error": "sync_failed"}, 500
    return res, 200

@web.route("/sync/nicks", methods=["POST"])
def sync_nicks_route():
    raw = request.get_data(as_text=True)
    ts  = request.headers.get("X-Ts", "")
    sig = request.headers.get("X-Sig", "")
    try:
        if abs(time.time() - int(ts)) > 600:
            return {"ok": False, "error": "expired"}, 403
    except (ValueError, TypeError):
        return {"ok": False, "error": "bad_ts"}, 403
    expected = hmac.new(HMAC_SECRET, f"{ts}:{raw}".encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, sig):
        return {"ok": False, "error": "bad_signature"}, 403
    data = request.get_json(silent=True) or {}
    nicks = data.get("nicks", [])
    if not isinstance(nicks, list) or not nicks:
        return {"ok": True, "note": "no_nicks"}, 200
    try:
        bot.loop.create_task(sync_nicknames(nicks))
    except Exception as e:
        log.warning(f"Не смог запланировать синхру ников: {e}")
        return {"ok": False, "error": "schedule_failed"}, 500
    return {"ok": True}, 200



def _verify_role_req(raw, ts, sig):
    try:
        if abs(time.time() - int(ts)) > 600:
            return False
    except (ValueError, TypeError):
        return False
    expected = hmac.new(HMAC_SECRET, f"{ts}:{raw}".encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, sig)


@web.route("/notify/complaint", methods=["POST"])
def notify_complaint_route():
    raw = request.get_data(as_text=True)
    ts  = request.headers.get("X-Ts", "")
    sig = request.headers.get("X-Sig", "")
    try:
        if abs(time.time() - int(ts)) > 600:
            return {"ok": False, "error": "expired"}, 403
    except (ValueError, TypeError):
        return {"ok": False, "error": "bad_ts"}, 403
    expected = hmac.new(HMAC_SECRET, f"{ts}:{raw}".encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, sig):
        return {"ok": False, "error": "bad_signature"}, 403
    d = request.get_json(silent=True) or {}
    kind = d.get("kind", "")
    text = d.get("text", "")
    tdisc = str(d.get("targetDiscord", "") or "")
    tname = str(d.get("targetName", "") or "")
    tid = d.get("targetId", 0)
    try:
        bot.loop.create_task(notify_complaint(kind, text, tdisc, tname, tid))
    except Exception as e:
        log.warning(f"complaint route: {e}")
        return {"ok": False, "error": "schedule_failed"}, 500
    return {"ok": True}, 200


@web.route("/notify/expel", methods=["POST"])
def notify_expel_route():
    raw = request.get_data(as_text=True)
    ts  = request.headers.get("X-Ts", "")
    sig = request.headers.get("X-Sig", "")
    try:
        if abs(time.time() - int(ts)) > 600:
            return {"ok": False, "error": "expired"}, 403
    except (ValueError, TypeError):
        return {"ok": False, "error": "bad_ts"}, 403
    expected = hmac.new(HMAC_SECRET, f"{ts}:{raw}".encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, sig):
        return {"ok": False, "error": "bad_signature"}, 403
    d = request.get_json(silent=True) or {}
    name = str(d.get("name", "") or "")
    discord = str(d.get("discord", "") or "")
    count = d.get("count", 3)
    try:
        bot.loop.create_task(notify_expel(name, discord, count))
    except Exception as e:
        log.warning(f"expel route: {e}")
        return {"ok": False, "error": "schedule_failed"}, 500
    return {"ok": True}, 200


@web.route("/notify/warning", methods=["POST"])
def notify_warning_route():
    raw = request.get_data(as_text=True)
    ts  = request.headers.get("X-Ts", "")
    sig = request.headers.get("X-Sig", "")
    try:
        if abs(time.time() - int(ts)) > 600:
            return {"ok": False, "error": "expired"}, 403
    except (ValueError, TypeError):
        return {"ok": False, "error": "bad_ts"}, 403
    expected = hmac.new(HMAC_SECRET, f"{ts}:{raw}".encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, sig):
        return {"ok": False, "error": "bad_signature"}, 403
    d = request.get_json(silent=True) or {}
    try:
        bot.loop.create_task(notify_warning(
            str(d.get("target","")), str(d.get("admin","")),
            str(d.get("reason","")), str(d.get("term","")), d.get("active",1),
            str(d.get("targetDiscord","") or "")))
    except Exception as e:
        log.warning(f"warning route: {e}")
        return {"ok": False, "error": "schedule_failed"}, 500
    return {"ok": True}, 200


@web.route("/notify/warning_removed", methods=["POST"])
def notify_warning_removed_route():
    raw = request.get_data(as_text=True)
    ts  = request.headers.get("X-Ts", "")
    sig = request.headers.get("X-Sig", "")
    try:
        if abs(time.time() - int(ts)) > 600:
            return {"ok": False, "error": "expired"}, 403
    except (ValueError, TypeError):
        return {"ok": False, "error": "bad_ts"}, 403
    expected = hmac.new(HMAC_SECRET, f"{ts}:{raw}".encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, sig):
        return {"ok": False, "error": "bad_signature"}, 403
    d = request.get_json(silent=True) or {}
    try:
        bot.loop.create_task(notify_warning_removed(
            str(d.get("target","")), str(d.get("admin","")),
            str(d.get("reason","")), str(d.get("targetDiscord","") or "")))
    except Exception as e:
        log.warning(f"warning_removed route: {e}")
        return {"ok": False, "error": "schedule_failed"}, 500
    return {"ok": True}, 200


@web.route("/notify/warning_expired", methods=["POST"])
def notify_warning_expired_route():
    raw = request.get_data(as_text=True)
    ts  = request.headers.get("X-Ts", "")
    sig = request.headers.get("X-Sig", "")
    try:
        if abs(time.time() - int(ts)) > 600:
            return {"ok": False, "error": "expired"}, 403
    except (ValueError, TypeError):
        return {"ok": False, "error": "bad_ts"}, 403
    expected = hmac.new(HMAC_SECRET, f"{ts}:{raw}".encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, sig):
        return {"ok": False, "error": "bad_signature"}, 403
    d = request.get_json(silent=True) or {}
    try:
        bot.loop.create_task(notify_warning_expired(
            str(d.get("target","")), str(d.get("reason","")),
            d.get("active",0), str(d.get("targetDiscord","") or "")))
    except Exception as e:
        log.warning(f"warning_expired route: {e}")
        return {"ok": False, "error": "schedule_failed"}, 500
    return {"ok": True}, 200


@web.route("/notify/command", methods=["POST"])
def notify_command_route():
    raw = request.get_data(as_text=True)
    ts  = request.headers.get("X-Ts", "")
    sig = request.headers.get("X-Sig", "")
    try:
        if abs(time.time() - int(ts)) > 600:
            return {"ok": False, "error": "expired"}, 403
    except (ValueError, TypeError):
        return {"ok": False, "error": "bad_ts"}, 403
    expected = hmac.new(HMAC_SECRET, f"{ts}:{raw}".encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, sig):
        return {"ok": False, "error": "bad_signature"}, 403
    d = request.get_json(silent=True) or {}
    try:
        bot.loop.create_task(notify_command(
            str(d.get("target","")), str(d.get("dept","")),
            str(d.get("targetDiscord","") or ""),
            str(d.get("deptRole","") or "")))
    except Exception as e:
        log.warning(f"command route: {e}")
        return {"ok": False, "error": "schedule_failed"}, 500
    return {"ok": True}, 200


@web.route("/notify/command_extend", methods=["POST"])
def notify_command_extend_route():
    raw = request.get_data(as_text=True)
    ts  = request.headers.get("X-Ts", "")
    sig = request.headers.get("X-Sig", "")
    try:
        if abs(time.time() - int(ts)) > 600:
            return {"ok": False, "error": "expired"}, 403
    except (ValueError, TypeError):
        return {"ok": False, "error": "bad_ts"}, 403
    expected = hmac.new(HMAC_SECRET, f"{ts}:{raw}".encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, sig):
        return {"ok": False, "error": "bad_signature"}, 403
    d = request.get_json(silent=True) or {}
    try:
        bot.loop.create_task(notify_command_extend(
            str(d.get("target","")), str(d.get("dept","")),
            str(d.get("targetDiscord","") or ""),
            str(d.get("deptRole","") or "")))
    except Exception as e:
        log.warning(f"command_extend route: {e}")
        return {"ok": False, "error": "schedule_failed"}, 500
    return {"ok": True}, 200

@web.route("/notify/command_removed", methods=["POST"])
def notify_command_removed_route():
    raw = request.get_data(as_text=True)
    ts  = request.headers.get("X-Ts", "")
    sig = request.headers.get("X-Sig", "")
    try:
        if abs(time.time() - int(ts)) > 600:
            return {"ok": False, "error": "expired"}, 403
    except (ValueError, TypeError):
        return {"ok": False, "error": "bad_ts"}, 403
    expected = hmac.new(HMAC_SECRET, f"{ts}:{raw}".encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, sig):
        return {"ok": False, "error": "bad_signature"}, 403
    d = request.get_json(silent=True) or {}
    try:
        bot.loop.create_task(notify_command_removed(
            str(d.get("target","")), str(d.get("dept","")),
            str(d.get("targetDiscord","") or ""),
            str(d.get("deptRole","") or ""),
            str(d.get("reason","") or ""),
            bool(d.get("noReelect", False))))
    except Exception as e:
        log.warning(f"command_removed route: {e}")
        return {"ok": False, "error": "schedule_failed"}, 500
    return {"ok": True}, 200

@web.route("/notify/election_apply_status", methods=["POST"])
def notify_election_apply_status_route():
    raw = request.get_data(as_text=True)
    ts  = request.headers.get("X-Ts", "")
    sig = request.headers.get("X-Sig", "")
    try:
        if abs(time.time() - int(ts)) > 600:
            return {"ok": False, "error": "expired"}, 403
    except (ValueError, TypeError):
        return {"ok": False, "error": "bad_ts"}, 403
    expected = hmac.new(HMAC_SECRET, f"{ts}:{raw}".encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, sig):
        return {"ok": False, "error": "bad_signature"}, 403
    d = request.get_json(silent=True) or {}
    goals = d.get("goals") or []
    if not isinstance(goals, list):
        goals = []
    try:
        bot.loop.create_task(notify_election_apply_status(
            str(d.get("targetDiscord","") or ""),
            str(d.get("dept","")),
            str(d.get("status","")),
            str(d.get("reason","") or ""),
            goals,
            str(d.get("pitch","") or "")))
    except Exception as e:
        log.warning(f"election_apply_status route: {e}")
        return {"ok": False, "error": "schedule_failed"}, 500
    return {"ok": True}, 200

@web.route("/notify/election_deadline", methods=["POST"])
def notify_election_deadline_route():
    raw = request.get_data(as_text=True)
    ts  = request.headers.get("X-Ts", "")
    sig = request.headers.get("X-Sig", "")
    try:
        if abs(time.time() - int(ts)) > 600:
            return {"ok": False, "error": "expired"}, 403
    except (ValueError, TypeError):
        return {"ok": False, "error": "bad_ts"}, 403
    expected = hmac.new(HMAC_SECRET, f"{ts}:{raw}".encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, sig):
        return {"ok": False, "error": "bad_signature"}, 403
    d = request.get_json(silent=True) or {}
    try:
        bot.loop.create_task(notify_election_deadline(
            str(d.get("kind","")),
            str(d.get("dept","")),
            str(d.get("deadline","") or ""),
            int(d.get("pending", 0) or 0),
            int(d.get("approved", 0) or 0),
            int(d.get("votes", 0) or 0),
            str(d.get("url","") or "")))
    except Exception as e:
        log.warning(f"election_deadline route: {e}")
        return {"ok": False, "error": "schedule_failed"}, 500
    return {"ok": True}, 200

@web.route("/notify/dept_request", methods=["POST"])
def notify_dept_request_route():
    raw = request.get_data(as_text=True)
    ts  = request.headers.get("X-Ts", "")
    sig = request.headers.get("X-Sig", "")
    try:
        if abs(time.time() - int(ts)) > 600:
            return {"ok": False, "error": "expired"}, 403
    except (ValueError, TypeError):
        return {"ok": False, "error": "bad_ts"}, 403
    expected = hmac.new(HMAC_SECRET, f"{ts}:{raw}".encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, sig):
        return {"ok": False, "error": "bad_signature"}, 403
    d = request.get_json(silent=True) or {}
    try:
        bot.loop.create_task(notify_dept_request(
            str(d.get("kind","") or ""),
            str(d.get("player","") or ""),
            str(d.get("playerDisc","") or ""),
            str(d.get("targetDept","") or ""),
            str(d.get("fromDept","") or ""),
            str(d.get("cmdRole","") or "")))
    except Exception as e:
        log.warning(f"dept_request route: {e}")
        return {"ok": False, "error": "schedule_failed"}, 500
    return {"ok": True}, 200

@web.route("/notify/dept_request_decided", methods=["POST"])
def notify_dept_request_decided_route():
    raw = request.get_data(as_text=True)
    ts  = request.headers.get("X-Ts", "")
    sig = request.headers.get("X-Sig", "")
    try:
        if abs(time.time() - int(ts)) > 600:
            return {"ok": False, "error": "expired"}, 403
    except (ValueError, TypeError):
        return {"ok": False, "error": "bad_ts"}, 403
    expected = hmac.new(HMAC_SECRET, f"{ts}:{raw}".encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, sig):
        return {"ok": False, "error": "bad_signature"}, 403
    d = request.get_json(silent=True) or {}
    try:
        bot.loop.create_task(notify_dept_request_decided(
            str(d.get("kind","") or ""),
            str(d.get("player","") or ""),
            str(d.get("playerDisc","") or ""),
            str(d.get("targetDept","") or ""),
            str(d.get("by","") or ""),
            str(d.get("byDisc","") or ""),
            bool(d.get("approved", True))))
    except Exception as e:
        log.warning(f"dept_request_decided route: {e}")
        return {"ok": False, "error": "schedule_failed"}, 500
    return {"ok": True}, 200

@web.route("/notify/mentor_graduated", methods=["POST"])
def notify_mentor_graduated_route():
    raw = request.get_data(as_text=True)
    ts  = request.headers.get("X-Ts", "")
    sig = request.headers.get("X-Sig", "")
    try:
        if abs(time.time() - int(ts)) > 600:
            return {"ok": False, "error": "expired"}, 403
    except (ValueError, TypeError):
        return {"ok": False, "error": "bad_ts"}, 403
    expected = hmac.new(HMAC_SECRET, f"{ts}:{raw}".encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, sig):
        return {"ok": False, "error": "bad_signature"}, 403
    d = request.get_json(silent=True) or {}
    grads = d.get("graduates") or []
    if not isinstance(grads, list):
        grads = []
    try:
        bot.loop.create_task(notify_mentor_graduated(grads))
    except Exception as e:
        log.warning(f"mentor_graduated route: {e}")
        return {"ok": False, "error": "schedule_failed"}, 500
    return {"ok": True}, 200

@web.route("/notify/mentor_invite", methods=["POST"])
def notify_mentor_invite_route():
    raw = request.get_data(as_text=True)
    ts  = request.headers.get("X-Ts", "")
    sig = request.headers.get("X-Sig", "")
    try:
        if abs(time.time() - int(ts)) > 600:
            return {"ok": False, "error": "expired"}, 403
    except (ValueError, TypeError):
        return {"ok": False, "error": "bad_ts"}, 403
    expected = hmac.new(HMAC_SECRET, f"{ts}:{raw}".encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, sig):
        return {"ok": False, "error": "bad_signature"}, 403
    d = request.get_json(silent=True) or {}
    try:
        bot.loop.create_task(notify_mentor_invite(
            str(d.get("mentor","") or ""),
            str(d.get("mentorDisc","") or ""),
            str(d.get("trainee","") or ""),
            str(d.get("traineeDisc","") or ""),
            str(d.get("dept","") or "")))
    except Exception as e:
        log.warning(f"mentor_invite route: {e}")
        return {"ok": False, "error": "schedule_failed"}, 500
    return {"ok": True}, 200

@web.route("/notify/mentor_accepted", methods=["POST"])
def notify_mentor_accepted_route():
    raw = request.get_data(as_text=True)
    ts  = request.headers.get("X-Ts", "")
    sig = request.headers.get("X-Sig", "")
    try:
        if abs(time.time() - int(ts)) > 600:
            return {"ok": False, "error": "expired"}, 403
    except (ValueError, TypeError):
        return {"ok": False, "error": "bad_ts"}, 403
    expected = hmac.new(HMAC_SECRET, f"{ts}:{raw}".encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, sig):
        return {"ok": False, "error": "bad_signature"}, 403
    d = request.get_json(silent=True) or {}
    try:
        bot.loop.create_task(notify_mentor_accepted(
            str(d.get("mentor","") or ""),
            str(d.get("mentorDisc","") or ""),
            str(d.get("trainee","") or ""),
            str(d.get("traineeDisc","") or ""),
            str(d.get("dept","") or "")))
    except Exception as e:
        log.warning(f"mentor_accepted route: {e}")
        return {"ok": False, "error": "schedule_failed"}, 500
    return {"ok": True}, 200

@web.route("/notify/election_turnout_full", methods=["POST"])
def notify_election_turnout_full_route():
    raw = request.get_data(as_text=True)
    ts  = request.headers.get("X-Ts", "")
    sig = request.headers.get("X-Sig", "")
    try:
        if abs(time.time() - int(ts)) > 600:
            return {"ok": False, "error": "expired"}, 403
    except (ValueError, TypeError):
        return {"ok": False, "error": "bad_ts"}, 403
    expected = hmac.new(HMAC_SECRET, f"{ts}:{raw}".encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, sig):
        return {"ok": False, "error": "bad_signature"}, 403
    d = request.get_json(silent=True) or {}
    try:
        bot.loop.create_task(notify_election_turnout_full(
            str(d.get("dept","")),
            int(d.get("voted", 0) or 0),
            int(d.get("eligible", 0) or 0),
            bool(d.get("runoff", False)),
            str(d.get("deadline","") or ""),
            str(d.get("url","") or "")))
    except Exception as e:
        log.warning(f"election_turnout_full route: {e}")
        return {"ok": False, "error": "schedule_failed"}, 500
    return {"ok": True}, 200

@web.route("/notify/election_turnout", methods=["POST"])
def notify_election_turnout_route():
    raw = request.get_data(as_text=True)
    ts  = request.headers.get("X-Ts", "")
    sig = request.headers.get("X-Sig", "")
    try:
        if abs(time.time() - int(ts)) > 600:
            return {"ok": False, "error": "expired"}, 403
    except (ValueError, TypeError):
        return {"ok": False, "error": "bad_ts"}, 403
    expected = hmac.new(HMAC_SECRET, f"{ts}:{raw}".encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, sig):
        return {"ok": False, "error": "bad_signature"}, 403
    d = request.get_json(silent=True) or {}
    try:
        bot.loop.create_task(edit_vote_turnout(
            str(d.get("messageId","") or ""),
            int(d.get("voted", 0) or 0),
            int(d.get("eligible", 0) or 0),
            int(d.get("percent", 0) or 0)))
    except Exception as e:
        log.warning(f"election_turnout route: {e}")
        return {"ok": False, "error": "schedule_failed"}, 500
    return {"ok": True}, 200

@web.route("/notify/election_early_close", methods=["POST"])
def notify_election_early_close_route():
    raw = request.get_data(as_text=True)
    ts  = request.headers.get("X-Ts", "")
    sig = request.headers.get("X-Sig", "")
    try:
        if abs(time.time() - int(ts)) > 600:
            return {"ok": False, "error": "expired"}, 403
    except (ValueError, TypeError):
        return {"ok": False, "error": "bad_ts"}, 403
    expected = hmac.new(HMAC_SECRET, f"{ts}:{raw}".encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, sig):
        return {"ok": False, "error": "bad_signature"}, 403
    d = request.get_json(silent=True) or {}
    try:
        bot.loop.create_task(notify_election_early_close(
            str(d.get("dept","")),
            str(d.get("deadline","") or ""),
            int(d.get("votes", 0) or 0),
            int(d.get("eligible", 0) or 0),
            int(d.get("percent", 0) or 0),
            str(d.get("by","") or ""),
            bool(d.get("runoff", False))))
    except Exception as e:
        log.warning(f"election_early_close route: {e}")
        return {"ok": False, "error": "schedule_failed"}, 500
    return {"ok": True}, 200

@web.route("/notify/election_apply_extended", methods=["POST"])
def notify_election_apply_extended_route():
    raw = request.get_data(as_text=True)
    ts  = request.headers.get("X-Ts", "")
    sig = request.headers.get("X-Sig", "")
    try:
        if abs(time.time() - int(ts)) > 600:
            return {"ok": False, "error": "expired"}, 403
    except (ValueError, TypeError):
        return {"ok": False, "error": "bad_ts"}, 403
    expected = hmac.new(HMAC_SECRET, f"{ts}:{raw}".encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, sig):
        return {"ok": False, "error": "bad_signature"}, 403
    d = request.get_json(silent=True) or {}
    roles = d.get("deptRoles") or []
    if not isinstance(roles, list):
        roles = []
    try:
        bot.loop.create_task(notify_election_apply_extended(
            str(d.get("dept","")),
            str(d.get("deadline","") or ""),
            str(d.get("url","") or ""),
            [str(r) for r in roles]))
    except Exception as e:
        log.warning(f"election_apply_extended route: {e}")
        return {"ok": False, "error": "schedule_failed"}, 500
    return {"ok": True}, 200

@web.route("/notify/election_candidate_removed", methods=["POST"])
def notify_election_candidate_removed_route():
    raw = request.get_data(as_text=True)
    ts  = request.headers.get("X-Ts", "")
    sig = request.headers.get("X-Sig", "")
    try:
        if abs(time.time() - int(ts)) > 600:
            return {"ok": False, "error": "expired"}, 403
    except (ValueError, TypeError):
        return {"ok": False, "error": "bad_ts"}, 403
    expected = hmac.new(HMAC_SECRET, f"{ts}:{raw}".encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, sig):
        return {"ok": False, "error": "bad_signature"}, 403
    d = request.get_json(silent=True) or {}
    roles = d.get("deptRoles") or []
    if not isinstance(roles, list):
        roles = []
    try:
        bot.loop.create_task(notify_election_candidate_removed(
            str(d.get("dept","")),
            str(d.get("candidate","") or ""),
            str(d.get("candDisc","") or ""),
            str(d.get("reason","") or ""),
            bool(d.get("voting", False)),
            [str(r) for r in roles]))
    except Exception as e:
        log.warning(f"election_candidate_removed route: {e}")
        return {"ok": False, "error": "schedule_failed"}, 500
    return {"ok": True}, 200

@web.route("/notify/election_vote_open", methods=["POST"])
def notify_election_vote_open_route():
    raw = request.get_data(as_text=True)
    ts  = request.headers.get("X-Ts", "")
    sig = request.headers.get("X-Sig", "")
    try:
        if abs(time.time() - int(ts)) > 600:
            return {"ok": False, "error": "expired"}, 403
    except (ValueError, TypeError):
        return {"ok": False, "error": "bad_ts"}, 403
    expected = hmac.new(HMAC_SECRET, f"{ts}:{raw}".encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, sig):
        return {"ok": False, "error": "bad_signature"}, 403
    d = request.get_json(silent=True) or {}
    roles = d.get("deptRoles") or []
    if not isinstance(roles, list):
        roles = []
    try:
        fut = asyncio.run_coroutine_threadsafe(
            notify_election_vote_open(
                str(d.get("dept","")),
                str(d.get("deadline","") or ""),
                bool(d.get("runoff", False)),
                str(d.get("url","") or ""),
                [str(r) for r in roles],
                [str(x) for x in (d.get("candidates") or []) if str(x).strip()],
                int(d.get("eligible", 0) or 0)),
            bot.loop)
        msg_id = fut.result(timeout=10)
    except Exception as e:
        log.warning(f"election_vote_open route: {e}")
        return {"ok": False, "error": "schedule_failed"}, 500
    return {"ok": True, "messageId": (str(msg_id) if msg_id else "")}, 200

@web.route("/notify/election_result", methods=["POST"])
def notify_election_result_route():
    raw = request.get_data(as_text=True)
    ts  = request.headers.get("X-Ts", "")
    sig = request.headers.get("X-Sig", "")
    try:
        if abs(time.time() - int(ts)) > 600:
            return {"ok": False, "error": "expired"}, 403
    except (ValueError, TypeError):
        return {"ok": False, "error": "bad_ts"}, 403
    expected = hmac.new(HMAC_SECRET, f"{ts}:{raw}".encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, sig):
        return {"ok": False, "error": "bad_signature"}, 403
    d = request.get_json(silent=True) or {}
    roles = d.get("deptRoles") or []
    if not isinstance(roles, list):
        roles = []
    try:
        bot.loop.create_task(notify_election_result(
            str(d.get("dept","")),
            str(d.get("winner","") or ""),
            str(d.get("winnerDisc","") or ""),
            int(d.get("votes", 0) or 0),
            int(d.get("total", 0) or 0),
            int(d.get("round", 1) or 1),
            [str(r) for r in roles]))
    except Exception as e:
        log.warning(f"election_result route: {e}")
        return {"ok": False, "error": "schedule_failed"}, 500
    return {"ok": True}, 200

@web.route("/notify/election_application_edit", methods=["POST"])
def notify_election_application_edit_route():
    raw = request.get_data(as_text=True)
    ts  = request.headers.get("X-Ts", "")
    sig = request.headers.get("X-Sig", "")
    try:
        if abs(time.time() - int(ts)) > 600:
            return {"ok": False, "error": "expired"}, 403
    except (ValueError, TypeError):
        return {"ok": False, "error": "bad_ts"}, 403
    expected = hmac.new(HMAC_SECRET, f"{ts}:{raw}".encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, sig):
        return {"ok": False, "error": "bad_signature"}, 403
    d = request.get_json(silent=True) or {}
    try:
        bot.loop.create_task(edit_application_notice(
            str(d.get("messageId","") or ""),
            str(d.get("status","")),
            str(d.get("by","") or ""),
            str(d.get("reason","") or "")))
    except Exception as e:
        log.warning(f"election_application_edit route: {e}")
        return {"ok": False, "error": "schedule_failed"}, 500
    return {"ok": True}, 200

@web.route("/notify/election_new_application", methods=["POST"])
def notify_election_new_application_route():
    raw = request.get_data(as_text=True)
    ts  = request.headers.get("X-Ts", "")
    sig = request.headers.get("X-Sig", "")
    try:
        if abs(time.time() - int(ts)) > 600:
            return {"ok": False, "error": "expired"}, 403
    except (ValueError, TypeError):
        return {"ok": False, "error": "bad_ts"}, 403
    expected = hmac.new(HMAC_SECRET, f"{ts}:{raw}".encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, sig):
        return {"ok": False, "error": "bad_signature"}, 403
    d = request.get_json(silent=True) or {}
    try:
        # await delivery so the message id can be returned and used for later status edits
        fut = asyncio.run_coroutine_threadsafe(
            notify_election_new_application(
                str(d.get("dept","")),
                str(d.get("candidate","") or ""),
                str(d.get("candidateDisc","") or ""),
                int(d.get("goals", 0) or 0),
                bool(d.get("reapplied", False)),
                str(d.get("url","") or "")),
            bot.loop)
        msg_id = fut.result(timeout=10)
    except Exception as e:
        log.warning(f"election_new_application route: {e}")
        return {"ok": False, "error": "schedule_failed"}, 500
    return {"ok": True, "messageId": (str(msg_id) if msg_id else "")}, 200

@web.route("/notify/election_open", methods=["POST"])
def notify_election_open_route():
    raw = request.get_data(as_text=True)
    ts  = request.headers.get("X-Ts", "")
    sig = request.headers.get("X-Sig", "")
    try:
        if abs(time.time() - int(ts)) > 600:
            return {"ok": False, "error": "expired"}, 403
    except (ValueError, TypeError):
        return {"ok": False, "error": "bad_ts"}, 403
    expected = hmac.new(HMAC_SECRET, f"{ts}:{raw}".encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, sig):
        return {"ok": False, "error": "bad_signature"}, 403
    d = request.get_json(silent=True) or {}
    try:
        bot.loop.create_task(notify_election_open(
            str(d.get("dept","")),
            str(d.get("applyDeadline","") or ""),
            str(d.get("url","") or ""),
            str(d.get("by","") or "")))
    except Exception as e:
        log.warning(f"election_open route: {e}")
        return {"ok": False, "error": "schedule_failed"}, 500
    return {"ok": True}, 200

@web.route("/notify/noreelect_cleared", methods=["POST"])
def notify_noreelect_cleared_route():
    raw = request.get_data(as_text=True)
    ts  = request.headers.get("X-Ts", "")
    sig = request.headers.get("X-Sig", "")
    try:
        if abs(time.time() - int(ts)) > 600:
            return {"ok": False, "error": "expired"}, 403
    except (ValueError, TypeError):
        return {"ok": False, "error": "bad_ts"}, 403
    expected = hmac.new(HMAC_SECRET, f"{ts}:{raw}".encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, sig):
        return {"ok": False, "error": "bad_signature"}, 403
    d = request.get_json(silent=True) or {}
    try:
        bot.loop.create_task(notify_noreelect_cleared(
            str(d.get("target","")),
            str(d.get("targetDiscord","") or ""),
            str(d.get("by","") or "")))
    except Exception as e:
        log.warning(f"noreelect_cleared route: {e}")
        return {"ok": False, "error": "schedule_failed"}, 500
    return {"ok": True}, 200

@web.route("/notify/acting_set", methods=["POST"])
def notify_acting_set_route():
    raw = request.get_data(as_text=True)
    ts  = request.headers.get("X-Ts", "")
    sig = request.headers.get("X-Sig", "")
    try:
        if abs(time.time() - int(ts)) > 600:
            return {"ok": False, "error": "expired"}, 403
    except (ValueError, TypeError):
        return {"ok": False, "error": "bad_ts"}, 403
    expected = hmac.new(HMAC_SECRET, f"{ts}:{raw}".encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, sig):
        return {"ok": False, "error": "bad_signature"}, 403
    d = request.get_json(silent=True) or {}
    try:
        bot.loop.create_task(notify_acting_set(
            str(d.get("target","")), str(d.get("dept","")),
            str(d.get("targetDiscord","") or ""),
            str(d.get("deptRole","") or "")))
    except Exception as e:
        log.warning(f"acting_set route: {e}")
        return {"ok": False, "error": "schedule_failed"}, 500
    return {"ok": True}, 200

@web.route("/notify/acting_removed", methods=["POST"])
def notify_acting_removed_route():
    raw = request.get_data(as_text=True)
    ts  = request.headers.get("X-Ts", "")
    sig = request.headers.get("X-Sig", "")
    try:
        if abs(time.time() - int(ts)) > 600:
            return {"ok": False, "error": "expired"}, 403
    except (ValueError, TypeError):
        return {"ok": False, "error": "bad_ts"}, 403
    expected = hmac.new(HMAC_SECRET, f"{ts}:{raw}".encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, sig):
        return {"ok": False, "error": "bad_signature"}, 403
    d = request.get_json(silent=True) or {}
    try:
        bot.loop.create_task(notify_acting_removed(
            str(d.get("target","")), str(d.get("dept","")),
            str(d.get("targetDiscord","") or ""),
            str(d.get("deptRole","") or ""),
            str(d.get("reason","") or ""),
            bool(d.get("noReelect", False))))
    except Exception as e:
        log.warning(f"acting_removed route: {e}")
        return {"ok": False, "error": "schedule_failed"}, 500
    return {"ok": True}, 200


@web.route("/notify/command_expiry", methods=["POST"])
def notify_command_expiry_route():
    raw = request.get_data(as_text=True)
    ts  = request.headers.get("X-Ts", "")
    sig = request.headers.get("X-Sig", "")
    try:
        if abs(time.time() - int(ts)) > 600:
            return {"ok": False, "error": "expired"}, 403
    except (ValueError, TypeError):
        return {"ok": False, "error": "bad_ts"}, 403
    expected = hmac.new(HMAC_SECRET, f"{ts}:{raw}".encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, sig):
        return {"ok": False, "error": "bad_signature"}, 403
    d = request.get_json(silent=True) or {}
    try:
        bot.loop.create_task(notify_command_expiry(
            str(d.get("target","")), str(d.get("dept","")),
            d.get("days",0), str(d.get("targetDiscord","") or "")))
    except Exception as e:
        log.warning(f"command_expiry route: {e}")
        return {"ok": False, "error": "schedule_failed"}, 500
    return {"ok": True}, 200


@web.route("/role/grant", methods=["POST"])
def role_grant_route():
    raw = request.get_data(as_text=True)
    if not _verify_role_req(raw, request.headers.get("X-Ts",""), request.headers.get("X-Sig","")):
        return {"ok": False, "error": "bad_signature"}, 403
    d = request.get_json(silent=True) or {}
    did = d.get("discord",""); role = d.get("role","")
    if not did or not role:
        return {"ok": False, "error": "bad_input"}, 400
    try:
        bot.loop.create_task(set_member_role(int(did), int(role), True))
    except Exception as e:
        log.warning(f"role_grant не запланирован: {e}")
        return {"ok": False, "error": "schedule_failed"}, 500
    return {"ok": True}, 200


@web.route("/role/revoke", methods=["POST"])
def role_revoke_route():
    raw = request.get_data(as_text=True)
    if not _verify_role_req(raw, request.headers.get("X-Ts",""), request.headers.get("X-Sig","")):
        return {"ok": False, "error": "bad_signature"}, 403
    d = request.get_json(silent=True) or {}
    did = d.get("discord",""); role = d.get("role","")
    if not did or not role:
        return {"ok": False, "error": "bad_input"}, 400
    try:
        bot.loop.create_task(set_member_role(int(did), int(role), False))
    except Exception as e:
        log.warning(f"role_revoke не запланирован: {e}")
        return {"ok": False, "error": "schedule_failed"}, 500
    return {"ok": True}, 200
def api_binding():
    if request.remote_addr not in ("127.0.0.1", "::1"):
        abort(403)
    discord_id = request.args.get("discord_id", "").strip()
    if not discord_id:
        return {"error": "missing discord_id"}, 400

    # page through IPS members until the target is found
    page = 1
    while True:
        url = f"{IPS_API_URL}/core/members?page={page}&perPage=100&key={IPS_API_KEY}"
        try:
            r = requests.get(url, timeout=30)
        except requests.RequestException as e:
            log.error(f"IPS API ошибка при поиске binding: {e}")
            return {"error": "ips_error"}, 503

        if r.status_code != 200:
            log.error(f"IPS binding: статус {r.status_code}")
            return {"error": "ips_error"}, 503

        data = r.json()
        members = data.get("results", [])
        if not members:
            break

        for m in members:
            fields = m.get("customFields", {})
            member_discord = None
            member_steam   = None
            for group in fields.values():
                for fid, field in group.get("fields", {}).items():
                    if fid == str(IPS_FIELD_DISCORD):
                        member_discord = str(field.get("value") or "").strip()
                    if fid == str(IPS_FIELD_STEAM):
                        member_steam = str(field.get("value") or "").strip()

            if member_discord == discord_id:
                ips_name = m.get("name", "")
                log.info(f"binding: discord {discord_id} → steam {member_steam}, name {ips_name}")
                return {"steam": member_steam or "", "name": ips_name}

        # stop once the last page has been read
        total = data.get("totalResults", 0)
        per_page = data.get("perPage", 100)
        if page * per_page >= total:
            break
        page += 1

    return {"steam": "", "name": ""}  # не найден


# ── Discord ──
@web.route("/link/discord")
def link_discord():
    m, ts, sig = request.args.get("member_id",""), request.args.get("ts",""), request.args.get("sig","")
    if not verify_token(m, ts, sig):
        abort(403, "Неверная подпись")
    params = {
        "client_id": DISCORD_CLIENT_ID, "redirect_uri": DISCORD_REDIRECT,
        "response_type": "code", "scope": "identify", "state": f"{m}|{ts}|{sig}",
    }
    return redirect(f"{DISCORD_API}/oauth2/authorize?{urlencode(params)}")


@web.route("/callback/discord")
def callback_discord():
    code, state = request.args.get("code",""), request.args.get("state","")
    try:
        m, ts, sig = state.split("|")
    except ValueError:
        abort(400)
    if not verify_token(m, ts, sig):
        abort(403)
    tok = requests.post(f"{DISCORD_API}/oauth2/token", data={
        "client_id": DISCORD_CLIENT_ID, "client_secret": DISCORD_CLIENT_SECRET,
        "grant_type": "authorization_code", "code": code, "redirect_uri": DISCORD_REDIRECT,
    }, headers={"Content-Type": "application/x-www-form-urlencoded"}, timeout=10)
    if tok.status_code != 200:
        return back("error")
    me = requests.get(f"{DISCORD_API}/users/@me",
                      headers={"Authorization": f"Bearer {tok.json().get('access_token')}"}, timeout=10)
    if me.status_code != 200:
        return back("error")
    discord_id = me.json().get("id")
    ok = write_to_ips(m, IPS_FIELD_DISCORD, discord_id)
    # the link succeeded, so grant the candidate role right away
    if ok and discord_id:
        try:
            bot.loop.create_task(give_candidate_by_id(int(discord_id)))
        except Exception as e:
            log.warning(f"Не смог запланировать выдачу роли: {e}")
    return back("discord_ok" if ok else "error")


# ── Steam ──
@web.route("/link/steam")
def link_steam():
    m, ts, sig = request.args.get("member_id",""), request.args.get("ts",""), request.args.get("sig","")
    if not verify_token(m, ts, sig):
        abort(403, "Неверная подпись")
    realm = request.url_root.rstrip("/")
    return_to = f"{realm}/callback/steam?member_id={m}&ts={ts}&sig={sig}"
    params = {
        "openid.ns": "http://specs.openid.net/auth/2.0",
        "openid.mode": "checkid_setup",
        "openid.return_to": return_to, "openid.realm": realm,
        "openid.identity": "http://specs.openid.net/auth/2.0/identifier_select",
        "openid.claimed_id": "http://specs.openid.net/auth/2.0/identifier_select",
    }
    return redirect(f"{STEAM_OPENID}?{urlencode(params)}")


@web.route("/callback/steam")
def callback_steam():
    m, ts, sig = request.args.get("member_id",""), request.args.get("ts",""), request.args.get("sig","")
    if not verify_token(m, ts, sig):
        abort(403)
    params = dict(request.args)
    params["openid.mode"] = "check_authentication"
    if "is_valid:true" not in requests.post(STEAM_OPENID, data=params, timeout=10).text:
        return back("error")
    claimed = request.args.get("openid.claimed_id", "")
    steam64 = claimed.rsplit("/", 1)[-1] if claimed else ""
    if not steam64.isdigit():
        return back("error")
    fivem_id = "steam:" + format(int(steam64), "x")   # формат FiveM
    ok = write_to_ips(m, IPS_FIELD_STEAM, fivem_id)
    return back("steam_ok" if ok else "error")


def run_web():
    # Flask development server in a worker thread; see README for production notes
    web.run(host="127.0.0.1", port=WEB_PORT)


# =============================== Entry point ===========================
if __name__ == "__main__":
    if not TOKEN:
        raise SystemExit("Нет DISCORD_TOKEN в .env")
    # 1) web layer in a worker thread
    threading.Thread(target=run_web, daemon=True).start()
    log.info(f"Веб-часть слушает 127.0.0.1:{WEB_PORT}")
    # 2) Discord client on the main thread
    bot.run(TOKEN)