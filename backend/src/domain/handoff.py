"""Mid-shift handoff estimates for shots that have not finished.

The handoff view is frozen at a point in time.  Work-log events received after
that instant do not exist for the handoff, so re-running an old handoff always
reproduces the same two quantile tiers.

Evidence entering the duration pool:

* every historical (``train``) shot is an exact-duration event observation;
* a pending shot whose completion was already received is an event observation
  at its total minutes;
* a pending shot still in progress is right-censored: its elapsed minutes are
  only a lower bound on the total duration.

For each in-progress shot the conditional total-duration quantile is read off
the Kaplan-Meier curve at its elapsed lower bound, then the elapsed minutes are
subtracted to obtain the remaining-work estimate.  A tier the surviving sample
cannot reach is left blank, and a shot without a P90 never takes a next-shift
station.
"""

from bisect import bisect_left
from math import ceil

from .settings import FEATURES, FIELDS
from .survival import Observation, conditional_quantile, kaplan_meier

PROGRESS = "progress"
COMPLETION = "completion"
QUANTILES = (50, 90)


def _received_before(event: dict, as_of: str) -> bool:
    return event["received_at"] <= as_of


def freeze_events(events: list[dict], as_of: str) -> list[dict]:
    """Keep only work-log facts known at the handoff instant."""
    return sorted((event for event in events if _received_before(event, as_of)),
                  key=lambda event: (event["received_at"], event["id"]))


def build_observations(rows: list[dict], events: list[dict]) -> tuple[list[Observation], dict[str, list[dict]]]:
    """Assemble the duration pool and the per-shot event history known as-of."""
    by_shot: dict[str, list[dict]] = {}
    pending = {row["id"]: row for row in rows if row["split"] == "pending"}
    for event in events:
        if event["shot_id"] in pending:
            by_shot.setdefault(event["shot_id"], []).append(event)
    observations = [Observation(row["actual_minutes"], False)
                    for row in rows if row["split"] == "train"]
    for shot_id, log in by_shot.items():
        completion = next((event for event in log if event["kind"] == COMPLETION), None)
        if completion is not None:
            observations.append(Observation(completion["minutes"], False))
        else:
            elapsed = max(event["minutes"] for event in log if event["kind"] == PROGRESS)
            observations.append(Observation(elapsed, True))
    return observations, by_shot


def estimate_handoff(rows: list[dict], events: list[dict], as_of: str,
                     shift_minutes: int) -> dict:
    """Compute remaining P50/P90 for every shot in progress at ``as_of``."""
    known = freeze_events(events, as_of)
    observations, by_shot = build_observations(rows, known)
    curve = kaplan_meier(observations)
    sample_size = len(observations)
    ordered_times = sorted(observation.time for observation in observations)
    pending_rows = sorted((row for row in rows if row["split"] == "pending"),
                          key=lambda row: row["id"])
    shot_rows = []
    in_progress = []
    finished_known = 0
    for row in pending_rows:
        log = by_shot.get(row["id"], [])
        completion = next((event for event in log if event["kind"] == COMPLETION), None)
        progress = [event for event in log if event["kind"] == PROGRESS]
        entry = {key: row[key] for key in ("id", "scene_id", *FEATURES)}
        entry.update({"due_minutes": row["due_minutes"], "event_count": len(log),
                      "p50_total_minutes": None, "p90_total_minutes": None,
                      "p50_remaining_minutes": None, "p90_remaining_minutes": None,
                      "p50_risk_size": None, "p90_risk_size": None,
                      "p90_supported": False, "state": "未开工", "elapsed_minutes": None,
                      "completed_minutes": completion["minutes"] if completion else None})
        if completion is not None:
            entry["state"] = "已完工"
            finished_known += 1
        elif progress:
            elapsed = max(event["minutes"] for event in progress)
            risk_size = sample_size - bisect_left(ordered_times, elapsed)
            entry.update({"state": "进行中", "elapsed_minutes": elapsed})
            for percentile in QUANTILES:
                total = conditional_quantile(curve, elapsed, percentile)
                if total is not None:
                    remaining = round(max(0.0, total - elapsed), 6)
                    entry[f"p{percentile}_total_minutes"] = round(total, 6)
                    entry[f"p{percentile}_remaining_minutes"] = remaining
                    entry[f"p{percentile}_risk_size"] = risk_size
            entry["p90_supported"] = entry["p90_remaining_minutes"] is not None
            in_progress.append(entry)
        shot_rows.append(entry)
    queue, scheduled, reserved = handoff_queue(in_progress, shift_minutes)
    censored = sum(1 for observation in observations if observation.censored)
    warnings = build_warnings(shot_rows, in_progress, sample_size, censored,
                              finished_known, len(known), as_of, len(events))
    profile = handoff_profile(as_of, shift_minutes, sample_size, censored,
                              len(known), len(events), len(in_progress),
                              finished_known, scheduled, reserved)
    summary = (
        f"交班时点 {as_of} 共采信 {len(known)} 条工时记录，"
        f"工时经验池含 {sample_size} 个镜头观测（其中 {censored} 个进行中删失）。"
        f"{len(in_progress)} 个镜头未完工：{scheduled} 个可排入下一班工位，"
        f"预留 {reserved} 分钟，{len(in_progress) - scheduled} 个顺延或样本不足不占工位。"
    )
    return {"summary": summary, "queue_table": queue,
            "shot_table": handoff_shots_table(shot_rows),
            "profile_table": profile, "warnings": warnings,
            "scheduled": scheduled, "reserved": reserved,
            "in_progress": len(in_progress), "deferred": len(in_progress) - scheduled}


def handoff_queue(in_progress: list[dict], shift_minutes: int) -> tuple[dict, int, int]:
    """Greedy single-station plan over remaining P90 for the next shift."""
    def ordering(row):
        return (row["due_minutes"], row["p90_remaining_minutes"] is None,
                -(row["p90_remaining_minutes"] or 0.0), row["id"])

    elapsed = scheduled = 0
    rows = []
    for position, shot in enumerate(sorted(in_progress, key=ordering), start=1):
        start = finish = None
        action, reason, reserved = "顺延复核", "", None
        if shot["p90_remaining_minutes"] is None:
            reason = "P90 样本支撑不足，不占下一班工位"
        else:
            reserved = ceil(shot["p90_remaining_minutes"])
            finish_at = elapsed + reserved
            latest = min(shift_minutes, shot["due_minutes"])
            if finish_at <= latest:
                start, finish = elapsed, finish_at
                elapsed = finish_at
                scheduled += 1
                action = "下一班可排"
                reason = "剩余 P90 预留在交接时限及下班前结束"
            else:
                reason = "预计剩余工时超出交接时限" if shot["due_minutes"] < shift_minutes \
                    else "预计剩余工时超出本班工位容量"
        rows.append({**shot, "position": position, "reserve_minutes": reserved,
                     "start_minute": start, "finish_minute": finish,
                     "action": action, "reason": reason})
    labels = {
        "position": "交接复核顺序", "id": FIELDS["id"], "scene_id": FIELDS["scene_id"],
        **{field: FIELDS[field] for field in FEATURES},
        "elapsed_minutes": "交班时已做分钟（下限）", "due_minutes": FIELDS["due_minutes"],
        "p50_remaining_minutes": "剩余 P50（分钟）", "p90_remaining_minutes": "剩余 P90（分钟）",
        "p50_risk_size": "P50 风险集镜头数", "p90_risk_size": "P90 风险集镜头数",
        "reserve_minutes": "剩余 P90 向上取整预留分钟", "start_minute": "开班后开始分钟",
        "finish_minute": "开班后完成分钟", "action": "排班建议", "reason": "建议依据",
    }
    table = {"key": "handoff_queue", "title": "擦线班交班重估工时单",
             "columns": list(labels), "labels": labels, "rows": rows}
    return table, scheduled, elapsed


def handoff_shots_table(shot_rows: list[dict]) -> dict:
    labels = {
        "id": FIELDS["id"], "scene_id": FIELDS["scene_id"], "state": "交班状态",
        "elapsed_minutes": "交班时已做分钟（下限）", "completed_minutes": "完工总分钟",
        "p50_total_minutes": "总工时 P50（分钟）", "p90_total_minutes": "总工时 P90（分钟）",
        "p50_remaining_minutes": "剩余 P50（分钟）", "p90_remaining_minutes": "剩余 P90（分钟）",
        "p50_risk_size": "P50 风险集镜头数", "p90_risk_size": "P90 风险集镜头数",
        "p90_supported": "P90 是否可估", "event_count": "采信记录条数",
        "due_minutes": FIELDS["due_minutes"],
    }
    return {"key": "handoff_shots", "title": "交班镜头分位重估明细",
            "columns": list(labels), "labels": labels, "rows": shot_rows}


def handoff_profile(as_of: str, shift_minutes: int, sample_size: int, censored: int,
                    known_events: int, total_events: int, in_progress: int,
                    finished: int, scheduled: int, reserved: int) -> dict:
    rows = [
        {"parameter": "as_of", "description": "交班冻结时点（UTC）", "value": as_of},
        {"parameter": "shift_minutes", "description": "单工位本班可用分钟数", "value": shift_minutes},
        {"parameter": "pool_size", "description": "工时经验池镜头观测数", "value": sample_size},
        {"parameter": "censored_size", "description": "其中右删失（进行中）观测数", "value": censored},
        {"parameter": "events_known", "description": "交班时点前采信的工时记录数", "value": known_events},
        {"parameter": "events_late", "description": "交班时点后才收到、本次不采信的记录数",
         "value": total_events - known_events},
        {"parameter": "in_progress", "description": "交班时未完工镜头数", "value": in_progress},
        {"parameter": "finished_logged", "description": "已收到完工记录的待排镜头数", "value": finished},
        {"parameter": "scheduled", "description": "下一班可排镜头数", "value": scheduled},
        {"parameter": "reserved_minutes", "description": "下一班累计预留分钟", "value": reserved},
    ]
    return {"key": "handoff_profile", "title": "交班冻结口径与样本规模",
            "columns": ["parameter", "description", "value"],
            "labels": {"parameter": "口径项", "description": "说明", "value": "取值"}, "rows": rows}


def build_warnings(shot_rows, in_progress, sample_size, censored, finished,
                   known_events, as_of, total_events) -> list[str]:
    warnings = []
    if sample_size == 0:
        warnings.append("工时经验池为空，无法估计任何分位。")
        return warnings
    unsupported_p90 = [row["id"] for row in in_progress if not row["p90_supported"]]
    if unsupported_p90:
        warnings.append(f"{len(unsupported_p90)} 个镜头的 P90 超出存活样本支撑，留空且不占下一班工位："
                        + "、".join(unsupported_p90) + "。")
    unsupported_p50 = [row["id"] for row in in_progress if row["p50_remaining_minutes"] is None]
    if unsupported_p50:
        warnings.append(f"{len(unsupported_p50)} 个镜头的 P50 同样超出样本支撑，两档均留空："
                        + "、".join(unsupported_p50) + "。")
    idle = [row["id"] for row in shot_rows if row["state"] == "未开工"]
    if idle:
        warnings.append(f"{len(idle)} 个待排镜头尚无任何工时记录，按未开工处理且不参与本次排班："
                        + "、".join(idle) + "。")
    if total_events > known_events:
        warnings.append(f"{total_events - known_events} 条完工或进度记录晚于交班时点 {as_of} 才收到，本次未采信。")
    if censored:
        warnings.append(f"{censored} 个进行中镜头仅以已做分钟作为总工时下限参与估计（右删失）。")
    if finished:
        warnings.append(f"{finished} 个待排镜头已在交班时点前完工，其总工时作为完工观测进入经验池。")
    return warnings
