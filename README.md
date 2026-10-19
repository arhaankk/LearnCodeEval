# LearnCodeEval

## Setup

Install the dependencies in your virtual environment:

```sh
pip install -r requirements.txt
```

Set `RAG_API_TOKEN` in a `.env` file in the project directory:

```dotenv
RAG_API_TOKEN=your_token_here
```

Then run the script:

```sh
python main.py
```

## Building a Q&A dataset for RAG

Generate a 1,000-example lesson-grounded Q&A dataset in JSONL, plus an XLSX
audit workbook:

```sh
python -m data.dataset_builder --output-dir data/export --batch-size 500
```

The exporter uses the database settings from `.env` (`DB_HOST`, `DB_PORT`,
`DB_NAME`, `DB_USER`, and `DB_PASSWORD`) and calls the Gemini `generateContent`
API. Set `GEMINI_API_KEY` in `.env` (`GOOGLE_API_KEY` is also accepted);
`GEMINI_MODEL` is optional and defaults to `gemini-2.5-flash`. It writes:

- `qa_dataset.jsonl`: the primary RAG/evaluation dataset, with one JSON object
  per question and answer. Each row contains `qa_id`, `category`, `question`,
  `answer`, and lesson provenance (`lesson_id`, `lesson_title`, `module_id`,
  and `categories`).
- `lessons.jsonl`: one object per published, non-deleted lesson, with the
  original lesson ID, title, module ID, linked categories, and parsed
  `lesson_contents.content` JSON. The complete parsed content is retained,
  including any courseware configuration stored inside it.
- `rag_chunks.jsonl`: existing chunks linked to those lessons, kept separate
  for auditing. Chunk embeddings are intentionally excluded.
- `issues.jsonl`: lessons skipped because content is missing or malformed, as
  well as any XLSX cell truncation notices.
- `dataset_audit.xlsx`: review workbook with Lessons, Q&A Dataset, RAG Chunks,
  and Issues sheets. Large workbook cells are truncated to Excel's limit; the
  JSONL files retain the complete content.

The Q&A category targets are 200 code explanations, 200 code-generation
examples, 200 debugging examples, 150 output-prediction examples, 150
LearnCoding-documentation questions, and 100 ambiguous multi-step questions.
The exporter distributes these targets across valid lessons in batches.
Oversized lesson content is split into bounded, source-path-labeled fragments
for generation; the original full content remains intact in `lessons.jsonl`.
Rate-limit responses are retried up to five times, honoring `Retry-After` when
provided and otherwise using exponential backoff. If rate limiting persists,
the exporter reports Gemini's final error instead of silently omitting data.
Malformed or missing lesson content is reported in the issue files rather than
silently omitted. Generation errors stop the export rather than producing a
partial Q&A dataset.