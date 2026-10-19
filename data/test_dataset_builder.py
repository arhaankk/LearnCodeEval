import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from openpyxl import Workbook, load_workbook

from data.dataset_builder import (
    QA_CATEGORY_COUNTS,
    _WorkbookSheet,
    _batch_category_counts,
    _distribute_category_counts,
    _generate_qa_batch,
    _json_line,
    _split_content_for_generation,
    build_dataset,
    parse_lesson_content,
)


class _FakeCursor:
    lesson_rows = [
        {
            "lesson_id": 1,
            "title": "Loops",
            "module_id": 10,
            "content_json": json.dumps({"courseware": {"enabled": True}}),
        },
        {
            "lesson_id": 2,
            "title": "Broken",
            "module_id": 10,
            "content_json": '{"broken":',
        },
        {
            "lesson_id": 3,
            "title": "Empty",
            "module_id": 11,
            "content_json": None,
        },
    ]

    def __init__(self) -> None:
        self.results = []

    def execute(self, query: str, params: tuple | list) -> None:
        if "FROM lessons AS l" in query:
            last_id, _, batch_size = params
            self.results = [
                row
                for row in self.lesson_rows
                if last_id is None or row["lesson_id"] > last_id
            ][:batch_size]
        elif "FROM category_lesson AS cl" in query:
            self.results = [
                {
                    "lesson_id": 1,
                    "category_id": 100,
                    "name": "Programming",
                    "slug": "programming",
                }
            ]
        elif "FROM rag_embeddings" in query:
            self.results = [
                {
                    "id": 50,
                    "source_type": "lesson",
                    "source_id": "1",
                    "lesson_id": 1,
                    "chunk_index": 0,
                    "chunk_text": "A loop repeats instructions.",
                    "embedding_model": "test-model",
                    "content_hash": "abc",
                    "created_at": None,
                    "updated_at": None,
                }
            ] if 1 in params else []
        else:
            raise AssertionError(f"Unexpected query: {query}")

    def fetchall(self) -> list[dict]:
        return self.results

    def close(self) -> None:
        pass


class _FakeConnection:
    def __init__(self) -> None:
        self.fake_cursor = _FakeCursor()

    def cursor(self, dictionary: bool) -> _FakeCursor:
        return self.fake_cursor

    def close(self) -> None:
        pass


class ParseLessonContentTests(unittest.TestCase):
    def test_default_category_targets_total_one_thousand(self) -> None:
        self.assertEqual(
            QA_CATEGORY_COUNTS,
            {
                "code_explanation": 200,
                "code_generation": 200,
                "debugging": 200,
                "output_prediction": 150,
                "learncoding_documentation": 150,
                "ambiguous_multistep": 100,
            },
        )
        self.assertEqual(sum(QA_CATEGORY_COUNTS.values()), 1000)

    def test_preserves_nested_courseware_configuration(self) -> None:
        content = {
            "blocks": [{"type": "text", "text": "A loop repeats code."}],
            "courseware": {"enabled": True, "settings": {"attempts": 3}},
        }

        self.assertEqual(parse_lesson_content(json.dumps(content)), content)

    def test_reports_malformed_json(self) -> None:
        with self.assertRaises(json.JSONDecodeError):
            parse_lesson_content('{"blocks": [}')

    def test_jsonl_keeps_non_ascii_content_and_compact_structure(self) -> None:
        record = {"title": "Café", "content": {"text": "你好"}}

        self.assertEqual(
            _json_line(record),
            '{"title":"Café","content":{"text":"你好"}}',
        )

    def test_workbook_writes_formula_like_text_as_a_string(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "audit.xlsx"
            workbook = Workbook(write_only=True)
            sheet = _WorkbookSheet(workbook, "Audit", ["value"])
            sheet.append(["=1+1"])
            workbook.save(path)

            saved_workbook = load_workbook(path, read_only=True, data_only=False)
            value_cell = next(saved_workbook["Audit"].iter_rows(min_row=2))[0]
            self.assertEqual(value_cell.value, "=1+1")
            self.assertEqual(value_cell.data_type, "s")
            saved_workbook.close()

    def test_qa_requests_are_batched_without_changing_category_counts(self) -> None:
        self.assertEqual(
            _batch_category_counts(
                {"code_explanation": 4, "debugging": 3, "output_prediction": 2},
                batch_size=5,
            ),
            [
                {"code_explanation": 4, "debugging": 1},
                {"debugging": 2, "output_prediction": 2},
            ],
        )

    def test_category_counts_are_spread_across_large_content_fragments(self) -> None:
        distributed = _distribute_category_counts(
            {"code_explanation": 2, "debugging": 3},
            fragment_count=6,
        )

        self.assertEqual(
            [fragment.get("code_explanation", 0) for fragment in distributed],
            [1, 0, 0, 1, 0, 0],
        )
        self.assertEqual(
            [fragment.get("debugging", 0) for fragment in distributed],
            [1, 0, 1, 0, 1, 0],
        )

    def test_oversized_lesson_content_is_split_without_losing_text(self) -> None:
        source_text = "alpha beta gamma\n" * 100
        with patch("data.dataset_builder.MAX_GENERATION_CONTENT_CHARS", 180):
            fragments = _split_content_for_generation({"sections": [{"text": source_text}]})

        self.assertGreater(len(fragments), 1)
        reconstructed = "".join(
            leaf["value"]
            for fragment in fragments
            for leaf in fragment["content"]
        )
        self.assertEqual(reconstructed, source_text)
        self.assertTrue(
            all(
                len(
                    json.dumps(
                        fragment["content"],
                        ensure_ascii=False,
                        separators=(",", ":"),
                    )
                )
                <= 180
                for fragment in fragments
            )
        )

    def test_gemini_response_must_match_requested_qa_counts(self) -> None:
        response = Mock()
        response.status_code = 200
        response.json.return_value = {
            "candidates": [
                {
                    "content": {
                        "parts": [
                            {
                                "text": json.dumps(
                                    {
                                        "pairs": [
                                            {
                                                "category": "code_explanation",
                                                "question": "What does this loop do?",
                                                "answer": "It repeats while its condition is true.",
                                            }
                                        ]
                                    }
                                )
                            }
                        ]
                    }
                }
            ]
        }
        with patch("data.dataset_builder.requests.post", return_value=response) as post:
            pairs = _generate_qa_batch(
                {
                    "lesson_id": 1,
                    "title": "Loops",
                    "module_id": 10,
                    "categories": [],
                    "content": {"text": "while"},
                },
                {"code_explanation": 1},
                "test-key",
                "test-model",
            )

        self.assertEqual(len(pairs), 1)
        self.assertEqual(pairs[0]["category"], "code_explanation")
        request_args = post.call_args.kwargs
        self.assertTrue(
            post.call_args.args[0].endswith("/models/test-model:generateContent")
        )
        self.assertEqual(request_args["headers"]["x-goog-api-key"], "test-key")
        self.assertEqual(
            request_args["json"]["generationConfig"]["responseMimeType"],
            "application/json",
        )

    def test_rate_limit_retries_after_server_delay(self) -> None:
        rate_limited_response = Mock()
        rate_limited_response.status_code = 429
        rate_limited_response.headers = {"Retry-After": "3"}
        rate_limited_response.text = "rate limited"
        rate_limited_response.json.return_value = {
            "error": {"status": "RESOURCE_EXHAUSTED", "message": "Try again later."}
        }
        successful_response = Mock()
        successful_response.status_code = 200
        successful_response.json.return_value = {
            "candidates": [
                {
                    "content": {
                        "parts": [
                            {
                                "text": json.dumps(
                                    {
                                        "pairs": [
                                            {
                                                "category": "code_explanation",
                                                "question": "What does this loop do?",
                                                "answer": "It repeats while its condition is true.",
                                            }
                                        ]
                                    }
                                )
                            }
                        ]
                    }
                }
            ]
        }

        with (
            patch(
                "data.dataset_builder.requests.post",
                side_effect=[rate_limited_response, successful_response],
            ) as post,
            patch("data.dataset_builder.time.sleep") as sleep,
        ):
            pairs = _generate_qa_batch(
                {
                    "lesson_id": 1,
                    "title": "Loops",
                    "module_id": 10,
                    "categories": [],
                    "content": {"text": "while"},
                },
                {"code_explanation": 1},
                "test-key",
                "test-model",
            )

        self.assertEqual(len(pairs), 1)
        self.assertEqual(post.call_count, 2)
        sleep.assert_called_once_with(3.0)

    def test_rate_limit_failure_is_bounded(self) -> None:
        response = Mock()
        response.status_code = 429
        response.headers = {}
        response.text = "rate limited"
        response.json.return_value = {
            "error": {"status": "RESOURCE_EXHAUSTED", "message": "Try again later."}
        }

        with (
            patch(
                "data.dataset_builder.requests.post",
                return_value=response,
            ) as post,
            patch("data.dataset_builder.time.sleep") as sleep,
        ):
            with self.assertRaisesRegex(RuntimeError, "Gemini rate limit persisted after 5 retries"):
                _generate_qa_batch(
                    {
                        "lesson_id": 1,
                        "title": "Loops",
                        "module_id": 10,
                        "categories": [],
                        "content": {"text": "while"},
                    },
                    {"code_explanation": 1},
                    "test-key",
                    "test-model",
                )

        self.assertEqual(post.call_count, 6)
        self.assertEqual(sleep.call_count, 5)

    def test_builds_incremental_jsonl_and_audit_workbook(self) -> None:
        connection = _FakeConnection()
        with tempfile.TemporaryDirectory() as directory:
            output_dir = Path(directory)
            with (
                patch("data.dataset_builder.get_connection", return_value=connection),
                patch.dict("os.environ", {"GEMINI_API_KEY": "test-key"}),
                patch(
                    "data.dataset_builder.QA_CATEGORY_COUNTS",
                    {"code_explanation": 2, "debugging": 1},
                ),
                patch(
                    "data.dataset_builder.MAX_GENERATION_CONTENT_CHARS",
                    128,
                ),
                patch.object(
                    _FakeCursor,
                    "lesson_rows",
                    [
                        {
                            **_FakeCursor.lesson_rows[0],
                            "content_json": json.dumps(
                                {
                                    "courseware": {"enabled": True},
                                    "text": "large lesson text " * 200,
                                }
                            ),
                        },
                        *_FakeCursor.lesson_rows[1:],
                    ],
                ),
            ):
                generated_content_paths: list[str] = []

                def fake_qa_generator(
                    lesson: dict,
                    counts: dict[str, int],
                    api_key: str,
                    model: str,
                ) -> list[dict[str, str]]:
                    generated_content_paths.append(lesson["content_path"])
                    return [
                        {
                            "category": category,
                            "question": f"Question {index + 1} about {lesson['title']}?",
                            "answer": f"Answer {index + 1}.",
                        }
                        for category, count in counts.items()
                        for index in range(count)
                    ]

                summary = build_dataset(
                    output_dir,
                    batch_size=2,
                    qa_generator=fake_qa_generator,
                )

            lessons = [
                json.loads(line)
                for line in (output_dir / "lessons.jsonl").read_text(encoding="utf-8").splitlines()
            ]
            qa_pairs = [
                json.loads(line)
                for line in (output_dir / "qa_dataset.jsonl").read_text(encoding="utf-8").splitlines()
            ]
            chunks = [
                json.loads(line)
                for line in (output_dir / "rag_chunks.jsonl").read_text(encoding="utf-8").splitlines()
            ]
            issues = [
                json.loads(line)
                for line in (output_dir / "issues.jsonl").read_text(encoding="utf-8").splitlines()
            ]

            self.assertEqual(summary["published_lessons_seen"], 3)
            self.assertEqual(summary["lessons_exported"], 1)
            self.assertEqual(summary["lessons_skipped"], 2)
            self.assertEqual(summary["malformed_json"], 1)
            self.assertEqual(summary["qa_pairs_exported"], 3)
            self.assertTrue(generated_content_paths)
            self.assertTrue(
                all(path.startswith("fragmented JSON leaves") for path in generated_content_paths)
            )
            self.assertEqual(
                lessons[0]["content"]["courseware"],
                {"enabled": True},
            )
            self.assertEqual(
                lessons[0]["content"]["text"],
                "large lesson text " * 200,
            )
            self.assertEqual(lessons[0]["categories"][0]["slug"], "programming")
            self.assertCountEqual(
                [pair["category"] for pair in qa_pairs],
                ["code_explanation", "code_explanation", "debugging"],
            )
            self.assertEqual(qa_pairs[0]["lesson_id"], 1)
            self.assertEqual(qa_pairs[0]["lesson_title"], "Loops")
            self.assertEqual(chunks[0]["chunk_text"], "A loop repeats instructions.")
            self.assertEqual(
                [issue["issue_type"] for issue in issues],
                ["malformed_json", "missing_content"],
            )

            workbook = load_workbook(output_dir / "dataset_audit.xlsx", read_only=True)
            self.assertEqual(
                workbook.sheetnames,
                ["Lessons", "Q&A Dataset", "RAG Chunks", "Issues"],
            )
            self.assertEqual(len(list(workbook["Lessons"].values)), 2)
            self.assertEqual(len(list(workbook["Q&A Dataset"].values)), 4)
            workbook.close()


if __name__ == "__main__":
    unittest.main()
