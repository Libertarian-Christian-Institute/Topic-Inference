"""Write the weekly narrative summary from categorized questions using Azure OpenAI.

The model returns JSON; HTML rendering happens in chatbot_summary.py so model output
is always escaped before it goes into the email.
"""
import json
import os
import re

DEPLOYMENT = os.getenv("OPENAI_SUMMARY_DEPLOYMENT", "gpt-4.1-mini")

SYSTEM_PROMPT = """\
You analyze questions that website visitors asked the Libertarian Christian Institute (LCI) \
chatbot during one reporting period (a month or a week). Each question was already assigned to a \
category. You get each category's count, share, and sample questions, plus the previous period's \
counts per category when available. \
Samples from "other" are questions that fit no existing category.

Write for LCI staff who produce articles, podcasts, and resources. Be concrete and brief. \
Only describe what the data shows; do not invent questions or numbers. Never repeat personal \
details (names, emails, phone numbers, addresses) even if a question contains them.

Return a JSON object with exactly these keys:
{
  "headline": "one sentence capturing the period",
  "themes": [{"title": str, "summary": "1-2 sentences", "example_questions": [str, ...up to 3]}],
  "trends": ["notable change vs. the previous period", ...],
  "notable_questions": ["question worth a closer look, verbatim or lightly trimmed", ...],
  "content_ideas": ["article/podcast idea grounded in what people asked", ...],
  "new_category_suggestions": [{"name": str, "reason": "what recurring 'other' questions it would cover"}]
}
Give 3-5 themes, up to 5 trends (empty list if no previous period provided; ignore tiny changes), \
up to 5 notable questions, 3-5 content ideas, and 0-3 new category suggestions (only if several \
"other" questions share a clear theme)."""

_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
_PHONE_RE = re.compile(r"(?:\+?1[\s.-]?)?\(?\d{3}\)?[\s.-]?\d{3}[\s.-]?\d{4}\b")
_LONG_NUM_RE = re.compile(r"\b\d{7,}\b")


def redact(text: str) -> str:
    """Strip obvious PII before text leaves our storage (LLM prompt, email)."""
    text = _EMAIL_RE.sub("[email]", text)
    text = _PHONE_RE.sub("[phone]", text)
    return _LONG_NUM_RE.sub("[number]", text)


def summarize_period(label: str, n_questions: int, categories: list[dict], prev_counts: dict | None) -> dict:
    """label: e.g. "October 2026"; categories: [{id, name, count, share, samples: [str]}];
    prev_counts: {id: count} for the previous period, or None."""
    from llm_client import chat_json

    payload = {
        "period": label,
        "total_questions": n_questions,
        "categories": categories,
        "previous_week_counts": prev_counts,
    }
    return chat_json(DEPLOYMENT, SYSTEM_PROMPT, json.dumps(payload, ensure_ascii=False), max_tokens=2500, temperature=0.3)
