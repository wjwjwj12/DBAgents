import os
from sqlalchemy import create_engine
from sqlalchemy import event
from sqlalchemy.orm import sessionmaker, declarative_base
from runtime_paths import DATABASE_FILE

DB_PATH = os.getenv("DATABASE_URL", f"sqlite:///{DATABASE_FILE.as_posix()}")

engine = create_engine(
    DB_PATH, 
    connect_args={"check_same_thread": False} if DB_PATH.startswith("sqlite") else {}
)

if DB_PATH.startswith("sqlite"):
    @event.listens_for(engine, "connect")
    def _enable_sqlite_foreign_keys(dbapi_connection, _connection_record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()

def init_db():
    from models import (
        ArtifactModel,
        AttachmentModel,
        ConversationModel,
        ConversationStateModel,
        DocumentChunkModel,
        MessageModel,
        PlanTaskModel,
        RunEventModel,
        RunModel,
        ThreadEventModel,
        UserModel,
    )

    Base.metadata.create_all(bind=engine)

    if DB_PATH.startswith("sqlite"):
        with engine.begin() as connection:
            columns = {row[1] for row in connection.exec_driver_sql("PRAGMA table_info(runs)")}
            additions = {
                "engine": "VARCHAR(30) NOT NULL DEFAULT 'react'",
                "router_confidence": "VARCHAR(10)",
                "router_reasons": "JSON",
                "selected_skills": "JSON",
                "pending_approval": "JSON",
            }
            for name, definition in additions.items():
                if name not in columns:
                    connection.exec_driver_sql(f"ALTER TABLE runs ADD COLUMN {name} {definition}")
            user_columns = {row[1] for row in connection.exec_driver_sql("PRAGMA table_info(users)")}
            user_additions = {
                "tenant_id": "VARCHAR(100) NOT NULL DEFAULT 'local'",
                "external_id": "VARCHAR(100) NOT NULL DEFAULT 'local-user'",
                "display_name": "VARCHAR(100)",
            }
            for name, definition in user_additions.items():
                if name not in user_columns:
                    connection.exec_driver_sql(f"ALTER TABLE users ADD COLUMN {name} {definition}")

    # Repair orphaned rows created by older versions before foreign keys were enabled.

    db = SessionLocal()
    try:
        local_user = db.query(UserModel).filter(
            UserModel.tenant_id == "local",
            UserModel.external_id == "local-user",
        ).first()
        if local_user is None:
            local_user = UserModel(
                tenant_id="local",
                external_id="local-user",
                username="local:local-user",
                display_name="local-user",
            )
            db.add(local_user)
            db.flush()

        conversation_ids = {
            value
            for (value,) in db.query(RunModel.conversation_id).distinct().all()
            if value
        }
        conversation_ids.update(
            value
            for (value,) in db.query(DocumentChunkModel.conversation_id).distinct().all()
            if value
        )
        existing_ids = {
            value
            for (value,) in db.query(ConversationModel.id)
            .filter(ConversationModel.id.in_(conversation_ids))
            .all()
        } if conversation_ids else set()

        for conversation_id in conversation_ids - existing_ids:
            db.add(ConversationModel(
                id=conversation_id,
                user_id=local_user.id,
                title="恢复的历史对话",
            ))
        db.commit()

        existing_state_ids = {value for (value,) in db.query(ConversationStateModel.conversation_id).all()}
        for conversation_id, in db.query(ConversationModel.id).all():
            if conversation_id not in existing_state_ids:
                db.add(ConversationStateModel(conversation_id=conversation_id))
        db.commit()

        # Older releases persisted runs/events/artifacts but not chat messages.
        # Recover only content that was actually recorded; never invent history.
        legacy_runs = (
            db.query(RunModel)
            .filter(~RunModel.messages.any())
            .order_by(RunModel.created_at)
            .all()
        )
        for run in legacy_runs:
            events = (
                db.query(RunEventModel)
                .filter(RunEventModel.run_id == run.id)
                .order_by(RunEventModel.sequence)
                .all()
            )
            query = next(
                (
                    str(event.payload.get("query", "")).strip()
                    for event in events
                    if event.event_type == "run_started" and event.payload.get("query")
                ),
                "",
            )
            answer = next(
                (
                    str(event.payload.get("content", "")).strip()
                    for event in reversed(events)
                    if event.event_type == "assistant_completed" and event.payload.get("content")
                ),
                "",
            )
            has_artifact = db.query(ArtifactModel.id).filter(ArtifactModel.run_id == run.id).first()
            if query:
                db.add(MessageModel(
                    conversation_id=run.conversation_id,
                    run_id=run.id,
                    role="user",
                    content=query,
                    created_at=run.created_at,
                ))
                if run.conversation.title in {"新对话", "恢复的历史对话"}:
                    run.conversation.title = query[:48]
            if answer or has_artifact:
                db.add(MessageModel(
                    conversation_id=run.conversation_id,
                    run_id=run.id,
                    role="assistant",
                    content=answer or "历史任务已完成，产物可在下方查看。",
                    created_at=run.completed_at or run.created_at,
                ))
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
