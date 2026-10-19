"""Run every question in qa_dataset.json (JSONL or JSON array) through the RAG API and
collect a full set of evaluation metrics for each answer.

Examples
    python eval_rag.py --limit 20 --delay 2                  # smoke test, basic metrics only
    python eval_rag.py --embed --judge anthropic             # + semantic similarity + LLM judge
    python eval_rag.py --repeats 3 --run-name rag            # 3 runs per question (variance)
    python eval_rag.py --extra-json '{"use_rag": false}' \
        --out no_rag.jsonl --run-name no_rag                  # baseline, if your API supports it

Every output line in the results file keeps the original top-level fields (qa_id, category,
question, reference, answer, sources, f1, source_hit, latency_s, error) and adds a nested
"metrics" object with these groups:

    lexical       f1, precision, recall, rouge_l, exact_match, length stats
    semantic      embedding cosine similarities                      (needs --embed)
    judge         LLM-judge correctness / completeness / groundedness (needs --judge)
    retrieval     hit@k, MRR, precision@k, first correct rank, scores, evidence-quote hit
    groundedness  lexical proxy: how much of the answer is supported by retrieved text
    lens          programming/spoken language match, cross-language leakage (heuristic)
    efficiency    latency (successful attempt and total), retries, tokens, optional cost

Optional extras: pip install sentence-transformers tiktoken langdetect
"""
import argparse
import json
import math
import os
import random
import re
import sys
import threading
import time
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import quote

import requests
from dotenv import load_dotenv

load_dotenv()

SCHEMA_VERSION = 2
API_URL = os.getenv("RAG_API_URL", "http://127.0.0.1:8001/api/v1/rag/ask")
TOKEN = os.getenv("RAG_API_TOKEN")
if not TOKEN and "--xlsx-only" not in sys.argv:
    raise RuntimeError("RAG_API_TOKEN is not set. Add it to a .env file or export it in your shell.")

RETRYABLE = {429, 500, 502, 503, 504}
REQUEST_DELAY = 0.0  # seconds to sleep before each request (set by --delay)
EXTRA_BODY = {}      # extra JSON fields merged into every request (set by --extra-json)
JUDGE = {"provider": None, "model": None}
EMBED = {"enabled": False, "name": None, "model": None, "lock": threading.Lock(), "failed": False}
PRICE = {"in": None, "out": None}  # USD per 1M tokens (set by --price-in / --price-out)

STOPWORDS = set(
    "a an the and or but if of to in on at for with by from as is are was were be been being it "
    "its this that these those you your can will would should could do does did not no yes so "
    "than then there here into out up about over under between also such which who whom what "
    "when where why how".split()
)


# --------------------------------------------------------------------------- data in / out
def load_dataset(path):
    with open(path, encoding="utf-8") as f:
        text = f.read().strip()
    if text.startswith("["):  # plain JSON array
        return json.loads(text)
    return [json.loads(line) for line in text.splitlines() if line.strip()]


def rec_key(qa_id, run):
    return f"{qa_id}#{run}"


def load_done(path):
    """Successful records written by this schema version. Older/failed records are re-run."""
    done, stale = {}, 0
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            for line in f:
                if not line.strip():
                    continue
                r = json.loads(line)
                if r.get("error"):
                    continue
                if r.get("schema") != SCHEMA_VERSION:
                    stale += 1
                    continue
                done[rec_key(r["qa_id"], r.get("run", 0))] = r
    return done, stale


# --------------------------------------------------------------------------- API call
def _retry_after(resp, attempt):
    """Seconds to wait: honor Retry-After if present, else exponential backoff."""
    header = resp.headers.get("Retry-After") if resp is not None else None
    if header:
        try:
            return max(0.0, float(header))
        except ValueError:
            pass
    return min(5 * (2 ** attempt), 120)  # 5s, 10s, 20s, 40s, 80s, 120s...


def ask(question, limit, retries=8):
    """Returns (json_or_None, error_or_None, timing_info)."""
    info = {"attempts": 0, "rate_limited": 0, "retry_wait_s": 0.0, "delay_s": 0.0,
            "last_attempt_s": None, "total_s": None, "http_status": None}
    t_start = time.perf_counter()

    def finish():
        info["total_s"] = max(0.0, time.perf_counter() - t_start - info["delay_s"])

    last_err = None
    for attempt in range(retries + 1):
        if REQUEST_DELAY:
            time.sleep(REQUEST_DELAY)
            info["delay_s"] += REQUEST_DELAY
        resp = None
        info["attempts"] += 1
        t0 = time.perf_counter()
        try:
            resp = requests.post(
                API_URL,
                headers={"Authorization": f"Bearer {TOKEN}", "Accept": "application/json"},
                json={"question": question, "limit": limit, **EXTRA_BODY},
                timeout=300,
            )
            info["last_attempt_s"] = time.perf_counter() - t0
            info["http_status"] = resp.status_code
            if resp.status_code not in RETRYABLE:
                resp.raise_for_status()
                finish()
                return resp.json(), None, info
            if resp.status_code == 429:
                info["rate_limited"] += 1
            last_err = f"HTTP {resp.status_code}: {resp.text[:200]}"
        except (requests.ConnectionError, requests.Timeout) as e:
            last_err = f"{type(e).__name__}: {e}"
        except Exception as e:  # noqa: BLE001  (non-retryable, e.g. 400/401/404)
            finish()
            return None, f"{type(e).__name__}: {e}", info
        if attempt < retries:
            wait = _retry_after(resp, attempt) + random.uniform(0, 1)
            info["retry_wait_s"] += wait
            time.sleep(wait)
    finish()
    return None, f"gave up after {retries} retries; last error: {last_err}", info


def post_json_with_retry(url, headers, body, retries=6, timeout=120):
    last = None
    for attempt in range(retries + 1):
        resp = None
        try:
            resp = requests.post(url, headers=headers, json=body, timeout=timeout)
            if resp.status_code not in RETRYABLE:
                resp.raise_for_status()
                return resp.json()
            last = f"HTTP {resp.status_code}: {resp.text[:200]}"
        except (requests.ConnectionError, requests.Timeout) as e:
            last = f"{type(e).__name__}: {e}"
        if attempt < retries:
            time.sleep(_retry_after(resp, attempt) + random.uniform(0, 1))
    raise RuntimeError(f"request failed after {retries} retries: {last}")


# --------------------------------------------------------------------------- lexical metrics
def tok(s):
    return re.findall(r"\w+", (s or "").lower())


def _norm(s):
    return " ".join(tok(s))


def _lcs_len(a, b):
    prev = [0] * (len(b) + 1)
    for x in a:
        cur = [0]
        for j, y in enumerate(b, 1):
            cur.append(prev[j - 1] + 1 if x == y else max(prev[j], cur[j - 1]))
        prev = cur
    return prev[-1]


def lexical_metrics(answer, reference):
    a, t = tok(answer), tok(reference)
    overlap = sum((Counter(a) & Counter(t)).values())
    prec = overlap / len(a) if a else 0.0
    rec = overlap / len(t) if t else 0.0
    f = 2 * prec * rec / (prec + rec) if prec + rec else 0.0
    a_cap, t_cap = a[:400], t[:400]
    l = _lcs_len(a_cap, t_cap) if a_cap and t_cap else 0
    lp = l / len(a_cap) if a_cap else 0.0
    lr = l / len(t_cap) if t_cap else 0.0
    rl = 2 * lp * lr / (lp + lr) if lp + lr else 0.0
    return {
        "f1": round(f, 4),
        "precision": round(prec, 4),
        "recall": round(rec, 4),
        "rouge_l": round(rl, 4),
        "exact_match": _norm(answer) == _norm(reference),
        "answer_words": len(a),
        "reference_words": len(t),
        "length_ratio": round(len(a) / len(t), 3) if t else None,
    }


# --------------------------------------------------------------------------- sources / retrieval
def normalize_sources(sources):
    """Best-effort normalisation of whatever the API returns in `sources`."""
    if isinstance(sources, dict):
        sources = sources.get("chunks") or sources.get("results") or [sources]
    items = []
    for rank, s in enumerate(sources or [], start=1):
        item = {"rank": rank, "lesson_id": None, "title": None, "text": "", "score": None,
                "score_kind": None, "raw": ""}
        if isinstance(s, dict):
            meta = s.get("metadata") if isinstance(s.get("metadata"), dict) else {}
            for k in ("lesson_id", "lessonId"):
                v = s.get(k, meta.get(k))
                if v is not None:
                    item["lesson_id"] = v
                    break
            for k in ("lesson_title", "title"):
                v = s.get(k, meta.get(k))
                if isinstance(v, str) and v:
                    item["title"] = v
                    break
            for k in ("chunk_text", "text", "content", "chunk", "snippet", "page_content"):
                v = s.get(k, meta.get(k))
                if isinstance(v, str) and v:
                    item["text"] = v
                    break
            for k in ("score", "similarity", "relevance", "distance"):
                v = s.get(k)
                if isinstance(v, (int, float)) and not isinstance(v, bool):
                    item["score"], item["score_kind"] = float(v), k
                    break
            item["raw"] = json.dumps(s, ensure_ascii=False)
        else:
            item["text"] = item["raw"] = str(s)
        items.append(item)
    return items


def retrieval_metrics(items, row):
    gold_id = row.get("lesson_id")
    gold_title = (row.get("lesson_title") or "").lower()
    has_ids = any(i["lesson_id"] is not None for i in items)
    basis = "lesson_id" if has_ids else ("title" if gold_title else "none")
    rels = []
    for i in items:
        if i["lesson_id"] is not None and gold_id is not None:
            rels.append(str(i["lesson_id"]) == str(gold_id))
        elif gold_title:
            rels.append(gold_title in i["raw"].lower())
        else:
            rels.append(False)
    first = next((r for r, ok in enumerate(rels, 1) if ok), None)
    out = {
        "n_retrieved": len(items),
        "relevance_basis": basis,
        "n_relevant_retrieved": sum(rels),
        "first_correct_rank": first,
        "mrr": round(1 / first, 4) if first else 0.0,
        "hit_any": first is not None,
    }
    for k in (1, 3, 5):
        top = rels[:k]
        out[f"hit_at_{k}"] = any(top)
        out[f"precision_at_{k}"] = round(sum(top) / len(top), 4) if top else None
    out["retrieved_lesson_ids"] = [i["lesson_id"] for i in items]
    scores = [i["score"] for i in items if i["score"] is not None]
    if scores:
        out["top_score"] = round(scores[0], 4)
        out["mean_score"] = round(sum(scores) / len(scores), 4)
        out["score_kind"] = next(i["score_kind"] for i in items if i["score"] is not None)
    quote_txt = row.get("evidence_quote")
    if isinstance(quote_txt, str) and quote_txt.strip():
        q = " ".join(quote_txt.lower().split())
        qr = next((i["rank"] for i in items if q in " ".join(i["text"].lower().split())), None)
        out["evidence_quote_hit"] = qr is not None
        out["evidence_quote_rank"] = qr
    return out


# --------------------------------------------------------------------------- groundedness proxy
def _content_tokens(text):
    return [t for t in tok(text) if t not in STOPWORDS and len(t) > 1]


def groundedness_proxy(answer, context):
    """Lexical proxy (not a substitute for the LLM judge): share of answer content words
    that appear anywhere in the retrieved text, and share of sentences mostly supported."""
    if not context.strip():
        return {"context_available": False}
    ctx = set(tok(context))
    a = _content_tokens(answer)
    out = {"context_available": True, "context_token_support": None,
           "novel_token_rate": None, "sentence_support_rate": None, "n_sentences": 0}
    if not a:
        return out
    support = sum(t in ctx for t in a) / len(a)
    out["context_token_support"] = round(support, 4)
    out["novel_token_rate"] = round(1 - support, 4)
    sents = [s for s in re.split(r"(?<=[.!?])\s+|\n+", answer or "") if len(_content_tokens(s)) >= 3]
    out["n_sentences"] = len(sents)
    if sents:
        ok = sum((sum(t in ctx for t in _content_tokens(s)) / len(_content_tokens(s))) >= 0.6
                 for s in sents)
        out["sentence_support_rate"] = round(ok / len(sents), 4)
    return out


# --------------------------------------------------------------------------- lens metrics
_LANG_ALIASES = {"cpp": "c++", "c plus plus": "c++", "csharp": "c#", "js": "javascript",
                 "py": "python"}
_LANG_PATTERNS = {
    "python": re.compile(r"\bpython\b|```python|```py\b", re.I),
    "java": re.compile(r"\bjava\b|```java\b", re.I),
    "javascript": re.compile(r"\bjavascript\b|\bnode\.?js\b|```(?:javascript|js)\b", re.I),
    "c++": re.compile(r"c\+\+|\bcpp\b|```(?:cpp|c\+\+)", re.I),
    "c#": re.compile(r"c#|\bcsharp\b|```(?:csharp|cs)\b", re.I),
    "c": re.compile(r"\b(?:in|using|with) C\b|\bC (?:language|programming|code|program)\b|```c\b"),
}
_LD = {"tried": False, "fn": None}


def detect_prog_langs(text):
    return sorted(name for name, pat in _LANG_PATTERNS.items() if pat.search(text or ""))


def detect_spoken(text):
    if not text or len(text.strip()) < 20:
        return None
    if not _LD["tried"]:
        _LD["tried"] = True
        try:
            import langdetect
            langdetect.DetectorFactory.seed = 0
            _LD["fn"] = langdetect.detect
        except Exception:  # noqa: BLE001
            _LD["fn"] = None
    if _LD["fn"] is None:
        return None
    try:
        return _LD["fn"](text)
    except Exception:  # noqa: BLE001
        return None


def expected_lens(row, question):
    lens = row.get("lens") if isinstance(row.get("lens"), dict) else {}
    lang = row.get("programming_language") or lens.get("programming_language")
    source = "dataset" if lang else None
    if lang:
        lang = str(lang).strip().lower()
        lang = _LANG_ALIASES.get(lang, lang)
    else:
        found = detect_prog_langs(question)
        if len(found) == 1:
            lang, source = found[0], "question"
    return {
        "programming_language": lang,
        "source": source,
        "spoken_language": row.get("spoken_language") or lens.get("spoken_language"),
        "domain": row.get("domain") or lens.get("domain"),
    }


def lens_metrics(row, question, answer, items):
    exp = expected_lens(row, question)
    lang = exp["programming_language"]
    ans_langs = detect_prog_langs(answer)
    out = {
        "expected_programming_language": lang,
        "expected_language_source": exp["source"],
        "expected_spoken_language": exp["spoken_language"],
        "expected_domain": exp["domain"],
        "answer_programming_languages": ans_langs,
        "prog_lang_match": None,
        "prog_lang_conflict": None,
        "retrieval_lang_leakage": None,
    }
    if lang:
        if ans_langs:
            out["prog_lang_match"] = lang in ans_langs
            out["prog_lang_conflict"] = lang not in ans_langs
        chunk_langs = [detect_prog_langs(i["text"]) for i in items]
        chunk_langs = [c for c in chunk_langs if c]
        if chunk_langs:
            out["retrieval_lang_leakage"] = round(
                sum(lang not in c for c in chunk_langs) / len(chunk_langs), 4)
    q_lang, a_lang = detect_spoken(question), detect_spoken(answer)
    out["question_spoken_language"] = q_lang
    out["answer_spoken_language"] = a_lang
    if q_lang and a_lang:
        out["answer_matches_question_language"] = q_lang == a_lang
    return out


# --------------------------------------------------------------------------- efficiency
_ENC = {"tried": False, "enc": None}


def count_tokens(text):
    text = text or ""
    if not _ENC["tried"]:
        _ENC["tried"] = True
        try:
            import tiktoken
            _ENC["enc"] = tiktoken.get_encoding("cl100k_base")
        except Exception:  # noqa: BLE001
            _ENC["enc"] = None
    if _ENC["enc"] is not None:
        try:
            return len(_ENC["enc"].encode(text)), "tiktoken"
        except Exception:  # noqa: BLE001
            pass
    return max(0, round(len(text) / 4)), "heuristic"


def efficiency_metrics(info, result, question, answer, context):
    out = {
        "latency_s": round(info["last_attempt_s"], 3) if info["last_attempt_s"] is not None else None,
        "latency_total_s": round(info["total_s"], 3) if info["total_s"] is not None else None,
        "attempts": info["attempts"],
        "rate_limited": info["rate_limited"],
        "retry_wait_s": round(info["retry_wait_s"], 2),
    }
    if result is None:
        return out
    q_tok, method = count_tokens(question)
    a_tok, _ = count_tokens(answer)
    c_tok, _ = count_tokens(context)
    out.update(question_tokens=q_tok, answer_tokens=a_tok, context_tokens=c_tok, tokens_source=method)
    usage = result.get("usage") or result.get("token_usage")
    in_tok = out_tok = None
    if isinstance(usage, dict):
        in_tok = usage.get("input_tokens", usage.get("prompt_tokens"))
        out_tok = usage.get("output_tokens", usage.get("completion_tokens"))
    if in_tok is not None or out_tok is not None:
        out["tokens_source"] = "api_usage"
    in_tok = in_tok if in_tok is not None else q_tok + c_tok  # estimate: excludes system prompt
    out_tok = out_tok if out_tok is not None else a_tok
    out.update(input_tokens=in_tok, output_tokens=out_tok)
    if PRICE["in"] is not None and PRICE["out"] is not None:
        out["cost_usd"] = round((in_tok * PRICE["in"] + out_tok * PRICE["out"]) / 1e6, 6)
    return out


# --------------------------------------------------------------------------- semantic (embeddings)
def get_embedder():
    if not EMBED["enabled"] or EMBED["failed"]:
        return None
    with EMBED["lock"]:
        if EMBED["model"] is None and not EMBED["failed"]:
            try:
                from sentence_transformers import SentenceTransformer
                EMBED["model"] = SentenceTransformer(EMBED["name"])
            except Exception as e:  # noqa: BLE001
                EMBED["failed"] = True
                print(f"[warn] embedding metrics disabled: {type(e).__name__}: {e}")
    return EMBED["model"]


def semantic_metrics(question, answer, reference, context):
    model = get_embedder()
    if model is None:
        return None
    texts = [question or "", answer or "", reference or "", (context or "")[:4000]]
    with EMBED["lock"]:
        vecs = model.encode(texts, normalize_embeddings=True)

    def cos(i, j):
        return round(float((vecs[i] * vecs[j]).sum()), 4)

    out = {"sim_answer_reference": cos(1, 2)}
    if (context or "").strip():
        out.update(sim_answer_context=cos(1, 3), sim_reference_context=cos(2, 3),
                   sim_question_context=cos(0, 3))
    return out


# --------------------------------------------------------------------------- LLM judge
JUDGE_SYSTEM = (
    "You are a strict, impartial grader of answers to questions about programming lessons. "
    "Respond with a single JSON object and nothing else."
)


def build_judge_prompt(question, answer, reference, context, lens):
    lens_lines = [f"- {k}: {v}" for k, v in lens.items() if v]
    return (
        "Grade the ANSWER to the QUESTION.\n"
        "The REFERENCE answer was written by an LLM from the lesson and may be imperfect or "
        "worded differently; accept a different answer if it is correct.\n\n"
        f"QUESTION:\n{question}\n\nREFERENCE:\n{reference}\n\nANSWER:\n{answer}\n\n"
        f"RETRIEVED CONTEXT (may be empty):\n{(context or '')[:6000] or '(none)'}\n\n"
        f"REQUESTED LENS (may be empty):\n{chr(10).join(lens_lines) or '(none)'}\n\n"
        "Return JSON with exactly these keys:\n"
        '  "correctness": integer 1-5 (5 = fully correct, 1 = wrong),\n'
        '  "completeness": integer 1-5 (does it cover what the question asks),\n'
        '  "groundedness": integer 1-5 = how well the answer is supported by the retrieved '
        "context, or null if the context is empty,\n"
        '  "hallucination": true if the answer states specifics that are false or unsupported,\n'
        '  "abstained": true if the answer declines or says it does not know,\n'
        '  "lens_appropriate": true/false if a lens was requested and the answer fits it, '
        "else null,\n"
        '  "rationale": at most 40 words.'
    )


def parse_json_object(text):
    text = (text or "").strip()
    text = re.sub(r"^```(?:json)?|```$", "", text, flags=re.M).strip()
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1:
        raise ValueError("no JSON object in judge response")
    return json.loads(text[start:end + 1])


def _score(v, lo=1.0, hi=5.0):
    try:
        return max(lo, min(hi, float(v)))
    except (TypeError, ValueError):
        return None


def _bool(v):
    if isinstance(v, bool):
        return v
    if isinstance(v, str):
        if v.strip().lower() in ("true", "yes"):
            return True
        if v.strip().lower() in ("false", "no"):
            return False
    return None


def validate_judge(d):
    out = {
        "correctness": _score(d.get("correctness")),
        "completeness": _score(d.get("completeness")),
        "groundedness": _score(d.get("groundedness")),
        "hallucination": _bool(d.get("hallucination")),
        "abstained": _bool(d.get("abstained")),
        "lens_appropriate": _bool(d.get("lens_appropriate")),
        "rationale": str(d.get("rationale", ""))[:300],
    }
    out["pass"] = (out["correctness"] >= 4) if out["correctness"] is not None else None
    return out


def _judge_text(prompt):
    provider, model = JUDGE["provider"], JUDGE["model"]
    if provider == "anthropic":
        data = post_json_with_retry(
            "https://api.anthropic.com/v1/messages",
            {"x-api-key": os.environ["ANTHROPIC_API_KEY"], "anthropic-version": "2023-06-01",
             "content-type": "application/json"},
            {"model": model, "max_tokens": 600, "temperature": 0, "system": JUDGE_SYSTEM,
             "messages": [{"role": "user", "content": prompt}]},
        )
        return "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text")
    key = os.getenv("GEMINI_API_KEY") or os.environ["GOOGLE_API_KEY"]
    data = post_json_with_retry(
        f"https://generativelanguage.googleapis.com/v1beta/models/{quote(model, safe='')}:generateContent",
        {"x-goog-api-key": key, "Content-Type": "application/json"},
        {"systemInstruction": {"parts": [{"text": JUDGE_SYSTEM}]},
         "contents": [{"role": "user", "parts": [{"text": prompt}]}],
         "generationConfig": {"temperature": 0, "responseMimeType": "application/json"}},
    )
    return "".join(p.get("text", "") for p in data["candidates"][0]["content"]["parts"])


def judge_metrics(row, question, answer, reference, context):
    lens = expected_lens(row, question)
    prompt = build_judge_prompt(question, answer, reference, context, lens)
    try:
        out = validate_judge(parse_json_object(_judge_text(prompt)))
    except Exception as e:  # noqa: BLE001
        out = {"error": f"{type(e).__name__}: {str(e)[:200]}"}
    out["provider"], out["model"] = JUDGE["provider"], JUDGE["model"]
    return out


# --------------------------------------------------------------------------- one question
def run_one(row, run, args):
    result, err, info = ask(row["question"], args.top_k)
    rec = {
        "schema": SCHEMA_VERSION,
        "run": run,
        "run_name": args.run_name,
        "qa_id": row["qa_id"],
        "category": row.get("category"),
        "lesson_id": row.get("lesson_id"),
        "lesson_title": row.get("lesson_title"),
        "module_id": row.get("module_id"),
        "question": row["question"],
        "reference": row["answer"],
        "error": err,
    }
    for k in ("programming_language", "spoken_language", "domain", "lens", "evidence_quote"):
        if k in row:
            rec[k] = row[k]
    if result is None:
        rec["latency_s"] = None
        rec["metrics"] = {"efficiency": efficiency_metrics(info, None, "", "", "")}
        return rec

    answer = result.get("answer") or ""
    sources = result.get("sources", [])
    items = normalize_sources(sources)
    context = "\n\n".join(i["text"] for i in items if i["text"])

    metrics = {
        "lexical": lexical_metrics(answer, row["answer"]),
        "retrieval": retrieval_metrics(items, row),
        "groundedness": groundedness_proxy(answer, context),
        "lens": lens_metrics(row, row["question"], answer, items),
        "efficiency": efficiency_metrics(info, result, row["question"], answer, context),
    }
    sem = semantic_metrics(row["question"], answer, row["answer"], context)
    if sem is not None:
        metrics["semantic"] = sem
    if JUDGE["provider"]:
        metrics["judge"] = judge_metrics(row, row["question"], answer, row["answer"], context)

    rec["answer"] = answer
    rec["sources"] = sources
    rec["f1"] = metrics["lexical"]["f1"]                  # kept for backwards compatibility
    rec["source_hit"] = metrics["retrieval"]["hit_any"]   # kept for backwards compatibility
    rec["latency_s"] = metrics["efficiency"]["latency_s"]
    rec["metrics"] = metrics
    extra = {k: v for k, v in result.items() if k not in ("answer", "sources")}
    if extra:
        rec["api_extra"] = extra if len(json.dumps(extra, default=str)) < 4000 else sorted(extra)
    return rec


# --------------------------------------------------------------------------- summary
def flatten(d, prefix=""):
    out = {}
    for k, v in d.items():
        key = f"{prefix}{k}"
        if isinstance(v, dict):
            out.update(flatten(v, key + "."))
        elif isinstance(v, bool):
            out[key] = float(v)
        elif isinstance(v, (int, float)):
            out[key] = float(v)
    return out


def bootstrap_ci(vals, boot, rng):
    if boot <= 0 or len(vals) < 2:
        return None, None
    n = len(vals)
    means = sorted(sum(rng.choices(vals, k=n)) / n for _ in range(boot))
    return means[int(0.025 * (boot - 1))], means[int(0.975 * (boot - 1))]


KEY_METRICS = ["lexical.f1", "lexical.rouge_l", "semantic.sim_answer_reference", "judge.correctness",
               "judge.groundedness", "retrieval.hit_at_5", "retrieval.mrr",
               "groundedness.context_token_support", "efficiency.latency_s"]


def summarize(records, boot, out_path, run_name):
    ok = [r for r in records if not r.get("error") and "answer" in r]
    print(f"\n{len(ok)}/{len(records)} succeeded")
    if not ok:
        return None
    flat = [flatten(r["metrics"]) for r in ok]
    keys = sorted({k for f in flat for k in f})
    rng = random.Random(0)
    overall = {}
    print(f"\n{'metric':44} {'n':>5} {'mean':>9} {'95% CI':>20}")
    for k in keys:
        vals = [f[k] for f in flat if k in f]
        mean = sum(vals) / len(vals)
        lo, hi = bootstrap_ci(vals, boot, rng)
        sd = math.sqrt(sum((v - mean) ** 2 for v in vals) / (len(vals) - 1)) if len(vals) > 1 else None
        overall[k] = {"n": len(vals), "mean": round(mean, 4), "sd": round(sd, 4) if sd is not None else None,
                      "ci95_low": round(lo, 4) if lo is not None else None,
                      "ci95_high": round(hi, 4) if hi is not None else None}
        ci = f"[{lo:.3f}, {hi:.3f}]" if lo is not None else ""
        print(f"{k:44} {len(vals):>5} {mean:>9.3f} {ci:>20}")

    by_cat = defaultdict(list)
    for r, f in zip(ok, flat):
        by_cat[str(r.get("category"))].append(f)
    by_cat_means = {}
    for cat, fs in by_cat.items():
        by_cat_means[cat] = {"n": len(fs)}
        for k in keys:
            vals = [f[k] for f in fs if k in f]
            if vals:
                by_cat_means[cat][k] = round(sum(vals) / len(vals), 4)
    shown = [k for k in KEY_METRICS if k in keys]
    if shown:
        print("\nPer category (means)")
        print(f"{'category':28} {'n':>4} " + " ".join(f"{k.split('.')[-1][:11]:>11}" for k in shown))
        for cat in sorted(by_cat_means):
            m = by_cat_means[cat]
            print(f"{cat:28} {m['n']:>4} " + " ".join(f"{m.get(k, float('nan')):>11.3f}" for k in shown))

    summary = {"schema": SCHEMA_VERSION, "run_name": run_name, "n_total": len(records),
               "n_ok": len(ok), "overall": overall, "by_category": by_cat_means}
    if out_path:  # optional separate JSON copy of the summary
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(summary, f, indent=2)
        print(f"\nSummary JSON written to {out_path}")
    return summary


# --------------------------------------------------------------------------- Excel export
GROUP_ORDER = ["lexical", "semantic", "judge", "retrieval", "groundedness", "lens", "efficiency"]
EXCEL_MAX_CELL = 32000

METRIC_GUIDE = {
    "lexical.f1": "Word-overlap F1 between RAG answer and reference (0-1). Ignores word order and meaning.",
    "lexical.precision": "Share of the RAG answer's words that also appear in the reference.",
    "lexical.recall": "Share of the reference's words that also appear in the RAG answer.",
    "lexical.rouge_l": "ROUGE-L F-score: longest common word subsequence (rewards matching order).",
    "lexical.exact_match": "1 if answer and reference are identical after lowercasing and removing punctuation.",
    "lexical.answer_words": "Number of words in the RAG answer.",
    "lexical.reference_words": "Number of words in the reference answer.",
    "lexical.length_ratio": "Answer words divided by reference words (verbosity).",
    "semantic.sim_answer_reference": "Embedding cosine similarity, answer vs reference (meaning-level match).",
    "semantic.sim_answer_context": "Embedding similarity, answer vs retrieved text (groundedness proxy).",
    "semantic.sim_reference_context": "Embedding similarity, reference vs retrieved text (did retrieval contain the answer).",
    "semantic.sim_question_context": "Embedding similarity, question vs retrieved text (retrieval relevance).",
    "judge.correctness": "LLM judge, 1-5: is the answer correct.",
    "judge.completeness": "LLM judge, 1-5: does it cover what the question asks.",
    "judge.groundedness": "LLM judge, 1-5: is the answer supported by the retrieved context.",
    "judge.hallucination": "1 if the judge found false or unsupported specifics.",
    "judge.abstained": "1 if the answer declines or says it does not know.",
    "judge.lens_appropriate": "1 if the answer fits the requested language/domain lens (blank if no lens).",
    "judge.pass": "1 if judge correctness >= 4.",
    "retrieval.n_retrieved": "Number of sources the API returned.",
    "retrieval.n_relevant_retrieved": "How many returned sources are from the gold lesson.",
    "retrieval.first_correct_rank": "Rank of the first source from the gold lesson (blank if none).",
    "retrieval.mrr": "Mean reciprocal rank: 1 / rank of first gold-lesson source (0 if none).",
    "retrieval.hit_any": "1 if any returned source is from the gold lesson.",
    "retrieval.top_score": "Retrieval score of the rank-1 source (meaning depends on score_kind).",
    "retrieval.mean_score": "Mean retrieval score over returned sources.",
    "retrieval.evidence_quote_hit": "1 if the dataset's evidence quote appears in the retrieved text.",
    "retrieval.evidence_quote_rank": "Rank of the first source containing the evidence quote.",
    "retrieval.relevance_basis": "How relevance was decided: lesson_id (exact), title (text match) or none.",
    "groundedness.context_token_support": "Share of the answer's content words found in the retrieved text (proxy).",
    "groundedness.novel_token_rate": "1 minus context_token_support: content words not in the retrieved text.",
    "groundedness.sentence_support_rate": "Share of answer sentences with >= 60% of content words in the retrieved text.",
    "groundedness.context_available": "1 if the API returned any source text.",
    "lens.prog_lang_match": "1 if the answer mentions the expected programming language.",
    "lens.prog_lang_conflict": "1 if the answer mentions languages but not the expected one.",
    "lens.retrieval_lang_leakage": "Share of language-tagged retrieved chunks that do not mention the expected language.",
    "lens.answer_matches_question_language": "1 if answer and question are in the same spoken language (needs langdetect).",
    "efficiency.latency_s": "Seconds for the successful request only (excludes retry waits and --delay).",
    "efficiency.latency_total_s": "Seconds including failed attempts and retry waits (excludes --delay).",
    "efficiency.attempts": "Requests made for this question (1 = no retries).",
    "efficiency.rate_limited": "Number of 429 responses received.",
    "efficiency.retry_wait_s": "Total seconds spent waiting between retries.",
    "efficiency.question_tokens": "Estimated tokens in the question.",
    "efficiency.answer_tokens": "Estimated tokens in the answer.",
    "efficiency.context_tokens": "Estimated tokens in the retrieved text.",
    "efficiency.input_tokens": "Input tokens: API usage if provided, else question + retrieved text estimate.",
    "efficiency.output_tokens": "Output tokens: API usage if provided, else answer estimate.",
    "efficiency.cost_usd": "Estimated cost from --price-in / --price-out and the token counts.",
}
for _k in (1, 3, 5):
    METRIC_GUIDE[f"retrieval.hit_at_{_k}"] = f"1 if a gold-lesson source is in the top {_k} results."
    METRIC_GUIDE[f"retrieval.precision_at_{_k}"] = f"Share of the top {_k} results from the gold lesson."


def flatten_all(d, prefix=""):
    """Like flatten() but keeps text values and joins lists, for the Results sheet."""
    out = {}
    for k, v in d.items():
        key = f"{prefix}{k}"
        if isinstance(v, dict):
            out.update(flatten_all(v, key + "."))
        elif isinstance(v, bool):
            out[key] = int(v)
        elif isinstance(v, (int, float, str)):
            out[key] = v
        elif isinstance(v, list):
            out[key] = ", ".join(str(x) for x in v)
    return out


def _metric_sort_key(k):
    g = k.split(".")[0]
    return (GROUP_ORDER.index(g) if g in GROUP_ORDER else 99, k)


def _cell(v):
    if isinstance(v, str) and len(v) > EXCEL_MAX_CELL:
        return v[:EXCEL_MAX_CELL] + " [truncated; see JSONL]"
    return v


def _sources_text(sources):
    lines = []
    for i in normalize_sources(sources):
        score = f" ({i['score']:.3f})" if i["score"] is not None else ""
        lines.append(f"{i['rank']}. lesson {i['lesson_id']} {i['title'] or ''}{score}".strip())
    return "\n".join(lines)


def build_run_info(records, args):
    ok = [r for r in records if not r.get("error")]
    judge = next((r["metrics"]["judge"] for r in ok if "judge" in r.get("metrics", {})), None)
    info = [
        ("generated_at", time.strftime("%Y-%m-%d %H:%M:%S")),
        ("run_name", records[0].get("run_name") if records else None),
        ("records", len(records)),
        ("records_ok", len(ok)),
        ("records_failed", len(records) - len(ok)),
        ("distinct_questions", len({r["qa_id"] for r in records})),
        ("runs_per_question", len({r.get("run", 0) for r in records})),
        ("embedding_metrics", any("semantic" in r.get("metrics", {}) for r in ok)),
        ("judge_provider", judge.get("provider") if judge else None),
        ("judge_model", judge.get("model") if judge else None),
        ("api_url", API_URL),
        ("schema_version", SCHEMA_VERSION),
    ]
    if args is not None:
        info += [("dataset", args.dataset), ("top_k", args.top_k), ("workers", args.workers),
                 ("delay_s", args.delay), ("extra_request_fields", json.dumps(EXTRA_BODY) if EXTRA_BODY else None),
                 ("bootstrap_resamples", args.boot)]
    return info


# Palette from the validated reference set: categorical slots 1-4 (fixed order), the blue
# ordinal ramp (steps 250/400/600) for ordered groups, and recessive chrome colours.
PAL = ["2A78D6", "EB6834", "1BAF7A", "EDA100"]
ORDINAL_BLUE = ["86B6EF", "3987E5", "184F95"]
GRID, AXIS = "E1E0D9", "C3C2B7"
SEQ_LIGHT, SEQ_DARK = "CDE2FB", "3987E5"
CHART_W = 16.5  # cm
ROW_CM = 0.53


def _percentile(vals, p):
    s = sorted(vals)
    if not s:
        return None
    k = (len(s) - 1) * p / 100
    f = int(k)
    c = min(f + 1, len(s) - 1)
    return s[f] + (s[c] - s[f]) * (k - f)


def add_visuals(wb, records, summary):
    """Charts dashboard (native Excel charts), Heatmap, By lesson and the Chart data table view."""
    from openpyxl.chart import BarChart, Reference, ScatterChart, Series
    from openpyxl.chart.data_source import NumDataSource, NumRef
    from openpyxl.chart.error_bar import ErrorBars
    from openpyxl.chart.marker import DataPoint
    from openpyxl.chart.shapes import GraphicalProperties
    from openpyxl.drawing.line import LineProperties
    from openpyxl.formatting.rule import ColorScaleRule
    from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
    from openpyxl.utils import get_column_letter

    ok = [r for r in records if not r.get("error") and "answer" in r and r.get("metrics")]
    if not ok:
        return
    flat = [flatten(r["metrics"]) for r in ok]
    present = {k for f in flat for k in f}
    head_fill = PatternFill("solid", fgColor="1F4E78")

    def vals(key, rows=None):
        return [f[key] for f in (rows if rows is not None else flat) if key in f]

    def mean(v):
        return round(sum(v) / len(v), 4) if v else None

    by_cat = defaultdict(list)
    for r, f in zip(ok, flat):
        by_cat[str(r.get("category"))].append(f)
    cats = sorted(by_cat)

    # ---------------- table view: every chart reads from this sheet
    cd = wb.create_sheet("Chart data")
    cursor = {"r": 1}

    def put(title, header, rows):
        r0 = cursor["r"]
        cd.cell(r0, 1, title).font = Font(bold=True)
        for j, h in enumerate(header, 1):
            c = cd.cell(r0 + 1, j, h)
            c.font = Font(bold=True, color="FFFFFF")
            c.fill = head_fill
        for i, row in enumerate(rows, 1):
            for j, v in enumerate(row, 1):
                cd.cell(r0 + 1 + i, j, v)
        cursor["r"] = r0 + len(rows) + 4
        return {"hdr": r0 + 1, "first": r0 + 2, "last": r0 + 1 + len(rows)}

    cd.column_dimensions["A"].width = 44
    for i in range(2, 8):
        cd.column_dimensions[get_column_letter(i)].width = 22

    # ---------------- chart helpers
    def base(chart, title, x_title=None, y_title=None, ymin=None, ymax=None, legend=True, yfmt=None):
        chart.title = title
        chart.x_axis.title = x_title
        chart.y_axis.title = y_title
        chart.x_axis.delete = False
        chart.y_axis.delete = False
        if ymin is not None:
            chart.y_axis.scaling.min = ymin
        if ymax is not None:
            chart.y_axis.scaling.max = ymax
        if yfmt:
            chart.y_axis.number_format = yfmt
        chart.y_axis.majorGridlines.spPr = GraphicalProperties(ln=LineProperties(solidFill=GRID, w=6350))
        chart.x_axis.majorGridlines = None
        chart.x_axis.spPr = GraphicalProperties(ln=LineProperties(solidFill=AXIS, w=9525))
        chart.y_axis.spPr = GraphicalProperties(ln=LineProperties(solidFill=AXIS, w=9525))
        if legend:
            chart.legend.position = "b"
        else:
            chart.legend = None
        chart.width, chart.height = CHART_W, 8.2

    def paint(series, color):
        series.graphicalProperties.solidFill = color
        series.graphicalProperties.line.noFill = True

    def bars(title, blk, val_cols, colors, horizontal=True, n_cats=None, **kw):
        ch = BarChart()
        ch.type = "bar" if horizontal else "col"
        ch.grouping = "clustered"
        for col in val_cols:
            ch.add_data(Reference(cd, min_col=col, min_row=blk["hdr"], max_row=blk["last"]), titles_from_data=True)
        ch.set_categories(Reference(cd, min_col=1, min_row=blk["first"], max_row=blk["last"]))
        for s, c in zip(ch.series, colors):
            paint(s, c)
        ch.gapWidth = 60
        base(ch, title, legend=len(val_cols) > 1, **kw)
        if horizontal:  # long category names read best horizontally, first category on top
            ch.x_axis.scaling.orientation = "maxMin"
            ch.y_axis.crosses = "max"
            n = n_cats or (blk["last"] - blk["first"] + 1)
            ch.height = max(8.2, min(14.0, 2.6 + n * (0.32 * len(val_cols) + 0.5)))
        return ch

    def scatter(title, blk, x_col, y_col, x_title, y_title, **kw):
        ch = ScatterChart()
        ch.scatterStyle = "lineMarker"
        xs = Reference(cd, min_col=x_col, min_row=blk["first"], max_row=blk["last"])
        ys = Reference(cd, min_col=y_col, min_row=blk["hdr"], max_row=blk["last"])
        s = Series(ys, xs, title_from_data=True)
        s.marker.symbol = "circle"
        s.marker.size = 8
        s.marker.graphicalProperties = GraphicalProperties(solidFill=PAL[0])
        s.marker.graphicalProperties.line.solidFill = PAL[0]
        s.graphicalProperties.line.noFill = True
        ch.series.append(s)
        base(ch, title, x_title, y_title, legend=False, **kw)
        ch.x_axis.majorGridlines = None
        ch.x_axis.scaling.min = 0
        return ch

    charts = []

    # 1. Answer quality by category
    spec = [("lexical.f1", "F1 (word overlap)", 1), ("lexical.rouge_l", "ROUGE-L", 1),
            ("semantic.sim_answer_reference", "Embedding similarity", 1),
            ("judge.correctness", "Judge correctness (divided by 5)", 5)]
    spec = [s for s in spec if s[0] in present]
    if spec:
        blk = put("Answer quality by category (means, 0-1)", ["category"] + [s[1] for s in spec],
                  [[c] + [round(mean(vals(k, by_cat[c])) / d, 4) if vals(k, by_cat[c]) else None
                          for k, _, d in spec] for c in cats])
        charts.append(bars("Answer quality by category", blk, list(range(2, 2 + len(spec))),
                           PAL[:len(spec)], ymin=0, ymax=1, y_title="mean score (0-1)", n_cats=len(cats)))

    # 2. Retrieval by category (hit@k is an ordered ramp; MRR is its own measure)
    rspec = [(f"retrieval.hit_at_{k}", f"Hit@{k}") for k in (1, 3, 5) if f"retrieval.hit_at_{k}" in present]
    rspec = [(k, n) for k, n in rspec]
    if "retrieval.mrr" in present:
        rspec.append(("retrieval.mrr", "MRR"))
    if rspec:
        blk = put("Retrieval quality by category (means, 0-1)", ["category"] + [n for _, n in rspec],
                  [[c] + [mean(vals(k, by_cat[c])) for k, _ in rspec] for c in cats])
        hit_n = sum(1 for k, _ in rspec if k.startswith("retrieval.hit"))
        colors = ORDINAL_BLUE[:hit_n] + ([PAL[1]] if "retrieval.mrr" in present else [])
        charts.append(bars("Retrieval quality by category", blk, list(range(2, 2 + len(rspec))), colors,
                           ymin=0, ymax=1, y_title="share of questions / MRR", n_cats=len(cats)))

    # 3. F1 distribution
    if "lexical.f1" in present:
        edges = [i / 10 for i in range(11)]
        counts = [0] * 10
        for v in vals("lexical.f1"):
            counts[min(int(v * 10), 9)] += 1
        blk = put("F1 distribution", ["F1 range", "questions"],
                  [[f"{edges[i]:.1f}-{edges[i + 1]:.1f}", counts[i]] for i in range(10)])
        ch = bars("F1 distribution (all questions)", blk, [2], [PAL[0]], horizontal=False,
                  x_title="F1 range", y_title="questions", ymin=0)
        ch.gapWidth = 8
        charts.append(ch)

    # 4. Distribution of the best available meaning-level score
    if "judge.correctness" in present:
        counts = Counter(int(round(v)) for v in vals("judge.correctness"))
        blk = put("Judge correctness distribution", ["score", "questions"],
                  [[str(i), counts.get(i, 0)] for i in range(1, 6)])
        ch = bars("Judge correctness distribution (1 to 5)", blk, [2], [PAL[0]], horizontal=False,
                  x_title="judge score", y_title="questions", ymin=0)
        ch.gapWidth = 30
        charts.append(ch)
    elif "semantic.sim_answer_reference" in present:
        counts = [0] * 10
        for v in vals("semantic.sim_answer_reference"):
            counts[max(0, min(int(v * 10), 9))] += 1
        blk = put("Embedding similarity distribution", ["similarity range", "questions"],
                  [[f"{i / 10:.1f}-{(i + 1) / 10:.1f}", counts[i]] for i in range(10)])
        ch = bars("Embedding similarity distribution", blk, [2], [PAL[0]], horizontal=False,
                  x_title="cosine similarity range", y_title="questions", ymin=0)
        ch.gapWidth = 8
        charts.append(ch)

    # 5. Where does the first correct source rank
    if "retrieval.hit_any" in present:
        ranks = [r["metrics"]["retrieval"].get("first_correct_rank") for r in ok]
        labels = ["1", "2", "3", "4", "5+", "Not retrieved"]
        counts = [0] * 6
        for rk in ranks:
            counts[5 if rk is None else min(rk, 5) - 1] += 1
        blk = put("Rank of first correct source", ["rank", "questions"],
                  [[labels[i], counts[i]] for i in range(6)])
        ch = bars("Rank of the first source from the right lesson", blk, [2], [PAL[0]], horizontal=False,
                  x_title="rank in results", y_title="questions", ymin=0)
        ch.gapWidth = 40
        miss = DataPoint(idx=5)
        miss.graphicalProperties.solidFill = PAL[1]
        ch.series[0].dPt.append(miss)
        charts.append(ch)

    # 6. Latency percentiles
    if "efficiency.latency_s" in present:
        a, b = vals("efficiency.latency_s"), vals("efficiency.latency_total_s")
        rows = [[f"p{p}", round(_percentile(a, p), 3), round(_percentile(b, p), 3) if b else None]
                for p in (50, 90, 95, 99)]
        blk = put("Latency percentiles (seconds)", ["percentile", "successful request", "including retries"], rows)
        charts.append(bars("Latency percentiles", blk, [2, 3], PAL[:2], horizontal=False,
                           x_title="percentile", y_title="seconds", ymin=0))

    # 7. Latency vs retrieved context size
    if "efficiency.latency_s" in present and "efficiency.context_tokens" in present:
        pts = [[f["efficiency.context_tokens"], f["efficiency.latency_s"]] for f in flat
               if "efficiency.context_tokens" in f and "efficiency.latency_s" in f][:3000]
        blk = put("Latency vs retrieved context size", ["retrieved context tokens", "latency (s)"], pts)
        charts.append(scatter("Latency vs retrieved context size", blk, 1, 2,
                              "retrieved context (tokens)", "latency (s)", ymin=0))

    # 8. Latency by category
    if "efficiency.latency_s" in present:
        lat_keys = [k for k in ("efficiency.latency_s", "efficiency.latency_total_s") if k in present]
        names = {"efficiency.latency_s": "successful request", "efficiency.latency_total_s": "including retries"}
        blk = put("Mean latency by category (seconds)", ["category"] + [names[k] for k in lat_keys],
                  [[c] + [mean(vals(k, by_cat[c])) for k in lat_keys] for c in cats])
        charts.append(bars("Mean latency by category", blk, list(range(2, 2 + len(lat_keys))), PAL[:len(lat_keys)],
                           ymin=0, y_title="seconds", n_cats=len(cats)))

    # 9. Do word overlap and meaning agree?
    ycol = "semantic.sim_answer_reference" if "semantic.sim_answer_reference" in present else "lexical.rouge_l"
    if "lexical.f1" in present and ycol in present:
        ylab = "embedding similarity" if ycol.startswith("semantic") else "ROUGE-L"
        pts = [[f["lexical.f1"], f[ycol]] for f in flat if "lexical.f1" in f and ycol in f][:3000]
        blk = put(f"F1 vs {ylab}", ["F1", ylab], pts)
        ch = scatter(f"Do F1 and {ylab} agree?", blk, 1, 2, "F1 (word overlap)", ylab, ymin=0, ymax=1)
        ch.x_axis.scaling.max = 1
        charts.append(ch)

    # 10. Groundedness proxy
    gspec = [(k, n) for k, n in (("groundedness.context_token_support", "answer words found in retrieved text"),
                                 ("groundedness.sentence_support_rate", "sentences mostly supported"))
             if k in present]
    if gspec:
        blk = put("Groundedness proxy by category (means, 0-1)", ["category"] + [n for _, n in gspec],
                  [[c] + [mean(vals(k, by_cat[c])) for k, _ in gspec] for c in cats])
        charts.append(bars("Groundedness (lexical proxy) by category", blk, list(range(2, 2 + len(gspec))),
                           PAL[:len(gspec)], ymin=0, ymax=1, y_title="share (0-1)", n_cats=len(cats)))

    # 11. Language lens by category
    lspec = [(k, n) for k, n in (("lens.prog_lang_match", "answer uses the expected language"),
                                 ("lens.retrieval_lang_leakage", "retrieved chunks from another language"))
             if k in present]
    if lspec:
        blk = put("Programming-language lens by category (means, 0-1)", ["category"] + [n for _, n in lspec],
                  [[c] + [mean(vals(k, by_cat[c])) for k, _ in lspec] for c in cats])
        charts.append(bars("Programming-language lens by category", blk, list(range(2, 2 + len(lspec))),
                           PAL[:len(lspec)], ymin=0, ymax=1, y_title="share (0-1)", n_cats=len(cats)))

    # 12. Tokens by category
    tspec = [(k, n) for k, n in (("efficiency.input_tokens", "input tokens"), ("efficiency.output_tokens", "output tokens"))
             if k in present]
    if tspec:
        blk = put("Mean tokens per question by category", ["category"] + [n for _, n in tspec],
                  [[c] + [mean(vals(k, by_cat[c])) for k, _ in tspec] for c in cats])
        charts.append(bars("Mean tokens per question by category", blk, list(range(2, 2 + len(tspec))),
                           PAL[:len(tspec)], ymin=0, y_title="tokens", n_cats=len(cats)))

    # 13. Headline metrics with 95% confidence intervals
    ci_specs = [("lexical.f1", "F1", 1), ("lexical.rouge_l", "ROUGE-L", 1),
                ("semantic.sim_answer_reference", "Embedding similarity", 1),
                ("judge.correctness", "Judge correctness (divided by 5)", 5),
                ("retrieval.hit_at_5", "Hit@5", 1), ("retrieval.mrr", "MRR", 1),
                ("groundedness.context_token_support", "Answer words in retrieved text", 1)]
    ci_rows = []
    for key, label, d in ci_specs:
        s = summary["overall"].get(key) if summary else None
        if s and s["ci95_low"] is not None:
            m, lo, hi = s["mean"] / d, s["ci95_low"] / d, s["ci95_high"] / d
            ci_rows.append([label, round(m, 4), round(max(0, hi - m), 4), round(max(0, m - lo), 4)])
    if ci_rows:
        blk = put("Headline metrics with 95% bootstrap CI", ["metric", "mean", "ci plus", "ci minus"], ci_rows)
        ch = bars("Headline metrics with 95% confidence intervals", blk, [2], [PAL[0]], ymin=0, ymax=1,
                  y_title="mean (error bars = 95% CI)", n_cats=len(ci_rows))
        eb = ErrorBars(errBarType="both", errValType="cust")
        eb.plus = NumDataSource(numRef=NumRef(f=f"'Chart data'!$C${blk['first']}:$C${blk['last']}"))
        eb.minus = NumDataSource(numRef=NumRef(f=f"'Chart data'!$D${blk['first']}:$D${blk['last']}"))
        ch.series[0].errBars = eb
        charts.append(ch)

    # 14. Request outcomes
    first_try = after_retry = failed = 0
    for r in records:
        att = (r.get("metrics", {}).get("efficiency") or {}).get("attempts", 1)
        if r.get("error"):
            failed += 1
        elif att and att > 1:
            after_retry += 1
        else:
            first_try += 1
    blk = put("Request outcomes", ["outcome", "requests"],
              [["Succeeded first try", first_try], ["Succeeded after retries", after_retry], ["Failed", failed]])
    ch = bars("Request outcomes (reliability)", blk, [2], [PAL[0]], horizontal=False,
              x_title="outcome", y_title="requests", ymin=0)
    ch.gapWidth = 60
    charts.append(ch)

    # 15. Quality by programming language lens
    lang_groups = defaultdict(list)
    for r, f in zip(ok, flat):
        lang = (r["metrics"].get("lens") or {}).get("expected_programming_language")
        if lang:
            lang_groups[lang].append(f)
    if len(lang_groups) >= 2:
        langs = sorted(lang_groups)
        lang_spec = [(k, n) for k, n in (("lexical.f1", "F1"), ("retrieval.hit_at_5", "Hit@5"),
                                         ("lens.prog_lang_match", "answer uses expected language")) if k in present]
        blk = put("Quality by expected programming language (means, 0-1)", ["language", "questions"] +
                  [n for _, n in lang_spec],
                  [[lg, len(lang_groups[lg])] + [mean(vals(k, lang_groups[lg])) for k, _ in lang_spec] for lg in langs])
        charts.append(bars("Quality by expected programming language", blk, list(range(3, 3 + len(lang_spec))),
                           PAL[:len(lang_spec)], ymin=0, ymax=1, y_title="mean (0-1)", n_cats=len(langs)))

    # 16. Weakest lessons
    lesson_groups = defaultdict(list)
    lesson_title = {}
    for r, f in zip(ok, flat):
        lesson_groups[r.get("lesson_id")].append(f)
        lesson_title[r.get("lesson_id")] = r.get("lesson_title") or ""
    if "lexical.f1" in present and len(lesson_groups) >= 2:
        ranked = sorted(((mean(vals("lexical.f1", fs)), lid) for lid, fs in lesson_groups.items()))[:15]
        blk = put("Lowest-scoring lessons by mean F1", ["lesson", "mean F1", "questions"],
                  [[f"{lid}: {lesson_title[lid][:34]}", m, len(lesson_groups[lid])] for m, lid in ranked])
        charts.append(bars("Lowest-scoring lessons (mean F1)", blk, [2], [PAL[0]], ymin=0, ymax=1,
                           y_title="mean F1", n_cats=len(ranked)))

    # ---------------- Charts dashboard sheet
    cs = wb.create_sheet("Charts", 0)
    wb.active = 0
    cs.sheet_view.showGridLines = False
    cs["A1"] = "RAG evaluation dashboard"
    cs["A1"].font = Font(size=16, bold=True)
    info = dict(build_run_info(records, None))
    cs["A2"] = (f"{len(ok)} of {len(records)} requests succeeded - run '{info.get('run_name')}' - "
                f"every chart reads from the 'Chart data' sheet")
    cs["A2"].font = Font(size=9, color="52514E")

    lat = vals("efficiency.latency_s")
    tiles = [("Questions answered", len(ok), "0", f"of {len(records)} requests"),
             ("Mean F1", mean(vals("lexical.f1")), "0.000", "word overlap vs reference")]
    if "retrieval.hit_at_5" in present:
        tiles.append(("Hit@5", mean(vals("retrieval.hit_at_5")), "0.0%", "right lesson in top 5"))
    if "judge.correctness" in present:
        tiles.append(("Judge correctness", mean(vals("judge.correctness")), '0.00" / 5"', "LLM-judged"))
    elif "semantic.sim_answer_reference" in present:
        tiles.append(("Embedding similarity", mean(vals("semantic.sim_answer_reference")), "0.000", "answer vs reference"))
    else:
        tiles.append(("Mean ROUGE-L", mean(vals("lexical.rouge_l")), "0.000", "word-order overlap"))
    if lat:
        tiles.append(("Median latency", round(_percentile(lat, 50), 3), '0.00" s"', "successful request"))
    tiles.append(("First-try success", first_try / len(records) if records else None, "0.0%", "no retries needed"))
    side = Side(style="thin", color=GRID)
    for i, (label, value, fmt, sub) in enumerate(tiles):
        c0 = 1 + i * 3
        for rr, text in ((4, label), (5, value), (6, sub)):
            cs.merge_cells(start_row=rr, start_column=c0, end_row=rr, end_column=c0 + 2)
            cell = cs.cell(rr, c0, text)
            cell.alignment = Alignment(horizontal="left", vertical="center", indent=1)
            cell.fill = PatternFill("solid", fgColor="F9F9F7")
        cs.cell(4, c0).font = Font(size=9, color="52514E")
        cs.cell(5, c0).font = Font(size=22, bold=True)
        cs.cell(5, c0).number_format = fmt
        cs.cell(6, c0).font = Font(size=8, color="898781")
        for rr in (4, 5, 6):
            for cc in range(c0, c0 + 3):
                cs.cell(rr, cc).border = Border(top=side if rr == 4 else None, bottom=side if rr == 6 else None,
                                                left=side if cc == c0 else None, right=side if cc == c0 + 2 else None)
    cs.row_dimensions[5].height = 34

    row = 8
    for i in range(0, len(charts), 2):
        pair = charts[i:i + 2]
        h = max(c.height for c in pair)
        for c, col in zip(pair, ("A", "K")):
            c.height = h
            cs.add_chart(c, f"{col}{row}")
        row += int(h / ROW_CM) + 2

    # ---------------- Heatmap (category x metric, sequential blue)
    idx = wb.sheetnames.index("By category") + 1 if "By category" in wb.sheetnames else len(wb.sheetnames)
    hm = wb.create_sheet("Heatmap", idx)
    hkeys = [k for k in ("lexical.f1", "lexical.rouge_l", "semantic.sim_answer_reference", "judge.correctness",
                         "retrieval.hit_at_1", "retrieval.hit_at_3", "retrieval.hit_at_5", "retrieval.mrr",
                         "groundedness.context_token_support", "groundedness.sentence_support_rate",
                         "lens.prog_lang_match", "efficiency.latency_s", "efficiency.input_tokens") if k in present]
    hm.append(["category", "n"] + hkeys)
    for c in cats:
        hm.append([c, len(by_cat[c])] + [mean(vals(k, by_cat[c])) for k in hkeys])
    for c in hm[1]:
        c.font = Font(bold=True, color="FFFFFF")
        c.fill = head_fill
        c.alignment = Alignment(wrap_text=True, vertical="center")
    hm.row_dimensions[1].height = 48
    hm.column_dimensions["A"].width = 28
    for j in range(2, len(hkeys) + 3):
        hm.column_dimensions[get_column_letter(j)].width = 16
    hm.freeze_panes = "C2"
    for j in range(3, len(hkeys) + 3):
        col = get_column_letter(j)
        for rr in range(2, len(cats) + 2):
            hm.cell(rr, j).number_format = "0.000"
        hm.conditional_formatting.add(
            f"{col}2:{col}{len(cats) + 1}",
            ColorScaleRule(start_type="min", start_color=SEQ_LIGHT, end_type="max", end_color=SEQ_DARK))
    hm.cell(len(cats) + 3, 1, "Each column is shaded on its own scale: darker blue = higher value.").font = \
        Font(size=9, color="52514E")

    # ---------------- By lesson table
    bl = wb.create_sheet("By lesson", idx + 1)
    lkeys = [k for k in ("lexical.f1", "lexical.rouge_l", "semantic.sim_answer_reference", "judge.correctness",
                         "retrieval.hit_at_5", "retrieval.mrr", "groundedness.context_token_support",
                         "efficiency.latency_s") if k in present]
    bl.append(["lesson_id", "lesson_title", "n"] + lkeys)
    for lid in sorted(lesson_groups, key=lambda x: (str(type(x)), x)):
        bl.append([lid, lesson_title[lid], len(lesson_groups[lid])] +
                  [mean(vals(k, lesson_groups[lid])) for k in lkeys])
    for c in bl[1]:
        c.font = Font(bold=True, color="FFFFFF")
        c.fill = head_fill
        c.alignment = Alignment(wrap_text=True, vertical="center")
    bl.row_dimensions[1].height = 48
    bl.column_dimensions["B"].width = 36
    for j in range(3, len(lkeys) + 4):
        bl.column_dimensions[get_column_letter(j)].width = 15
    bl.freeze_panes = "D2"
    bl.auto_filter.ref = bl.dimensions
    last_l = bl.max_row
    for j in range(4, len(lkeys) + 4):
        col = get_column_letter(j)
        for rr in range(2, last_l + 1):
            bl.cell(rr, j).number_format = "0.000"
        if last_l > 1:
            bl.conditional_formatting.add(
                f"{col}2:{col}{last_l}",
                ColorScaleRule(start_type="min", start_color=SEQ_LIGHT, end_type="max", end_color=SEQ_DARK))

    # keep the table view last
    wb.move_sheet("Chart data", offset=len(wb.sheetnames) - 1 - wb.sheetnames.index("Chart data"))


def write_excel(records, summary, path, args=None):
    """One workbook: Results, Summary, By category, Metric guide, Run info."""
    try:
        from openpyxl import Workbook
        from openpyxl.formatting.rule import ColorScaleRule
        from openpyxl.styles import Alignment, Font, PatternFill
        from openpyxl.utils import get_column_letter
    except ImportError:
        print("[warn] openpyxl is not installed, so no Excel file was written. Run: pip install openpyxl")
        return

    head_fill = PatternFill("solid", fgColor="1F4E78")

    def style_header(ws, ncols):
        for c in ws[1][:ncols]:
            c.font = Font(bold=True, color="FFFFFF")
            c.fill = head_fill
            c.alignment = Alignment(vertical="center", wrap_text=True)
        ws.freeze_panes = "A2"

    wb = Workbook()

    # ---- Results
    ws = wb.active
    ws.title = "Results"
    flats = [flatten_all(r.get("metrics", {})) for r in records]
    metric_cols = sorted({k for f in flats for k in f}, key=_metric_sort_key)
    id_cols = ["qa_id", "run", "category", "lesson_id", "lesson_title", "question",
               "reference_answer", "rag_answer"]
    ws.append(id_cols + metric_cols + ["sources", "error"])
    for r, f in zip(records, flats):
        ws.append([_cell(v) for v in (
            r.get("qa_id"), r.get("run", 0), r.get("category"), r.get("lesson_id"), r.get("lesson_title"),
            r.get("question"), r.get("reference"), r.get("answer"))]
            + [_cell(f.get(k)) for k in metric_cols]
            + [_cell(_sources_text(r.get("sources"))), _cell(r.get("error"))])
    ncols = len(id_cols) + len(metric_cols) + 2
    style_header(ws, ncols)
    ws.auto_filter.ref = ws.dimensions
    widths = {"qa_id": 26, "run": 6, "category": 20, "lesson_id": 9, "lesson_title": 24,
              "question": 48, "reference_answer": 52, "rag_answer": 52, "sources": 40, "error": 28}
    headers = [c.value for c in ws[1]]
    for idx, h in enumerate(headers, 1):
        ws.column_dimensions[get_column_letter(idx)].width = widths.get(h, 14)
    long_cols = {headers.index(h) for h in ("question", "reference_answer", "rag_answer", "sources", "error")}
    for row in ws.iter_rows(min_row=2):
        for idx, c in enumerate(row):
            if idx in long_cols or (isinstance(c.value, str) and len(c.value) > 30):
                c.alignment = Alignment(wrap_text=True, vertical="top")
            else:
                c.alignment = Alignment(vertical="top")
    last = ws.max_row
    for name, lo, hi in (("lexical.f1", 0, 1), ("semantic.sim_answer_reference", 0, 1), ("judge.correctness", 1, 5),
                         ("retrieval.mrr", 0, 1), ("groundedness.context_token_support", 0, 1)):
        if name in headers and last > 1:
            col = get_column_letter(headers.index(name) + 1)
            ws.conditional_formatting.add(
                f"{col}2:{col}{last}",
                ColorScaleRule(start_type="num", start_value=lo, start_color="F8696B",
                               mid_type="num", mid_value=(lo + hi) / 2, mid_color="FFEB84",
                               end_type="num", end_value=hi, end_color="63BE7B"))

    # ---- Summary + By category
    if summary:
        ss = wb.create_sheet("Summary")
        ss.append(["group", "metric", "n", "mean", "sd", "ci95_low", "ci95_high", "description"])
        for k in sorted(summary["overall"], key=_metric_sort_key):
            s = summary["overall"][k]
            ss.append([k.split(".")[0], k, s["n"], s["mean"], s["sd"], s["ci95_low"], s["ci95_high"],
                       METRIC_GUIDE.get(k, "")])
        style_header(ss, 8)
        for i, w in enumerate([14, 40, 7, 11, 11, 11, 11, 90], 1):
            ss.column_dimensions[get_column_letter(i)].width = w
        ss.auto_filter.ref = ss.dimensions

        bc = wb.create_sheet("By category")
        cats = sorted(summary["by_category"])
        bc.append(["metric"] + cats + ["ALL"])
        bc.append(["n"] + [summary["by_category"][c]["n"] for c in cats] + [summary["n_ok"]])
        for k in sorted(summary["overall"], key=_metric_sort_key):
            bc.append([k] + [summary["by_category"][c].get(k) for c in cats] + [summary["overall"][k]["mean"]])
        style_header(bc, len(cats) + 2)
        bc.column_dimensions["A"].width = 40
        for i in range(2, len(cats) + 3):
            bc.column_dimensions[get_column_letter(i)].width = 18
        for c in bc[2]:
            c.font = Font(bold=True)

    # ---- Metric guide
    mg = wb.create_sheet("Metric guide")
    mg.append(["metric", "description"])
    for k in sorted(METRIC_GUIDE, key=_metric_sort_key):
        mg.append([k, METRIC_GUIDE[k]])
    style_header(mg, 2)
    mg.column_dimensions["A"].width = 40
    mg.column_dimensions["B"].width = 100

    # ---- Run info
    ri = wb.create_sheet("Run info")
    ri.append(["setting", "value"])
    for k, v in build_run_info(records, args):
        ri.append([k, v if isinstance(v, (int, float, str)) or v is None else str(v)])
    style_header(ri, 2)
    ri.column_dimensions["A"].width = 26
    ri.column_dimensions["B"].width = 60

    add_visuals(wb, records, summary)

    wb.save(path)
    print(f"Excel workbook written to {path}")


# --------------------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dataset", default="data/export/qa_dataset.jsonl")
    ap.add_argument("--out", default="rag_results.jsonl")
    ap.add_argument("--xlsx", default="rag_results.xlsx", help="Excel workbook with all sheets")
    ap.add_argument("--xlsx-only", action="store_true",
                    help="rebuild the Excel file from an existing --out JSONL without calling the API")
    ap.add_argument("--summary-json", default=None,
                    help="optionally also write the summary as a separate JSON file")
    ap.add_argument("--limit", type=int, default=None, help="only run first N rows")
    ap.add_argument("--top-k", type=int, default=5, help="'limit' sent to the API")
    ap.add_argument("--workers", type=int, default=1)
    ap.add_argument("--delay", type=float, default=0.0,
                    help="seconds to wait before each request (use to stay under rate limits)")
    ap.add_argument("--repeats", type=int, default=1, help="runs per question (for variance)")
    ap.add_argument("--run-name", default="rag", help="label stored in every record")
    ap.add_argument("--extra-json", default=None,
                    help='JSON object merged into each request body, e.g. \'{"use_rag": false}\'')
    ap.add_argument("--embed", action="store_true", help="add embedding similarity metrics")
    ap.add_argument("--embed-model", default="sentence-transformers/all-MiniLM-L6-v2")
    ap.add_argument("--judge", choices=["none", "anthropic", "gemini"], default="none",
                    help="add LLM-judge metrics (use a different model than the data generator)")
    ap.add_argument("--judge-model", default=None)
    ap.add_argument("--price-in", type=float, default=None, help="USD per 1M input tokens")
    ap.add_argument("--price-out", type=float, default=None, help="USD per 1M output tokens")
    ap.add_argument("--boot", type=int, default=1000, help="bootstrap resamples for CIs (0 = off)")
    args = ap.parse_args()

    if args.xlsx_only:
        with open(args.out, encoding="utf-8") as f:
            records = [json.loads(line) for line in f if line.strip()]
        records.sort(key=lambda r: (str(r["qa_id"]), r.get("run", 0)))
        summary = summarize(records, args.boot, args.summary_json,
                            records[0].get("run_name") if records else None)
        write_excel(records, summary, args.xlsx, None)
        return

    global REQUEST_DELAY, EXTRA_BODY
    REQUEST_DELAY = args.delay
    if args.extra_json:
        EXTRA_BODY = json.loads(args.extra_json)
        if not isinstance(EXTRA_BODY, dict):
            raise SystemExit("--extra-json must be a JSON object")
    EMBED.update(enabled=args.embed, name=args.embed_model)
    PRICE.update({"in": args.price_in, "out": args.price_out})
    if args.judge != "none":
        JUDGE["provider"] = args.judge
        if args.judge == "anthropic":
            if not os.getenv("ANTHROPIC_API_KEY"):
                raise SystemExit("--judge anthropic needs ANTHROPIC_API_KEY in .env or the environment")
            JUDGE["model"] = args.judge_model or os.getenv("ANTHROPIC_JUDGE_MODEL", "claude-sonnet-5-5")
        else:
            if not (os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")):
                raise SystemExit("--judge gemini needs GEMINI_API_KEY in .env or the environment")
            JUDGE["model"] = args.judge_model or os.getenv(
                "GEMINI_JUDGE_MODEL", os.getenv("GEMINI_MODEL", "gemini-3.1-flash-lite"))

    rows = load_dataset(args.dataset)
    if args.limit:
        rows = rows[: args.limit]
    done, stale = load_done(args.out)
    work = [(r, run) for run in range(args.repeats) for r in rows]
    todo = [(r, run) for r, run in work if rec_key(r["qa_id"], run) not in done]
    print(f"{len(work)} jobs ({len(rows)} rows x {args.repeats} runs), {len(done)} already done, "
          f"{len(todo)} to run" + (f"; {stale} older-format records will be re-run" if stale else ""))

    # Rewrite file with only valid prior results, then append new ones.
    wanted = {rec_key(r["qa_id"], run) for r, run in work}
    kept = {k: v for k, v in done.items() if k in wanted}
    with open(args.out, "w", encoding="utf-8") as f:
        for r in kept.values():
            f.write(json.dumps(r, ensure_ascii=False) + "\n")

    new = []
    with open(args.out, "a", encoding="utf-8") as f, ThreadPoolExecutor(args.workers) as pool:
        futs = [pool.submit(run_one, r, run, args) for r, run in todo]
        for i, fut in enumerate(as_completed(futs), 1):
            out = fut.result()
            new.append(out)
            f.write(json.dumps(out, ensure_ascii=False) + "\n")
            f.flush()
            if i % 25 == 0 or i == len(todo):
                print(f"  {i}/{len(todo)}")

    all_records = sorted(list(kept.values()) + new, key=lambda r: (str(r["qa_id"]), r.get("run", 0)))
    summary = summarize(all_records, args.boot, args.summary_json, args.run_name)
    write_excel(all_records, summary, args.xlsx, args)
    print(f"Raw records (checkpoint, full sources) in {args.out}")


if __name__ == "__main__":
    main()