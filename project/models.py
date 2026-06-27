"""SQLAlchemy models for the Competitor Intelligence Hub.

Defines the SQLite-backed schema used to store collected competitor emails,
their AI analysis results, and application settings.
"""

from __future__ import annotations

import datetime as dt

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Float,
    Integer,
    String,
    Text,
    create_engine,
    event,
)
from sqlalchemy.orm import declarative_base, sessionmaker

Base = declarative_base()

DB_URL = "sqlite:///competitor_intel.db"

# check_same_thread=False so APScheduler background jobs can share the engine.
engine = create_engine(
    DB_URL, echo=False, connect_args={"check_same_thread": False}
)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)


@event.listens_for(engine, "connect")
def _register_sqlite_functions(dbapi_connection, _record):
    """Register a Unicode-aware lowercase function.

    SQLite's built-in ``lower()``/``LIKE`` only fold case for ASCII, so a
    search for ``скидка`` would not match ``Скидка``. ``pylower`` uses Python's
    full Unicode-aware ``str.lower`` to make keyword search work in Russian.
    """
    dbapi_connection.create_function(
        "pylower", 1, lambda value: value.lower() if isinstance(value, str) else value
    )


class Email(Base):
    """A single competitor email plus the full GPT-5.5 analysis payload."""

    __tablename__ = "emails"

    id = Column(Integer, primary_key=True, autoincrement=True)

    # --- Raw email data ---
    sender = Column(String(320), index=True)
    sender_domain = Column(String(255), index=True)
    competitor_type = Column(String(32), index=True, default="unknown")
    subject = Column(String(1024), default="")
    body_text = Column(Text, default="")
    body_html = Column(Text, default="")
    attachments = Column(Text, default="")  # JSON-encoded list of filenames
    received_at = Column(DateTime, index=True, default=dt.datetime.utcnow)

    # --- Analysis bookkeeping ---
    analyzed = Column(Boolean, default=False, index=True)
    is_western = Column(Boolean, default=False, index=True)

    # --- GPT-5.5 analysis output ---
    category = Column(String(64), index=True)
    tags = Column(Text)  # JSON-encoded list of tags
    summary_ru = Column(Text)
    sentiment = Column(String(32))
    importance_score = Column(Integer, default=0, index=True)
    importance_reason = Column(Text)
    usp_detected = Column(Text)
    offer_structure = Column(Text)
    cta_detected = Column(Text)
    tone = Column(String(64))
    threat_level = Column(String(16), index=True, default="none")
    positioning_vs_us = Column(Text)
    recommended_actions = Column(Text)
    confidence_score = Column(Float, default=0.0)

    # --- User actions (UI state) ---
    starred = Column(Boolean, default=False, index=True)
    archived = Column(Boolean, default=False, index=True)
    threat_reviewed = Column(Boolean, default=False, index=True)

    created_at = Column(DateTime, default=dt.datetime.utcnow)

    def to_dict(self) -> dict:
        """Serialize the row into a JSON-friendly dict for the API."""
        import json

        def _load_json(value, default):
            if not value:
                return default
            try:
                return json.loads(value)
            except (ValueError, TypeError):
                return default

        return {
            "id": self.id,
            "sender": self.sender,
            "sender_domain": self.sender_domain,
            "competitor_type": self.competitor_type,
            "subject": self.subject,
            "body_text": self.body_text,
            "body_html": self.body_html,
            "attachments": _load_json(self.attachments, []),
            "received_at": self.received_at.isoformat() if self.received_at else None,
            "analyzed": bool(self.analyzed),
            "is_western": bool(self.is_western),
            "category": self.category,
            "tags": _load_json(self.tags, []),
            "summary_ru": self.summary_ru,
            "sentiment": self.sentiment,
            "importance_score": self.importance_score or 0,
            "importance_reason": self.importance_reason,
            "usp_detected": self.usp_detected,
            "offer_structure": self.offer_structure,
            "cta_detected": self.cta_detected,
            "tone": self.tone,
            "threat_level": self.threat_level or "none",
            "positioning_vs_us": self.positioning_vs_us,
            "recommended_actions": self.recommended_actions,
            "confidence_score": self.confidence_score or 0.0,
            "starred": bool(self.starred),
            "archived": bool(self.archived),
            "threat_reviewed": bool(self.threat_reviewed),
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


class Setting(Base):
    """Simple key/value store for runtime configuration set via the UI."""

    __tablename__ = "settings"

    key = Column(String(64), primary_key=True)
    value = Column(Text)


def init_db() -> None:
    """Create all tables if they do not yet exist."""
    Base.metadata.create_all(bind=engine)


def get_session():
    """Return a new SQLAlchemy session."""
    return SessionLocal()
