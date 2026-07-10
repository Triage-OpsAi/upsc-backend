"""
All OpenAI calls go through this module so model routing + guardrails
live in exactly one place.

Model routing (cost control):
  - MODEL_MAIN   -> bulk main-question generation (runs once per topic, quality matters)
  - MODEL_CHEAP  -> breakdown slides + nightly personalised reports (runs A LOT, cost matters most)
  - MODEL_SEARCH -> daily midnight IST "what happened today" research step

Guardrail: every single call carries the same strict system prompt that
locks the model to Indian competitive-exam syllabus + current affairs,
refusing anything else (chit-chat, code help, unrelated trivia, etc).
"""

import asyncio
import json
import logging
import httpx
from openai import AsyncOpenAI, OpenAIError
from app.config import get_settings

settings = get_settings()
_client: AsyncOpenAI | None = None
_http_client: httpx.AsyncClient | None = None
logger = logging.getLogger(__name__)

SCOPE_GUARDRAIL = (
    "You are the content engine for an Indian competitive-exam "
    "(UPSC / State PSC / SSC / banking, etc.) current-affairs practice app. "
    "You ONLY produce exam-syllabus and current-affairs educational content: "
    "polity, economy, history, geography, environment & ecology, science & tech, "
    "ethics, international relations, and government schemes - always tied to "
    "real, verifiable current affairs. "
    "You NEVER produce content unrelated to this scope (no general chit-chat, "
    "no coding help, no entertainment, no personal advice unrelated to exam "
    "prep, no unverified rumours or speculation). "
    "If asked to do anything outside this scope, refuse and output an empty "
    "JSON object matching the requested schema instead. "
    "Always respond with STRICT JSON only - no markdown fences, no preamble."
)


def _get_client() -> AsyncOpenAI:
    global _client, _http_client
    if _client is None:
        _http_client = httpx.AsyncClient(timeout=60.0)
        _client = AsyncOpenAI(
            api_key=settings.OPENAI_API_KEY,
            http_client=_http_client,
            max_retries=0,
        )
    return _client


async def _chat_json(model: str, system: str, user: str, max_tokens: int = 2000) -> dict:
    resp = await _get_client().chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": f"{SCOPE_GUARDRAIL}\n\n{system}"},
            {"role": "user", "content": user},
        ],
        response_format={"type": "json_object"},
        max_tokens=max_tokens,
        temperature=0.4,
    )
    raw = resp.choices[0].message.content
    return json.loads(raw)


# ---------------------------------------------------------------------------
# 1. Main question generation (bulk seed Jan 2025 - Jul 2026). Uses
#    MODEL_MAIN and batches requests so one malformed response does not
#    abort an entire month.
# ---------------------------------------------------------------------------
async def generate_topics_and_questions(month: int, year: int, count: int = 30) -> list[dict]:
    target_count = max(0, count)
    if target_count == 0:
        return []

    batch_size = 6
    max_batches = max(2, (target_count + batch_size - 1) // batch_size + 4)
    topics: list[dict] = []
    seen_titles: set[str] = set()
    consecutive_short_batches = 0

    for batch_number in range(1, max_batches + 1):
        if len(topics) >= target_count:
            break

        requested = min(batch_size, target_count - len(topics))
        batch_topics = await _generate_topics_batch(
            month=month,
            year=year,
            count=requested,
            existing_titles=[str(t.get("title", "")) for t in topics],
            batch_number=batch_number,
        )
        if batch_topics is None:
            continue

        if len(batch_topics) < requested:
            consecutive_short_batches += 1
        else:
            consecutive_short_batches = 0

        for topic in batch_topics:
            title = topic.get("title")
            if not title:
                logger.warning("Skipping generated topic without title for %04d-%02d", year, month)
                continue

            title_key = _title_key(title)
            if title_key in seen_titles:
                logger.warning("Skipping duplicate generated topic title for %04d-%02d: %s", year, month, title)
                continue

            seen_titles.add(title_key)
            topics.append(topic)
            if len(topics) >= target_count:
                break

        if consecutive_short_batches >= 2:
            logger.warning(
                "Stopping bulk topic generation early for %04d-%02d after two short batches (%s/%s topics).",
                year,
                month,
                len(topics),
                target_count,
            )
            break

    if len(topics) < target_count:
        logger.warning(
            "Generated %s/%s requested bulk topics for %04d-%02d.",
            len(topics),
            target_count,
            year,
            month,
        )

    return topics[:target_count]


async def _generate_topics_batch(
    month: int,
    year: int,
    count: int,
    existing_titles: list[str],
    batch_number: int,
) -> list[dict] | None:
    system = (
        "Generate a JSON object {\"topics\": [...]} with the requested number "
        "of realistic, syllabus-relevant Indian current-affairs items for the "
        "given month/year that a UPSC/State-PSC/SSC aspirant should know. Each "
        "topic object must have: title (string), summary (2-3 sentences), "
        "subject_tags (array from [polity, economy, history, geography, "
        "environment, science_tech, ethics, international_relations, schemes]), "
        "source_date (YYYY-MM-DD within that month/year), and question: "
        "{question_text, options: [{key:'A',text},...4 options], correct_option "
        "(A/B/C/D), explanation}. The answer key field must be named exactly "
        "`correct_option`; do not use `correct_answer`, `answer`, or any other "
        "alternate key. Base topics on real, well-known events from "
        "that period; do not invent fictitious events. Avoid repeating any title "
        "listed by the user. If there are fewer real events left than requested, "
        "return fewer topics rather than padding with filler."
    )
    user = json.dumps(
        {
            "month": month,
            "year": year,
            "count": count,
            "batch_number": batch_number,
            "already_collected_titles": existing_titles,
        }
    )
    retry_delays = [1, 3]
    last_error: Exception | None = None
    logger.info("Requesting bulk topic batch for %04d-%02d batch %s count=%s", year, month, batch_number, count)

    for attempt in range(len(retry_delays) + 1):
        try:
            data = await _chat_json(settings.MODEL_MAIN, system, user, max_tokens=4500)
            if not isinstance(data, dict):
                raise ValueError("generated JSON root was not an object")
            topics = data.get("topics", [])
            if not isinstance(topics, list):
                raise ValueError("generated topics payload was not a list")
            logger.info(
                "Received %s topics for %04d-%02d batch %s",
                len(topics),
                year,
                month,
                batch_number,
            )
            return topics
        except (json.JSONDecodeError, OpenAIError, TypeError, ValueError) as exc:
            last_error = exc
            if attempt < len(retry_delays):
                await asyncio.sleep(retry_delays[attempt])
                continue

    logger.warning(
        "Bulk topic batch failed for %04d-%02d batch %s after retries: %s",
        year,
        month,
        batch_number,
        last_error,
    )
    return None


def _title_key(title: str) -> str:
    return " ".join(str(title).casefold().split())


# ---------------------------------------------------------------------------
# 2. Breakdown slide generation (6 slides: 3 theory + 3 practice) for a
#    question the student got wrong. Uses MODEL_CHEAP — this is the
#    highest-volume call in the whole system.
# ---------------------------------------------------------------------------
async def generate_breakdown(question_text: str, correct_option: str, explanation: str, subject_tags: list[str]) -> list[dict]:
    system = (
        "A student answered the given exam question incorrectly. Produce a JSON "
        "object {\"slides\": [...]} with EXACTLY 6 slides that will be shown to "
        "the student in order to rebuild their understanding before they retry "
        "the same question:\n"
        "- slides 1-3: slide_type='theory'. Each covers one underlying subject "
        "concept needed to answer correctly (pick from polity/economy/history/"
        "geography/environment/science_tech/ethics/international_relations - "
        "whichever actually apply to this question). Fields: slide_order, "
        "slide_type, subject, content (150-250 word clear explanation, markdown ok).\n"
        "- slides 4-6: slide_type='practice'. Each is a short MCQ testing ONE of "
        "the 3 theory concepts just taught. Fields: slide_order, slide_type, "
        "subject, practice_question, practice_options (4 options with key/text), "
        "practice_correct_option, practice_explanation.\n"
        "Keep everything tightly scoped to what's needed to re-answer the "
        "original question correctly."
    )
    user = json.dumps({
        "question_text": question_text,
        "correct_option": correct_option,
        "explanation": explanation,
        "subject_tags": subject_tags,
    })
    retry_delays = [1, 3]
    last_error: Exception | None = None
    for attempt in range(len(retry_delays) + 1):
        try:
            data = await _chat_json(settings.MODEL_CHEAP, system, user, max_tokens=3000)
            if not isinstance(data, dict):
                raise ValueError("generated breakdown JSON root was not an object")
            slides = data.get("slides", [])
            if not isinstance(slides, list):
                raise ValueError("generated breakdown slides payload was not a list")
            if len(slides) != 6:
                raise ValueError(f"expected 6 breakdown slides, got {len(slides)}")
            return slides
        except (json.JSONDecodeError, OpenAIError, TypeError, ValueError) as exc:
            last_error = exc
            if attempt < len(retry_delays):
                await asyncio.sleep(retry_delays[attempt])
                continue
    raise RuntimeError(f"breakdown generation failed after retries: {last_error}")


# ---------------------------------------------------------------------------
# 3. Nightly personalised report. Uses MODEL_CHEAP.
# ---------------------------------------------------------------------------
async def generate_report_feedback(stats: dict) -> str:
    system = (
        "Given a student's practice stats for the day (JSON), write a short "
        "(80-120 word) personalised, encouraging but honest feedback paragraph "
        "for a competitive-exam aspirant: what they did well, weakest subject "
        "area to focus on next, and one concrete next step. Return JSON: "
        "{\"feedback\": \"...\"}."
    )
    data = await _chat_json(settings.MODEL_CHEAP, system, json.dumps(stats), max_tokens=400)
    return data.get("feedback", "")


# ---------------------------------------------------------------------------
# 4. Daily "what happened today" research step (from 10 Jul 2026 onward).
#    Requires a browsing-capable model/tool. See README for the caveat on
#    plain chat-completions models not having live internet access.
# ---------------------------------------------------------------------------
async def research_todays_current_affairs(date_str: str, count: int = 10) -> list[dict]:
    resp = await _get_client().responses.create(
        model=settings.MODEL_SEARCH,
        tools=[{"type": "web_search"}],
        input=(
            f"{SCOPE_GUARDRAIL}\n\n"
            f"Find {count} real, syllabus-relevant Indian current-affairs stories "
            f"from {date_str} suitable for UPSC/State-PSC/SSC aspirants. "
            "Return STRICT JSON only: {\"topics\": [{title, summary, subject_tags, "
            "source_date, question:{question_text, options:[{key,text}x4], "
            "correct_option, explanation}}]}"
        ),
    )
    text = resp.output_text
    data = json.loads(text)
    return data.get("topics", [])
