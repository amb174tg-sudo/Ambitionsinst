import asyncio
import logging

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    Message,
    CallbackQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)

from telethon.sessions import StringSession
from telethon import TelegramClient

import config
import database as db
import worker


logging.basicConfig(level=logging.INFO)
router = Router()


class FS(StatesGroup):
    add_parser_session = State()
    add_sender_session = State()
    parse_group = State()
    broadcast_text = State()


MAIN_KB = InlineKeyboardMarkup(inline_keyboard=[
    [InlineKeyboardButton(text="➕ Добавить аккаунт (парсер)", callback_data="add_parser")],
    [InlineKeyboardButton(text="➕ Добавить аккаунт (рассылка)", callback_data="add_sender")],
    [InlineKeyboardButton(text="📥 Пополнить базу", callback_data="parse_menu")],
    [InlineKeyboardButton(text="📤 Начать рассылку", callback_data="bc_menu")],
    [InlineKeyboardButton(text="🧾 Выдать 100 юзеров", callback_data="export_100")],
    [InlineKeyboardButton(text="👥 Аккаунты", callback_data="accounts")],
    [InlineKeyboardButton(text="📊 Статистика", callback_data="stats")],
])


def owner_only(m: Message):
    return m.from_user and m.from_user.id == config.OWNER_ID


@router.message(CommandStart())
async def cmd_start(m: Message):
    if not owner_only(m):
        return
    await db.init_db()
    await m.answer("Панель управления:", reply_markup=MAIN_KB)


@router.callback_query(F.data == "menu")
async def back_menu(c: CallbackQuery):
    await c.message.edit_text("Панель управления:", reply_markup=MAIN_KB)
    await c.answer()


@router.callback_query(F.data == "stats")
async def stats(c: CallbackQuery):
    new_cnt = await db.count_by_status("new")
    sent_cnt = await db.count_by_status("sent")
    failed_cnt = await db.count_by_status("failed")
    await c.message.edit_text(
        f"📊 База:\n• Новые: {new_cnt}\n• Отправлено: {sent_cnt}\n• Пропущено: {failed_cnt}",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="⬅️ Назад", callback_data="menu")]
        ])
    )
    await c.answer()


@router.callback_query(F.data == "accounts")
async def accounts_menu(c: CallbackQuery):
    accs = await db.list_accounts()
    if not accs:
        text = "Аккаунтов нет."
    else:
        lines = []
        for a in accs:
            lines.append(f"#{a.id} [{a.role}] {a.label or a.username or '-'} | {a.status} | sent={a.sent_count}")
        text = "\n".join(lines)
    await c.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="menu")]
    ]))
    await c.answer()


# ---------- ВЫДАТЬ 100 ЮЗЕРОВ ----------
@router.callback_query(F.data == "export_100")
async def export_100(c: CallbackQuery):
    if c.from_user.id != config.OWNER_ID:
        await c.answer()
        return

    users = await db.take_users_for_export(100)
    if not users:
        await c.message.answer("База пуста.")
        await c.answer()
        return

    lines = []
    for u in users:
        username = u["username"] if isinstance(u, dict) else getattr(u, "username", "")
        user_id = u["user_id"] if isinstance(u, dict) else getattr(u, "user_id", None)
        lines.append(f"@{username}" if username else str(user_id))

    chunk = ""
    for line in lines:
        if len(chunk) + len(line) + 1 > 3500:
            await c.message.answer(chunk)
            chunk = ""
        chunk += line + "\n"
    if chunk:
        await c.message.answer(chunk)

    await c.message.answer(
        f"✅ Выдано и удалено из базы: {len(users)} юзеров",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="⬅️ Назад", callback_data="menu")]
        ])
    )
    await c.answer()


# ---------- ADD SESSION ----------
@router.callback_query(F.data == "add_parser")
async def add_parser(c: CallbackQuery, state: FSMContext):
    await state.set_state(FS.add_parser_session)
    await c.message.edit_text("Отправь <b>строку сессии</b> для аккаунта-парсера:")
    await c.answer()


@router.callback_query(F.data == "add_sender")
async def add_sender(c: CallbackQuery, state: FSMContext):
    await state.set_state(FS.add_sender_session)
    await c.message.edit_text("Отправь <b>строку сессии</b> для аккаунта-рассылки:")
    await c.answer()


async def _validate_session(session_string: str) -> str:
    client = TelegramClient(StringSession(session_string), config.API_ID, config.API_HASH)
    await client.connect()
    try:
        if not await client.is_user_authorized():
            return ""
        me = await client.get_me()
        return f"@{me.username}" if me.username else f"id{me.id}"
    finally:
        await client.disconnect()


@router.message(FS.add_parser_session)
async def save_parser(m: Message, state: FSMContext):
    if not owner_only(m):
        return
    ss = m.text.strip()
    try:
        uname = await _validate_session(ss)
    except Exception as e:
        await m.answer(f"Ошибка сессии: {e}")
        return
    if not uname:
        await m.answer("Сессия не авторизована.")
        return
    aid = await db.add_account(ss, role="parser", label=uname, username=uname)
    await state.clear()
    await m.answer(f"✅ Парсер добавлен: #{aid} {uname}", reply_markup=MAIN_KB)


@router.message(FS.add_sender_session)
async def save_sender(m: Message, state: FSMContext):
    if not owner_only(m):
        return
    ss = m.text.strip()
    try:
        uname = await _validate_session(ss)
    except Exception as e:
        await m.answer(f"Ошибка сессии: {e}")
        return
    if not uname:
        await m.answer("Сессия не авторизована.")
        return
    aid = await db.add_account(ss, role="sender", label=uname, username=uname)
    await state.clear()
    await m.answer(f"✅ Рассыльщик добавлен: #{aid} {uname}", reply_markup=MAIN_KB)


# ---------- PARSE ----------
@router.callback_query(F.data == "parse_menu")
async def parse_menu(c: CallbackQuery):
    parsers = await db.list_accounts(role="parser")
    if not parsers:
        await c.message.edit_text("Нет парсеров.", reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="⬅️ Назад", callback_data="menu")]
        ]))
        await c.answer()
        return
    kb = [[InlineKeyboardButton(text=f"#{a.id} {a.label}", callback_data=f"parse_with:{a.id}")] for a in parsers]
    kb.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="menu")])
    await c.message.edit_text("Выбери парсер:", reply_markup=InlineKeyboardMarkup(inline_keyboard=kb))
    await c.answer()


@router.callback_query(F.data.startswith("parse_with:"))
async def parse_with(c: CallbackQuery, state: FSMContext):
    aid = int(c.data.split(":")[1])
    await state.update_data(parser_id=aid)
    await state.set_state(FS.parse_group)
    await c.message.edit_text("Отправь ссылку/@username группы:")
    await c.answer()


@router.message(FS.parse_group)
async def do_parse(m: Message, state: FSMContext):
    if not owner_only(m):
        return
    data = await state.get_data()
    aid = data.get("parser_id")
    acc = await db.get_account_by_id(aid)
    if not acc:
        await m.answer("Парсер не найден.")
        await state.clear()
        return
    link = m.text.strip()
    status = await m.answer("⏳ Парсинг запущен...")
    fetched = {"n": 0}

    async def progress(n):
        fetched["n"] = n

    try:
        users = await worker.parse_group(acc.session_string, link, progress_cb=progress)
    except Exception as e:
        await m.answer(f"Ошибка: {e}")
        await state.clear()
        return

    added = await db.add_parsed_users(users, source=link)
    await state.clear()
    await m.answer(f"✅ Найдено: {len(users)}\nДобавлено в базу: {added}", reply_markup=MAIN_KB)


# ---------- BROADCAST ----------
@router.callback_query(F.data == "bc_menu")
async def bc_menu(c: CallbackQuery):
    senders = await db.list_accounts(role="sender")
    if not senders:
        await c.message.edit_text("Нет рассыльщиков.", reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="⬅️ Назад", callback_data="menu")]
        ]))
        await c.answer()
        return
    kb = [[InlineKeyboardButton(text=f"#{a.id} {a.label}", callback_data=f"bc_with:{a.id}")] for a in senders]
    kb.append([InlineKeyboardButton(text="🚀 Все сразу", callback_data="bc_with:all")])
    kb.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="menu")])
    await c.message.edit_text("Выбери аккаунты для рассылки:", reply_markup=InlineKeyboardMarkup(inline_keyboard=kb))
    await c.answer()


@router.callback_query(F.data.startswith("bc_with:"))
async def bc_with(c: CallbackQuery, state: FSMContext):
    val = c.data.split(":")[1]
    await state.update_data(sender_sel=val)
    await state.set_state(FS.broadcast_text)
    await c.message.edit_text("Отправь текст рассылки:")
    await c.answer()


@router.message(FS.broadcast_text)
async def do_broadcast(m: Message, state: FSMContext):
    if not owner_only(m):
        return
    data = await state.get_data()
    sel = data.get("sender_sel")
    text = m.text
    senders = await db.list_accounts(role="sender")
    if sel == "all":
        ids = [a.id for a in senders]
    else:
        ids = [int(sel)]
    await state.clear()

    status_msg = await m.answer("🚀 Запускаю рассылку...")

    async def report(label, stats, final=False):
        try:
            await status_msg.edit_text(
                f"Аккаунт {label}: sent={stats['sent']} skip={stats['skipped']} "
                f"fail={stats['failed']}{' | COOLDOWN' if stats.get('cooldown') else ''}"
                + ("\n✅ Завершён" if final else "")
            )
        except Exception:
            pass

    await worker.run_broadcast(text, ids, report_cb=report)
    await m.answer("✅ Рассылка завершена.", reply_markup=MAIN_KB)


async def main():
    await db.init_db()
    bot = Bot(token=config.BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
