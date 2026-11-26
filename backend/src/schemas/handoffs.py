from pydantic import BaseModel, ConfigDict, Field

from src.constants.workflow import HandoffStatus


class HandoffCreate(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)
    label: str = Field(min_length=2, max_length=140)
    dataset_id: int = Field(gt=0)
    as_of: str | None = Field(default=None, min_length=16, max_length=32)
    shift_minutes: int | None = Field(default=None, ge=15, le=720)


class HandoffInfo(BaseModel):
    id: int
    label: str
    dataset_id: int
    as_of: str
    shift_minutes: int
    created_at: str
    status: HandoffStatus
