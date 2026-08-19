from typing import Any, Dict

from sqlalchemy import func

from models import RunEventModel, utc_now


class RunRecorder:
    def __init__(self, db, run):
        self.db = db
        self.run = run
        self.sequence = (
            db.query(func.max(RunEventModel.sequence))
            .filter(RunEventModel.run_id == run.id)
            .scalar()
            or 0
        )

    def record(self, event_type: str, payload: Dict[str, Any]) -> RunEventModel:
        self.sequence += 1
        event = RunEventModel(
            run_id=self.run.id,
            sequence=self.sequence,
            event_type=event_type,
            payload=payload,
        )
        self.db.add(event)
        self.db.commit()
        return event

    def is_cancelled(self) -> bool:
        self.db.refresh(self.run)
        return self.run.status == "cancelled"

    def complete(self) -> None:
        self.run.status = "completed"
        self.run.completed_at = utc_now()
        self.db.commit()

    def fail(self, error: Exception) -> None:
        self.run.status = "failed"
        self.run.error_message = str(error)
        self.run.completed_at = utc_now()
        self.db.commit()
