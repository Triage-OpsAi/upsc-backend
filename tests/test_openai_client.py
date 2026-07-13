import pytest

from app.openai_client import (
    _question_format_plan,
    _validate_breakdown_slides,
    _validate_question_payload,
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


def test_question_payload_accepts_audited_precision_details():
    _validate_question_payload(_valid_question(), "statement", 0)


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
