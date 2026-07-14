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
    "ethics, international relations, and government schemes. Content may be "
    "either real, verifiable current affairs or explicitly requested static-syllabus "
    "practice grounded in authoritative textbook, statutory, and constitutional facts. "
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
    "legislative.gov.in",
    "indiacode.nic.in",
    "sci.gov.in",
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
`1.`, `2.`, `3.` (and `4.` if used). EVERY numbered statement or pair must
start on its own new line; never serialize `1. ... 2. ... 3. ...` into one
paragraph. Put Assertion (A) and Reason (R) on separate lines as well. A
`statement` question must put all 3-4 numbered statements in the stem and use
combination answers. A `negative` question must
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


STATIC_SUBJECT_QUALITY_INSTRUCTIONS = """
STATIC-SYLLABUS DIFFICULTY OVERRIDE:
Static-syllabus content must be written at a HARDER tier than current-affairs
questions. There is no news event to hide behind: the entire test must turn on
precise, authoritative static knowledge.

1. Every question MUST use exactly one of the four assigned UPSC archetypes:
`statement`, `assertion_reason`, `negative`, or `matching`. Follow the supplied
`question_format_plan` by question index exactly. Never write a plain
direct-recall question, a one-line "What is...?" question, or any fifth format.
The selected archetype must be structurally visible in the stem and recorded in
the question object's `format` field. Numbered statements/pairs and Assertion/
Reason components must each start on their own line.

For every `assertion_reason` question the option meanings and letters are fixed
and MUST NEVER be shuffled or paraphrased into another slot:
A = Both A and R are true, and R is the correct explanation of A.
B = Both A and R are true, but R is not the correct explanation of A.
C = A is true, but R is false.
D = A is false, but R is true.
Assess whether the Reason sentence is factually true independently from whether
it explains the Assertion. "R is true but does not explain A" is option B, not C.
For this static hard-tier batch, construct Assertion-Reason questions with
exactly one factually false component, so the correct answer is C or D; still
include all four fixed standard options.

2. At least one statement, pair, Assertion, or Reason in EVERY question must
test an EXACT figure aspirants commonly confuse: an Article number, amendment
number, year, Schedule number, Union/State/Concurrent List placement, or a
specific case name or bench size. A general conceptual restatement does not
satisfy this requirement.

3. Construct every false statement by starting from a true,
textbook-accurate statement and altering exactly ONE precise detail. Examples:
Article 32 -> Article 226; 42nd Amendment -> 44th Amendment; 9 judges -> 7
judges. The rest of that sentence must remain entirely accurate. Record one
such mutation in `precision_trap.false_component`,
`precision_trap.wrong_detail`, and `precision_trap.correct_detail`. The wrong
detail must appear verbatim in the false component/stem, and the explanation
must identify it and supply the exact corrected detail. Never use a vague,
subjective, unrelated, or stylistically obvious falsehood. True and false
components must use the same confident textbook register.

4. This question must be difficult enough that an aspirant who has only read
NCERT or a summary article will get it wrong. Only someone who has studied
Laxmikanth-level detail and the bare constitutional text should be able to
answer confidently. Do not write a question that can be answered through
general awareness or elimination of obviously wrong options.

5. Reject formulaic answer patterns. Follow the supplied `correct_option_plan`
exactly. For statement-combination questions, the correct answer must NEVER be
"1, 2 and 3", "1, 2 and 3 only", or an equivalent all-three formulation.
Vary which combination and how many statements/pairs are correct across the
batch. Never reuse the same statement-combination answer within a batch, and
never place the same correct option letter on consecutive questions. Students
must not be able to pattern-match the answer key.

Stay strictly inside the requested subject and chapter boundary. Do not depend
on a current event, and do not drift into another chapter merely to create a
distractor. Every option and explanation must be factually auditable against
the bare text, an authoritative statute/case, or a standard advanced textbook.
Silently audit every exact number, Article, amendment, year, Schedule, List,
case name, authority, and bench size before returning JSON.

ANSWER CONSISTENCY CONTRACT:
Before writing options, determine the truth value of every component. Include an
internal `answer_audit` object. For statement/negative/matching questions it must
be {component_truth: {"1": true|false, "2": true|false, ...}} covering every
numbered component exactly once. For Assertion-Reason it must be
{assertion_true: true|false, reason_true: true|false,
reason_explains_assertion: true|false}. Also add `false_component_id` to
`precision_trap` ("1"-"4", "A", or "R") identifying the component containing
the recorded wrong detail. Derive the correct answer from this audit, then place
it at the assigned option letter. The explanation must agree with the audit and
must explicitly name the correct option letter AND reproduce its exact option
text. Never force an assigned answer letter by attaching it to a factually
inconsistent option.
""".strip()


STATIC_EXACT_DETAIL_RE = re.compile(
    r"(?:\b(?:Article|Articles|Amendment|Schedule|List|Entry|Section)\s+[\w-]+"
    r"|\b\d+(?:st|nd|rd|th)?\s+(?:Constitutional\s+)?Amendment\b"
    r"|\b(?:First|Second|Third|Fourth|Fifth|Sixth|Seventh|Eighth|Ninth|Tenth)\s+(?:Schedule|Amendment)\b"
    r"|\b(?:Union|State|Concurrent)\s+List\b"
    r"|\b(?:17|18|19|20)\d{2}\b"
    r"|\b\d+\s*(?:-|\s)?(?:judge|judges|member|members|seat|seats)\b"
    r"|\b[A-Z][A-Za-z.&' -]+\s+v(?:s\.)?\s+[A-Z][A-Za-z.&' -]+)",
    flags=re.IGNORECASE,
)


def _question_format_plan(count: int) -> list[str]:
    """Return a randomized plan that still guarantees variety in larger batches."""
    plan: list[str] = []
    while len(plan) < count:
        cycle = list(QUESTION_FORMATS)
        random.SystemRandom().shuffle(cycle)
        plan.extend(cycle)
    return plan[:count]


def _subject_answer_key_plan(format_plan: list[str]) -> list[str]:
    """Vary answer letters while respecting the harder Assertion-Reason rule."""
    rng = random.SystemRandom()
    plan: list[str] = []
    previous: str | None = None
    for question_format in format_plan:
        choices = ["C", "D"] if question_format == "assertion_reason" else ["A", "B", "C", "D"]
        if previous in choices and len(choices) > 1:
            choices.remove(previous)
        selected = rng.choice(choices)
        plan.append(selected)
        previous = selected
    return plan


def _normalized_question_key(question_text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(question_text).casefold()).strip()


def _question_token_set(question_text: str) -> set[str]:
    return {
        token
        for token in _normalized_question_key(question_text).split()
        if len(token) > 2 and token not in {"the", "and", "was", "with", "following"}
    }


def _is_near_duplicate(question_text: str, existing_texts: list[str], threshold: float = 0.72) -> bool:
    candidate = _question_token_set(question_text)
    if not candidate:
        return True
    for existing in existing_texts:
        other = _question_token_set(existing)
        union = candidate | other
        if union and len(candidate & other) / len(union) >= threshold:
            return True
    return False


def _statement_answer_signature(answer_text: str) -> str | None:
    numbers = re.findall(r"(?<!\d)[1-4](?!\d)", answer_text)
    if not numbers:
        return None
    return ",".join(sorted(set(numbers)))


def _validate_subject_question(
    question: dict,
    expected_format: str,
    expected_correct_option: str,
    index: int,
    require_sources: bool = True,
) -> None:
    if not isinstance(question, dict):
        raise ValueError(f"subject question {index + 1} is not an object")
    if question.get("format") != expected_format:
        raise ValueError(
            f"subject question {index + 1} used format {question.get('format')!r}; "
            f"expected {expected_format!r}"
        )
    _validate_question_payload(question, expected_format, index, require_cross_topic=False)
    if question.get("correct_option") != expected_correct_option:
        raise ValueError(
            f"subject question {index + 1} used correct option "
            f"{question.get('correct_option')!r}; expected {expected_correct_option!r}"
        )

    question_text = str(question.get("question_text", ""))
    if not STATIC_EXACT_DETAIL_RE.search(question_text):
        raise ValueError(
            f"subject question {index + 1} does not test an exact static-syllabus detail"
        )
    precision_trap = question["precision_trap"]
    wrong_detail = str(precision_trap["wrong_detail"]).strip()
    correct_detail = str(precision_trap["correct_detail"]).strip()
    if not STATIC_EXACT_DETAIL_RE.search(wrong_detail) or not STATIC_EXACT_DETAIL_RE.search(correct_detail):
        raise ValueError(
            f"subject question {index + 1} precision mutation is not an exact static detail"
        )
    if _normalized_detail(wrong_detail) not in _normalized_detail(question_text):
        raise ValueError(
            f"subject question {index + 1} wrong detail does not appear in its stem"
        )
    false_component_id = str(precision_trap.get("false_component_id", "")).upper()
    audited_component_text = _component_text(question_text, expected_format, false_component_id)
    if _normalized_detail(wrong_detail) not in _normalized_detail(audited_component_text):
        raise ValueError(
            f"subject question {index + 1} wrong detail does not appear in its audited false component"
        )

    if require_sources:
        sources = question.get("verification_sources")
        if not isinstance(sources, list) or not sources:
            raise ValueError(f"subject question {index + 1} has no official verification sources")
        if not all(isinstance(url, str) and url.startswith(("https://", "http://")) for url in sources):
            raise ValueError(f"subject question {index + 1} has malformed verification sources")
        if not any(_is_primary_source(url) for url in sources):
            raise ValueError(f"subject question {index + 1} lacks a primary verification source")

    options = question["options"]
    correct_index = ord(expected_correct_option) - ord("A")
    correct_text = str(options[correct_index]["text"])
    signature = _statement_answer_signature(correct_text)
    if expected_format == "statement" and signature == "1,2,3":
        raise ValueError(
            f"subject question {index + 1} uses the forbidden 1, 2 and 3 answer pattern"
        )
    if expected_format == "matching" and not _is_canonical_count_option(correct_text):
        raise ValueError(
            f"subject question {index + 1} matching answer is not a canonical count option"
        )
    if _normalized_detail(correct_text) not in _normalized_detail(question["explanation"]):
        raise ValueError(
            f"subject question {index + 1} explanation does not reproduce the correct option text"
        )
    if not re.search(
        rf"(?i)\b(?:option|answer)\s*(?:is|:)\s*{re.escape(expected_correct_option)}\b",
        str(question["explanation"]),
    ):
        raise ValueError(
            f"subject question {index + 1} explanation does not explicitly confirm option {expected_correct_option}"
        )
    _validate_subject_answer_audit(question, expected_format, correct_text, index)


def _component_text(question_text: str, question_format: str, component_id: str) -> str:
    if question_format == "assertion_reason" and component_id in {"A", "R"}:
        label = "Assertion" if component_id == "A" else "Reason"
        match = re.search(rf"(?mi)^\s*{label}\s*\({component_id}\)\s*:\s*(.+)$", question_text)
        return match.group(1) if match else ""
    if component_id in {"1", "2", "3", "4"}:
        match = re.search(rf"(?m)^\s*{component_id}[.)]\s*(.+)$", question_text)
        return match.group(1) if match else ""
    return ""


def _validate_subject_chapter_scope(question: dict, subject: str, chapter: str, index: int) -> None:
    if subject.casefold() != "polity" or chapter.casefold() != "constitutional framework":
        return
    searchable = " ".join(
        [
            str(question.get("question_text", "")),
            str(question.get("explanation", "")),
            str(question.get("precision_trap", {})),
        ]
    )
    forbidden = re.compile(
        r"\b(?:Union List|State List|Concurrent List|Seventh Schedule|Parliament|"
        r"Lok Sabha|Rajya Sabha|Supreme Court|High Court|Fundamental Rights|"
        r"Directive Principles|Fundamental Duties|ordinance|Article 123|Article 246|"
        r"Article 248|Article 226|Article 32)\b",
        flags=re.IGNORECASE,
    )
    if forbidden.search(searchable):
        raise ValueError(f"subject question {index + 1} strays outside Constitutional Framework")
    allowed_articles = set(range(1, 12)) | {368, 393, 394, 395}
    article_numbers = {
        int(value)
        for value in re.findall(r"\bArticle\s+(\d+)\b", searchable, flags=re.IGNORECASE)
    }
    if article_numbers - allowed_articles:
        raise ValueError(
            f"subject question {index + 1} uses out-of-scope Articles "
            f"{sorted(article_numbers - allowed_articles)}"
        )


def _numbered_component_ids(question_text: str) -> set[str]:
    return set(re.findall(r"(?m)^\s*([1-4])[.)]\s+", question_text))


def _count_from_option(text: str, total: int) -> int | None:
    normalized = _normalized_detail(text)
    if re.search(r"\bnone\b|\bzero\b", normalized):
        return 0
    word_counts = {"one": 1, "two": 2, "three": 3, "four": 4}
    for word, value in word_counts.items():
        if re.search(rf"\b{word}\b", normalized):
            return value
    digit = re.search(r"(?<!\d)([0-4])(?!\d)", normalized)
    if digit:
        return int(digit.group(1))
    if re.search(r"\ball\b", normalized):
        return total
    return None


def _is_canonical_count_option(text: str) -> bool:
    value = _normalized_detail(text)
    return bool(
        re.fullmatch(
            r"(?:only )?(?:one|two|three|four|[1-4]) pairs?(?: (?:is|are))? "
            r"(?:correctly matched|not correctly matched)",
            value,
        )
        or re.fullmatch(r"(?:only )?(?:one|two|three|four|[1-4]) pairs?", value)
        or re.fullmatch(r"all (?:one|two|three|four|[1-4]) pairs?", value)
        or value == "none of the pairs"
    )


def _assertion_option_kind(text: str) -> str | None:
    value = _normalized_detail(text)
    a_true = "assertion a is true" in value or "a is true" in value
    a_false = "assertion a is false" in value or "a is false" in value
    r_true = "reason r is true" in value or "r is true" in value
    r_false = "reason r is false" in value or "r is false" in value
    if a_true and r_false:
        return "a_true_r_false"
    if a_false and r_true:
        return "a_false_r_true"
    if ("both" in value or (a_true and r_true)) and "correct explanation" in value:
        if "not the correct explanation" in value or "not correct explanation" in value:
            return "both_true_not_explains"
        return "both_true_explains"
    return None


def _validate_subject_answer_audit(
    question: dict,
    question_format: str,
    correct_text: str,
    index: int,
) -> None:
    prefix = f"subject question {index + 1}"
    audit = question.get("answer_audit")
    if not isinstance(audit, dict):
        raise ValueError(f"{prefix} is missing answer_audit")
    false_component_id = str(question["precision_trap"].get("false_component_id", "")).upper()

    if question_format == "assertion_reason":
        expected_option_kinds = {
            "A": "both_true_explains",
            "B": "both_true_not_explains",
            "C": "a_true_r_false",
            "D": "a_false_r_true",
        }
        for option in question["options"]:
            if _assertion_option_kind(str(option.get("text", ""))) != expected_option_kinds[option["key"]]:
                raise ValueError(
                    f"{prefix} shuffled or malformed standard Assertion-Reason option {option['key']}"
                )
        assertion_true = audit.get("assertion_true")
        reason_true = audit.get("reason_true")
        explains = audit.get("reason_explains_assertion")
        if not all(isinstance(value, bool) for value in (assertion_true, reason_true, explains)):
            raise ValueError(f"{prefix} has an incomplete Assertion-Reason answer_audit")
        if assertion_true and reason_true:
            expected_kind = "both_true_explains" if explains else "both_true_not_explains"
        elif assertion_true and not reason_true:
            expected_kind = "a_true_r_false"
        elif not assertion_true and reason_true:
            expected_kind = "a_false_r_true"
        else:
            raise ValueError(f"{prefix} makes both Assertion and Reason false")
        if _assertion_option_kind(correct_text) != expected_kind:
            raise ValueError(f"{prefix} correct option contradicts its Assertion-Reason audit")
        if false_component_id not in {"A", "R"}:
            raise ValueError(f"{prefix} precision trap must identify false component A or R")
        if false_component_id == "A" and assertion_true:
            raise ValueError(f"{prefix} marks true Assertion as the false precision component")
        if false_component_id == "R" and reason_true:
            raise ValueError(f"{prefix} marks true Reason as the false precision component")
        return

    component_truth = audit.get("component_truth")
    component_ids = _numbered_component_ids(str(question.get("question_text", "")))
    if not isinstance(component_truth, dict) or set(component_truth) != component_ids:
        raise ValueError(f"{prefix} answer_audit must cover every numbered component exactly once")
    if not all(isinstance(value, bool) for value in component_truth.values()):
        raise ValueError(f"{prefix} answer_audit truth values must be booleans")
    true_ids = {key for key, value in component_truth.items() if value}
    false_ids = component_ids - true_ids
    if not false_ids:
        raise ValueError(f"{prefix} must contain at least one false component")
    if false_component_id not in false_ids:
        raise ValueError(f"{prefix} precision trap does not identify a false audited component")

    lowered = str(question.get("question_text", "")).casefold()
    if question_format == "matching":
        if not all(_is_canonical_count_option(str(option.get("text", ""))) for option in question["options"]):
            raise ValueError(f"{prefix} matching options must all be canonical pair counts")
        if _count_from_option(correct_text, len(component_ids)) != len(true_ids):
            raise ValueError(f"{prefix} matching answer count contradicts component_truth")
    elif question_format == "negative" and "how many" in lowered:
        if _count_from_option(correct_text, len(component_ids)) != len(false_ids):
            raise ValueError(f"{prefix} negative answer count contradicts component_truth")
    elif question_format == "negative":
        if set(re.findall(r"(?<!\d)[1-4](?!\d)", correct_text)) != false_ids:
            raise ValueError(f"{prefix} negative answer combination contradicts component_truth")
    elif set(re.findall(r"(?<!\d)[1-4](?!\d)", correct_text)) != true_ids:
        raise ValueError(f"{prefix} statement answer combination contradicts component_truth")


def _validate_subject_breakdown_slides(slides: list[dict]) -> None:
    if len(slides) != 4:
        raise ValueError(f"expected 4 subject breakdown slides, got {len(slides)}")
    if [slide.get("slide_order") for slide in slides] != [1, 2, 3, 4]:
        raise ValueError("subject breakdown slide_order values must be exactly 1 through 4")
    if [slide.get("slide_type") for slide in slides] != ["theory", "theory", "practice", "practice"]:
        raise ValueError("subject breakdown slides must be two theory followed by two practice")
    for index, slide in enumerate(slides):
        concept = slide.get("concept")
        if not isinstance(concept, str) or not concept.strip():
            raise ValueError(f"subject breakdown slide {index + 1} is missing its precise concept")
    first_content = slides[0].get("content")
    if not isinstance(first_content, str) or "precision hinge:" not in first_content.casefold():
        raise ValueError("first subject theory slide must identify the Precision hinge")
    for slide in slides[2:]:
        options = slide.get("practice_options")
        if not isinstance(options, list) or len(options) != 4:
            raise ValueError("each subject practice slide must contain exactly four options")
        if [option.get("key") for option in options if isinstance(option, dict)] != ["A", "B", "C", "D"]:
            raise ValueError("subject practice option keys must be exactly A, B, C, D")
        if slide.get("practice_correct_option") not in QUESTION_OPTION_KEYS:
            raise ValueError("subject practice_correct_option must be A, B, C, or D")


def _normalize_option_array(value) -> list[dict] | None:
    if isinstance(value, dict):
        normalized = []
        for key in ("A", "B", "C", "D"):
            item = value.get(key)
            if item is None:
                item = value.get(key.casefold())
            if isinstance(item, dict):
                text = item.get("text") or item.get("option") or item.get("value")
            else:
                text = item
            if not isinstance(text, str):
                return None
            normalized.append({"key": key, "text": text})
        return normalized
    if isinstance(value, list) and len(value) == 4:
        normalized = []
        for index, item in enumerate(value):
            key = chr(ord("A") + index)
            if isinstance(item, dict):
                text = item.get("text") or item.get("option") or item.get("value")
                supplied_key = str(item.get("key") or key).upper()
                if supplied_key in QUESTION_OPTION_KEYS:
                    key = supplied_key
            else:
                text = item
            if not isinstance(text, str):
                return None
            normalized.append({"key": key, "text": text})
        normalized.sort(key=lambda option: option["key"])
        return normalized
    return None


def _normalize_subject_breakdown_slides(slides: list[dict]) -> list[dict]:
    normalized: list[dict] = []
    for raw in slides:
        if not isinstance(raw, dict):
            normalized.append(raw)
            continue
        slide = dict(raw)
        slide["concept"] = slide.get("concept") or slide.get("subject") or slide.get("topic")
        if slide.get("slide_type") == "practice":
            slide["practice_question"] = (
                slide.get("practice_question") or slide.get("question_text") or slide.get("question")
            )
            slide["practice_options"] = _normalize_option_array(
                slide.get("practice_options") or slide.get("options")
            )
            slide["practice_correct_option"] = str(
                slide.get("practice_correct_option")
                or slide.get("correct_option")
                or slide.get("correct_answer")
                or ""
            ).upper()
            slide["practice_explanation"] = (
                slide.get("practice_explanation") or slide.get("explanation")
            )
        normalized.append(slide)
    return normalized


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


def _validate_question_payload(
    question: dict,
    question_format: str,
    index: int,
    require_cross_topic: bool = True,
) -> None:
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
    if require_cross_topic and (
        not isinstance(question.get("cross_topic_link"), str)
        or not question["cross_topic_link"].strip()
    ):
        raise ValueError(f"{prefix} is missing its cross_topic_link")
    if not isinstance(explanation, str) or not explanation.strip():
        raise ValueError(f"{prefix} is missing its audited explanation")
    if _normalized_detail(precision_trap["correct_detail"]) not in _normalized_detail(explanation):
        raise ValueError(f"{prefix} explanation does not state the corrected precision detail")

    lowered = question_text.casefold()
    numbered_items = sum(marker in question_text for marker in ("1.", "2.", "3.", "4."))
    numbered_line_items = len(re.findall(r"(?m)^\s*[1-4][.)]\s+", question_text))
    if question_format != "matching" and re.search(r"(?:^|\n)\s*[ABCD][).]\s", question_text):
        raise ValueError(f"{prefix} repeats answer choices inside question_text")
    if question_format == "assertion_reason":
        if "assertion (a)" not in lowered or "reason (r)" not in lowered:
            raise ValueError(f"{prefix} does not contain labelled Assertion and Reason text")
        if not re.search(r"(?mi)^\s*assertion \(a\)\s*:", question_text) or not re.search(
            r"(?mi)^\s*reason \(r\)\s*:", question_text
        ):
            raise ValueError(f"{prefix} must put Assertion and Reason on separate lines")
        if correct_option == "A":
            raise ValueError(f"{prefix} Assertion-Reason answer must contain a non-trivial trap")
    elif question_format == "negative":
        if "not" not in lowered or numbered_items < 3 or numbered_line_items < 3:
            raise ValueError(f"{prefix} is not a numbered negative-framing question")
        if "all statements are correct" in str(options[correct_option_index]["text"]).casefold():
            raise ValueError(f"{prefix} negative question has no false component")
    elif question_format in {"statement", "matching"} and (
        numbered_items < 3 or numbered_line_items < 3
    ):
        raise ValueError(f"{prefix} must put at least three numbered items on separate lines")
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
            r"\d|\b(?:article|section|schedule|list|entry|tier|amendment|case|"
            r"judge|judges|bench|president|governor|parliament|ministry|authority|"
            r"commission|council|court|board|agency|department|institute|"
            r"organisation|organization|bank|tribunal|secretariat|office)\b",
            value,
            flags=re.IGNORECASE,
        )
    )


def _normalized_detail(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(value).casefold()).strip()



def _get_client() -> AsyncOpenAI:
    global _client, _http_client
    if _client is None:
        _http_client = httpx.AsyncClient(timeout=settings.OPENAI_REQUEST_TIMEOUT_SECONDS)
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


async def _search_json(
    model: str,
    system: str,
    user: str,
    max_tokens: int = 7500,
    restrict_domains: bool = True,
) -> dict:
    """Generate grounded JSON with web search available to the model."""
    web_search_tool: dict = {
        "type": "web_search",
        "search_context_size": "high",
    }
    if restrict_domains:
        web_search_tool["filters"] = {"allowed_domains": list(PRIMARY_SOURCE_DOMAINS)}
    resp = await _get_client().responses.create(
        model=model,
        tools=[web_search_tool],
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
# 3. Static subject generation. This stays separate from current-affairs
#    generation because chapters are reusable and have stricter factual traps.
# ---------------------------------------------------------------------------
def _subject_chapter_scope(subject: str, chapter: str) -> str:
    if subject.casefold() == "polity" and chapter.casefold() == "constitutional framework":
        return (
            "Constituent Assembly, making and commencement of the Constitution, "
            "the Preamble, salient structural features, citizenship at commencement, "
            "and the Union and its territory. Do not enter Fundamental Rights, DPSP, "
            "Fundamental Duties, Parliament, the judiciary, federal relations, or other "
            "standalone Polity chapters except where a bare-text cross-reference is "
            "indispensable to the tested Constitutional Framework fact."
        )
    return (
        f"Only the standard static syllabus ordinarily taught inside the chapter "
        f"'{chapter}' under {subject}. Do not drift into adjacent chapters."
    )


async def generate_subject_questions(
    subject: str,
    chapter: str,
    count: int,
    existing_titles: list[str],
) -> list[dict]:
    target_count = max(0, count)
    if target_count == 0:
        return []

    batch_size = 15
    expected_batches = (target_count + batch_size - 1) // batch_size
    max_batches = expected_batches + 10
    questions: list[dict] = []
    seen = {
        _normalized_question_key(text)
        for text in existing_titles
        if isinstance(text, str) and text.strip()
    }
    seen_texts = [text for text in existing_titles if isinstance(text, str) and text.strip()]

    for batch_number in range(1, max_batches + 1):
        if len(questions) >= target_count:
            break
        requested = min(batch_size, target_count - len(questions))
        try:
            batch = await _generate_subject_batch(
                subject=subject,
                chapter=chapter,
                count=requested,
                existing_question_texts=[*existing_titles, *[q["question_text"] for q in questions]],
                batch_number=batch_number,
            )
        except RuntimeError as exc:
            logger.warning("Static batch %s failed without valid output: %s", batch_number, exc)
            # Preserve validated progress so the resumable script can store it,
            # then request the remaining shortfall in a fresh call.
            if questions:
                break
            continue
        for question in batch:
            key = _normalized_question_key(question.get("question_text", ""))
            question_text = str(question.get("question_text", ""))
            if not key or key in seen or _is_near_duplicate(question_text, seen_texts):
                logger.warning("Skipping duplicate static question in %s / %s", subject, chapter)
                continue
            seen.add(key)
            seen_texts.append(question_text)
            questions.append(question)
            if len(questions) >= target_count:
                break

    if len(questions) < target_count:
        logger.warning(
            "Generated %s/%s requested static questions for %s / %s",
            len(questions),
            target_count,
            subject,
            chapter,
        )
    return questions[:target_count]


async def _generate_subject_batch(
    subject: str,
    chapter: str,
    count: int,
    existing_question_texts: list[str],
    batch_number: int,
) -> list[dict]:
    format_plan = _question_format_plan(count)
    answer_key_plan = _subject_answer_key_plan(format_plan)
    system = (
        "Generate a JSON object {\"questions\": [...]} containing exactly the "
        "requested number of permanent static-syllabus UPSC Civil Services Prelims "
        "questions. Each candidate will be independently checked against official "
        "constitutional, legislative, judicial, or government sources after this "
        "call, so do not guess or invent any fact. Each question object must be: "
        "{format, precision_trap: "
        "{false_component, false_component_id, wrong_detail, correct_detail}, "
        "answer_audit, question_text, options: "
        "[{key:'A',text},{key:'B',text},{key:'C',text},{key:'D',text}], "
        "correct_option, explanation}. The explanation must end with `Correct option "
        "is X: <exact option text>` copied verbatim from the selected option. The "
        "answer field must be named exactly "
        "`correct_option`. Every question must contain at least one false component, "
        "and the explanation must audit every numbered statement/pair or both A/R "
        "components individually. For `statement`, use 3-4 numbered statements and "
        "combination answers. For `assertion_reason`, put Assertion (A) and Reason "
        "(R) on separate lines and use the fixed standard UPSC mapping exactly: "
        "A=both true and R explains A; B=both true but R does not explain A; "
        "C=A true/R false; D=A false/R true. Never shuffle these meanings. For `negative`, "
        "use 3-4 numbered statements/pairs and explicitly ask which/how many is NOT "
        "correct. For `matching`, show 3-4 numbered pairs, ask how many are correctly "
        "matched, and use count options. Do not place answer choices in question_text. "
        "Do not make Assertion-Reason option A correct; do not make all/none correct "
        "for matching; and do not make all statements true or all false.\n\n"
        f"{STATIC_SUBJECT_QUALITY_INSTRUCTIONS}"
    )
    # Keep enough semantic context to prevent repeated facts without allowing an
    # ever-growing seed to consume the request context.
    recent_existing = [
        str(text)[:500]
        for text in existing_question_texts[-60:]
        if isinstance(text, str) and text.strip()
    ]
    user = json.dumps(
        {
            "subject": subject,
            "chapter": chapter,
            "chapter_scope": _subject_chapter_scope(subject, chapter),
            "count": count,
            "batch_number": batch_number,
            "question_format_plan": format_plan,
            "correct_option_plan": answer_key_plan,
            "avoid_testing_the_same_specific_fact_as": recent_existing,
        }
    )
    retry_delays = [1, 3, 8]
    last_error: Exception | None = None
    for attempt in range(len(retry_delays) + 1):
        try:
            data = await _chat_json(settings.MODEL_MAIN, system, user, max_tokens=16000)
            if not isinstance(data, dict):
                raise ValueError("static generation JSON root was not an object")
            raw_questions = data.get("questions", [])
            if not isinstance(raw_questions, list):
                raise ValueError("static questions payload was not a list")

            valid: list[dict] = []
            statement_signatures: set[str] = set()
            for index, question in enumerate(raw_questions[:count]):
                try:
                    _validate_subject_question(
                        question,
                        format_plan[index],
                        answer_key_plan[index],
                        index,
                        require_sources=False,
                    )
                    _validate_subject_chapter_scope(question, subject, chapter, index)
                    verification_sources = await _verify_subject_question(
                        question,
                        subject,
                        chapter,
                    )
                    question["verification_sources"] = verification_sources
                    _validate_subject_question(
                        question,
                        format_plan[index],
                        answer_key_plan[index],
                        index,
                        require_sources=True,
                    )
                    if format_plan[index] == "statement":
                        correct_index = ord(answer_key_plan[index]) - ord("A")
                        signature = _statement_answer_signature(
                            str(question["options"][correct_index]["text"])
                        )
                        if signature and signature in statement_signatures:
                            raise ValueError(
                                f"subject question {index + 1} repeats statement answer {signature}"
                            )
                        if signature:
                            statement_signatures.add(signature)
                except (OpenAIError, TypeError, ValueError, IndexError) as exc:
                    logger.warning(
                        "Skipping invalid static question %s in %s / %s batch %s: %s",
                        index + 1,
                        subject,
                        chapter,
                        batch_number,
                        exc,
                    )
                    continue
                valid.append(question)
            if not valid:
                raise ValueError("static batch contained no individually valid questions")
            return valid
        except (json.JSONDecodeError, OpenAIError, TypeError, ValueError) as exc:
            last_error = exc
            if attempt < len(retry_delays):
                await asyncio.sleep(retry_delays[attempt])
    raise RuntimeError(
        f"static question generation failed for {subject} / {chapter} after retries: {last_error}"
    )


async def _verify_subject_question(
    question: dict,
    subject: str,
    chapter: str,
) -> list[str]:
    system = (
        "Act as a strict factual verifier for one static UPSC Prelims question. "
        "Use web search and authoritative primary sources: the bare Constitution, "
        "official legislative or government material, and official court judgments. "
        "Check every statement/pair or both Assertion and Reason independently, the "
        "component_truth/answer_audit, the precision trap's wrong and corrected detail, "
        "the selected option, and every claim in the explanation. Also confirm the "
        "question stays inside the supplied chapter scope. Return JSON only: "
        "{\"approved\": true|false, \"verification_sources\": [exact official URLs], "
        "\"issues\": [short strings]}. Set approved=true only if all facts, truth "
        "values, option logic, explanation, and scope are correct. Do not repair or "
        "reinterpret a flawed candidate; reject it. Include at least one official "
        "primary URL when approved."
    )
    payload = json.dumps(
        {
            "subject": subject,
            "chapter": chapter,
            "chapter_scope": _subject_chapter_scope(subject, chapter),
            "candidate": question,
        }
    )
    data = await _search_json(
        settings.MODEL_SEARCH,
        system,
        payload,
        max_tokens=1800,
        restrict_domains=True,
    )
    if not isinstance(data, dict) or data.get("approved") is not True:
        issues = data.get("issues") if isinstance(data, dict) else None
        raise ValueError(f"official-source verifier rejected question: {issues or 'unspecified issue'}")
    sources = data.get("verification_sources")
    if not isinstance(sources, list) or not any(
        isinstance(url, str) and _is_primary_source(url) for url in sources
    ):
        raise ValueError("official-source verifier returned no primary source URL")
    return [url for url in sources if isinstance(url, str) and url.startswith(("https://", "http://"))]


async def generate_subject_breakdown(
    question_text: str,
    correct_option: str,
    explanation: str,
    subject: str,
    chapter: str,
) -> list[dict]:
    system = (
        "A student missed a very hard static-syllabus question. Return a JSON "
        "object {\"slides\": [...]} with EXACTLY 4 slides. Slides 1-2 must be "
        "theory and each must cover ONE precise concept the answer hinged on, not "
        "the chapter generally. Each has slide_order, slide_type='theory', concept, "
        "and content. Slide 1 content must begin exactly `Precision hinge:` and "
        "contrast the planted wrong number/Article/amendment/year/Schedule/List/case/"
        "bench size with the exact correct fact. Slides 3-4 must be practice, with "
        "slide_order, slide_type='practice', concept matching theory slides 1 and 2 "
        "respectively, practice_question, four practice_options with A-D key/text, "
        "practice_correct_option, and practice_explanation. Each practice distractor "
        "must differ from an accurate statement by one exact factual detail. The "
        "student may have been tripped by a precision detail rather than a conceptual "
        "gap: at least one theory slide must address the EXACT factual distinction "
        "the question hinged on, not just the general topic area. Stay inside the "
        "specified subject and chapter."
    )
    user = json.dumps(
        {
            "question_text": question_text,
            "correct_option": correct_option,
            "explanation": explanation,
            "subject": subject,
            "chapter": chapter,
            "chapter_scope": _subject_chapter_scope(subject, chapter),
        }
    )
    retry_delays = [1, 3, 8]
    last_error: Exception | None = None
    for attempt in range(len(retry_delays) + 1):
        try:
            data = await _chat_json(settings.MODEL_CHEAP, system, user, max_tokens=4500)
            if not isinstance(data, dict):
                raise ValueError("subject breakdown JSON root was not an object")
            raw_slides = data.get("slides", [])
            if not isinstance(raw_slides, list):
                raise ValueError("subject breakdown slides payload was not a list")
            slides = _normalize_subject_breakdown_slides(raw_slides)
            _validate_subject_breakdown_slides(slides)
            return slides
        except (json.JSONDecodeError, OpenAIError, TypeError, ValueError) as exc:
            last_error = exc
            if attempt < len(retry_delays):
                await asyncio.sleep(retry_delays[attempt])
    raise RuntimeError(f"subject breakdown generation failed after retries: {last_error}")


# ---------------------------------------------------------------------------
# 4. Nightly personalised report. Uses MODEL_CHEAP.
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
# 5. Daily "what happened today" research step (from 10 Jul 2026 onward).
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
