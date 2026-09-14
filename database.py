import os
from datetime import datetime
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy import select, delete, func, update, BigInteger
from config import DATABASE_URL

os.makedirs("data", exist_ok=True)

engine = create_async_engine(DATABASE_URL, echo=False)
Session = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)


class Base(DeclarativeBase):
    pass


class Account(Base):
    __tablename__ = "accounts"
    id: Mapped[int] = mapped_column(primary_key=True)
    session_string: Mapped[str] = mapped_column(unique=True)
    role: Mapped[str] = mapped_column(default="parser")
    label: Mapped[str] = mapped_column(default="")
    username: Mapped[str] = mapped_column(default="")
    status: Mapped[str] = mapped_column(default="active")
    cooldown_until: Mapped[datetime | None] = mapped_column(nullable=True)
    sent_count: Mapped[int] = mapped_column(default=0)
    created_at: Mapped[datetime] = mapped_column(default=datetime.utcnow)


class ParsedUser(Base):
    __tablename__ = "parsed_users"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(BigInteger, index=True)
    username: Mapped[str | None] = mapped_column(nullable=True, index=True)
    access_hash: Mapped[int] = mapped_column(BigInteger, default=0)
    source: Mapped[str] = mapped_column(default="")
    status: Mapped[str] = mapped_column(default="new")
    added_at: Mapped[datetime] = mapped_column(default=datetime.utcnow)
    sent_at: Mapped[datetime | None] = mapped_column(nullable=True)
    sent_by: Mapped[str] = mapped_column(default="")


class SentLog(Base):
    __tablename__ = "sent_log"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(BigInteger, index=True)
    account_label: Mapped[str] = mapped_column(default="")
    result: Mapped[str] = mapped_column(default="")
    error: Mapped[str] = mapped_column(default="")
    ts: Mapped[datetime] = mapped_column(default=datetime.utcnow)


async def init_db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def add_account(session_string: str, role: str, label: str = "", username: str = "") -> int:
    async with Session() as s:
        existing = await s.execute(
            select(Account).where(Account.session_string == session_string)
        )
        acc = existing.scalar_one_or_none()
        if acc:
            return acc.id
        acc = Account(session_string=session_string, role=role, label=label, username=username)
        s.add(acc)
        await s.commit()
        return acc.id


async def list_accounts(role: str | None = None):
    async with Session() as s:
        q = select(Account)
        if role:
            q = q.where(Account.role.in_([role, "both"]))
        res = await s.execute(q)
        return res.scalars().all()


async def get_account_by_id(aid: int):
    async with Session() as s:
        return await s.get(Account, aid)


async def set_account_status(aid: int, status: str, cooldown_until=None):
    async with Session() as s:
        await s.execute(
            update(Account).where(Account.id == aid).values(
                status=status, cooldown_until=cooldown_until
            )
        )
        await s.commit()


async def inc_sent(aid: int, delta: int = 1):
    async with Session() as s:
        acc = await s.get(Account, aid)
        if acc:
            acc.sent_count += delta
            await s.commit()


async def delete_account(aid: int):
    async with Session() as s:
        await s.execute(delete(Account).where(Account.id == aid))
        await s.commit()


async def add_parsed_users(users: list[dict], source: str) -> int:
    added = 0
    async with Session() as s:
        for u in users:
            exists = await s.execute(
                select(ParsedUser.id).where(ParsedUser.user_id == u["user_id"])
            )
            if exists.scalar():
                continue
            s.add(ParsedUser(
                user_id=u["user_id"],
                username=u.get("username"),
                access_hash=u.get("access_hash", 0),
                source=source,
                status="new",
            ))
            added += 1
        await s.commit()
    return added


async def take_batch_for_account(limit: int):
    async with Session() as s:
        res = await s.execute(
            select(ParsedUser).where(ParsedUser.status == "new").limit(limit)
        )
        return res.scalars().all()


# ================= NEW: выдать N юзеров и удалить из базы =================
async def take_users_for_export(limit: int = 100) -> list[dict]:
    """
    Атомарно берёт до `limit` юзеров со статусом 'new',
    удаляет их из базы и возвращает список dict:
    [{"user_id": int, "username": str|None}, ...]
    Если база пуста — вернёт [].
    """
    async with Session() as s:
        res = await s.execute(
            select(ParsedUser).where(ParsedUser.status == "new").limit(limit)
        )
        rows = res.scalars().all()
        if not rows:
            return []

        result = [{"user_id": r.user_id, "username": r.username} for r in rows]
        ids = [r.id for r in rows]

        await s.execute(delete(ParsedUser).where(ParsedUser.id.in_(ids)))
        await s.commit()

    return result
# ========================================================================


async def count_by_status(status: str) -> int:
    async with Session() as s:
        res = await s.execute(
            select(func.count()).select_from(ParsedUser).where(ParsedUser.status == status)
        )
        return res.scalar() or 0


async def mark_user(uid: int, status: str, sent_by: str = ""):
    async with Session() as s:
        await s.execute(
            update(ParsedUser).where(ParsedUser.user_id == uid).values(
                status=status,
                sent_by=sent_by,
                sent_at=datetime.utcnow(),
            )
        )
        await s.commit()


async def log_sent(user_id: int, account_label: str, result: str, error: str = ""):
    async with Session() as s:
        s.add(SentLog(user_id=user_id, account_label=account_label,
                      result=result, error=error))
        await s.commit()


async def reset_sent_to_new():
    async with Session() as s:
        await s.execute(update(ParsedUser).where(ParsedUser.status == "sent").values(status="new"))
        await s.commit()
