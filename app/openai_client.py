"""
All OpenAI calls go through this module so model routing + guardrails
live in exactly one place.

Model routing (cost control):
  - MODEL_SEARCH -> grounded bulk, breakdowns, and daily current-affairs research
  - MODEL_CHEAP  -> nightly personalised reports

Guardrail: every single call carries the same strict system prompt that
locks the model to Indian competitive-exam syllabus + current affairs,
refusing anything else (chit-chat, code help, unrelated trivia, etc).
"""

import asyncio
import json
import logging
import random
import re
import httpx
from urllib.parse import urlparse
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

QUESTION_FORMATS = ("statement", "assertion_reason", "negative", "matching")
QUESTION_OPTION_KEYS = {"A", "B", "C", "D"}
PRIMARY_SOURCE_DOMAINS = (
    "gov.in",
    "nic.in",
    "rbi.org.in",
    "sansad.in",
    "europa.eu",
    "un.org",
    "worldbank.org",
    "imf.org",
    "who.int",
)

QUESTION_QUALITY_INSTRUCTIONS = """
Write these at the hardest tier of UPSC Civil Services Prelims - the level that
filters serious toppers, not just qualified candidates. A well-read newspaper
reader should get this wrong; only someone who has studied the underlying static
syllabus concept precisely should get it right.

FORMAT VARIETY:
For every question, first select exactly ONE archetype and record it in the
question object's internal `format` field. The only allowed values are
`statement`, `assertion_reason`, `negative`, and `matching`. Follow any
`question_format_plan` supplied by the user exactly; it is a randomized plan
designed to prevent repeatedly defaulting to statement-combination questions.
Use the selected archetype as follows:
1. `statement`: Give 3-4 numbered statements and combination options such as
   "1 and 2 only".
2. `assertion_reason`: Write "Assertion (A): ... Reason (R): ..." and use the
   four standard options: both A and R are true and R is the correct explanation
   of A; both are true but R is not the correct explanation of A; A is true but
   R is false; A is false but R is true.
3. `negative`: Explicitly invert the normal framing by asking which statement
   is NOT correct or how many pairs are NOT correctly matched. Make the negative
   word unmistakable in the question stem, while keeping the underlying trap
   difficult.
4. `matching`: Show two short columns or 3-4 clearly numbered pairs, such as
   scheme-ministry, species-habitat/status, or event-year/article, and ask how
   many are correctly matched. Use four count-based options appropriate to the
   number of pairs, such as "Only one pair", "Only two pairs", "All three
   pairs", and "None of the pairs".

The archetype must be visible in `question_text`, not merely asserted in the
`format` field or topic title. Label every numbered statement or pair exactly
`1.`, `2.`, `3.` (and `4.` if used). A `statement` question must put all 3-4
numbered statements in the stem and use combination answers. A `negative` question must
put numbered statements or pairs in the stem and ask what/how many is NOT
correct; do not disguise an ordinary one-correct-answer MCQ by merely adding
"NOT". A `matching` question must put every numbered match in the stem and use
count answers. An `assertion_reason` question must contain both labelled parts.
The topic title must describe the news event and must never contain an archetype
label such as "Assertion and Reason" or "Matching".

PRECISION TRAPS:
Every false statement or pair must be false because of exactly ONE specific,
precise, independently checkable detail: an exact number, an Article or section
number, a year, a Schedule/List/tier classification, or a named authority.
Never use a vague, broad, subjective, or obviously wrong falsehood. Write true
and false statements in equally confident textbook-register language so style
does not reveal the answer. In the explanation, identify the exact detail that
makes each false statement or pair false and give the corrected detail.
Do not use giveaway absolutes such as "completely", "exclusively", "only", or
"all" as the sole reason a claim is false. Do not make a distractor false merely
because it is unrelated to the topic. Each explanation must audit every numbered
statement, A/R component, or pair individually, marking it true or false; for
each false component it must name both the planted incorrect detail and its exact
replacement. Before returning JSON, silently verify that every claimed number,
year, Article/section, classification, and authority is factually accurate.

CROSS-TOPIC SYNTHESIS:
Where the topic allows, at least one statement, assertion/reason component, or
pair in every question must connect the current event to a different static
syllabus area from the obvious one. For example, test an environment event partly
through a constitutional or statutory provision rather than using only ecology
facts. State that cross-topic connection in the explanation.

OUTPUT INTEGRITY:
Return exactly four option objects and no other array members. Their keys must be
exactly A, B, C, and D once each, and each must contain a `text` string. Put
`correct_option` beside `options` in the question object, never inside the
options array. It must be exactly one of A/B/C/D. Do not invent a current event,
policy, bill, summit host, target, or launch. If you cannot confidently ground a
topic in a real event from the requested period, omit it rather than fabricate
one. Silently audit each question against all these requirements before output.
Each question object must also contain an internal `precision_trap` object with
`false_component`, `wrong_detail`, and `correct_detail` strings, plus a non-empty
`cross_topic_link` string. The wrong and corrected details must isolate the exact
number, year, Article/section, Schedule/List/tier, or named authority used as the
trap. Do not repeat answer choices inside `question_text`; choices belong only in
the `options` array. Every question must contain at least one false component.
For Assertion-Reason, do not make option A the answer. For matching questions,
do not make all or none of the pairs correct. For statement and negative formats,
do not make all statements correct or all statements false.
""".strip()


def _question_format_plan(count: int) -> list[str]:
    """Return a randomized plan that still guarantees variety in larger batches."""
    plan: list[str] = []
    while len(plan) < count:
        cycle = list(QUESTION_FORMATS)
        random.SystemRandom().shuffle(cycle)
        plan.extend(cycle)
    return plan[:count]


def _validate_question_formats(topics: list[dict], format_plan: list[str]) -> None:
    """Reject a batch that ignored its assigned archetypes."""
    for index, topic in enumerate(topics):
        if index >= len(format_plan):
            raise ValueError("generated more topics than requested by the format plan")
        question = topic.get("question")
        actual_format = question.get("format") if isinstance(question, dict) else None
        if actual_format != format_plan[index]:
            raise ValueError(
                f"question {index + 1} used format {actual_format!r}; "
                f"expected {format_plan[index]!r}"
            )
        _validate_question_payload(question, actual_format, index)


def validate_generated_topic(topic: dict, expected_format: str | None = None) -> None:
    """Reject legacy or weak question payloads before they can be persisted."""
    if not isinstance(topic, dict):
        raise ValueError("generated topic must be an object")
    _validate_topic_sources([topic])
    question = topic.get("question")
    if not isinstance(question, dict):
        raise ValueError("generated topic is missing its question object")
    question_format = question.get("format")
    if question_format not in QUESTION_FORMATS:
        raise ValueError(f"question used unsupported format {question_format!r}")
    if expected_format is not None and question_format != expected_format:
        raise ValueError(
            f"question used format {question_format!r}; expected {expected_format!r}"
        )
    _validate_question_payload(question, question_format, 0)


def validate_topics_for_storage(topics: list[dict]) -> None:
    """Preflight a complete generated batch before any database writes occur."""
    if not isinstance(topics, list):
        raise ValueError("generated topics payload must be a list")
    for topic in topics:
        validate_generated_topic(topic)


def _validate_topic_sources(topics: list[dict]) -> None:
    for index, topic in enumerate(topics):
        source_urls = topic.get("source_urls")
        if not isinstance(source_urls, list) or not source_urls:
            raise ValueError(f"topic {index + 1} has no research source URLs")
        if not all(isinstance(url, str) and url.startswith(("https://", "http://")) for url in source_urls):
            raise ValueError(f"topic {index + 1} has a malformed research source URL")
        if not any(_is_primary_source(url) for url in source_urls):
            raise ValueError(f"topic {index + 1} has no official or primary source URL")


def _is_primary_source(url: str) -> bool:
    hostname = (urlparse(url).hostname or "").casefold()
    return hostname in {"un.org", "sansad.in"} or hostname.endswith(
        (
            ".gov.in",
            ".nic.in",
            ".europa.eu",
            ".un.org",
            ".org.in",
            "rbi.org.in",
            "worldbank.org",
            "imf.org",
            "who.int",
        )
    )


def _validate_breakdown_slides(slides: list[dict]) -> None:
    if len(slides) != 6:
        raise ValueError(f"expected 6 breakdown slides, got {len(slides)}")
    if [slide.get("slide_order") for slide in slides] != [1, 2, 3, 4, 5, 6]:
        raise ValueError("breakdown slide_order values must be exactly 1 through 6")
    if [slide.get("slide_type") for slide in slides[:3]] != ["theory"] * 3:
        raise ValueError("breakdown slides 1-3 must be theory slides")
    if [slide.get("slide_type") for slide in slides[3:]] != ["practice"] * 3:
        raise ValueError("breakdown slides 4-6 must be practice slides")
    first_content = slides[0].get("content")
    if not isinstance(first_content, str) or "precision hinge:" not in first_content.casefold():
        raise ValueError("first theory slide must explicitly identify the Precision hinge")
    for slide in slides[3:]:
        options = slide.get("practice_options")
        if not isinstance(options, list) or len(options) != 4:
            raise ValueError("each practice slide must contain exactly four options")
        if [option.get("key") for option in options if isinstance(option, dict)] != ["A", "B", "C", "D"]:
            raise ValueError("practice option keys must be exactly A, B, C, D")
        if slide.get("practice_correct_option") not in QUESTION_OPTION_KEYS:
            raise ValueError("practice_correct_option must be A, B, C, or D")


def _validate_question_payload(question: dict, question_format: str, index: int) -> None:
    """Enforce machine-checkable parts of the prompt before persistence."""
    question_text = str(question.get("question_text", ""))
    options = question.get("options")
    correct_option = question.get("correct_option")
    explanation = question.get("explanation")
    prefix = f"question {index + 1}"

    if not isinstance(options, list) or len(options) != 4:
        raise ValueError(f"{prefix} must contain exactly four options")
    if not all(isinstance(option, dict) and isinstance(option.get("text"), str) for option in options):
        raise ValueError(f"{prefix} contains a malformed option")
    if [option.get("key") for option in options] != ["A", "B", "C", "D"]:
        raise ValueError(f"{prefix} option keys must be exactly A, B, C, D")
    if correct_option not in QUESTION_OPTION_KEYS:
        raise ValueError(f"{prefix} has an invalid correct_option")
    correct_option_index = ord(correct_option) - ord("A")

    precision_trap = question.get("precision_trap")
    if not isinstance(precision_trap, dict):
        raise ValueError(f"{prefix} is missing its precision_trap")
    for field in ("false_component", "wrong_detail", "correct_detail"):
        value = precision_trap.get(field)
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{prefix} precision_trap is missing {field}")
    if not _is_precision_value(precision_trap["wrong_detail"]):
        raise ValueError(f"{prefix} wrong_detail is not a precise factual detail")
    if not _is_precision_value(precision_trap["correct_detail"]):
        raise ValueError(f"{prefix} correct_detail is not a precise factual detail")
    if precision_trap["wrong_detail"].casefold() == precision_trap["correct_detail"].casefold():
        raise ValueError(f"{prefix} precision trap does not change the factual detail")
    if not isinstance(question.get("cross_topic_link"), str) or not question["cross_topic_link"].strip():
        raise ValueError(f"{prefix} is missing its cross_topic_link")
    if not isinstance(explanation, str) or not explanation.strip():
        raise ValueError(f"{prefix} is missing its audited explanation")
    if precision_trap["correct_detail"].casefold() not in explanation.casefold():
        raise ValueError(f"{prefix} explanation does not state the corrected precision detail")

    lowered = question_text.casefold()
    numbered_items = sum(marker in question_text for marker in ("1.", "2.", "3.", "4."))
    if question_format != "matching" and re.search(r"(?:^|\n)\s*[ABCD][).]\s", question_text):
        raise ValueError(f"{prefix} repeats answer choices inside question_text")
    if question_format == "assertion_reason":
        if "assertion (a)" not in lowered or "reason (r)" not in lowered:
            raise ValueError(f"{prefix} does not contain labelled Assertion and Reason text")
        if correct_option == "A":
            raise ValueError(f"{prefix} Assertion-Reason answer must contain a non-trivial trap")
    elif question_format == "negative":
        if "not" not in lowered or numbered_items < 3:
            raise ValueError(f"{prefix} is not a numbered negative-framing question")
        if "all statements are correct" in str(options[correct_option_index]["text"]).casefold():
            raise ValueError(f"{prefix} negative question has no false component")
    elif question_format in {"statement", "matching"} and numbered_items < 3:
        raise ValueError(f"{prefix} does not contain at least three numbered items")
    if question_format == "matching" and "how many" not in lowered:
        raise ValueError(f"{prefix} matching stem must ask how many pairs")
    correct_text = str(options[correct_option_index]["text"]).casefold()
    if question_format == "matching" and ("all" in correct_text or "none" in correct_text):
        raise ValueError(f"{prefix} matching answer must not be all or none")
    if question_format == "statement" and "all" in correct_text:
        raise ValueError(f"{prefix} statement question must contain a false statement")


def _is_precision_value(value: str) -> bool:
    return bool(
        re.search(
            r"\d|\b(?:article|section|schedule|list|tier|ministry|authority|"
            r"commission|council|court|board|agency|department|institute|"
            r"organisation|organization|bank|tribunal|secretariat|office)\b",
            value,
            flags=re.IGNORECASE,
        )
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


async def _search_json(model: str, system: str, user: str, max_tokens: int = 7500) -> dict:
    """Generate grounded JSON with web search available to the model."""
    resp = await _get_client().responses.create(
        model=model,
        tools=[
            {
                "type": "web_search",
                "filters": {"allowed_domains": list(PRIMARY_SOURCE_DOMAINS)},
                "search_context_size": "high",
            }
        ],
        instructions=f"{SCOPE_GUARDRAIL}\n\n{system}",
        input=user,
        max_output_tokens=max_tokens,
    )
    return json.loads(resp.output_text)


# ---------------------------------------------------------------------------
# 1. Main question generation (bulk seed Jan 2025 - Jul 2026). Uses
#    MODEL_SEARCH with web search and batches requests so one malformed response does not
#    abort an entire month.
# ---------------------------------------------------------------------------
async def generate_topics_and_questions(month: int, year: int, count: int = 30) -> list[dict]:
    target_count = max(0, count)
    if target_count == 0:
        return []

    batch_size = 4
    expected_batches = (target_count + batch_size - 1) // batch_size
    max_batches = max(2, expected_batches + 20)
    topics: list[dict] = []
    seen_titles: set[str] = set()

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
    format_plan = _question_format_plan(count)
    system = (
        "Generate a JSON object {\"topics\": [...]} with the requested number "
        "of realistic, syllabus-relevant Indian current-affairs items for the "
        "given month/year that a UPSC/State-PSC/SSC aspirant should know. Use "
        "web search before writing every topic and question. Every current-event "
        "premise, precision trap, and correction must be grounded in reliable "
        "sources. Every topic must include at least one official government, "
        "statutory, intergovernmental, or other primary source; coaching sites "
        "and news summaries may supplement but never replace that primary source. "
        "Each topic object must have: title (string), "
        "summary (2-3 sentences), source_urls (a non-empty array of exact source "
        "pages used), "
        "subject_tags (array from [polity, economy, history, geography, "
        "environment, science_tech, ethics, international_relations, schemes]), "
        "source_date (YYYY-MM-DD within that month/year), and question: "
        "{format ('statement'|'assertion_reason'|'negative'|'matching'), "
        "precision_trap, cross_topic_link, "
        "question_text, options: [{key:'A',text},...4 options], correct_option "
        "(A/B/C/D), explanation}. The answer key field must be named exactly "
        "`correct_option`; do not use `correct_answer`, `answer`, or any other "
        "alternate key. Base topics on real, well-known events from "
        "that period; do not invent fictitious events. Avoid repeating any title "
        "listed by the user. If there are fewer real events left than requested, "
        "return fewer topics rather than padding with filler.\n\n"
        f"{QUESTION_QUALITY_INSTRUCTIONS}"
    )
    user = json.dumps(
        {
            "month": month,
            "year": year,
            "count": count,
            "batch_number": batch_number,
            "already_collected_titles": existing_titles,
            "question_format_plan": format_plan,
        }
    )
    retry_delays = [1, 3]
    last_error: Exception | None = None
    logger.info("Requesting bulk topic batch for %04d-%02d batch %s count=%s", year, month, batch_number, count)

    for attempt in range(len(retry_delays) + 1):
        try:
            data = await _search_json(settings.MODEL_SEARCH, system, user, max_tokens=7500)
            if not isinstance(data, dict):
                raise ValueError("generated JSON root was not an object")
            topics = data.get("topics", [])
            if not isinstance(topics, list):
                raise ValueError("generated topics payload was not a list")
            valid_topics: list[dict] = []
            for index, topic in enumerate(topics):
                if index >= len(format_plan):
                    logger.warning("Skipping surplus topic in %04d-%02d batch %s", year, month, batch_number)
                    continue
                try:
                    _validate_topic_sources([topic])
                    _validate_question_formats([topic], [format_plan[index]])
                except (TypeError, ValueError) as exc:
                    logger.warning(
                        "Skipping invalid question %s in %04d-%02d batch %s: %s",
                        index + 1,
                        year,
                        month,
                        batch_number,
                        exc,
                    )
                    continue
                valid_topics.append(topic)
            if not valid_topics:
                raise ValueError("batch contained no individually valid topics")
            topics = valid_topics
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
#    question the student got wrong. Uses grounded search so dated facts do not
#    get overwritten by stale model knowledge.
# ---------------------------------------------------------------------------
async def generate_breakdown(
    question_text: str,
    correct_option: str,
    explanation: str,
    subject_tags: list[str],
    source_urls: list[str] | None = None,
) -> list[dict]:
    system = (
        "A student answered the given exam question incorrectly. Produce a JSON "
        "object {\"slides\": [...]} with EXACTLY 6 slides that will be shown to "
        "the student in order to rebuild their understanding before they retry "
        "the same question:\n"
        "- slides 1-3: slide_type='theory'. Each covers one underlying subject "
        "concept needed to answer correctly (pick from polity/economy/history/"
        "geography/environment/science_tech/ethics/international_relations - "
        "whichever actually apply to this question). Slide 1 must begin with "
        "the exact label 'Precision hinge:' and state the planted incorrect "
        "number/article/year/classification/authority alongside the exact corrected "
        "fact. Fields: slide_order, "
        "slide_type, subject, content (150-250 word clear explanation, markdown ok).\n"
        "- slides 4-6: slide_type='practice'. Each is a short MCQ testing ONE of "
        "the 3 theory concepts just taught. Fields: slide_order, slide_type, "
        "subject, practice_question, practice_options (4 options with key/text), "
        "practice_correct_option, practice_explanation. Each practice distractor "
        "must follow the same precision-trap rule as the original question, and "
        "its explanation must give the exact corrected detail.\n"
        "Keep everything tightly scoped to what's needed to re-answer the "
        "original question correctly. The student may have been tripped by a "
        "precision detail (a number, article, year, or classification) rather "
        "than a conceptual gap - make sure at least one theory slide addresses "
        "the EXACT factual distinction the question hinged on, not just the "
        "general topic area. Use web search and the supplied source URLs to verify "
        "the distinction. Treat current, dated primary sources as authoritative; "
        "never contradict them using stale model knowledge."
    )
    user = json.dumps({
        "question_text": question_text,
        "correct_option": correct_option,
        "explanation": explanation,
        "subject_tags": subject_tags,
        "source_urls": source_urls or [],
    })
    retry_delays = [1, 3, 8, 15]
    last_error: Exception | None = None
    for attempt in range(len(retry_delays) + 1):
        try:
            data = await _search_json(settings.MODEL_SEARCH, system, user, max_tokens=4500)
            if not isinstance(data, dict):
                raise ValueError("generated breakdown JSON root was not an object")
            slides = data.get("slides", [])
            if not isinstance(slides, list):
                raise ValueError("generated breakdown slides payload was not a list")
            _validate_breakdown_slides(slides)
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
    format_plan = _question_format_plan(count)
    system = (
        "Research current affairs for the exact requested date. Use web search "
        "before writing every topic and include at least one official or primary "
        "source URL per topic. Return exactly the requested number of topics; if "
        "that cannot be done without weakening the requirements, fail rather than "
        "returning legacy-format or filler questions.\n\n"
        f"{QUESTION_QUALITY_INSTRUCTIONS}"
    )
    user = (
            f"Find {count} real, syllabus-relevant Indian current-affairs stories "
            f"from {date_str} suitable for UPSC/State-PSC/SSC aspirants. "
            "Return STRICT JSON only: {\"topics\": [{title, summary, source_urls, subject_tags, "
            "source_date, question:{format, precision_trap, cross_topic_link, "
            "question_text, options:[{key,text}x4], "
            "correct_option, explanation}}]}. Follow this randomized question "
            f"format plan exactly, in topic order: {json.dumps(format_plan)}"
    )
    retry_delays = [1, 3, 8]
    last_error: Exception | None = None
    for attempt in range(len(retry_delays) + 1):
        try:
            data = await _search_json(settings.MODEL_SEARCH, system, user, max_tokens=7500)
            if not isinstance(data, dict):
                raise ValueError("researched JSON root was not an object")
            topics = data.get("topics", [])
            if not isinstance(topics, list):
                raise ValueError("researched topics payload was not a list")
            if len(topics) != count:
                raise ValueError(f"daily generation returned {len(topics)}/{count} topics")
            _validate_topic_sources(topics)
            _validate_question_formats(topics, format_plan)
            validate_topics_for_storage(topics)
            return topics
        except (json.JSONDecodeError, OpenAIError, TypeError, ValueError) as exc:
            last_error = exc
            if attempt < len(retry_delays):
                await asyncio.sleep(retry_delays[attempt])
    raise RuntimeError(f"daily current-affairs generation failed validation after retries: {last_error}")
