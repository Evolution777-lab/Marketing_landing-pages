"""Yandex IMAP email collector for the Competitor Intelligence Hub.

Connects to a Yandex mailbox over IMAP (SSL), fetches unread competitor
emails, classifies the competitor type by sender domain, skips duplicates
(same subject+sender within 24h), stores them, and marks them read.
"""

from __future__ import annotations

import datetime as dt
import json
import logging

from imap_tools import AND, MailBox

from models import Email, get_session

logger = logging.getLogger(__name__)

IMAP_HOST = "imap.yandex.ru"
IMAP_PORT = 993

# --- Competitor classification by sender domain -------------------------------
# Federal procurement platforms (44-FZ / 223-FZ / 615-PP and state operators).
FEDERAL_DOMAINS = {
    "zakupki.gov.ru",
    "roseltorg.ru",
    "rts-tender.ru",
    "sberbank-ast.ru",
    "etp-ets.ru",
    "rad.ru",
    "lot-online.ru",
    "etpgpb.ru",
    "tektorg.ru",
    "gz-spb.ru",
    "etp.gpb.ru",
    "gpb.ru",
}

# Commercial procurement platforms (B2B, non-state).
COMMERCIAL_DOMAINS = {
    "b2b-center.ru",
    "fabrikant.ru",
    "otc.ru",
    "tender.pro",
    "onlinecontract.ru",
    "supplyonline.ru",
    "trade.su",
    "regtorg.ru",
    "zakupki.mos.ru",
    "komita.ru",
}

# Western procurement / e-sourcing platforms.
WESTERN_DOMAINS = {
    "sap.com",
    "ariba.com",
    "coupa.com",
    "jaggaer.com",
    "gep.com",
    "ivalua.com",
    "oracle.com",
    "scoutbee.com",
    "keelvar.com",
    "tradeshift.com",
    "basware.com",
}


def classify_competitor(domain: str) -> tuple[str, bool]:
    """Return ``(competitor_type, is_western)`` for a sender domain."""
    if not domain:
        return "unknown", False
    domain = domain.lower().strip()
    # Match by suffix so subdomains (mail.rts-tender.ru) still classify.
    for known in WESTERN_DOMAINS:
        if domain == known or domain.endswith("." + known):
            return "western", True
    for known in FEDERAL_DOMAINS:
        if domain == known or domain.endswith("." + known):
            return "federal", False
    for known in COMMERCIAL_DOMAINS:
        if domain == known or domain.endswith("." + known):
            return "commercial", False
    return "unknown", False


def _extract_domain(address: str) -> str:
    """Pull the domain part out of an email address."""
    if not address or "@" not in address:
        return ""
    return address.rsplit("@", 1)[-1].lower().strip(">").strip()


def _is_duplicate(session, sender: str, subject: str, received_at: dt.datetime) -> bool:
    """Skip an email if the same subject+sender arrived within 24 hours."""
    window_start = received_at - dt.timedelta(hours=24)
    window_end = received_at + dt.timedelta(hours=24)
    existing = (
        session.query(Email)
        .filter(
            Email.sender == sender,
            Email.subject == subject,
            Email.received_at >= window_start,
            Email.received_at <= window_end,
        )
        .first()
    )
    return existing is not None


def collect_emails(username: str, password: str, folder: str = "INBOX") -> dict:
    """Fetch unread emails from Yandex and store new ones.

    Returns a summary dict: ``{"fetched", "saved", "skipped", "error"}``.

    Notes:
        * ``password`` must be a Yandex *App Password*, not the account's
          main password (IMAP requires this when 2FA is enabled).
    """
    summary = {"fetched": 0, "saved": 0, "skipped": 0, "error": None}

    if not username or not password:
        summary["error"] = "IMAP credentials are not configured"
        return summary

    session = get_session()
    try:
        with MailBox(IMAP_HOST, port=IMAP_PORT).login(
            username, password, initial_folder=folder
        ) as mailbox:
            # Fetch unread, mark as read after handling.
            messages = list(
                mailbox.fetch(AND(seen=False), mark_seen=True, bulk=True)
            )
            summary["fetched"] = len(messages)

            for msg in messages:
                sender = (msg.from_ or "").lower().strip()
                subject = msg.subject or ""
                received_at = msg.date or dt.datetime.utcnow()
                # imap-tools returns tz-aware datetimes; normalize to naive UTC.
                if received_at.tzinfo is not None:
                    received_at = received_at.astimezone(dt.timezone.utc).replace(
                        tzinfo=None
                    )

                if _is_duplicate(session, sender, subject, received_at):
                    summary["skipped"] += 1
                    continue

                domain = _extract_domain(sender)
                competitor_type, is_western = classify_competitor(domain)

                attachments = [att.filename for att in msg.attachments if att.filename]

                email = Email(
                    sender=sender,
                    sender_domain=domain,
                    competitor_type=competitor_type,
                    is_western=is_western,
                    subject=subject,
                    body_text=msg.text or "",
                    body_html=msg.html or "",
                    attachments=json.dumps(attachments, ensure_ascii=False),
                    received_at=received_at,
                    analyzed=False,
                    threat_level="none",
                )
                session.add(email)
                summary["saved"] += 1

            session.commit()
    except Exception as exc:  # noqa: BLE001 - report any IMAP/parse failure
        logger.exception("IMAP collection failed")
        summary["error"] = str(exc)
        session.rollback()
    finally:
        session.close()

    return summary


def test_connection(username: str, password: str) -> dict:
    """Attempt an IMAP login to validate credentials.

    Returns ``{"ok": bool, "message": str}``.
    """
    if not username or not password:
        return {"ok": False, "message": "Укажите логин и пароль приложения"}
    try:
        with MailBox(IMAP_HOST, port=IMAP_PORT).login(username, password):
            return {"ok": True, "message": "Подключение успешно"}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "message": f"Ошибка подключения: {exc}"}
