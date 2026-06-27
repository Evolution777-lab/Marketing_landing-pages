"""GPT-5.5 analysis pipeline for competitor emails.

Sends each email through OpenAI (model ``gpt-5.5``) using the prompt defined in
``gpt-analyst-prompt.txt`` and persists the structured JSON result. When no API
key is configured it falls back to a lightweight keyword-based heuristic so the
product still functions for demos / offline use.
"""

from __future__ import annotations

import json
import logging
import os
import re

from models import Email, get_session

logger = logging.getLogger(__name__)

MODEL = "gpt-5.5"
TEMPERATURE = 0.2
MAX_TOKENS = 2500

PROMPT_FILE = os.path.join(os.path.dirname(__file__), "gpt-analyst-prompt.txt")

# Fields the model is expected to return.
ANALYSIS_FIELDS = (
    "category",
    "tags",
    "summary_ru",
    "sentiment",
    "importance_score",
    "importance_reason",
    "usp_detected",
    "offer_structure",
    "cta_detected",
    "tone",
    "threat_level",
    "positioning_vs_us",
    "recommended_actions",
    "confidence_score",
)


def _load_prompts() -> tuple[str, str]:
    """Read system + user prompt templates from the prompt file."""
    try:
        with open(PROMPT_FILE, encoding="utf-8") as fh:
            content = fh.read()
    except OSError:
        logger.warning("Prompt file not found: %s", PROMPT_FILE)
        return ("You are a competitor marketing analyst. Return JSON.", "{body_text}")

    system_part, _, rest = content.partition("### USER PROMPT TEMPLATE ###")
    system_prompt = system_part.replace("### SYSTEM PROMPT ###", "").strip()
    user_template = rest.strip()
    if not user_template:
        user_template = "{body_text}"
    return system_prompt, user_template


def _get_api_key() -> str | None:
    """Resolve the OpenAI API key from the DB settings or the environment."""
    try:
        from models import Setting

        session = get_session()
        try:
            row = session.query(Setting).filter(Setting.key == "openai_api_key").first()
            if row and row.value:
                return row.value
        finally:
            session.close()
    except Exception:  # noqa: BLE001
        pass
    return os.getenv("OPENAI_API_KEY")


# --- Keyword fallback ---------------------------------------------------------
_CATEGORY_KEYWORDS = {
    "акция_скидка": ["скидк", "акци", "бесплатно", "промокод", "распродаж", "тариф"],
    "вебинар_мероприятие": ["вебинар", "конференц", "митап", "форум", "встреч"],
    "обучение": ["обучени", "курс", "мастер-класс", "инструкц", "урок"],
    "продуктовый_анонс": ["запуск", "нов", "обновлени", "функц", "релиз"],
    "новость_платформы": ["новост", "изменени", "регламент", "закон", "44-фз", "223-фз"],
    "кейс_клиента": ["кейс", "история успеха", "отзыв", "результат"],
    "дайджест": ["дайджест", "обзор", "итоги", "недел"],
}


def _keyword_fallback(email: Email) -> dict:
    """Cheap heuristic analysis used when no API key is available."""
    text = f"{email.subject or ''} {email.body_text or ''}".lower()

    category = "прочее"
    for cat, keywords in _CATEGORY_KEYWORDS.items():
        if any(kw in text for kw in keywords):
            category = cat
            break

    urgent = any(kw in text for kw in ["срочно", "последний день", "только сегодня", "успей"])
    importance = 55 if urgent else 35

    return {
        "category": category,
        "tags": ["requires_api_key"],
        "summary_ru": (email.subject or "Без темы")[:280],
        "sentiment": "нейтральный",
        "importance_score": importance,
        "importance_reason": "requires_api_key",
        "usp_detected": "requires_api_key",
        "offer_structure": "requires_api_key",
        "cta_detected": "requires_api_key",
        "tone": "requires_api_key",
        "threat_level": "medium" if urgent else "low",
        "positioning_vs_us": "requires_api_key",
        "recommended_actions": "requires_api_key",
        "confidence_score": 0.2,
    }


def _call_openai(email: Email, api_key: str) -> dict:
    """Run the GPT-5.5 pipeline for a single email and return parsed JSON."""
    from openai import OpenAI

    system_prompt, user_template = _load_prompts()
    user_prompt = user_template.format(
        competitor_type=email.competitor_type or "unknown",
        sender=email.sender or "",
        sender_domain=email.sender_domain or "",
        subject=email.subject or "",
        received_at=email.received_at.isoformat() if email.received_at else "",
        body_text=(email.body_text or email.body_html or "")[:12000],
    )

    client = OpenAI(api_key=api_key)
    response = client.chat.completions.create(
        model=MODEL,
        temperature=TEMPERATURE,
        max_tokens=MAX_TOKENS,
        response_format={"type": "json_object"},
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
    )
    content = response.choices[0].message.content or "{}"
    return json.loads(content)


def _coerce_result(raw: dict) -> dict:
    """Normalize and clamp the model output into known fields/types."""
    result: dict = {}

    result["category"] = str(raw.get("category", "прочее"))[:64]

    tags = raw.get("tags", [])
    if isinstance(tags, str):
        tags = [t.strip() for t in re.split(r"[;,]", tags) if t.strip()]
    result["tags"] = tags if isinstance(tags, list) else []

    result["summary_ru"] = str(raw.get("summary_ru", "") or "")
    result["sentiment"] = str(raw.get("sentiment", "нейтральный"))[:32]

    try:
        score = int(float(raw.get("importance_score", 0)))
    except (ValueError, TypeError):
        score = 0
    result["importance_score"] = max(0, min(100, score))

    result["importance_reason"] = str(raw.get("importance_reason", "") or "")
    result["usp_detected"] = str(raw.get("usp_detected", "") or "")
    result["offer_structure"] = str(raw.get("offer_structure", "") or "")
    result["cta_detected"] = str(raw.get("cta_detected", "") or "")
    result["tone"] = str(raw.get("tone", "") or "")[:64]

    threat = str(raw.get("threat_level", "none")).lower().strip()
    if threat not in {"none", "low", "medium", "high", "critical"}:
        threat = "none"
    result["threat_level"] = threat

    result["positioning_vs_us"] = str(raw.get("positioning_vs_us", "") or "")
    result["recommended_actions"] = str(raw.get("recommended_actions", "") or "")

    try:
        conf = float(raw.get("confidence_score", 0.0))
    except (ValueError, TypeError):
        conf = 0.0
    result["confidence_score"] = max(0.0, min(1.0, conf))

    return result


def _apply_result(email: Email, result: dict) -> None:
    """Write a normalized analysis result back onto the Email row."""
    email.category = result["category"]
    email.tags = json.dumps(result["tags"], ensure_ascii=False)
    email.summary_ru = result["summary_ru"]
    email.sentiment = result["sentiment"]
    email.importance_score = result["importance_score"]
    email.importance_reason = result["importance_reason"]
    email.usp_detected = result["usp_detected"]
    email.offer_structure = result["offer_structure"]
    email.cta_detected = result["cta_detected"]
    email.tone = result["tone"]
    email.threat_level = result["threat_level"]
    email.positioning_vs_us = result["positioning_vs_us"]
    email.recommended_actions = result["recommended_actions"]
    email.confidence_score = result["confidence_score"]
    email.analyzed = True


def analyze_email(email_id: int) -> dict:
    """Analyze a single email by id. Returns the analysis result dict."""
    session = get_session()
    try:
        email = session.query(Email).filter(Email.id == email_id).first()
        if not email:
            return {"error": "email not found"}

        api_key = _get_api_key()
        try:
            if api_key:
                raw = _call_openai(email, api_key)
                result = _coerce_result(raw)
            else:
                result = _keyword_fallback(email)
        except Exception as exc:  # noqa: BLE001 - API/parse failure -> fallback
            logger.exception("GPT analysis failed for email %s", email_id)
            result = _keyword_fallback(email)
            result["importance_reason"] = f"analysis_error: {exc}"

        _apply_result(email, result)
        session.commit()
        return result
    finally:
        session.close()


def analyze_batch() -> dict:
    """Analyze every email that has not yet been analyzed."""
    session = get_session()
    try:
        ids = [
            row.id
            for row in session.query(Email.id).filter(Email.analyzed.is_(False)).all()
        ]
    finally:
        session.close()

    analyzed = 0
    for email_id in ids:
        result = analyze_email(email_id)
        if "error" not in result:
            analyzed += 1

    return {"total": len(ids), "analyzed": analyzed}
