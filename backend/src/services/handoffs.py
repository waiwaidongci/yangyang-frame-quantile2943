"""Point-in-time handoff re-estimation runs and their exports."""

from datetime import datetime, timezone

from fastapi import HTTPException
from sqlalchemy import select

from src.constants.workflow import HandoffStatus
from src.domain.handoff import estimate_handoff
from src.models.handoff import Handoff
from src.models.work_event import WorkEvent
from src.services.audit import record
from src.services.catalog import get_dataset
from src.utils.time import utc_stamp

EXPORTABLE_TABLES = ("handoff_queue", "handoff_shots", "handoff_profile")


def parse_as_of(value: str | None) -> str:
    """Validate an explicit handoff instant or stamp the current UTC time."""
    if value is None:
        return utc_stamp()
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as error:
        raise ValueError("as_of 须为带时区的 ISO 8601 时间，例如 2026-11-26T13:00:00+00:00。") from error
    if parsed.tzinfo is None:
        raise ValueError("as_of 必须携带时区偏移，无法解释无时区时间。")
    now = datetime.now(timezone.utc)
    if parsed.astimezone(timezone.utc) > now:
        raise ValueError("交班时点不能晚于当前时间，晚到的记录应等待下一次交班。")
    # Normalize to UTC so lexicographic cutoff against UTC receipt stamps is chronological.
    return parsed.astimezone(timezone.utc).isoformat(timespec="seconds")


def info(handoff: Handoff) -> dict:
    return {"id": handoff.id, "label": handoff.label, "dataset_id": handoff.dataset_id,
            "as_of": handoff.as_of, "shift_minutes": handoff.shift_minutes,
            "created_at": handoff.created_at, "status": handoff.status}


def create_handoff(session, payload, actor) -> dict:
    dataset = get_dataset(session, payload.dataset_id)
    as_of = parse_as_of(payload.as_of)
    shift_minutes = payload.shift_minutes if payload.shift_minutes is not None else 240
    handoff = Handoff(label=payload.label, dataset_id=dataset.id, as_of=as_of,
                      shift_minutes=shift_minutes, created_by=actor.id,
                      created_at=utc_stamp(), status=HandoffStatus.COMPLETED)
    try:
        events = [
            {"id": event.id, "shot_id": event.shot_id, "kind": event.kind,
             "minutes": float(event.minutes), "received_at": event.received_at}
            for event in session.scalars(
                select(WorkEvent).where(WorkEvent.dataset_id == dataset.id).order_by(WorkEvent.id))
        ]
        outcome = estimate_handoff(dataset.rows, events, as_of, shift_minutes)
        handoff.result = {
            "summary": outcome["summary"], "metrics": {
                "未完工镜头数": outcome["in_progress"],
                "下一班已分配镜头数": outcome["scheduled"],
                "待顺延镜头数": outcome["deferred"],
                "下一班累计预留分钟": outcome["reserved"],
            },
            "tables": [outcome["queue_table"], outcome["shot_table"], outcome["profile_table"]],
            "warnings": outcome["warnings"],
        }
    except ValueError as error:
        handoff.status = HandoffStatus.FAILED
        handoff.error = str(error)
    session.add(handoff)
    session.flush()
    record(session, 'handoff.' + handoff.status, handoff.id, actor.id)
    session.commit()
    if handoff.status == HandoffStatus.FAILED:
        raise ValueError(handoff.error)
    return info(handoff)


def list_handoffs(session, actor):
    statement = select(Handoff).order_by(Handoff.id.desc()).limit(100)
    return [info(handoff) for handoff in session.scalars(statement)]


def get_handoff(session, handoff_id: int, actor) -> Handoff:
    handoff = session.get(Handoff, handoff_id)
    if handoff is None:
        raise LookupError("找不到交班记录")
    return handoff


def export_table(session, handoff_id: int, key: str, actor):
    handoff = get_handoff(session, handoff_id, actor)
    if handoff.status != HandoffStatus.COMPLETED:
        raise HTTPException(409, "交班计算失败，没有可导出的工时单")
    if key not in EXPORTABLE_TABLES:
        raise LookupError("找不到结果表")
    for table in handoff.result["tables"]:
        if table["key"] == key:
            return table
    raise LookupError("找不到结果表")
