"""Persistent unlimited history management for bili2am."""

from __future__ import annotations

import json
import threading
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

from bili2am.models import PipelineResult


class HistorySummary(BaseModel):
    """Lightweight metadata entry stored in history/index.json."""

    id: str
    created_at: str
    updated_at: str
    input_url: str
    bvid: str = ""
    title: str = ""
    mode: str = "auto"
    storefront: str = "cn"
    status: Literal["running", "success", "partial", "failed"] = "running"
    stage: str = ""
    total_tracks: int = 0
    matched_count: int = 0
    imported_count: int = 0
    failed_import_count: int = 0
    error_message: str = ""


class HistoryRecord(BaseModel):
    """Full persisted snapshot for a single history entry."""

    summary: HistorySummary
    result: PipelineResult | None = None
    import_items: list[dict[str, Any]] = Field(default_factory=list)
    import_logs: list[str] = Field(default_factory=list)
    import_message: str = ""


class HistoryStore:
    """Thread-safe file-backed unlimited history store under cache_dir/history."""

    def __init__(self, cache_dir: Path) -> None:
        self.root = cache_dir.expanduser() / "history"
        self.index_path = self.root / "index.json"
        self._lock = threading.Lock()
        self.root.mkdir(parents=True, exist_ok=True)

    def _read_index(self) -> list[HistorySummary]:
        if not self.index_path.exists():
            return []
        try:
            raw = json.loads(self.index_path.read_text(encoding="utf-8"))
            return [HistorySummary.model_validate(item) for item in raw]
        except Exception:  # noqa: BLE001
            return []

    def _write_index(self, items: list[HistorySummary]) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        self.index_path.write_text(
            json.dumps([x.model_dump(mode="json") for x in items], ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def _record_path(self, record_id: str) -> Path:
        safe_id = "".join(c for c in record_id if c.isalnum() or c in ("-", "_"))
        return self.root / f"{safe_id}.json"

    def new_record(self, input_url: str, mode: str = "auto", storefront: str = "cn") -> HistoryRecord:
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        rec_id = f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:6]}"
        summary = HistorySummary(
            id=rec_id,
            created_at=now,
            updated_at=now,
            input_url=input_url.strip(),
            title=input_url.strip(),
            mode=mode,
            storefront=storefront,
            status="running",
            stage="准备解析",
        )
        rec = HistoryRecord(summary=summary)
        self.save_record(rec)
        return rec

    def save_record(self, record: HistoryRecord) -> HistoryRecord:
        with self._lock:
            record.summary.updated_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            rec_path = self._record_path(record.summary.id)
            rec_path.write_text(
                json.dumps(record.model_dump(mode="json"), ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            items = self._read_index()
            items = [x for x in items if x.id != record.summary.id]
            items.insert(0, record.summary)
            self._write_index(items)
            return record

    def list_summaries(self, query: str = "") -> list[HistorySummary]:
        with self._lock:
            items = self._read_index()
        q = query.strip().lower()
        if not q:
            return items
        return [
            x
            for x in items
            if q in x.title.lower()
            or q in x.bvid.lower()
            or q in x.input_url.lower()
            or q in x.error_message.lower()
        ]

    def get_record(self, record_id: str) -> HistoryRecord | None:
        with self._lock:
            rec_path = self._record_path(record_id)
            if not rec_path.exists():
                return None
            try:
                return HistoryRecord.model_validate_json(rec_path.read_text(encoding="utf-8"))
            except Exception:  # noqa: BLE001
                return None

    def delete_record(self, record_id: str) -> bool:
        with self._lock:
            items = self._read_index()
            next_items = [x for x in items if x.id != record_id]
            existed = len(next_items) != len(items)
            self._write_index(next_items)
            rec_path = self._record_path(record_id)
            if rec_path.exists():
                rec_path.unlink()
                existed = True
            return existed

    def clear_all(self) -> int:
        with self._lock:
            items = self._read_index()
            count = len(items)
            self._write_index([])
            for p in self.root.glob("*.json"):
                if p.name != "index.json":
                    p.unlink(missing_ok=True)
            return count
