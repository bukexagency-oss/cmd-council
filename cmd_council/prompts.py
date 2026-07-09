"""Prompt templates for the three council stages.

All prompts instruct the model to answer in the language of the user's
question (PRD decision, 2026-07-08): Indonesian question -> Indonesian
answer, English question -> English answer.
"""
from __future__ import annotations

LANGUAGE_RULE = (
    "Always respond in the same language as the user's question "
    "(e.g. an Indonesian question gets an Indonesian answer)."
)

STAGE1_SYSTEM = (
    "You are one advisor on a council of AI models. Several advisors answer "
    "the same question independently; your answer will later be peer-reviewed "
    "anonymously.\n"
    "Answer the question directly and completely, but stay concise: no "
    "filler, no restating the question, no meta commentary about being an AI "
    "or about the council.\n" + LANGUAGE_RULE
)

STAGE3_SYSTEM = (
    "You are the Chairman of an LLM Council. Several advisor models answered "
    "the user's question, then anonymously peer-reviewed and ranked each "
    "other's answers. Your job is to synthesize the single best final answer "
    "representing the council's collective wisdom.\n"
    "Structure your output with these four sections, translating the "
    "headings into the language of the question:\n"
    "1. 'Final Answer' — a complete, self-contained answer to the question. "
    "This is the main deliverable; it must stand on its own.\n"
    "2. 'Consensus' — points most advisors agree on (brief bullets).\n"
    "3. 'Contradictions' — where advisors disagree, and which position is "
    "stronger and why (brief).\n"
    "4. 'Blind spots' — anything relevant that all advisors missed (brief; "
    "write 'none' if nothing significant).\n"
    "Never mention model names or response labels inside the Final Answer "
    "section.\n" + LANGUAGE_RULE
)


def stage2_user(question: str, labeled_answers: list[tuple[str, str]]) -> str:
    blocks = "\n\n".join(f"### {label}\n{text}" for label, text in labeled_answers)
    labels = ", ".join(label for label, _ in labeled_answers)
    return (
        "Below is a question and several anonymized responses from different "
        "AI models (one of them may be yours).\n\n"
        f"## Question\n{question}\n\n"
        f"## Responses\n\n{blocks}\n\n"
        "## Your task\n"
        "1. Evaluate each response for accuracy and depth of insight. Be "
        "specific and critical; note factual errors, gaps, and especially "
        "strong points.\n"
        "2. End your message with a final ranking of ALL responses from best "
        "to worst, in EXACTLY this format (one per line, no ties, using only "
        f"these labels: {labels}):\n\n"
        "FINAL RANKING:\n"
        "1. Response X\n"
        "2. Response Y\n"
        "...\n\n"
        "The 'FINAL RANKING:' block is mandatory and must be the last thing "
        "in your message.\n" + LANGUAGE_RULE
    )


def stage3_user(
    question: str,
    labeled_answers: list[tuple[str, str]],
    aggregate_lines: list[str],
    review_digest: list[tuple[str, str]] | None = None,
) -> str:
    blocks = "\n\n".join(f"### {label}\n{text}" for label, text in labeled_answers)
    ranking = (
        "\n".join(aggregate_lines)
        if aggregate_lines
        else "(peer review was skipped in this session)"
    )
    reviews = ""
    if review_digest:
        reviews = "\n\n## Peer review excerpts (anonymized)\n" + "\n\n".join(
            f"[{who}]\n{text}" for who, text in review_digest
        )
    return (
        f"## Question\n{question}\n\n"
        f"## Advisor responses (anonymized)\n\n{blocks}\n\n"
        f"## Aggregate peer ranking (best first)\n{ranking}"
        f"{reviews}\n\n"
        "Now write the final synthesis."
    )


def ranking_extraction_user(review_text: str, labels: list[str]) -> str:
    return (
        "Extract the ranking of responses from the review below. Reply with "
        "ONLY a JSON array of labels, best first, e.g. "
        '["Response B", "Response A"]. Use only these labels: '
        f"{labels}.\n\n---\n{review_text}"
    )
