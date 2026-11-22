"""Chatbot question summary (monthly by default, or weekly).

Runs as a scheduled Azure Container Apps Job:

  1. Load logged questions from blob storage (chat-logs/logs) for the last full period
     (calendar month, or Mon-Mon week), in Central time.
  2. Categorize each question against categories.yaml with Azure OpenAI.
  3. Ask Azure OpenAI for a narrative summary (themes, trends, content ideas).
  4. Save the period's JSON (weekly-summaries/json/<period>.json) and the text log.
  5. Email the HTML report via Azure Communication Services Email.

Usage:
  python chatbot_summary.py                    # last full month, upload + email
  python chatbot_summary.py --dry-run          # write report.html/report.json locally, no upload/email
  python chatbot_summary.py --dry-run --ago 1  # the month before last
  python chatbot_summary.py --period week      # weekly instead (or set REPORT_PERIOD=week)
"""
import argparse
import html
import io
import json
import logging
import os
import sys
from datetime import UTC, datetime, timedelta

import pandas as pd
import pytz
from azure.core.exceptions import ResourceExistsError, ResourceNotFoundError
from azure.storage.blob import BlobServiceClient
from dotenv import load_dotenv

load_dotenv()  # no-op in the container; env vars come from the job config

from classify import DEPLOYMENT as CLASSIFY_DEPLOYMENT, classify_questions, load_categories  # noqa: E402
from llm_summary import DEPLOYMENT as SUMMARY_DEPLOYMENT, redact, summarize_period  # noqa: E402

log = logging.getLogger("chatbot_summary")

PERIOD = os.getenv("REPORT_PERIOD", "month")  # "month" or "week"

AZURE_CONN_STR = os.environ.get("AZURE_CSV_CONTAINER", "")
DATA_CONTAINER = os.getenv("DATA_CONTAINER", "chat-logs")
DATA_BLOB_NAME = os.getenv("DATA_BLOB_NAME", "logs")
SUMMARY_CONTAINER = os.getenv("SUMMARY_CONTAINER", "weekly-summaries")
SUMMARY_TEXT_BLOB = os.getenv("SUMMARY_TEXT_BLOB", "topic_discovery.txt")
TZ_NAME = os.getenv("REPORT_TZ", "America/Chicago")
TOP_K = int(os.getenv("TOP_K", "15"))
SAMPLES_PER_CATEGORY = int(os.getenv("SAMPLES_PER_CATEGORY", "10"))
SAMPLES_OTHER = int(os.getenv("SAMPLES_OTHER", "40"))


# ---------------------------------------------------------------- time window
def _shift_month(d, months):
    m = d.year * 12 + (d.month - 1) + months
    return d.replace(year=m // 12, month=m % 12 + 1)


def compute_window(period=PERIOD, ago=0, tz_name=TZ_NAME, now_utc=None):
    """[start, end) in local time for the last full period, `ago` periods further back.
    month: 1st 00:00 -> next 1st 00:00.  week: Monday 00:00 -> next Monday 00:00."""
    tz = pytz.timezone(tz_name)
    now_local = (now_utc or datetime.now(UTC)).astimezone(tz)
    midnight = now_local.replace(hour=0, minute=0, second=0, microsecond=0, tzinfo=None)
    if period == "month":
        this_first = midnight.replace(day=1)
        start, end = _shift_month(this_first, -(ago + 1)), _shift_month(this_first, -ago)
    elif period == "week":
        this_monday = midnight - timedelta(days=now_local.isoweekday() - 1)
        start, end = this_monday - timedelta(weeks=ago + 1), this_monday - timedelta(weeks=ago)
    else:
        raise ValueError(f"Unknown period {period!r}; use 'month' or 'week'")
    return tz.localize(start), tz.localize(end)


def period_id(period, start):
    if period == "month":
        return f"{start.year}-{start.month:02d}"
    iso = start.isocalendar()
    return f"{iso.year}-W{iso.week:02d}"


def period_label(period, start):
    return start.strftime("%B %Y") if period == "month" else f"week {period_id(period, start)}"


# ---------------------------------------------------------------- blob helpers
def blob_service():
    return BlobServiceClient.from_connection_string(AZURE_CONN_STR)


def read_blob(container, name):
    try:
        return blob_service().get_blob_client(container, name).download_blob().readall()
    except ResourceNotFoundError:
        return None


def ensure_container(svc, container):
    try:
        svc.create_container(container)
    except ResourceExistsError:
        pass


def upload_json(container, name, obj):
    svc = blob_service()
    ensure_container(svc, container)
    data = json.dumps(obj, ensure_ascii=False, indent=2).encode("utf-8")
    svc.get_blob_client(container, name).upload_blob(data, overwrite=True)
    log.info("Uploaded %s/%s", container, name)


def append_text(container, name, text):
    svc = blob_service()
    ensure_container(svc, container)
    client = svc.get_blob_client(container, name)
    if not client.exists():
        client.create_append_blob()
    client.append_block((text.rstrip() + "\n\n" + "-" * 60 + "\n\n").encode("utf-8"))
    log.info("Appended to %s/%s", container, name)


# ---------------------------------------------------------------- data
def load_questions(start, end):
    raw = read_blob(DATA_CONTAINER, DATA_BLOB_NAME)
    if not raw:
        raise RuntimeError(f"No question log found at {DATA_CONTAINER}/{DATA_BLOB_NAME}")
    df = pd.read_json(io.BytesIO(raw), lines=True)
    df["time"] = pd.to_datetime(df["time"], errors="coerce", utc=True)
    df = df.dropna(subset=["time", "question"])
    df["question"] = (
        df["question"].astype(str)
        .str.replace(r"https?://\S+", " ", regex=True)
        .str.replace(r"\s+", " ", regex=True)
        .str.strip()
    )
    local = df["time"].dt.tz_convert(TZ_NAME)
    window = df.loc[(local >= start) & (local < end) & (df["question"] != "")].copy()
    log.info("Loaded %d questions total, %d in window", len(df), len(window))
    return window.reset_index(drop=True)


def build_category_table(qdf, categories):
    names = {c["id"]: c["name"] for c in categories}
    names["unclassified"] = "Unclassified (classifier error)"
    n = len(qdf)
    table = []
    for cid, cnt in qdf["category"].value_counts().items():
        k = SAMPLES_OTHER if cid == "other" else SAMPLES_PER_CATEGORY
        samples = (
            qdf.loc[qdf["category"] == cid, "question"]
            .drop_duplicates()
            .sample(frac=1, random_state=42)
            .head(k)
            .map(lambda q: redact(q)[:300])
            .tolist()
        )
        table.append({"id": cid, "name": names.get(cid, cid), "count": int(cnt), "share": round(cnt / n, 3), "samples": samples})
    return table


def load_previous_counts(prev_id):
    raw = read_blob(SUMMARY_CONTAINER, f"json/{prev_id}.json")
    if not raw:
        return None
    prev = json.loads(raw)
    if "categories" not in prev:  # older BERTopic-format record
        return None
    return {"n_questions": prev["n_questions"], "counts": {c["id"]: c["count"] for c in prev["categories"]}}


# ---------------------------------------------------------------- rendering
def _e(x):
    return html.escape(str(x))


def _ul(items, unit):
    if not items:
        return f"<p style='color:#666;'>None this {unit}.</p>"
    return "<ul style='margin:4px 0 0 20px;padding:0;'>" + "".join(f"<li style='margin:4px 0;'>{_e(i)}</li>" for i in items) + "</ul>"


def report_title(period, start):
    return f"{'Monthly' if period == 'month' else 'Weekly'} Chatbot Summary — {period_label(period, start)}"


def build_html(period, start, end, n_questions, table, llm, prev, flagged):
    unit = period
    h3 = "style='margin:20px 0 6px;font-size:16px;'"
    parts = [
        f"<h2 style='margin:0 0 6px;'>{_e(report_title(period, start))}</h2>",
        f"<p style='margin:0 0 12px;color:#444;'>{start:%b %d} – {(end - timedelta(days=1)):%b %d, %Y}"
        f" · <strong>{n_questions}</strong> questions"
        + (f" (last {unit}: {prev['n_questions']})" if prev else "")
        + (f" · {flagged} flagged" if flagged is not None else "")
        + "</p>",
    ]

    if n_questions == 0:
        parts.append(f"<p>No questions were logged this {unit}.</p>")
    else:
        if llm:
            parts.append(f"<p style='font-size:15px;margin:0 0 8px;'><strong>{_e(llm.get('headline', ''))}</strong></p>")
            parts.append(f"<h3 {h3}>Themes</h3>")
            for t in llm.get("themes", []):
                ex = "".join(f"<li style='color:#555;'>“{_e(q)}”</li>" for q in t.get("example_questions", [])[:3])
                parts.append(
                    f"<p style='margin:10px 0 2px;'><strong>{_e(t.get('title', ''))}</strong> — {_e(t.get('summary', ''))}</p>"
                    f"<ul style='margin:0 0 0 20px;padding:0;font-size:13px;'>{ex}</ul>"
                )
            if llm.get("trends"):
                parts.append(f"<h3 {h3}>Changes vs. last {unit}</h3>" + _ul(llm["trends"], unit))
            parts.append(f"<h3 {h3}>Content ideas</h3>" + _ul(llm.get("content_ideas", []), unit))
            parts.append(f"<h3 {h3}>Questions worth a look</h3>" + _ul(llm.get("notable_questions", []), unit))
        else:
            parts.append(f"<p style='color:#a00;'>The AI summary failed this {unit}; category counts are below.</p>")

        prev_counts = (prev or {}).get("counts", {})
        cell = "style='padding:4px 8px;text-align:right;'"
        rows = []
        for t in table[:TOP_K]:
            delta = f"<td {cell}>{t['count'] - prev_counts.get(t['id'], 0):+d}</td>" if prev else ""
            rows.append(
                f"<tr><td style='padding:4px 8px;'>{_e(t['name'])}</td>"
                f"<td {cell}>{t['count']}</td><td {cell}>{t['share'] * 100:.1f}%</td>{delta}</tr>"
            )
        head = ("<th style='text-align:left;padding:4px 8px;'>Category</th><th style='padding:4px 8px;'>Count</th>"
                "<th style='padding:4px 8px;'>Share</th>" + ("<th style='padding:4px 8px;'>Δ</th>" if prev else ""))
        parts.append(
            f"<h3 {h3}>Questions by category</h3>"
            f"<table style='border-collapse:collapse;font-size:13px;'><tr style='border-bottom:1px solid #ccc;'>{head}</tr>{''.join(rows)}</table>"
        )

        suggestions = (llm or {}).get("new_category_suggestions") or []
        if suggestions:
            parts.append(
                f"<h3 {h3}>Suggested new categories</h3>"
                + _ul([f"{s.get('name', '')} — {s.get('reason', '')}" for s in suggestions], unit)
                + "<p style='font-size:12px;color:#666;'>Add these to categories.yaml if they keep showing up.</p>"
            )

    return (
        "<!doctype html><html><body style='font-family:Arial,Helvetica,sans-serif;font-size:14px;color:#111;max-width:680px;'>"
        + "".join(parts)
        + "<p style='margin-top:24px;font-size:11px;color:#888;'>Generated automatically from chatbot logs "
        "with Azure OpenAI. Personal details are redacted.</p></body></html>"
    )


def build_text(period, start, end, n_questions, table, llm):
    lines = [report_title(period, start), f"Window: {start.date()} to {end.date()}", f"Total messages: {n_questions}", ""]
    if llm and llm.get("headline"):
        lines += [llm["headline"], ""]
    lines.append("Questions by category:")
    for t in table[:TOP_K]:
        lines.append(f"  • {t['name']} — {t['share'] * 100:.1f}% ({t['count']} messages)")
    return "\n".join(lines)


# ---------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true", help="write report.html/report.json locally; skip uploads and email")
    ap.add_argument("--period", choices=["month", "week"], default=PERIOD, help="default: $REPORT_PERIOD or month")
    ap.add_argument("--ago", type=int, default=0, help="0 = last full period, 1 = the one before, ...")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    logging.getLogger("azure").setLevel(logging.WARNING)  # Azure SDK dumps full request/response headers at INFO

    if not AZURE_CONN_STR:
        sys.exit("AZURE_CSV_CONTAINER is not set")

    categories = load_categories()
    period = args.period
    start, end = compute_window(period, args.ago)
    run_id = period_id(period, start)
    prev_id = period_id(period, compute_window(period, args.ago + 1)[0])
    log.info("Summarizing %s (%s to %s)", run_id, start.isoformat(), end.isoformat())

    qdf = load_questions(start, end)
    n = len(qdf)
    flagged = int(qdf["flagged"].eq(True).sum()) if "flagged" in qdf else None

    table, llm, prev = [], None, None
    if n:
        qdf["category"] = classify_questions(qdf["question"].tolist(), categories)
        table = build_category_table(qdf, categories)
        prev = load_previous_counts(prev_id)
        try:
            llm = summarize_period(period_label(period, start), n, table, prev["counts"] if prev else None)
        except Exception:
            log.exception("LLM summary failed; sending counts only")

    record = {
        "run_id": run_id,
        "period": period,
        "period_start": start.date().isoformat(),
        "period_end": end.date().isoformat(),
        "generated_at": datetime.now(UTC).isoformat(),
        "n_questions": n,
        "flagged": flagged,
        "categories": [{k: t[k] for k in ("id", "name", "count", "share")} for t in table],
        "llm_summary": llm,
        "method": {"classifier": CLASSIFY_DEPLOYMENT, "summarizer": SUMMARY_DEPLOYMENT},
    }

    report_html = build_html(period, start, end, n, table, llm, prev, flagged)
    report_text = build_text(period, start, end, n, table, llm)

    if args.dry_run:
        with open("report.html", "w", encoding="utf-8") as f:
            f.write(report_html)
        with open("report.json", "w", encoding="utf-8") as f:
            json.dump(record, f, ensure_ascii=False, indent=2)
        log.info("Dry run: wrote report.html and report.json")
        return

    upload_json(SUMMARY_CONTAINER, f"json/{run_id}.json", record)
    append_text(SUMMARY_CONTAINER, SUMMARY_TEXT_BLOB, report_text)

    from send_email import send_email
    send_email(report_html, subject=report_title(period, start), text=report_text)
    log.info("Done")


if __name__ == "__main__":
    main()
