import uuid
from datetime import datetime, timezone
from sqlalchemy import Column, String, Text, DateTime, ForeignKey, Integer, Boolean, JSON, UniqueConstraint
from sqlalchemy.orm import relationship
from database import Base

def generate_uuid():
    return str(uuid.uuid4())

def utc_now():
    return datetime.now(timezone.utc).replace(tzinfo=None)

class UserModel(Base):
    __tablename__ = "users"

    id = Column(String(36), primary_key=True, default=generate_uuid)
    username = Column(String(50), unique=True, nullable=False, index=True)
    role = Column(String(20), default="user") # "user" | "admin"
    created_at = Column(DateTime, default=utc_now)

    conversations = relationship("ConversationModel", back_populates="user")

class ConversationModel(Base):
    __tablename__ = "conversations"

    id = Column(String(36), primary_key=True, default=generate_uuid)
    user_id = Column(String(36), ForeignKey("users.id"), nullable=False, index=True)
    title = Column(String(200), default="新对话")
    created_at = Column(DateTime, default=utc_now)
    updated_at = Column(DateTime, default=utc_now, onupdate=utc_now)

    user = relationship("UserModel", back_populates="conversations")
    messages = relationship("MessageModel", back_populates="conversation", cascade="all, delete-orphan")
    runs = relationship("RunModel", back_populates="conversation", cascade="all, delete-orphan")
    thread_events = relationship("ThreadEventModel", back_populates="conversation", cascade="all, delete-orphan")
    attachments = relationship("AttachmentModel", back_populates="conversation", cascade="all, delete-orphan")
    state = relationship("ConversationStateModel", back_populates="conversation", uselist=False, cascade="all, delete-orphan")

class ConversationStateModel(Base):
    __tablename__ = "conversation_states"

    conversation_id = Column(String(36), ForeignKey("conversations.id"), primary_key=True)
    is_archived = Column(Boolean, default=False, nullable=False, index=True)
    is_pinned = Column(Boolean, default=False, nullable=False, index=True)
    updated_at = Column(DateTime, default=utc_now, onupdate=utc_now)

    conversation = relationship("ConversationModel", back_populates="state")

class MessageModel(Base):
    __tablename__ = "messages"

    id = Column(String(36), primary_key=True, default=generate_uuid)
    conversation_id = Column(String(36), ForeignKey("conversations.id"), nullable=False, index=True)
    run_id = Column(String(36), ForeignKey("runs.id"), nullable=True, index=True)
    role = Column(String(20), nullable=False) # "user" | "assistant"
    content = Column(Text, default="")
    attachment_name = Column(String(200), nullable=True)
    created_at = Column(DateTime, default=utc_now, index=True)

    conversation = relationship("ConversationModel", back_populates="messages")

class ThreadEventModel(Base):
    __tablename__ = "thread_events"
    __table_args__ = (UniqueConstraint("conversation_id", "sequence", name="uq_thread_event_sequence"),)

    id = Column(String(36), primary_key=True, default=generate_uuid)
    conversation_id = Column(String(36), ForeignKey("conversations.id"), nullable=False, index=True)
    run_id = Column(String(36), ForeignKey("runs.id"), nullable=True, index=True)
    sequence = Column(Integer, nullable=False)
    event_type = Column(String(50), nullable=False, index=True)
    payload = Column(JSON, default=dict)
    created_at = Column(DateTime, default=utc_now, index=True)

    conversation = relationship("ConversationModel", back_populates="thread_events")

class AttachmentModel(Base):
    __tablename__ = "attachments"

    id = Column(String(36), primary_key=True, default=generate_uuid)
    conversation_id = Column(String(36), ForeignKey("conversations.id"), nullable=False, index=True)
    run_id = Column(String(36), ForeignKey("runs.id"), nullable=True, index=True)
    file_name = Column(String(200), nullable=False)
    mime_type = Column(String(100), nullable=False)
    size_bytes = Column(Integer, nullable=False)
    storage_path = Column(String(500), nullable=False)
    extracted_text = Column(Text, default="")
    created_at = Column(DateTime, default=utc_now)

    conversation = relationship("ConversationModel", back_populates="attachments")

class RunModel(Base):
    __tablename__ = "runs"

    id = Column(String(36), primary_key=True, default=generate_uuid)
    conversation_id = Column(String(36), ForeignKey("conversations.id"), nullable=False, index=True)
    intent_type = Column(String(50), default="general") # Loaded Skill names, kept for API compatibility
    status = Column(String(20), default="pending") # pending | running | awaiting_approval | completed | failed | cancelled
    engine = Column(String(30), default="react", nullable=False)
    router_confidence = Column(String(10), nullable=True)
    router_reasons = Column(JSON, default=list)
    pending_approval = Column(JSON, nullable=True)
    error_message = Column(Text, nullable=True)
    created_at = Column(DateTime, default=utc_now)
    completed_at = Column(DateTime, nullable=True)

    conversation = relationship("ConversationModel", back_populates="runs")
    messages = relationship("MessageModel", foreign_keys="MessageModel.run_id")
    artifacts = relationship("ArtifactModel", back_populates="run", cascade="all, delete-orphan")
    events = relationship("RunEventModel", back_populates="run", cascade="all, delete-orphan")
    plan_tasks = relationship("PlanTaskModel", back_populates="run", cascade="all, delete-orphan")

class PlanTaskModel(Base):
    __tablename__ = "plan_tasks"
    __table_args__ = (UniqueConstraint("run_id", "position", name="uq_plan_task_position"),)

    id = Column(String(36), primary_key=True, default=generate_uuid)
    run_id = Column(String(36), ForeignKey("runs.id"), nullable=False, index=True)
    position = Column(Integer, nullable=False)
    title = Column(String(300), nullable=False)
    status = Column(String(20), default="pending", nullable=False)
    depends_on = Column(JSON, default=list)
    attempt = Column(Integer, default=0, nullable=False)
    error_message = Column(Text, nullable=True)
    created_at = Column(DateTime, default=utc_now)
    completed_at = Column(DateTime, nullable=True)

    run = relationship("RunModel", back_populates="plan_tasks")

class RunEventModel(Base):
    __tablename__ = "run_events"
    __table_args__ = (UniqueConstraint("run_id", "sequence", name="uq_run_event_sequence"),)

    id = Column(String(36), primary_key=True, default=generate_uuid)
    run_id = Column(String(36), ForeignKey("runs.id"), nullable=False, index=True)
    sequence = Column(Integer, nullable=False)
    event_type = Column(String(50), nullable=False, index=True)
    payload = Column(JSON, default=dict)
    created_at = Column(DateTime, default=utc_now)

    run = relationship("RunModel", back_populates="events")

class ArtifactModel(Base):
    __tablename__ = "artifacts"

    id = Column(String(36), primary_key=True, default=generate_uuid)
    run_id = Column(String(36), ForeignKey("runs.id"), nullable=False, index=True)
    title = Column(String(200), nullable=False)
    artifact_type = Column(String(50), nullable=False) # "ppt" | "document" | "bidding" | "report"
    mime_type = Column(String(100), nullable=False)
    version = Column(Integer, default=1)
    storage_path = Column(String(500), nullable=False)
    need_audit = Column(Boolean, default=False)
    audit_status = Column(String(20), default="approved") # "pending" | "approved" | "rejected"
    missing_fields = Column(JSON, default=list) # List of missing required fields
    sources = Column(JSON, default=list) # List of source citations
    created_at = Column(DateTime, default=utc_now)

    run = relationship("RunModel", back_populates="artifacts")

class DocumentChunkModel(Base):
    __tablename__ = "document_chunks"

    id = Column(String(36), primary_key=True, default=generate_uuid)
    conversation_id = Column(String(36), ForeignKey("conversations.id"), nullable=False, index=True)
    file_name = Column(String(200), nullable=False)
    chunk_index = Column(Integer, nullable=False)
    content = Column(Text, nullable=False)
    token_count = Column(Integer, default=0)
    created_at = Column(DateTime, default=utc_now)
