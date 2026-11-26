from sqlalchemy import ForeignKey, JSON, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from src.config.database import Base


class Handoff(Base):
    """A point-in-time re-estimation run frozen at ``as_of``.

    The full result is persisted so re-opening an old handoff returns the exact
    P50/P90 tiers computed at that instant, regardless of later work events.
    """

    __tablename__ = 'handoffs'

    id: Mapped[int] = mapped_column(primary_key=True)
    label: Mapped[str] = mapped_column(String(140))
    dataset_id: Mapped[int] = mapped_column(ForeignKey('datasets.id'), index=True)
    as_of: Mapped[str] = mapped_column(String(32))
    shift_minutes: Mapped[int] = mapped_column()
    created_by: Mapped[int] = mapped_column(ForeignKey('accounts.id'))
    created_at: Mapped[str] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(String(16), index=True)
    result: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    error: Mapped[str | None] = mapped_column(Text)
