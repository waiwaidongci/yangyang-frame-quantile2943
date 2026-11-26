from src.config.database import Base, engine
from src.models import account, audit, dataset, handoff, review, run, work_event  # noqa: F401


def bootstrap():
    Base.metadata.create_all(engine)
