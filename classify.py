"""Assign each question to one category from categories.yaml using Azure OpenAI.

Questions are deduplicated, redacted, and sent in numbered batches; the model returns
{"results": [{"n": 1, "category": "<id>"}, ...]}. Unknown ids map to "other"; a batch
that fails after retries is marked "unclassified" so one bad call can't sink the run.
"""
import json
import logging
import os
from pathlib import Path

import yaml

from llm_client import chat_json
from llm_summary import redact

DEPLOYMENT = os.getenv("OPENAI_CLASSIFY_DEPLOYMENT", "gpt-4.1-mini")
BATCH_SIZE = int(os.getenv("CLASSIFY_BATCH_SIZE", "50"))
CATEGORIES_FILE = Path(os.getenv("CATEGORIES_FILE", Path(__file__).with_name("categories.yaml")))

OTHER = {"id": "other", "name": "Other / doesn't fit a category", "description": "Fits none of the categories above."}
UNCLASSIFIED = {"id": "unclassified", "name": "Unclassified (classifier error)", "description": ""}

log = logging.getLogger(__name__)


def load_categories(path: Path = CATEGORIES_FILE) -> list[dict]:
    cats = yaml.safe_load(path.read_text(encoding="utf-8"))["categories"]
    ids = [c["id"] for c in cats]
    if len(ids) != len(set(ids)):
        raise ValueError(f"Duplicate category ids in {path}")
    if "other" in ids or "unclassified" in ids:
        raise ValueError("'other' and 'unclassified' are built in; remove them from categories.yaml")
    return cats + [OTHER]


def _system_prompt(categories: list[dict]) -> str:
    listing = "\n".join(f"- {c['id']}: {c['name']} — {c['description']}" for c in categories)
    return f"""\
You categorize questions that visitors asked the Libertarian Christian Institute (LCI) chatbot.
Assign each question exactly ONE category id from this list — the single best fit. Use "other"
only if no category reasonably fits.

Categories:
{listing}

You receive a JSON list of {{"n": number, "q": question}}. Return a JSON object:
{{"results": [{{"n": number, "category": "<category id>"}}, ...]}} with one entry per question."""


def _classify_batch(system: str, batch: list[tuple[int, str]], valid: set[str]) -> dict[int, str]:
    user = json.dumps([{"n": n, "q": q} for n, q in batch], ensure_ascii=False)
    out = chat_json(DEPLOYMENT, system, user, max_tokens=40 * len(batch) + 200, temperature=0)
    got = {}
    for r in out.get("results", []):
        try:
            n, cat = int(r["n"]), str(r["category"]).strip()
        except (KeyError, TypeError, ValueError):
            continue
        got[n] = cat if cat in valid else "other"
    return got


def classify_questions(questions: list[str], categories: list[dict]) -> list[str]:
    """Return a category id for each question, in input order."""
    valid = {c["id"] for c in categories}
    system = _system_prompt(categories)

    # Dedupe on normalized text so repeated questions cost one classification.
    key = lambda q: " ".join(q.lower().split())
    uniques = list(dict.fromkeys(key(q) for q in questions))
    items = list(enumerate((redact(u)[:500] for u in uniques), start=1))

    labels: dict[int, str] = {}
    for i in range(0, len(items), BATCH_SIZE):
        batch = items[i:i + BATCH_SIZE]
        try:
            got = _classify_batch(system, batch, valid)
            missing = [(n, q) for n, q in batch if n not in got]
            if missing:  # model skipped some; one retry for just those
                got.update(_classify_batch(system, missing, valid))
        except Exception:
            log.exception("Classification batch %d failed", i // BATCH_SIZE + 1)
            got = {}
        for n, _ in batch:
            labels[n] = got.get(n, "unclassified")
        log.info("Classified %d/%d unique questions", min(i + BATCH_SIZE, len(items)), len(items))

    by_key = {u: labels[n] for n, u in enumerate(uniques, start=1)}
    return [by_key[key(q)] for q in questions]
