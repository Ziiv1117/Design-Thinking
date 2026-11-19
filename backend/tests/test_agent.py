from __future__ import annotations

import asyncio
import json
import unittest
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

import httpx

from backend.app.adapters.agent_adapter import (
    AgentAdapter, AgentConfigurationError, AgentSettings, AnalysisTimeout,
    InputTooComplex, LLMInvalidOutput, LLMUpstreamError,
)
from backend.app.schemas.agent import ExtractedTask, ExtractionRequest, MatchBatchRequest

REQUEST = ExtractionRequest.model_validate_json(
    (Path(__file__).parent / "fixtures/extraction.json").read_bytes(), strict=True,
)
SETTINGS = AgentSettings("https://model.example/v1", "test-only", "test-model")


def task(**changes):
    return {
        "title": "实验报告", "description": None, "course_name": "机器学习", "external_ref": "ML-03",
        "due_kind": "DATE", "due_date": "2026-11-23", "due_at": None, "timezone": "Asia/Shanghai",
        "requirements": "上传 PDF", "observed_fields": ["title", "course_name", "external_ref", "deadline", "requirements"],
        "uncertain_fields": [], "reason": "报告延期并指定文件格式", "source_excerpt": REQUEST.sanitized_text.split("。")[0],
    } | changes


def resolution(draft="draft-a", candidate="old-a", **changes):
    return {
        "draft_item_id": draft, "relation": "SAME", "matched_task_id": candidate,
        "reason": "同一课程的报告延期", "uncertain_fields": [],
    } | changes


def batch_input() -> MatchBatchRequest:
    new_task = task()
    fields = ExtractedTask.model_validate(new_task).model_dump(
        exclude={"observed_fields", "uncertain_fields", "reason", "source_excerpt"})
    return MatchBatchRequest.model_validate({"items": [
        {
            "draft_item_id": f"draft-{suffix}", "new_task": new_task,
            "candidate_tasks": [{
                **fields, "id": f"old-{suffix}", "version": 1,
                "created_at": "2026-11-15T08:00:00Z", "updated_at": "2026-11-16T08:00:00Z",
            }],
        } for suffix in ("a", "b")
    ]})


class AgentTests(unittest.IsolatedAsyncioTestCase):
    def mock_agent(self, result, *, status=200, finish="stop", refusal=None, settings=SETTINGS):
        calls = []

        def handle(request):
            calls.append(request)
            content = result if isinstance(result, str) else json.dumps(result, ensure_ascii=False)
            return httpx.Response(status, json={
                "choices": [{"finish_reason": finish, "message": {"content": content, "refusal": refusal}}],
            })

        return AgentAdapter(settings, transport=httpx.MockTransport(handle)), calls

    async def test_dashscope_disables_thinking(self):
        settings = AgentSettings("https://dashscope.aliyuncs.com/compatible-mode/v1", "test-only", "qwen3.8-max")
        agent, calls = self.mock_agent({"tasks": [task()], "warnings": []}, settings=settings)
        async with agent:
            await agent.extract_tasks(REQUEST)
        sent = json.loads(calls[0].content)
        self.assertIs(sent["enable_thinking"], False)
        self.assertNotIn("thinking", sent)

    async def test_multiple_tasks_missing_timestamp(self):
        quiz = task(
            title="课堂测验", course_name=None, external_ref="Q-04", requirements=None,
            due_kind="UNRESOLVED", due_date=None, observed_fields=["title", "external_ref"],
            uncertain_fields=["deadline"], source_excerpt="课堂测验 Q-04 明天完成",
        )
        output = {"tasks": [task(), quiz], "warnings": [{"code": "SOURCE_TIME_UNKNOWN", "message": "缺少通知时间"}]}
        agent, calls = self.mock_agent(output)
        async with agent:
            result = await agent.extract_tasks(REQUEST)
        self.assertEqual(len(calls), 1)
        self.assertEqual(result.tasks[0].due_date.isoformat(), "2026-11-23")
        self.assertIsNone(result.tasks[0].due_at)
        self.assertEqual(result.tasks[0].external_ref, "ML-03")
        self.assertEqual(result.tasks[0].requirements, "上传 PDF")
        self.assertEqual(result.tasks[1].uncertain_fields, ["deadline"])
        sent = json.loads(calls[0].content)
        self.assertEqual(sent["messages"][0]["role"], "system")
        self.assertNotIn(REQUEST.sanitized_text, sent["messages"][0]["content"])
        self.assertEqual(json.loads(sent["messages"][1]["content"])["sanitized_text"], REQUEST.sanitized_text)
        self.assertEqual(sent["response_format"], {"type": "json_object"})

    async def test_datetime_normalizes_to_utc(self):
        source = "实验报告 ML-03 明天 00:30 上传 PDF。"
        req = REQUEST.model_copy(update={"sanitized_text": source, "source_time_known": True})
        output = task(
            course_name=None, due_kind="DATETIME", due_date="2026-11-18", due_at="2026-11-18T00:30:00+08:00",
            observed_fields=["title", "external_ref", "requirements", "deadline"], source_excerpt=source,
        )
        agent, _ = self.mock_agent({"tasks": [output], "warnings": []})
        async with agent:
            result = await agent.extract_tasks(req)
        self.assertEqual(result.tasks[0].due_at, datetime(2026, 11, 17, 16, 30, tzinfo=UTC))
        self.assertEqual(result.tasks[0].model_dump(mode="json")["due_at"], "2026-11-17T16:30:00Z")

    async def test_normal_no_task(self):
        agent, _ = self.mock_agent({"tasks": [], "warnings": [{"code": "NO_TASK_FOUND", "message": "没有待办事项"}]})
        async with agent:
            self.assertEqual((await agent.extract_tasks(REQUEST)).tasks, [])

    async def test_reference_only_requirements_do_not_replace_prior_instructions(self):
        for requirements, expected in (
            ("提交细节见 [URL_1]。", None),
            ("到 [URL_1] 上传 PDF。", "到 [URL_1] 上传 PDF。"),
        ):
            with self.subTest(requirements=requirements):
                text = f"实验报告 ML-03 延至2026年11月23日。{requirements}"
                req = REQUEST.model_copy(update={"sanitized_text": text})
                output = task(requirements=requirements, source_excerpt=text)
                agent, _ = self.mock_agent({"tasks": [output], "warnings": []})
                async with agent:
                    result = await agent.extract_tasks(req)
                self.assertEqual(result.tasks[0].requirements, expected)
                self.assertEqual("requirements" in result.tasks[0].observed_fields, expected is not None)

    async def test_guessed_relative_deadline_is_rejected(self):
        text = "请明天提交实验报告。"
        req = REQUEST.model_copy(update={"sanitized_text": text})
        agent, _ = self.mock_agent({"tasks": [task(source_excerpt=text)], "warnings": []})
        async with agent:
            with self.assertRaisesRegex(LLMInvalidOutput, "relative deadline"):
                await agent.extract_tasks(req)

    async def test_ocr_line_wraps_in_evidence_are_restored(self):
        text = "软件工程课程：需求分析报告 SE-02，请\n于 2026 年 11 月 26 日 23:59 前上传课\n程平台。"
        req = REQUEST.model_copy(update={"sanitized_text": text})
        output = task(
            title="需求分析报告", course_name="软件工程", external_ref="SE-02",
            due_kind="DATETIME", due_date="2026-11-26", due_at="2026-11-26T15:59:00Z",
            requirements="上传课程平台", source_excerpt="软件工程课程：需求分析报告 SE-02，请于 2026 年 11 月 26 日 23:59 前上传课程平台。",
        )
        agent, _ = self.mock_agent({"tasks": [output], "warnings": []})
        async with agent:
            result = await agent.extract_tasks(req)
        self.assertEqual(result.tasks[0].source_excerpt, text)

    async def test_absolute_date_ignores_redundant_source_time_warning(self):
        agent, _ = self.mock_agent({"tasks": [task()], "warnings": [{"code": "SOURCE_TIME_UNKNOWN", "message": "通知时间未知"}]})
        async with agent:
            result = await agent.extract_tasks(REQUEST)
        self.assertEqual(result.warnings, [])

    async def test_invalid_extractions(self):
        for changed in (
            task(extra=True), task(source_excerpt="捏造的证据"), task(timezone="UTC"),
            task(due_at="2026-11-23T10:00:00Z"), task(due_kind="DATETIME", due_at="2026-11-23T20:00:00Z"),
            task(due_date="2026-02-30"), task(title=None), task(uncertain_fields=["deadline"]),
            task(observed_fields=["title", "title"]), task(observed_fields=["title", "deadline"]),
            task(due_kind="UNRESOLVED", due_date=None), task(due_kind="TYPO"),
        ):
            with self.subTest(changes=changed):
                agent, _ = self.mock_agent({"tasks": [changed], "warnings": []})
                async with agent:
                    with self.assertRaises(LLMInvalidOutput):
                        await agent.extract_tasks(REQUEST)

    async def test_batch_relations_and_order(self):
        outcomes = ("DIFFERENT", "UNCERTAIN")
        for relation in outcomes:
            with self.subTest(relation=relation):
                agent, calls = self.mock_agent({"items": [
                    resolution("draft-b", None, relation=relation,
                               uncertain_fields=["task_identity"] if relation == "UNCERTAIN" else []),
                    resolution(),
                ]})
                async with agent:
                    result = await agent.match_tasks(batch_input())
                self.assertEqual([(item.draft_item_id, item.relation) for item in result.items],
                                 [("draft-a", "SAME"), ("draft-b", relation)])
                self.assertEqual(len(calls), 1)

    async def test_batch_rejects_unknown_cross_item_and_duplicate_ids(self):
        invalid = [
            [resolution(), resolution("draft-a", "old-a")],
            [resolution()],
            [resolution(), resolution("draft-c", "old-b")],
            [resolution(), resolution("draft-b", "old-a")],
            [resolution(), resolution("draft-b", "old-b", relation="DIFFERENT")],
            [resolution(), resolution("draft-b", None)],
        ]
        for results in invalid:
            with self.subTest(results=results):
                agent, _ = self.mock_agent({"items": results})
                async with agent:
                    with self.assertRaises(LLMInvalidOutput):
                        await agent.match_tasks(batch_input())

    async def test_input_limits(self):
        agent, calls = self.mock_agent({"tasks": [task()] * 21, "warnings": []})
        async with agent:
            with self.assertRaises(InputTooComplex):
                await agent.extract_tasks(REQUEST)
            request = batch_input()
            item = request.items[0]
            with self.assertRaises(InputTooComplex):
                await agent.match_tasks(MatchBatchRequest(items=[item] * 21))
            candidates = [
                item.candidate_tasks[0].model_copy(update={"id": f"unique-{index}", "requirements": "字" * 4000})
                for index in range(6)
            ]
            item.candidate_tasks = candidates
            with self.assertRaises(InputTooComplex):
                await agent.match_tasks(request)
            request.items = [
                item.model_copy(update={"draft_item_id": f"draft-{index}", "candidate_tasks": candidates[:5]})
                for index in range(4)
            ]
            with self.assertRaises(InputTooComplex):
                await agent.match_tasks(request)
        self.assertEqual(len(calls), 1)

    async def test_upstream_failure_invalid_json_refusal_and_truncation(self):
        for params, error in [
            ({"result": "{broken"}, LLMInvalidOutput),
            ({"result": '{"tasks":[],"tasks":[],"warnings":[]}'}, LLMInvalidOutput),
            ({"result": "{}", "finish": "length"}, LLMInvalidOutput),
            ({"result": "{}", "finish": "content_filter"}, LLMUpstreamError),
            ({"result": "{}", "refusal": "private detail"}, LLMUpstreamError),
            ({"result": "{}", "status": 401}, LLMUpstreamError),
        ]:
            with self.subTest(params=params):
                agent, calls = self.mock_agent(**params)
                async with agent:
                    with self.assertRaises(error) as context:
                        await agent.extract_tasks(REQUEST)
                self.assertNotIn("private detail", str(context.exception))
                self.assertEqual(len(calls), 1)

    async def test_timeout_and_external_cancellation(self):
        started = asyncio.Event()
        cancelled = asyncio.Event()

        async def wait(request):
            started.set()
            try:
                await asyncio.Future()
            finally:
                cancelled.set()

        transport = httpx.MockTransport(wait)
        short = AgentSettings(SETTINGS.base_url, SETTINGS.api_key, SETTINGS.model, timeout_seconds=0.02)
        async with AgentAdapter(short, transport=transport) as agent:
            with self.assertRaises(AnalysisTimeout):
                await agent.extract_tasks(REQUEST)
        self.assertTrue(cancelled.is_set())

        started.clear()
        cancelled.clear()
        async with AgentAdapter(SETTINGS, transport=transport) as agent:
            call = asyncio.create_task(agent.extract_tasks(REQUEST))
            await started.wait()
            call.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await call
        self.assertTrue(cancelled.is_set())

    async def test_concurrent_extraction_does_not_share_messages(self):
        async def echo(request):
            data = json.loads(json.loads(request.content)["messages"][1]["content"])
            await asyncio.sleep(0)
            extracted = task(source_excerpt=data["sanitized_text"])
            return httpx.Response(200, json={"choices": [
                {"finish_reason": "stop", "message": {"content": json.dumps({"tasks": [extracted], "warnings": []})}},
            ]})

        original = task()["source_excerpt"]
        texts = [original + str(index) for index in range(2)]
        async with AgentAdapter(SETTINGS, transport=httpx.MockTransport(echo)) as agent:
            results = await asyncio.gather(*(agent.extract_tasks(REQUEST.model_copy(update={"sanitized_text": text}))
                                             for text in texts))
        self.assertEqual([result.tasks[0].source_excerpt for result in results], texts)

    def test_missing_configuration_is_detected_at_initialization(self):
        with patch.dict("os.environ", {}, clear=True):
            with self.assertRaisesRegex(AgentConfigurationError, "LLM_BASE_URL"):
                AgentAdapter()


if __name__ == "__main__":
    unittest.main()
