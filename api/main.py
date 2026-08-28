"""HTTPS-facing metric ingestion for Apple Health/iPhone Shortcuts."""

from __future__ import annotations

import hmac
import math
import os
import sys
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel, Field, field_validator

OPS_DIR = Path(__file__).resolve().parents[1] / "ops"
if str(OPS_DIR) not in sys.path:
    sys.path.insert(0, str(OPS_DIR))

from location import current_tz  # noqa: E402
from logs import Logs  # noqa: E402
from habitify import HabitifyClient, HabitifyError, HabitifyHabitSync  # noqa: E402


class MetricInput(BaseModel):
    key: str = Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")
    value: float
    unit: str = Field(default="", max_length=16)
    recorded_at: datetime | None = None

    @field_validator("value")
    @classmethod
    def finite_value(cls, value: float) -> float:
        if not math.isfinite(value):
            raise ValueError("value must be finite")
        return value

    @field_validator("recorded_at")
    @classmethod
    def timezone_required(cls, value: datetime | None) -> datetime | None:
        if value is not None and value.tzinfo is None:
            raise ValueError("recorded_at must include a timezone")
        return value


def create_app(
    *,
    ingest_token: str | None = None,
    logs: Logs | None = None,
    habitify_sync: HabitifyHabitSync | None = None,
) -> FastAPI:
    token = ingest_token if ingest_token is not None else os.getenv("INGEST_TOKEN", "")
    data_dir = Path(os.getenv("OPS_DATA_DIR", OPS_DIR / "log"))
    log_store = logs
    habit_sync = habitify_sync

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        nonlocal log_store, habit_sync
        if len(token) < 32:
            raise RuntimeError("INGEST_TOKEN must be set and at least 32 characters")
        if log_store is None:
            log_store = Logs(str(data_dir))
        habitify_key = os.getenv("HABITIFY_API_KEY", "").strip()
        if habit_sync is None and habitify_key:
            habit_sync = HabitifyHabitSync(HabitifyClient(habitify_key))
        yield

    app = FastAPI(title="Personal Ops Ingest", lifespan=lifespan)

    def authorize(authorization: str | None = Header(default=None)) -> None:
        scheme, _, supplied = (authorization or "").partition(" ")
        if scheme.lower() != "bearer" or not hmac.compare_digest(supplied, token):
            raise HTTPException(status_code=401, detail="unauthorized")

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/metrics", dependencies=[Depends(authorize)])
    def write_metric(metric: MetricInput) -> dict:
        assert log_store is not None
        when = (
            metric.recorded_at.astimezone(current_tz())
            if metric.recorded_at is not None
            else datetime.now(current_tz())
        )
        ts = when.isoformat(timespec="seconds")
        duplicate = log_store.db.metric_exists(ts, metric.key)
        if not duplicate:
            try:
                log_store.write_metric(
                    metric.key, metric.value, metric.unit, when=when
                )
            except Exception as exc:
                raise HTTPException(
                    status_code=500,
                    detail={"message": "db write failed", "kept_in_jsonl": True},
                ) from exc
        habitify_synced = None
        if metric.key == "weight" and habit_sync is not None:
            try:
                habit_sync.complete("Weigh in", when.date().isoformat())
                habitify_synced = True
            except HabitifyError as exc:
                raise HTTPException(
                    status_code=502,
                    detail={
                        "message": "metric saved but Habitify sync failed; retry safely",
                        "metric_saved": True,
                    },
                ) from exc
        return {
            "logged": {
                "key": metric.key,
                "value": metric.value,
                "unit": metric.unit,
                "recorded_at": ts,
            },
            "duplicate": duplicate,
            "habitify_synced": habitify_synced,
        }

    return app


app = create_app()
