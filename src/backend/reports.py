"""Read-only, bounded report snapshots with filters applied before sampling."""
from collections import Counter
from datetime import datetime, timedelta, timezone

from pydantic import BaseModel, ConfigDict, Field, model_validator

from backend.workbriefing.service import WorkBriefingService

REPORT_LIMIT = 10000


class ReportFilters(BaseModel):
    model_config = ConfigDict(extra="forbid")
    start: datetime
    end: datetime
    project_ids: list[str] = Field(default_factory=list, max_length=100)
    event_type: str = Field(default="", max_length=100)
    status: str = Field(default="", max_length=100)
    search: str = Field(default="", max_length=200)

    @model_validator(mode="after")
    def validate_window(self):
        if self.start.tzinfo is None or self.end.tzinfo is None:
            raise ValueError("Report dates require an explicit timezone.")
        self.start = self.start.astimezone(timezone.utc)
        self.end = self.end.astimezone(timezone.utc)
        if not timedelta(0) < self.end - self.start <= timedelta(days=366):
            raise ValueError("Choose a nonempty report window no longer than 366 days.")
        if any(not item.strip() or len(item) > 200 for item in self.project_ids):
            raise ValueError("Invalid project identifier.")
        return self


async def build_report(service: WorkBriefingService, filters: ReportFilters) -> dict:
    rows, truncated = await service.report_rows(**filters.model_dump(), limit=REPORT_LIMIT)
    activities = [{
        "activity_id": row.activity_id, "project_id": row.project_id,
        "event_type": row.event_type, "status": row.status or "unspecified",
        "title": row.title, "summary": row.summary,
        "occurred_at": row.occurred_at.isoformat(),
    } for row in rows]
    return {
        "schema_version": "cga-report.v1", "generated_at": datetime.now(timezone.utc).isoformat(),
        "filters": filters.model_dump(mode="json"), "activities": activities,
        "counts": {
            "events": len(rows), "projects": len({row.project_id for row in rows}),
            "by_type": dict(Counter(row.event_type for row in rows)),
            "by_status": dict(Counter(row.status or "unspecified" for row in rows)),
            "by_project": dict(Counter(row.project_id for row in rows)),
            "by_day": dict(sorted(Counter(
                row.occurred_at.astimezone(timezone.utc).date().isoformat() for row in rows
            ).items())),
        },
        "coverage": {
            "truncated": truncated, "limit": REPORT_LIMIT,
            "note": (
                "Partial report: newest 10,000 matching records only. Narrow the filters before interpreting totals."
                if truncated else "All matching valid activity records in the selected window."
            ),
            "scope": "Recorded CGA work activity only; not all repository work, productivity or proven AI ROI.",
        },
    }
