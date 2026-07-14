import pytest

from app.openai_client import (
    _question_format_plan,
    _subject_answer_key_plan,
    _normalize_subject_breakdown_slides,
    _is_near_duplicate,
    _validate_breakdown_slides,
    _validate_question_payload,
    _validate_subject_question,
    _validate_topic_sources,
    validate_generated_topic,
)


def _valid_question() -> dict:
    return {
        "question_text": (
            "Consider the following statements:\n"
            "1. Article 280 establishes the Finance Commission.\n"
            "2. Article 324 concerns the Election Commission.\n"
            "3. The GST Council is constituted under Article 279."
        ),
        "options": [
            {"key": "A", "text": "1 and 2 only"},
            {"key": "B", "text": "2 and 3 only"},
            {"key": "C", "text": "1 and 3 only"},
            {"key": "D", "text": "1, 2 and 3"},
        ],
        "correct_option": "A",
        "precision_trap": {
            "false_component": "Statement 3",
            "wrong_detail": "Article 279",
            "correct_detail": "Article 279A",
        },
        "cross_topic_link": "Links fiscal policy to a constitutional provision.",
        "explanation": "Statements 1 and 2 are correct. Statement 3 is false: the GST Council is constituted under Article 279A, not Article 279.",
    }


def test_format_plan_guarantees_all_archetypes_in_four_question_batch():
    plan = _question_format_plan(4)

    assert len(plan) == 4
    assert set(plan) == {"statement", "assertion_reason", "negative", "matching"}


def test_subject_answer_plan_varies_and_avoids_assertion_reason_a():
    formats = ["statement", "assertion_reason", "negative", "matching"] * 3
    plan = _subject_answer_key_plan(formats)

    assert all(left != right for left, right in zip(plan, plan[1:]))
    assert all(plan[index] in {"C", "D"} for index, value in enumerate(formats) if value == "assertion_reason")


def test_subject_near_duplicate_rejects_same_fact_with_changed_wrong_date():
    first = (
        "Assertion (A): The Constitution came into effect on 26 January 1950.\n"
        "Reason (R): It was adopted on 26 January 1950."
    )
    second = (
        "Assertion (A): The Constitution came into effect on 26 January 1950.\n"
        "Reason (R): It was adopted on 15 August 1947."
    )

    assert _is_near_duplicate(second, [first])


def test_question_payload_accepts_audited_precision_details():
    _validate_question_payload(_valid_question(), "statement", 0)


def test_subject_question_requires_exact_static_detail_and_one_detail_trap():
    question = _valid_question()
    question["format"] = "statement"
    question.pop("cross_topic_link")
    question["precision_trap"]["false_component"] = (
        "The GST Council is constituted under Article 279."
    )
    question["precision_trap"]["false_component_id"] = "3"
    question["answer_audit"] = {"component_truth": {"1": True, "2": True, "3": False}}
    question["verification_sources"] = ["https://legislative.gov.in/constitution-of-india/"]
    question["explanation"] += " Correct option is A: 1 and 2 only."

    _validate_subject_question(question, "statement", "A", 0)

    question["question_text"] = (
        "Consider the following statements:\n"
        "1. The Finance Commission is a constitutional body.\n"
        "2. The Election Commission is a constitutional body.\n"
        "3. The Finance Commission conducts elections."
    )
    question["precision_trap"] = {
        "false_component": "The Finance Commission conducts elections.",
        "wrong_detail": "Finance Commission",
        "correct_detail": "Election Commission",
    }
    question["explanation"] = "Statement 3 is false: Election Commission is the corrected authority."
    with pytest.raises(ValueError, match="exact static-syllabus detail"):
        _validate_subject_question(question, "statement", "A", 0)


def test_subject_statement_rejects_all_three_correct_pattern():
    question = _valid_question()
    question["format"] = "statement"
    question["correct_option"] = "D"
    question["precision_trap"]["false_component"] = (
        "The GST Council is constituted under Article 279."
    )
    question["precision_trap"]["false_component_id"] = "3"
    question["answer_audit"] = {"component_truth": {"1": True, "2": True, "3": False}}
    question["verification_sources"] = ["https://legislative.gov.in/constitution-of-india/"]
    question["explanation"] += " Correct option is D: 1, 2 and 3."
    with pytest.raises(ValueError, match="forbidden 1, 2 and 3"):
        _validate_subject_question(question, "statement", "D", 0)


def test_storage_guard_rejects_legacy_question_shape():
    topic = {
        "source_urls": ["https://example.gov.in/source"],
        "question": _valid_question(),
    }
    topic["question"]["format"] = "statement"
    validate_generated_topic(topic)

    del topic["question"]["precision_trap"]
    with pytest.raises(ValueError, match="precision_trap"):
        validate_generated_topic(topic)


def test_explanation_must_name_the_exact_corrected_detail():
    question = _valid_question()
    question["explanation"] = "Statement 3 is incorrect for a constitutional reason."

    with pytest.raises(ValueError, match="corrected precision detail"):
        _validate_question_payload(question, "statement", 0)


def test_numbered_statements_must_be_on_separate_lines():
    question = _valid_question()
    question["question_text"] = question["question_text"].replace("\n", " ")

    with pytest.raises(ValueError, match="separate lines"):
        _validate_question_payload(question, "statement", 0)


def test_topic_sources_require_at_least_one_url():
    _validate_topic_sources([{"source_urls": ["https://example.gov.in/source"]}])

    with pytest.raises(ValueError, match="no research source URLs"):
        _validate_topic_sources([{}])


def test_breakdown_requires_precision_hinge_and_valid_practice_options():
    slides = [
        {
            "slide_order": order,
            "slide_type": "theory",
            "content": "Precision hinge: Article 280, not Article 279A." if order == 1 else "Theory",
        }
        for order in range(1, 4)
    ]
    slides.extend(
        {
            "slide_order": order,
            "slide_type": "practice",
            "practice_options": [
                {"key": "A", "text": "A"},
                {"key": "B", "text": "B"},
                {"key": "C", "text": "C"},
                {"key": "D", "text": "D"},
            ],
            "practice_correct_option": "A",
        }
        for order in range(4, 7)
    )

    _validate_breakdown_slides(slides)

    slides[0]["content"] = "General overview only"
    with pytest.raises(ValueError, match="Precision hinge"):
        _validate_breakdown_slides(slides)


def test_subject_breakdown_normalizes_equivalent_practice_json_shape():
    slides = [
        {"slide_order": 1, "slide_type": "theory", "concept": "Article 1", "content": "Precision hinge: Article 1, not Article 2."},
        {"slide_order": 2, "slide_type": "theory", "concept": "Article 2", "content": "Exact distinction."},
    ]
    slides.extend({
            "slide_order": order,
            "slide_type": "practice",
            "subject": f"Concept {order}",
            "question": "Which statement is correct?",
            "options": {"A": "One", "B": "Two", "C": "Three", "D": "Four"},
            "correct_option": "B",
            "explanation": "B is exact.",
        }
        for order in (3, 4)
    )

    normalized = _normalize_subject_breakdown_slides(slides)
    assert normalized[2]["practice_options"] == [
        {"key": "A", "text": "One"},
        {"key": "B", "text": "Two"},
        {"key": "C", "text": "Three"},
        {"key": "D", "text": "Four"},
    ]
