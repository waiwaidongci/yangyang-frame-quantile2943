from fastapi import APIRouter

from src.controllers.dependencies import Actor, Db, Writer
from src.schemas.work_events import WorkEventBatch, WorkEventInfo
from src.services import work_events

router = APIRouter(prefix='/datasets/{dataset_id}/work-events')


@router.get('', response_model=list[WorkEventInfo])
def list_all(dataset_id: int, session: Db, actor: Actor):
    return work_events.list_events(session, dataset_id)


@router.post('', response_model=list[WorkEventInfo], status_code=201)
def ingest(dataset_id: int, payload: WorkEventBatch, session: Db, actor: Writer):
    return work_events.ingest_events(session, dataset_id, payload, actor)
