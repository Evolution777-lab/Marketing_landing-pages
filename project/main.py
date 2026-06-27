"""FastAPI application + APScheduler for the Competitor Intelligence Hub.

Run with:
    pip install -r requirements.txt && python main.py
Then open http://localhost:8000
"""

from __future__ import annotations

import csv
import datetime as dt
import io
import logging
import os
from collections import Counter, defaultdict

from contextlib import asynccontextmanager

from apscheduler.schedulers.background import BackgroundScheduler
from dotenv import load_dotenv
from fastapi import FastAPI, Query
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel
from sqlalchemy import func

import analyzer
import email_collector
from models import Email, Setting, get_session, init_db

load_dotenv()

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("competitor_intel")

BASE_DIR = os.path.dirname(__file__)
DASHBOARD_FILE = os.path.join(BASE_DIR, "dashboard.html")

scheduler = BackgroundScheduler(timezone="UTC")


@asynccontextmanager
async def lifespan(_app: FastAPI):
    init_db()
    reschedule_polling()
    if not scheduler.running:
        scheduler.start()
    yield
    if scheduler.running:
        scheduler.shutdown(wait=False)


app = FastAPI(title="Competitor Intelligence Hub", version="1.0.0", lifespan=lifespan)


# --- Settings helpers ---------------------------------------------------------
DEFAULT_SETTINGS = {
    "imap_username": "",
    "imap_password": "",
    "imap_host": email_collector.IMAP_HOST,
    "imap_port": str(email_collector.IMAP_PORT),
    "openai_api_key": "",
    "polling_interval": "30",  # minutes
}


def get_setting(key: str, default: str = "") -> str:
    session = get_session()
    try:
        row = session.query(Setting).filter(Setting.key == key).first()
        if row and row.value is not None:
            return row.value
    finally:
        session.close()
    # Fall back to environment for credentials/keys.
    env_map = {
        "imap_username": "IMAP_USERNAME",
        "imap_password": "IMAP_PASSWORD",
        "openai_api_key": "OPENAI_API_KEY",
    }
    if key in env_map and os.getenv(env_map[key]):
        return os.getenv(env_map[key], default)
    return DEFAULT_SETTINGS.get(key, default)


def set_setting(key: str, value: str) -> None:
    session = get_session()
    try:
        row = session.query(Setting).filter(Setting.key == key).first()
        if row:
            row.value = value
        else:
            session.add(Setting(key=key, value=value))
        session.commit()
    finally:
        session.close()


# --- Scheduled collection job -------------------------------------------------
def scheduled_collect() -> None:
    """Background job: collect new emails and analyze them."""
    username = get_setting("imap_username")
    password = get_setting("imap_password")
    if not username or not password:
        logger.info("Scheduled collect skipped: IMAP not configured")
        return
    logger.info("Scheduled collect running...")
    result = email_collector.collect_emails(username, password)
    logger.info("Collected: %s", result)
    if result.get("saved"):
        analyzer.analyze_batch()


def reschedule_polling() -> None:
    """(Re)configure the polling job based on the saved interval."""
    try:
        interval = int(get_setting("polling_interval", "30"))
    except ValueError:
        interval = 30
    interval = max(1, interval)

    if scheduler.get_job("collect_job"):
        scheduler.remove_job("collect_job")
    scheduler.add_job(
        scheduled_collect,
        "interval",
        minutes=interval,
        id="collect_job",
        replace_existing=True,
    )
    logger.info("Polling scheduled every %s minutes", interval)


# --- Frontend -----------------------------------------------------------------
@app.get("/")
def index() -> FileResponse:
    return FileResponse(DASHBOARD_FILE)


# --- Emails -------------------------------------------------------------------
@app.get("/api/emails")
def list_emails(
    category: str | None = None,
    competitor_type: str | None = None,
    threat_level: str | None = None,
    importance_gte: int | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    search: str | None = None,
    starred: bool | None = None,
    archived: bool | None = None,
    limit: int = Query(200, le=1000),
    offset: int = 0,
):
    session = get_session()
    try:
        q = session.query(Email)

        if category:
            q = q.filter(Email.category == category)
        if competitor_type:
            q = q.filter(Email.competitor_type == competitor_type)
        if threat_level:
            levels = [t.strip() for t in threat_level.split(",") if t.strip()]
            if levels:
                q = q.filter(Email.threat_level.in_(levels))
        if importance_gte is not None:
            q = q.filter(Email.importance_score >= importance_gte)
        if date_from:
            try:
                q = q.filter(Email.received_at >= dt.datetime.fromisoformat(date_from))
            except ValueError:
                pass
        if date_to:
            try:
                end = dt.datetime.fromisoformat(date_to) + dt.timedelta(days=1)
                q = q.filter(Email.received_at < end)
            except ValueError:
                pass
        if starred is not None:
            q = q.filter(Email.starred.is_(starred))
        if archived is not None:
            q = q.filter(Email.archived.is_(archived))
        if search:
            # Unicode-aware, case-insensitive search (works for Cyrillic too).
            like = f"%{search.lower()}%"
            q = q.filter(
                func.pylower(Email.subject).like(like)
                | func.pylower(Email.body_text).like(like)
                | func.pylower(Email.sender).like(like)
                | func.pylower(Email.summary_ru).like(like)
            )

        total = q.count()
        rows = (
            q.order_by(Email.received_at.desc()).offset(offset).limit(limit).all()
        )
        return {"total": total, "emails": [r.to_dict() for r in rows]}
    finally:
        session.close()


@app.get("/api/emails/{email_id}")
def get_email(email_id: int):
    session = get_session()
    try:
        email = session.query(Email).filter(Email.id == email_id).first()
        if not email:
            return JSONResponse({"error": "not found"}, status_code=404)
        return email.to_dict()
    finally:
        session.close()


@app.post("/api/emails/{email_id}/reanalyze")
def reanalyze_email(email_id: int):
    result = analyzer.analyze_email(email_id)
    if "error" in result:
        return JSONResponse(result, status_code=404)
    return {"ok": True, "result": result}


def _toggle_field(email_id: int, field: str):
    session = get_session()
    try:
        email = session.query(Email).filter(Email.id == email_id).first()
        if not email:
            return JSONResponse({"error": "not found"}, status_code=404)
        setattr(email, field, not bool(getattr(email, field)))
        session.commit()
        return {"ok": True, field: bool(getattr(email, field))}
    finally:
        session.close()


@app.post("/api/emails/{email_id}/star")
def star_email(email_id: int):
    return _toggle_field(email_id, "starred")


@app.post("/api/emails/{email_id}/archive")
def archive_email(email_id: int):
    return _toggle_field(email_id, "archived")


@app.post("/api/emails/{email_id}/reviewed")
def reviewed_email(email_id: int):
    session = get_session()
    try:
        email = session.query(Email).filter(Email.id == email_id).first()
        if not email:
            return JSONResponse({"error": "not found"}, status_code=404)
        email.threat_reviewed = True
        session.commit()
        return {"ok": True, "threat_reviewed": True}
    finally:
        session.close()


# --- Collection & analysis ----------------------------------------------------
@app.post("/api/collect")
def collect_now():
    username = get_setting("imap_username")
    password = get_setting("imap_password")
    result = email_collector.collect_emails(username, password)
    analyzed = {"total": 0, "analyzed": 0}
    if result.get("saved"):
        analyzed = analyzer.analyze_batch()
    return {"collect": result, "analyze": analyzed}


@app.post("/api/analyze/batch")
def analyze_batch():
    return analyzer.analyze_batch()


# --- Analytics ----------------------------------------------------------------
def _iso_week(d: dt.datetime) -> str:
    iso = d.isocalendar()
    return f"{iso[0]}-W{iso[1]:02d}"


@app.get("/api/analytics")
def analytics():
    session = get_session()
    try:
        emails = session.query(Email).filter(Email.archived.is_(False)).all()

        by_category = Counter()
        by_competitor_type = Counter()
        tone_dist = Counter()
        volume_by_day = defaultdict(int)
        sender_counter = Counter()
        threat_by_week = defaultdict(lambda: Counter())
        competitor_usp = defaultdict(lambda: Counter())

        today = dt.datetime.utcnow().date()
        start_30 = today - dt.timedelta(days=29)
        for i in range(30):
            day = start_30 + dt.timedelta(days=i)
            volume_by_day[day.isoformat()] = 0

        threat_levels = ["low", "medium", "high", "critical"]

        for e in emails:
            if e.category:
                by_category[e.category] += 1
            if e.competitor_type:
                by_competitor_type[e.competitor_type] += 1
            if e.tone:
                tone_dist[e.tone] += 1
            if e.sender:
                sender_counter[e.sender] += 1

            if e.received_at:
                day = e.received_at.date()
                if start_30 <= day <= today:
                    volume_by_day[day.isoformat()] += 1
                week = _iso_week(e.received_at)
                if e.threat_level and e.threat_level in threat_levels:
                    threat_by_week[week][e.threat_level] += 1

            if e.competitor_type and e.usp_detected and e.usp_detected not in (
                "requires_api_key",
                "",
            ):
                usp_key = (e.usp_detected or "")[:60]
                competitor_usp[e.competitor_type][usp_key] += 1

        # Top senders (limit 10).
        top_senders = sender_counter.most_common(10)

        # Threat stacked by week (sorted).
        weeks = sorted(threat_by_week.keys())
        threat_stacked = {
            "weeks": weeks,
            "series": {
                level: [threat_by_week[w].get(level, 0) for w in weeks]
                for level in threat_levels
            },
        }

        # Heatmap competitor x USP.
        usp_heatmap = {
            ctype: dict(usps.most_common(10))
            for ctype, usps in competitor_usp.items()
        }

        volume_sorted = sorted(volume_by_day.items())

        # KPIs.
        total = len(emails)
        threats_open = (
            session.query(func.count(Email.id))
            .filter(
                Email.threat_level.in_(["high", "critical"]),
                Email.threat_reviewed.is_(False),
                Email.archived.is_(False),
            )
            .scalar()
        )
        unanalyzed = (
            session.query(func.count(Email.id))
            .filter(Email.analyzed.is_(False))
            .scalar()
        )

        return {
            "kpis": {
                "total": total,
                "threats_open": threats_open or 0,
                "unanalyzed": unanalyzed or 0,
            },
            "by_category": dict(by_category),
            "by_competitor_type": dict(by_competitor_type),
            "tone_distribution": dict(tone_dist),
            "volume_30_days": {
                "labels": [d for d, _ in volume_sorted],
                "values": [v for _, v in volume_sorted],
            },
            "top_senders": {
                "labels": [s for s, _ in top_senders],
                "values": [c for _, c in top_senders],
            },
            "threat_by_week": threat_stacked,
            "usp_heatmap": usp_heatmap,
        }
    finally:
        session.close()


# --- Settings -----------------------------------------------------------------
class SettingsPayload(BaseModel):
    imap_username: str | None = None
    imap_password: str | None = None
    imap_host: str | None = None
    imap_port: str | None = None
    openai_api_key: str | None = None
    polling_interval: str | None = None


def _mask(value: str) -> str:
    if not value:
        return ""
    if len(value) <= 4:
        return "•" * len(value)
    return value[:2] + "•" * (len(value) - 4) + value[-2:]


@app.get("/api/settings")
def get_settings():
    return {
        "imap_username": get_setting("imap_username"),
        "imap_password_set": bool(get_setting("imap_password")),
        "imap_host": get_setting("imap_host"),
        "imap_port": get_setting("imap_port"),
        "openai_api_key_masked": _mask(get_setting("openai_api_key")),
        "openai_api_key_set": bool(get_setting("openai_api_key")),
        "polling_interval": get_setting("polling_interval"),
    }


@app.post("/api/settings")
def save_settings(payload: SettingsPayload):
    data = payload.model_dump(exclude_unset=True)
    for key, value in data.items():
        if value is None:
            continue
        # Don't overwrite secrets with blank/masked values.
        if key in ("imap_password", "openai_api_key"):
            if not value.strip() or "•" in value:
                continue
        set_setting(key, value)
    if "polling_interval" in data and data["polling_interval"]:
        reschedule_polling()
    return {"ok": True}


@app.post("/api/settings/test-imap")
def test_imap(payload: SettingsPayload):
    username = payload.imap_username or get_setting("imap_username")
    password = payload.imap_password
    if not password or "•" in password:
        password = get_setting("imap_password")
    return email_collector.test_connection(username, password)


# --- Export -------------------------------------------------------------------
@app.get("/api/export/csv")
def export_csv(
    category: str | None = None,
    competitor_type: str | None = None,
    threat_level: str | None = None,
    importance_gte: int | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    search: str | None = None,
):
    data = list_emails(
        category=category,
        competitor_type=competitor_type,
        threat_level=threat_level,
        importance_gte=importance_gte,
        date_from=date_from,
        date_to=date_to,
        search=search,
        limit=1000,
    )
    emails = data["emails"]

    fields = [
        "id", "received_at", "sender", "sender_domain", "competitor_type",
        "subject", "category", "threat_level", "importance_score", "sentiment",
        "tone", "summary_ru", "usp_detected", "cta_detected",
        "positioning_vs_us", "recommended_actions", "confidence_score",
    ]

    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=fields, extrasaction="ignore")
    writer.writeheader()
    for e in emails:
        writer.writerow({k: e.get(k, "") for k in fields})
    buffer.seek(0)

    return StreamingResponse(
        iter([buffer.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=competitor_emails.csv"},
    )


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)
