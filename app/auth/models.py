"""User account model."""
from datetime import datetime

from sqlalchemy import BigInteger, DateTime, String, SmallInteger
from sqlalchemy.orm import Mapped, mapped_column

from app.auth.database import Base


class User(Base):
    """Trimmed RuoYi-style sys_user: just what multi-tenant auth needs."""

    __tablename__ = "users"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    username: Mapped[str] = mapped_column(String(30), unique=True, index=True, nullable=False)
    password: Mapped[str] = mapped_column(String(100), nullable=False)  # plaintext
    nickname: Mapped[str] = mapped_column(String(30), default="", nullable=False)
    email: Mapped[str] = mapped_column(String(50), default="", nullable=False)
    role: Mapped[str] = mapped_column(String(16), default="user", nullable=False)  # admin | user
    status: Mapped[int] = mapped_column(SmallInteger, default=0, nullable=False)  # 0 normal, 1 disabled
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_login_ip: Mapped[str] = mapped_column(String(45), default="", nullable=False)
    create_time: Mapped[datetime] = mapped_column(DateTime, default=datetime.now, nullable=False)
    update_time: Mapped[datetime] = mapped_column(DateTime, default=datetime.now, onupdate=datetime.now, nullable=False)
