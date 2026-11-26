"""Register all SQLAlchemy tables in every entry point using this package."""

from .account import Account
from .audit import Audit
from .dataset import Dataset
from .handoff import Handoff
from .review import Review
from .run import Run
from .work_event import WorkEvent

__all__ = ['Account', 'Audit', 'Dataset', 'Handoff', 'Review', 'Run', 'WorkEvent']
