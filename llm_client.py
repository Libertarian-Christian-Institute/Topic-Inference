"""Shared Azure OpenAI helper for the weekly summary job.

Uses the same env vars as LCI-Chatbot (OPENAI_CHAT_ENDPOINT, OPENAI_CHAT_API).
Works with both classic chat models (gpt-4.1-*) and reasoning models (gpt-5-*):
reasoning models reject `temperature`, so we drop it and retry if the API says so.
"""
import json
import logging
import os
import time
from functools import lru_cache

from openai import AzureOpenAI, BadRequestError

API_VERSION = os.getenv("OPENAI_API_VERSION", "2024-12-01-preview")
log = logging.getLogger(__name__)


@lru_cache(maxsize=1)
def client() -> AzureOpenAI:
    return AzureOpenAI(
        azure_endpoint=os.environ["OPENAI_CHAT_ENDPOINT"],
        api_key=os.environ["OPENAI_CHAT_API"],
        api_version=API_VERSION,
        max_retries=3,  # SDK handles 429/5xx backoff
    )


def chat_json(deployment: str, system: str, user: str, max_tokens: int = 2000, temperature: float | None = 0.2) -> dict:
    """One chat call that must return a JSON object. Retries once on malformed JSON."""
    kwargs = dict(
        model=deployment,
        max_completion_tokens=max_tokens,
        response_format={"type": "json_object"},
        messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
    )
    if temperature is not None:
        kwargs["temperature"] = temperature

    for attempt in range(2):
        try:
            resp = client().chat.completions.create(**kwargs)
        except BadRequestError as e:
            if "temperature" in kwargs and "temperature" in str(e):
                log.info("Deployment %s rejects temperature; retrying without it", deployment)
                kwargs.pop("temperature")
                resp = client().chat.completions.create(**kwargs)
            else:
                raise
        try:
            return json.loads(resp.choices[0].message.content)
        except (json.JSONDecodeError, TypeError):
            log.warning("Malformed JSON from %s (attempt %d)", deployment, attempt + 1)
            time.sleep(1)
    raise ValueError(f"{deployment} did not return valid JSON")
