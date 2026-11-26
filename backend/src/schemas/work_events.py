from pydantic import BaseModel, ConfigDict, Field

from src.constants.workflow import WorkEventKind


class WorkEventInput(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)
    shot_id: str = Field(min_length=1, max_length=64)
    kind: WorkEventKind
    minutes: float = Field(gt=0, le=1440)


class WorkEventBatch(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)
    events: list[WorkEventInput] = Field(min_length=1, max_length=1000)


class WorkEventInfo(BaseModel):
    id: int
    dataset_id: int
    shot_id: str
    kind: WorkEventKind
    minutes: float
    received_at: str
