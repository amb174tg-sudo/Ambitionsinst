import asyncio
import random
from datetime import datetime, timedelta

from telethon import TelegramClient
from telethon.sessions import StringSession
from telethon.errors import (
    FloodWaitError,
    PeerFloodError,
    PeerIdInvalidError,
    UserDeactivatedBanError,
    UserDeactivatedError,
)

import database as db
from config import (
    API_ID,
    API_HASH,
    SEND_MIN_DELAY,
    SEND_MAX_DELAY,
    ACCOUNT_COOLDOWN,
    USERS_PER_ACCOUNT,
)


async def run_account_worker(account_row, text: str, lock: asyncio.Lock):
    """Один аккаунт-отправитель. Резолв только по @username."""
    stats = {"sent": 0, "failed": 0, "skipped": 0}
    label = account_row.label or account_row.username or f"acc:{account_row.id}"
    aid = account_row.id

    # ── Подключение ───────────────────────────────────────────
    client = TelegramClient(StringSession(account_row.session_string), API_ID, API_HASH)
    try:
        await client.connect()
        if not await client.is_user_authorized():
            await db.set_account_status(aid, "dead")
            await client.disconnect()
            return stats
    except Exception as e:
        print(f"[{label}] connect_failed: {e}")
        await db.set_account_status(aid, "dead")
        try:
            await client.disconnect()
        except Exception:
            pass
        return stats

    # ── Батч под локом ────────────────────────────────────────
    async with lock:
        batch = await db.take_batch_for_account(USERS_PER_ACCOUNT)

    if not batch:
        await client.disconnect()
        return stats

    for u in batch:
        await db.mark_user(u.user_id, "taken", label)

    # ── Отправка ──────────────────────────────────────────────
    try:
        for user_row in batch:
            # нет @username — отправить некуда
            if not user_row.username:
                await db.mark_user(user_row.user_id, "failed", label)
                await db.log_sent(user_row.user_id, label, "skipped", "no_username")
                stats["skipped"] += 1
                continue

            try:
                peer = await client.get_entity(f"@{user_row.username}")
                await client.send_message(peer, text)

                await db.mark_user(user_row.user_id, "sent", label)
                await db.log_sent(user_row.user_id, label, "sent")
                await db.inc_sent(aid, 1)
                stats["sent"] += 1

            except FloodWaitError as e:
                # Telegram сказал ждать — ставим cooldown на аккаунт
                wait_until = datetime.utcnow() + timedelta(seconds=e.seconds)
                await db.set_account_status(aid, "cooldown", wait_until)
                await db.mark_user(user_row.user_id, "new", "")
                print(f"[{label}] FloodWait {e.seconds}s → cooldown")
                break

            except PeerFloodError:
                # лимит на аккаунт — длинный cooldown
                wait_until = datetime.utcnow() + timedelta(seconds=ACCOUNT_COOLDOWN)
                await db.set_account_status(aid, "cooldown", wait_until)
                await db.mark_user(user_row.user_id, "new", "")
                print(f"[{label}] PeerFlood → cooldown {ACCOUNT_COOLDOWN}s")
                break

            except (UserDeactivatedBanError, UserDeactivatedError) as e:
                await db.set_account_status(aid, "dead")
                await db.mark_user(user_row.user_id, "new", "")
                print(f"[{label}] BANNED: {e}")
                break

            except (PeerIdInvalidError, ValueError):
                await db.mark_user(user_row.user_id, "failed", label)
                await db.log_sent(user_row.user_id, label, "failed", "peer_invalid")
                stats["failed"] += 1

            except Exception as e:
                await db.mark_user(user_row.user_id, "failed", label)
                await db.log_sent(user_row.user_id, label, "failed", f"{type(e).__name__}: {e}")
                stats["failed"] += 1

            # пауза между отправками — рандом из твоих env
            await asyncio.sleep(random.uniform(SEND_MIN_DELAY, SEND_MAX_DELAY))

    finally:
        try:
            await client.disconnect()
        except Exception:
            pass

    print(f"[{label}] sent={stats['sent']} failed={stats['failed']} skipped={stats['skipped']}")
    return stats


async def run_broadcast(text: str):
    """Запускает воркер на всех аккаунтах-отправителях параллельно."""
    # берём всё, у чего роль допускает отправку
    accounts = await db.list_accounts(role="sender")
    if not accounts:
        accounts = await db.list_accounts()  # fallback — все

    # фильтр: только активные и без активного cooldown
    now = datetime.utcnow()
    accounts = [
        a for a in accounts
        if a.status in ("active", "cooldown")
        and (a.cooldown_until is None or a.cooldown_until <= now)
    ]

    if not accounts:
        print("❌ Нет доступных аккаунтов (active и не в cooldown)")
        return

    print(f"🚀 Старт: {len(accounts)} аккаунтов, батч={USERS_PER_ACCOUNT}, "
          f"delay={SEND_MIN_DELAY}-{SEND_MAX_DELAY}s")

    lock = asyncio.Lock()
    results = await asyncio.gather(
        *[run_account_worker(a, text, lock) for a in accounts],
        return_exceptions=True,
    )

    total = {"sent": 0, "failed": 0, "skipped": 0}
    for r in results:
        if isinstance(r, Exception):
            print(f"⚠️ Воркер упал: {r}")
            continue
        for k in total:
            total[k] += r.get(k, 0)

    print(f"📊 ИТОГО: sent={total['sent']} failed={total['failed']} skipped={total['skipped']}")


if __name__ == "__main__":
    asyncio.run(run_broadcast("Привет! Это тестовое сообщение."))
