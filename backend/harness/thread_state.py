from typing import Any, Dict, Iterable

from sqlalchemy import func

from models import ConversationStateModel, ThreadEventModel, utc_now


class ThreadRecorder:
    """Append-only timeline for a conversation that can be replayed by any client."""

    def __init__(self, db, conversation_id: str):
        self.db = db
        self.conversation_id = conversation_id

    def record(self, event_type: str, payload: Dict[str, Any], run_id: str | None = None) -> ThreadEventModel:
        sequence = (
            self.db.query(func.max(ThreadEventModel.sequence))
            .filter(ThreadEventModel.conversation_id == self.conversation_id)
            .scalar()
            or 0
        ) + 1
        event = ThreadEventModel(
            conversation_id=self.conversation_id,
            run_id=run_id,
            sequence=sequence,
            event_type=event_type,
            payload=payload,
        )
        self.db.add(event)
        self.db.commit()
        return event

    def events_after(self, after_sequence: int = 0) -> Iterable[ThreadEventModel]:
        return (
            self.db.query(ThreadEventModel)
            .filter(
                ThreadEventModel.conversation_id == self.conversation_id,
                ThreadEventModel.sequence > after_sequence,
            )
            .order_by(ThreadEventModel.sequence)
            .all()
        )


def get_or_create_state(db, conversation_id: str) -> ConversationStateModel:
    state = db.get(ConversationStateModel, conversation_id)
    if state is None:
        state = ConversationStateModel(conversation_id=conversation_id)
        db.add(state)
        db.commit()
    return state


def update_state(db, conversation_id: str, *, is_archived=None, is_pinned=None) -> ConversationStateModel:
    state = get_or_create_state(db, conversation_id)
    if is_archived is not None:
        state.is_archived = is_archived
    if is_pinned is not None:
        state.is_pinned = is_pinned
    state.updated_at = utc_now()
    db.commit()
    return state
