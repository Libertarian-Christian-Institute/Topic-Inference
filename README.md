# Topic-Inference

Weekly summary of questions asked to the LCI chatbot, emailed to staff.

1. Loads last week's questions (Mon–Mon, Central) from `chat-logs/logs`.
2. Categorizes each question against [`categories.yaml`](categories.yaml) with Azure OpenAI (batched, deduplicated, PII-redacted).
3. Asks Azure OpenAI for a summary: headline, themes, week-over-week changes, content ideas, notable questions, and suggested new categories (from questions that landed in "other").
4. Saves `weekly-summaries/json/<week>.json` and appends to `weekly-summaries/topic_discovery.txt`.
5. Emails the HTML report via SendGrid.

| File | Purpose |
|---|---|
| `weekly_summary.py` | Entry point for the scheduled job |
| `categories.yaml` | **The topic list — edit this** to add/rename categories |
| `classify.py` | Per-question categorization |
| `llm_summary.py` | Summary prompt + PII redaction |
| `llm_client.py` | Azure OpenAI helper (works with gpt-4.1 and gpt-5-family deployments) |
| `send_email.py` | SendGrid |
| `build.py`, `*.ipynb`, `requirements-bertopic.txt` | Legacy BERTopic approach, not used by the job |

## Environment variables

| Var | Purpose |
|---|---|
| `AZURE_CSV_CONTAINER` | Blob storage connection string (same as LCI-Chatbot) |
| `OPENAI_CHAT_ENDPOINT`, `OPENAI_CHAT_API` | Azure OpenAI (same as LCI-Chatbot) |
| `OPENAI_CLASSIFY_DEPLOYMENT` | Optional, default `gpt-4.1-mini` |
| `OPENAI_SUMMARY_DEPLOYMENT` | Optional, default `gpt-4.1-mini` (a stronger model here is cheap: one call/week) |
| `SENDGRID_API_KEY`, `MAIL_FROM`, `MAIL_TO` | Email; `MAIL_TO` can be comma-separated |

## Run locally

```bash
pip install -r requirements.txt
python weekly_summary.py --dry-run               # writes report.html + report.json, no upload/email
python weekly_summary.py --dry-run --weeks-ago 1
```

## Deploy as an Azure Container Apps Job

Lives alongside the chatbot (`inferencing-container`) and `monthly-qdrant-updater` in the LCI subscription.
Use the same `OPENAI_CHAT_ENDPOINT`/`OPENAI_CHAT_API`/`AZURE_CSV_CONTAINER` values as the chatbot
(the chat models are deployed on the `benca-mcxxprxy-eastus2` resource).

```bash
az acr build -r ragchatbottest -t topic-summary:latest .

az containerapp job create \
  -n lci-weekly-summary -g qdrantdb-lci_group --environment inferencing-container \
  --trigger-type Schedule --cron-expression "0 13 * * 1" \
  --replica-timeout 900 --replica-retry-limit 0 --parallelism 1 --replica-completion-count 1 \
  --image ragchatbottest.azurecr.io/topic-summary:latest \
  --registry-server ragchatbottest.azurecr.io --registry-identity system-environment \
  --cpu 0.25 --memory 0.5Gi \
  --secrets azure-conn="<conn str>" openai-key="<key>" sendgrid-key="<key>" \
  --env-vars AZURE_CSV_CONTAINER=secretref:azure-conn OPENAI_CHAT_API=secretref:openai-key \
             SENDGRID_API_KEY=secretref:sendgrid-key OPENAI_CHAT_ENDPOINT=<endpoint> \
             OPENAI_CLASSIFY_DEPLOYMENT=gpt-4.1-mini OPENAI_SUMMARY_DEPLOYMENT=gpt-4.1-mini \
             MAIL_FROM=<from> MAIL_TO=<to1>,<to2>

# Trigger a run now to test:
az containerapp job start -n lci-weekly-summary -g qdrantdb-lci_group
```

Cron is UTC: `0 13 * * 1` = Monday 7am CST / 8am CDT. `--replica-retry-limit 0` avoids duplicate emails if a run fails after sending.

After editing `categories.yaml`, rebuild and push the image (`az acr build ...`); the job picks up `:latest` on its next run.
