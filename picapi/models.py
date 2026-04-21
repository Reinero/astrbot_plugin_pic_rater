from typing import Any, Optional

from pydantic import BaseModel, Field


class ApiError(BaseModel):
    code: str
    message: str
    detail: Any | None = None


class RandomPicOut(BaseModel):
    id: str
    relpath: str
    filename: str
    category: Optional[str] = None
    url: str


class RateIn(BaseModel):
    id: str
    score: float = Field(ge=0.0, le=5.0)
    note: Optional[str] = None


class RateOut(BaseModel):
    id: str
    avg: float
    count: int
    wrote_meta: bool


class ReindexOut(BaseModel):
    indexed: int
    purged: int


class SearchOut(BaseModel):
    q: str
    mode: str
    latency_ms: int
    items: list[dict]
