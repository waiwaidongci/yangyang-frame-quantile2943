import json

from sqlalchemy import func, select

from migrations.bootstrap import bootstrap
from src.config.database import Session
from src.constants.workflow import Role, WorkEventKind
from src.domain.project import SPEC, demo_rows
from src.models.account import Account
from src.models.dataset import Dataset
from src.models.work_event import WorkEvent
from src.services.catalog import add_dataset
from src.utils.security import hash_password
from src.utils.time import utc_stamp

# Deterministic mid-shift facts for the demo handoff; keyed by pending shot.
DEMO_EVENTS = {
    'RA-P01-01': [(WorkEventKind.PROGRESS, 42.0)],
    'RA-P01-02': [(WorkEventKind.PROGRESS, 18.5), (WorkEventKind.COMPLETION, 64.0)],
    'RA-P01-03': [(WorkEventKind.PROGRESS, 55.0)],
    'RA-P02-01': [(WorkEventKind.PROGRESS, 71.0)],
    'RA-P02-02': [(WorkEventKind.PROGRESS, 9.0)],
    'RA-P02-03': [(WorkEventKind.PROGRESS, 30.0)],
}


def seed():
    with Session.begin() as session:
        owner = None
        for role in Role:
            email = f'{role.value}@demo.local'
            account = session.scalar(select(Account).where(Account.email == email))
            if account is None:
                account = Account(email=email, role=role, password_hash=hash_password('Demo-2611!'))
                session.add(account)
                session.flush()
            if role == Role.ADMIN:
                owner = account.id
        name = SPEC['dataset_name']
        dataset = session.scalar(select(Dataset).where(Dataset.name == name))
        if not dataset:
            dataset = add_dataset(session, name, demo_rows(), owner)
        if session.scalar(select(func.count()).select_from(WorkEvent)
                          .where(WorkEvent.dataset_id == dataset.id)) == 0:
            received_at = utc_stamp()
            pending = {row['id'] for row in dataset.rows if row['split'] == 'pending'}
            for shot_id, log in DEMO_EVENTS.items():
                if shot_id not in pending:
                    continue
                for kind, minutes in log:
                    session.add(WorkEvent(dataset_id=dataset.id, shot_id=shot_id, kind=kind,
                                          minutes=minutes, received_at=received_at, recorded_by=owner))
        return {'accounts': session.scalar(select(func.count()).select_from(Account)),
                'datasets': session.scalar(select(func.count()).select_from(Dataset)),
                'demo_rows': len(dataset.rows), 'checksum': dataset.checksum,
                'work_events': session.scalar(select(func.count()).select_from(WorkEvent)
                                              .where(WorkEvent.dataset_id == dataset.id))}


if __name__ == '__main__':
    bootstrap()
    print(json.dumps(seed(), ensure_ascii=False))
