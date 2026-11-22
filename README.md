# Topic-Inference

Monthly summary of questions asked to the LCI chatbot, emailed to staff.

1. Loads last month's questions (Central time) from `chat-logs/logs`.
2. Categorizes each question against [`categories.yaml`](categories.yaml) with Azure OpenAI (batched, deduplicated, PII-redacted).
3. Asks Azure OpenAI for a summary: headline, themes, changes vs. the previous month, content ideas, notable questions, and suggested new categories (from questions that landed in "other").
4. Saves `weekly-summaries/json/<YYYY-MM>.json` and appends to `weekly-summaries/topic_discovery.txt`.
5. Emails the HTML report via Azure Communication Services (ACS) Email.

Set `REPORT_PERIOD=week` to switch to weekly reports (Mon–Mon) once chatbot traffic grows.

| File | Purpose |
|---|---|
| `chatbot_summary.py` | Entry point for the scheduled job |
| `categories.yaml` | **The topic list — edit this** to add/rename categories |
| `classify.py` | Per-question categorization |
| `llm_summary.py` | Summary prompt + PII redaction |
| `llm_client.py` | Azure OpenAI helper (works with gpt-4.1 and newer gpt-5/6 deployments) |
| `send_email.py` | Email via ACS |
| `build.py`, `*.ipynb`, `requirements-bertopic.txt` | Legacy BERTopic approach, not used by the job |

## Environment variables

| Var | Purpose |
|---|---|
| `AZURE_CSV_CONTAINER` | Blob storage connection string (same as the chatbot) |
| `OPENAI_CHAT_ENDPOINT`, `OPENAI_CHAT_API` | Azure OpenAI (same as the chatbot) |
| `OPENAI_CLASSIFY_DEPLOYMENT` | Optional, default `gpt-4.1-mini` |
| `OPENAI_SUMMARY_DEPLOYMENT` | Optional, default `gpt-4.1-mini` |
| `ACS_CONNECTION_STRING` | Communication Services connection string |
| `MAIL_FROM` | Sender on a domain linked to the ACS resource, e.g. `DoNotReply@<guid>.azurecomm.net` |
| `MAIL_TO` | Recipients, comma-separated |
| `REPORT_PERIOD` | Optional, `month` (default) or `week` |

## Run locally

```bash
pip install -r requirements.txt
python chatbot_summary.py --dry-run           # last full month → report.html + report.json, no upload/email
python chatbot_summary.py --dry-run --ago 1   # the month before
```

## Deploy to Azure (Container Apps Job)

Everything lives in the LCI subscription next to the chatbot (`inferencing-container`) and `monthly-qdrant-updater`.
The commands below assume the `az-me` alias for the LCI login (`alias az-me='AZURE_CONFIG_DIR=~/.azure-personal az'`)
and are run **in one terminal session** (later steps use variables from earlier ones).

### 1. Select the LCI subscription and install CLI extensions

```bash
az-me account set -s 11c20312-8a31-4731-96d8-79ee940decff
az-me extension add -n containerapp --upgrade     # Container Apps jobs
az-me extension add -n communication --upgrade    # for ACS Email
RG=qdrantdb-lci_group
```

### 2. Set up ACS Email (one time)

Creates an Email Communication Service with a free Azure-managed sender domain
(`DoNotReply@<guid>.azurecomm.net`), a Communication Services resource, and links them.
ACS resource names are globally unique; change them if a name is taken.

```bash
az-me provider register -n Microsoft.Communication --wait

az-me communication email create -n lci-chatbot-email -g $RG --location global --data-location "United States"
az-me communication email domain create -n AzureManagedDomain --email-service-name lci-chatbot-email -g $RG \
  --location global --domain-management AzureManaged
az-me communication create -n lci-chatbot-acs -g $RG --location global --data-location "United States"

DOMAIN_ID=$(az-me communication email domain show -n AzureManagedDomain --email-service-name lci-chatbot-email -g $RG --query id -o tsv)
az-me communication update -n lci-chatbot-acs -g $RG --linked-domains "$DOMAIN_ID"
```

Emails from the Azure-managed domain can land in spam at first; have recipients mark the first one "Not spam".
To send from `@libertarianchristians.com` later, add a custom domain to `lci-chatbot-email` in the portal
(Provision domains → Add domain → add the DNS records it shows), link it, and change `MAIL_FROM`.

### 3. Build the image in Azure Container Registry

Runs the Docker build in Azure, so Docker isn't needed locally. Run from this folder.

```bash
TAG=$(git rev-parse --short HEAD)
az-me acr build -r ragchatbottest -t chatbot-summary:$TAG -t chatbot-summary:latest .
```

### 4. Collect settings

Read into shell variables; nothing is printed.

```bash
OPENAI_ENDPOINT=$(az-me containerapp show -n inferencing-container -g $RG \
  --query "properties.template.containers[0].env[?name=='OPENAI_CHAT_ENDPOINT'].value | [0]" -o tsv)
OPENAI_KEY=$(az-me containerapp secret show -n inferencing-container -g $RG --secret-name open-chat-api --query value -o tsv)
BLOB_CONN=$(az-me containerapp secret show -n inferencing-container -g $RG --secret-name blob-storage-endpoint --query value -o tsv)
ACS_CONN=$(az-me communication list-key -n lci-chatbot-acs -g $RG --query primaryConnectionString -o tsv)
MAIL_FROM="DoNotReply@$(az-me communication email domain show -n AzureManagedDomain --email-service-name lci-chatbot-email -g $RG --query mailFromSenderDomain -o tsv)"

MAIL_TO=person1@libertarianchristians.com,person2@libertarianchristians.com
```

Check that everything loaded (prints only names; zsh syntax):

```bash
for v in OPENAI_ENDPOINT OPENAI_KEY BLOB_CONN ACS_CONN MAIL_FROM MAIL_TO; do [ -n "${(P)v}" ] && echo "$v ok" || echo "$v MISSING"; done
```

### 5. Create the job

Runs at 13:00 UTC on the 1st of each month (7am CST / 8am CDT).

```bash
az-me containerapp job create \
  -n lci-chatbot-summary -g $RG --environment inferencing-container \
  --trigger-type Schedule --cron-expression "0 13 1 * *" \
  --replica-timeout 900 --replica-retry-limit 0 --parallelism 1 --replica-completion-count 1 \
  --image ragchatbottest.azurecr.io/chatbot-summary:$TAG \
  --registry-server ragchatbottest.azurecr.io \
  --registry-username "$(az-me acr credential show -n ragchatbottest --query username -o tsv)" \
  --registry-password "$(az-me acr credential show -n ragchatbottest --query 'passwords[0].value' -o tsv)" \
  --cpu 0.25 --memory 0.5Gi \
  --secrets "openai-key=$OPENAI_KEY" "blob-conn=$BLOB_CONN" "acs-conn=$ACS_CONN" \
  --env-vars "AZURE_CSV_CONTAINER=secretref:blob-conn" "OPENAI_CHAT_API=secretref:openai-key" \
             "ACS_CONNECTION_STRING=secretref:acs-conn" "OPENAI_CHAT_ENDPOINT=$OPENAI_ENDPOINT" \
             "MAIL_FROM=$MAIL_FROM" "MAIL_TO=$MAIL_TO" REPORT_PERIOD=month \
             OPENAI_CLASSIFY_DEPLOYMENT=gpt-4.1-mini OPENAI_SUMMARY_DEPLOYMENT=gpt-4.1-mini
```

Registry auth uses the ACR admin user. The Container Apps environment's managed identity
(`--registry-identity system-environment`) stopped working when the subscription moved to a different Azure AD tenant
(Nov 2026); the chatbot and `monthly-qdrant-updater` were switched to the admin user too. To go back to identity-based
pulls, re-create the environment's system identity and have a subscription Owner grant it `AcrPull` on `ragchatbottest`.

`--replica-retry-limit 0` avoids duplicate emails if a run fails after sending.

### 6. Test it

This sends a real email for last month and saves its JSON.

```bash
az-me containerapp job start -n lci-chatbot-summary -g $RG
az-me containerapp job execution list -n lci-chatbot-summary -g $RG -o table   # wait for Succeeded
```

Logs: Azure portal → `lci-chatbot-summary` → **Execution history** → **Console logs**.

### Updating

```bash
# code or categories.yaml changed → rebuild and point the job at the new tag
TAG=$(git rev-parse --short HEAD)
az-me acr build -r ragchatbottest -t chatbot-summary:$TAG -t chatbot-summary:latest .
az-me containerapp job update -n lci-chatbot-summary -g $RG --image ragchatbottest.azurecr.io/chatbot-summary:$TAG

# change recipients
az-me containerapp job update -n lci-chatbot-summary -g $RG --set-env-vars "MAIL_TO=a@libertarianchristians.com,b@libertarianchristians.com"

# switch models (e.g. once gpt-6-luna is deployed)
az-me containerapp job update -n lci-chatbot-summary -g $RG --set-env-vars OPENAI_CLASSIFY_DEPLOYMENT=gpt-6-luna OPENAI_SUMMARY_DEPLOYMENT=gpt-6-luna

# go weekly (Mondays 13:00 UTC)
az-me containerapp job update -n lci-chatbot-summary -g $RG --cron-expression "0 13 * * 1" --set-env-vars REPORT_PERIOD=week
```
