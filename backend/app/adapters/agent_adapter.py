"""Async adapter for OpenAI-compatible Chat Completions in JSON mode."""

import asyncio
import json
import os
import re
from dataclasses import dataclass, field
from typing import Any, Self, TypeVar

import httpx
from pydantic import ValidationError

from ..agent.prompts.extraction import EXTRACTION_PROMPT
from ..agent.prompts.matching import MATCHING_PROMPT
from ..schemas.agent import (
    AgentModel, ExtractionRequest, ExtractionResult, MatchBatchRequest, MatchBatchResult,
)

Result = TypeVar("Result", bound=AgentModel)
RELATIVE_DATE = re.compile(
    r"今天|今晚|今早|明天|明晚|后天|[本这下上](?:个)?(?:周|星期|礼拜)|"
    r"\b(?:today|tonight|tomorrow|(?:this|next) (?:week|monday|tuesday|wednesday|thursday|friday|saturday|sunday))\b",
    re.IGNORECASE,
)
EXPLICIT_YEAR = re.compile(r"[0-9０-９]{4}\s*(?:年|[-/.])|[〇零一二三四五六七八九]{4}年")
REQUIREMENTS_POINTER = re.compile(
    r"(?:(?:作品|材料)?提交(?:细节|详情|要求|方式|说明)?\s*(?:请)?详?见\s*"
    r"(?:链接\s*)?(?:\[URL_\d+\]|https?://\S+|原通知|原公告)|仍按原(?:提交)?要求)[。.!！]?"
)


def _restore_excerpt_whitespace(text: str, excerpt: str) -> str | None:
    if excerpt in text:
        return excerpt
    normalized_excerpt = "".join(excerpt.split())
    if not normalized_excerpt:
        return None
    indexes = [index for index, char in enumerate(text) if not char.isspace()]
    normalized_text = "".join(text[index] for index in indexes)
    start = normalized_text.find(normalized_excerpt)
    if start < 0 or normalized_text.find(normalized_excerpt, start + 1) >= 0:
        return None
    end = start + len(normalized_excerpt)
    return text[indexes[start]:indexes[end - 1] + 1]


class AgentError(Exception):
    code = "AGENT_ERROR"


class AgentConfigurationError(AgentError):
    code = "AGENT_CONFIGURATION_ERROR"


class LLMUpstreamError(AgentError):
    code = "LLM_UPSTREAM_ERROR"


class LLMInvalidOutput(AgentError):
    code = "LLM_INVALID_OUTPUT"


class InputTooComplex(AgentError):
    code = "INPUT_TOO_COMPLEX"


class AnalysisTimeout(TimeoutError, AgentError):
    code = "ANALYSIS_TIMEOUT"


@dataclass(frozen=True)
class AgentSettings:
    base_url: str
    api_key: str = field(repr=False)
    model: str
    timeout_seconds: float = 40

    def __post_init__(self) -> None:
        for name, value in (
            ("LLM_BASE_URL", self.base_url), ("LLM_API_KEY", self.api_key), ("LLM_MODEL", self.model)
        ):
            if not value.strip():
                raise AgentConfigurationError(f"Missing {name}")
        try:
            url = httpx.URL(self.base_url)
        except httpx.InvalidURL:
            raise AgentConfigurationError("Invalid LLM_BASE_URL") from None
        if (
            url.scheme not in {"http", "https"} or not url.host or url.userinfo
            or url.query or url.fragment or url.path.endswith("/chat/completions")
        ):
            raise AgentConfigurationError("LLM_BASE_URL must be the API base URL")
        if not 0 < self.timeout_seconds <= 40:
            raise AgentConfigurationError("LLM_TIMEOUT_SECONDS must be in (0, 40]")

    @classmethod
    def from_env(cls) -> Self:
        try:
            timeout = float(os.getenv("LLM_TIMEOUT_SECONDS", "40"))
        except ValueError:
            raise AgentConfigurationError("Invalid LLM_TIMEOUT_SECONDS") from None
        return cls(
            base_url=os.getenv("LLM_BASE_URL", "").strip().rstrip("/"),
            api_key=os.getenv("LLM_API_KEY", "").strip(),
            model=os.getenv("LLM_MODEL", "").strip(),
            timeout_seconds=timeout,
        )


def unique_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    obj = dict(pairs)
    if len(obj) != len(pairs):
        raise ValueError("Duplicate JSON key")
    return obj


class AgentAdapter:
    def __init__(
        self, settings: AgentSettings | None = None, *, transport: httpx.AsyncBaseTransport | None = None
    ) -> None:
        self.settings = settings if settings is not None else AgentSettings.from_env()
        self._client = httpx.AsyncClient(
            timeout=self.settings.timeout_seconds,
            headers={"Authorization": f"Bearer {self.settings.api_key}"},
            transport=transport,
            follow_redirects=False,
        )

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._client.aclose()

    async def extract_tasks(self, request: ExtractionRequest) -> ExtractionResult:
        result = await self._complete(
            EXTRACTION_PROMPT, request.model_dump_json(), ExtractionResult,
        )
        for index, task in enumerate(result.tasks):
            source_excerpt = _restore_excerpt_whitespace(request.sanitized_text, task.source_excerpt)
            if task.timezone != request.timezone or source_excerpt is None:
                raise LLMInvalidOutput("Extraction returned an invalid time zone or evidence excerpt")
            if source_excerpt != task.source_excerpt:
                task = task.model_copy(update={"source_excerpt": source_excerpt})
                result.tasks[index] = task
            if (
                not request.source_time_known and task.due_kind in {"DATE", "DATETIME"}
                and RELATIVE_DATE.search(task.source_excerpt)
                and not EXPLICIT_YEAR.search(request.sanitized_text)
            ):
                raise LLMInvalidOutput("Extraction resolved a relative deadline without a known source time")
            pointer_only = task.requirements is not None and REQUIREMENTS_POINTER.fullmatch(task.requirements)
            pointer_in_evidence = task.requirements is None and REQUIREMENTS_POINTER.search(task.source_excerpt)
            if pointer_only or pointer_in_evidence:
                result.tasks[index] = task.model_copy(update={
                    "requirements": None,
                    "observed_fields": [name for name in task.observed_fields if name != "requirements"],
                    "uncertain_fields": [name for name in task.uncertain_fields if name != "requirements"],
                })

        unresolved = [task for task in result.tasks if task.due_kind == "UNRESOLVED"]
        if not request.source_time_known and not unresolved:
            result.warnings = [warning for warning in result.warnings if warning.code != "SOURCE_TIME_UNKNOWN"]
        warnings = {warning.code for warning in result.warnings}
        if (
            len(warnings) != len(result.warnings)
            or warnings - {"NO_TASK_FOUND", "SOURCE_TIME_UNKNOWN"}
            or ("NO_TASK_FOUND" in warnings) != (not result.tasks)
        ):
            raise LLMInvalidOutput("Extraction returned inconsistent warnings")

        if "SOURCE_TIME_UNKNOWN" in warnings:
            if request.source_time_known or not unresolved:
                raise LLMInvalidOutput("SOURCE_TIME_UNKNOWN conflicts with extraction context")
        elif not request.source_time_known and any(RELATIVE_DATE.search(task.source_excerpt) for task in unresolved):
            raise LLMInvalidOutput("Extraction omitted SOURCE_TIME_UNKNOWN")
        return result

    async def match_tasks(self, request: MatchBatchRequest) -> MatchBatchResult:
        if len(request.items) > 20 or any(len(item.candidate_tasks) > 5 for item in request.items):
            raise InputTooComplex("Match batch exceeds 20 drafts or 5 candidates per draft")
        candidate_ids: dict[str, set[str]] = {}
        for item in request.items:
            ids = {candidate.id for candidate in item.candidate_tasks}
            if item.draft_item_id in candidate_ids or not ids or len(ids) != len(item.candidate_tasks):
                raise ValueError("Match input must have unique draft IDs and 1–5 unique candidates per draft")
            candidate_ids[item.draft_item_id] = ids

        if not candidate_ids:
            return MatchBatchResult(items=[])
        payload = request.model_dump_json()
        if len(payload) > 60_000:
            raise InputTooComplex("Serialized match batch exceeds 60,000 characters")
        result = await self._complete(MATCHING_PROMPT, payload, MatchBatchResult)
        by_draft = {item.draft_item_id: item for item in result.items}
        if by_draft.keys() != candidate_ids.keys() or len(by_draft) != len(result.items):
            raise LLMInvalidOutput("Match output draft IDs must cover the batch exactly once")
        for item in result.items:
            if item.matched_task_id is not None and item.matched_task_id not in candidate_ids[item.draft_item_id]:
                raise LLMInvalidOutput("Match output refers to a candidate outside its draft")
        return MatchBatchResult(items=[by_draft[item.draft_item_id] for item in request.items])

    async def _complete(self, prompt: str, payload: str, result_type: type[Result]) -> Result:
        schema = json.dumps(result_type.model_json_schema(), ensure_ascii=False)
        endpoint = f"{self.settings.base_url.rstrip('/')}/chat/completions"
        body = {
            "model": self.settings.model,
            "messages": [
                {"role": "system", "content": f"{prompt}\nJSON Schema:\n{schema}"},
                {"role": "user", "content": payload},
            ],
            "response_format": {"type": "json_object"},
            "temperature": 0,
            "max_tokens": 16_384,
        }
        host = httpx.URL(endpoint).host
        if host == "api.deepseek.com":
            body["thinking"] = {"type": "disabled"}
        elif host == "dashscope.aliyuncs.com":
            body["enable_thinking"] = False

        try:
            async with asyncio.timeout(self.settings.timeout_seconds):
                response = await self._client.post(endpoint, json=body)
                response.raise_for_status()
        except (TimeoutError, httpx.TimeoutException):
            raise AnalysisTimeout("Model request timed out") from None
        except httpx.HTTPStatusError as exc:
            raise LLMUpstreamError(f"Model returned HTTP {exc.response.status_code}") from None
        except httpx.RequestError:
            raise LLMUpstreamError("Model request failed") from None

        try:
            choice = response.json()["choices"][0]
            message = choice["message"]
            finish_reason = choice["finish_reason"]
            if message.get("refusal") or finish_reason == "content_filter":
                raise LLMUpstreamError("Model refused to process the request")
            content = message["content"]
            if finish_reason != "stop" or not isinstance(content, str) or message.get("tool_calls"):
                raise LLMInvalidOutput("Model response was incomplete or was not a JSON answer")
            parsed = json.loads(content, object_pairs_hook=unique_keys)
        except (KeyError, TypeError, IndexError, AttributeError, ValueError, RecursionError):
            raise LLMInvalidOutput("Model response did not contain valid JSON") from None

        if result_type is ExtractionResult and isinstance(parsed, dict):
            tasks = parsed.get("tasks")
            if isinstance(tasks, list) and len(tasks) > 20:
                raise InputTooComplex("Notification contains more than 20 extracted tasks")
        try:
            return result_type.model_validate_json(content, strict=True)
        except (ValidationError, OverflowError):
            raise LLMInvalidOutput("Model JSON did not satisfy the Agent result schema") from None
