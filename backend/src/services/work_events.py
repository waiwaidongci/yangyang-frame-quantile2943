"""Append-only ingestion of progress and completion work-log events."""

from sqlalchemy import select

from src.constants.workflow import WorkEventKind
from src.models.work_event import WorkEvent
from src.services.audit import record
from src.services.catalog import get_dataset
from src.utils.time import utc_stamp


def _event_info(event: WorkEvent) -> dict:
    return {"id": event.id, "dataset_id": event.dataset_id, "shot_id": event.shot_id,
            "kind": event.kind, "minutes": float(event.minutes), "received_at": event.received_at}


def ingest_events(session, dataset_id: int, payload, actor) -> list[dict]:
    dataset = get_dataset(session, dataset_id)
    pending = {row["id"]: row for row in dataset.rows if row["split"] == "pending"}
    history = {
        shot_id: {"completed": False, "max_progress": 0.0}
        for shot_id in pending
    }
    for event in session.scalars(select(WorkEvent).where(WorkEvent.dataset_id == dataset_id)):
        state = history.setdefault(event.shot_id, {"completed": False, "max_progress": 0.0})
        if event.kind == WorkEventKind.COMPLETION:
            state["completed"] = True
        else:
            state["max_progress"] = max(state["max_progress"], float(event.minutes))

    received_at = utc_stamp()
    stored = []
    for item in payload.events:
        shot_id = item.shot_id
        if shot_id not in pending:
            raise ValueError(f"镜头 {shot_id} 不在台账 {dataset.name} 的待排镜头中，不能登记工时记录。")
        state = history[shot_id]
        minutes = round(float(item.minutes), 2)
        if state["completed"]:
            raise ValueError(f"镜头 {shot_id} 已登记完工，不能再追加工时记录。")
        if item.kind == WorkEventKind.PROGRESS:
            if minutes <= state["max_progress"]:
                raise ValueError(
                    f"镜头 {shot_id} 的进度工时须大于已登记的 {state['max_progress']:.2f} 分钟。")
            state["max_progress"] = minutes
        else:
            if minutes < state["max_progress"]:
                raise ValueError(
                    f"镜头 {shot_id} 的完工总工时 {minutes:.2f} 分钟不能低于已做分钟 "
                    f"{state['max_progress']:.2f}。")
            state["completed"] = True
        event = WorkEvent(dataset_id=dataset_id, shot_id=shot_id, kind=item.kind,
                          minutes=minutes, received_at=received_at, recorded_by=actor.id)
        session.add(event)
        stored.append(event)
    session.flush()
    record(session, 'work_event.ingested', dataset_id, actor.id)
    session.commit()
    return [_event_info(event) for event in stored]


def list_events(session, dataset_id: int) -> list[dict]:
    get_dataset(session, dataset_id)
    events = session.scalars(select(WorkEvent).where(WorkEvent.dataset_id == dataset_id)
                             .order_by(WorkEvent.id)).all()
    return [_event_info(event) for event in events]
