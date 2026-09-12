print("=== BOT FILE STARTED ===", flush=true) 
import asyncio
import random
from datetime import datetime, timedelta

from telethon import TelegramClient
from telethon.sessions import StringSession
from telethon.tl.functions.channels import GetParticipantsRequest
from telethon.tl.functions.users import GetFullUserRequest
from telethon.tl.types import (
    ChannelParticipantsSearch,
    InputPeerUser,
)
from telethon.errors import (
    PeerFloodError,
    FloodWaitError,
    UserPrivacyRestrictedError,
    UserIsBlockedError,
    UserNotMutualContactError,
    InputUserDeactivatedError,
    ChatWriteForbiddenError,
    YouBlockedUserError,
)

import config
import database as db


SPAMBOT_USERNAME = "SpamBot"


# ============================================================
#  КЛИЕНТ
# ============================================================

async def make_client(session_string: str) -> TelegramClient:
    client = TelegramClient(StringSession(session_string), config.API_ID, config.API_HASH)
    await client.connect()
    if not await client.is_user_authorized():
        raise RuntimeError("Session is not authorized")
    return client


# ============================================================
#  ПАРСИНГ УЧАСТНИКОВ
# ============================================================

async def parse_group(session_string: str, group_link: str, progress_cb=None) -> list[dict]:
    client = await make_client(session_string)
    try:
        entity = await client.get_entity(group_link)
        collected: list[dict] = []
        offset = 0
        limit = 200

        while True:
            try:
                res = await client(GetParticipantsRequest(
                    channel=entity,
                    filter=ChannelParticipantsSearch(""),
                    offset=offset,
                    limit=limit,
                    hash=0,
                ))
            except FloodWaitError as e:
                await asyncio.sleep(e.seconds + 5)
                continue

            if not res.users:
                break

            for u in res.users:
                if u.bot or u.deleted:
                    continue
                collected.append({
                    "user_id": u.id,
                    "username": u.username,
                    "access_hash": u.access_hash,
                })

            offset += len(res.users)
            if progress_cb:
                await progress_cb(len(collected))

            await asyncio.sleep(1.5)

            if offset >= res.count:
                break

        return collected
    finally:
        await client.disconnect()


# ============================================================
#  SPAMBOT — ЧТЕНИЕ КНОПКИ + НАЖАТИЕ
# ============================================================

def _extract_first_button_text(reply_markup) -> str | None:
    try:
        for row in getattr(reply_markup, "rows", []):
            for btn in getattr(row, "buttons", []):
                text = getattr(btn, "text", None)
                if text:
                    return text
    except Exception:
        pass
    return None


async def try_unblock_via_spambot(client: TelegramClient) -> str:
    try:
        spambot = await client.get_entity(SPAMBOT_USERNAME)

        sent = await client.send_message(spambot, "/start")

        reply = None
        for _ in range(10):
            await asyncio.sleep(2)
            messages = await client.get_messages(spambot, limit=5)
            for msg in messages:
                if msg.id > sent.id and not msg.out and msg.reply_markup:
                    reply = msg
                    break
            if reply:
                break

        if reply is None:
            return "no_button"

        button_text = _extract_first_button_text(reply.reply_markup)
        if button_text is None:
            return "no_button"

        try:
            await reply.click(0, 0)
        except Exception:
            return "error"

        await asyncio.sleep(config.SPAMBOT_UNBLOCK_WAIT)

        bt = button_text.strip().lower()
        if bt in ("ок", "ok"):
            return "button_ok"
        if "спасибо" in bt or "thanks" in bt or "thank" in bt:
            return "button_thanks"
        return "no_button"

    except Exception:
        return "error"


# ============================================================
#  ПРОВЕРКА ЮЗЕРА ПЕРЕД ОТПРАВКОЙ
# ============================================================

async def check_user_skip(client: TelegramClient, user_row) -> tuple[bool, str]:
    try:
        peer = InputPeerUser(user_row.user_id, user_row.access_hash)
        full = await client(GetFullUserRequest(peer))
        u = full.users[0]

        if getattr(u, "bot", False):
            return True, "bot"
        if getattr(u, "deleted", False):
            return True, "deleted"
        if getattr(u, "scam", False) or getattr(u, "fake", False):
            return True, "scam_fake"
        if getattr(u, "premium", False):
            return True, "premium"
        return False, ""
    except Exception as e:
        return False, f"check_error:{type(e).__name__}"


# ============================================================
#  ОТПРАВКА ОДНОГО СООБЩЕНИЯ
# ============================================================

async def send_one(client: TelegramClient, user_row, text: str) -> tuple[bool, str]:
    try:
        peer = InputPeerUser(user_row.user_id, user_row.access_hash)
        await client.send_message(peer, text)
        return True, "ok"
    except PeerFloodError:
        return False, "peerflood"
    except FloodWaitError as e:
        return False, f"floodwait:{e.seconds}"
    except (UserPrivacyRestrictedError, UserNotMutualContactError):
        return False, "privacy"
    except (UserIsBlockedError, YouBlockedUserError):
        return False, "blocked"
    except InputUserDeactivatedError:
        return False, "deactivated"
    except ChatWriteForbiddenError:
        return False, "forbidden"
    except Exception as e:
        return False, f"unknown:{type(e).__name__}:{str(e)[:80]}"


# ============================================================
#  ВОРКЕР ОДНОГО АККАУНТА
# ============================================================

async def run_account_worker(account_row, message_text: str, report_cb=None) -> dict:
    client = await make_client(account_row.session_string)
    label = account_row.label or f"acc#{account_row.id}"
    stats = {"sent": 0, "skipped": 0, "failed": 0, "cooldown": False}

    try:
        while True:
            batch = await db.take_batch_for_account(config.USERS_PER_ACCOUNT)
            if not batch:
                break

            for user_row in batch:
                should_skip, reason = await check_user_skip(client, user_row)
                if should_skip:
                    await db.mark_user(user_row.user_id, "failed", label)
                    await db.log_sent(user_row.user_id, label, "skipped", reason)
                    stats["skipped"] += 1
                    continue

                ok, err = await send_one(client, user_row, message_text)

                if ok:
                    await db.mark_user(user_row.user_id, "sent", label)
                    await db.log_sent(user_row.user_id, label, "sent", "")
                    await db.inc_sent(account_row.id, 1)
                    stats["sent"] += 1
                    await asyncio.sleep(random.randint(config.SEND_MIN_DELAY, config.SEND_MAX_DELAY))
                    continue

                if err == "peerflood":
                    sb = await try_unblock_via_spambot(client)
                    await db.log_sent(user_row.user_id, label, f"spambot:{sb}", "")

                    ok2, err2 = await send_one(client, user_row, message_text)
                    if ok2:
                        await db.mark_user(user_row.user_id, "sent", label)
                        await db.log_sent(user_row.user_id, label, "sent_after_unblock", "")
                        await db.inc_sent(account_row.id, 1)
                        stats["sent"] += 1
                        await asyncio.sleep(random.randint(config.SEND_MIN_DELAY, config.SEND_MAX_DELAY))
                        continue
                    else:
                        cd = datetime.utcnow() + timedelta(seconds=config.ACCOUNT_COOLDOWN)
                        await db.set_account_status(account_row.id, "cooldown", cd)
                        await db.log_sent(user_row.user_id, label, "cooldown_after_unblock", err2)
                        stats["cooldown"] = True
                        break

                if err.startswith("floodwait"):
                    try:
                        secs = int(err.split(":")[1])
                    except Exception:
                        secs = 60
                    await asyncio.sleep(secs + 5)
                    continue

                if err in ("privacy", "blocked", "deactivated", "forbidden"):
                    await db.mark_user(user_row.user_id, "failed", label)
                    await db.log_sent(user_row.user_id, label, "failed", err)
                    stats["failed"] += 1
                    continue

                await db.log_sent(user_row.user_id, label, "error", err)
                stats["failed"] += 1
                continue

            if report_cb:
                await report_cb(label, stats)

            if stats["cooldown"]:
                break

        if report_cb:
            await report_cb(label, stats, final=True)

    finally:
        try:
            await client.disconnect()
        except Exception:
            pass

    return stats


# ============================================================
#  ЗАПУСК РАССЫЛКИ ПАРАЛЛЕЛЬНО
# ============================================================

async def run_broadcast(message_text: str, account_ids: list[int], report_cb=None):
    accounts = []
    for aid in account_ids:
        acc = await db.get_account_by_id(aid)
        if acc and acc.status != "dead":
            accounts.append(acc)

    if not accounts:
        return []

    tasks = [run_account_worker(acc, message_text, report_cb) for acc in accounts]
    return await asyncio.gather(*tasks, return_exceptions=True)

print("=== END FILE ===", flush=true)
