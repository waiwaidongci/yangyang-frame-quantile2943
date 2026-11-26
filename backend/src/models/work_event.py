from sqlalchemy import ForeignKey, Index, Numeric, String
from sqlalchemy.orm import Mapped, mapped_column

from src.config.database import Base


class WorkEvent(Base):
    """Append-only work-log fact.

    Progress minutes are lower bounds on a shot's total duration; a completion
    records the total.  Receipt time is stamped by the server at ingestion so
    late arrivals can never alter a handoff frozen at an earlier instant.
    """

    __tablename__ = 'work_events'
    __table_args__ = (Index('ix_work_events_dataset_shot', 'dataset_id', 'shot_id'),)

    id: Mapped[int] = mapped_column(primary_key=True)
    dataset_id: Mapped[int] = mapped_column(ForeignKey('datasets.id'))
    shot_id: Mapped[str] = mapped_column(String(64))
    kind: Mapped[str] = mapped_column(String(16))
    minutes: Mapped[float] = mapped_column(Numeric(8, 2))
    received_at: Mapped[str] = mapped_column(String(32), index=True)
    recorded_by: Mapped[int] = mapped_column(ForeignKey('accounts.id'))
