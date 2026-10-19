"""Convert rag_results.jsonl into an Excel workbook.

Usage:
    python jsonl_to_excel.py
    python jsonl_to_excel.py --in rag_results.jsonl --out rag_results.xlsx

Requires: pip install openpyxl
"""
import argparse
import json
from collections import defaultdict

from openpyxl import Workbook
from openpyxl.chart import BarChart, Reference
from openpyxl.formatting.rule import ColorScaleRule
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter


def load(path):
    rows = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            if line.strip():
                rows.append(json.loads(line))
    return rows


def short_sources(sources, max_len=500):
    """Flatten the sources field into readable text for a single cell."""
    if not sources:
        return ""
    parts = []
    for s in sources:
        if isinstance(s, dict):
            title = s.get("lesson_title") or s.get("title") or s.get("source") or ""
            lid = s.get("lesson_id")
            label = f"{title} (lesson {lid})" if lid is not None else str(title)
            parts.append(label.strip() or json.dumps(s, ensure_ascii=False)[:100])
        else:
            parts.append(str(s))
    text = "; ".join(parts)
    return text[:max_len]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--in", dest="inp", default="rag_results.jsonl")
    ap.add_argument("--out", default="rag_results.xlsx")
    args = ap.parse_args()

    rows = load(args.inp)
    wb = Workbook()

    # ---------- Results sheet ----------
    ws = wb.active
    ws.title = "Results"
    headers = ["qa_id", "category", "lesson_id", "question", "reference_answer",
               "rag_answer", "f1", "source_hit", "latency_s", "sources", "error"]
    ws.append(headers)
    for r in rows:
        ws.append([
            r.get("qa_id"), r.get("category"), r.get("lesson_id"),
            r.get("question"), r.get("reference"), r.get("answer"),
            r.get("f1"), r.get("source_hit"), r.get("latency_s"),
            short_sources(r.get("sources")), r.get("error"),
        ])

    header_fill = PatternFill("solid", fgColor="1F4E78")
    for c in ws[1]:
        c.font = Font(bold=True, color="FFFFFF")
        c.fill = header_fill
        c.alignment = Alignment(vertical="center")
    widths = {"A": 26, "B": 22, "C": 10, "D": 50, "E": 55, "F": 55,
              "G": 8, "H": 11, "I": 11, "J": 40, "K": 25}
    for col, w in widths.items():
        ws.column_dimensions[col].width = w
    for row in ws.iter_rows(min_row=2):
        for c in row:
            c.alignment = Alignment(wrap_text=True, vertical="top")
    ws.freeze_panes = "A2"
    ws.auto_filter.ref = ws.dimensions
    if len(rows) > 0:
        ws.conditional_formatting.add(
            f"G2:G{len(rows) + 1}",
            ColorScaleRule(start_type="num", start_value=0, start_color="F8696B",
                           mid_type="num", mid_value=0.5, mid_color="FFEB84",
                           end_type="num", end_value=1, end_color="63BE7B"),
        )

    # ---------- Summary sheet ----------
    ss = wb.create_sheet("Summary")
    ss.append(["category", "n", "avg_f1", "source_hit_rate", "avg_latency_s", "errors"])
    by_cat = defaultdict(list)
    for r in rows:
        by_cat[r.get("category")].append(r)

    def stats(rs):
        ok = [x for x in rs if not x.get("error") and x.get("f1") is not None]
        n_err = len(rs) - len(ok)
        if not ok:
            return [len(rs), None, None, None, n_err]
        return [
            len(rs),
            round(sum(x["f1"] for x in ok) / len(ok), 4),
            round(sum(bool(x.get("source_hit")) for x in ok) / len(ok), 4),
            round(sum(x["latency_s"] for x in ok) / len(ok), 2),
            n_err,
        ]

    for cat, rs in sorted(by_cat.items(), key=lambda kv: str(kv[0])):
        ss.append([cat] + stats(rs))
    ss.append(["ALL"] + stats(rows))

    for c in ss[1]:
        c.font = Font(bold=True, color="FFFFFF")
        c.fill = header_fill
    for c in ss[ss.max_row]:
        c.font = Font(bold=True)
    for i in range(1, 7):
        ss.column_dimensions[get_column_letter(i)].width = 26 if i == 1 else 16
    for r_ in range(2, ss.max_row + 1):
        ss.cell(r_, 4).number_format = "0.0%"

    n_cats = len(by_cat)
    if n_cats:
        chart = BarChart()
        chart.type = "col"
        chart.title = "Average F1 and source hit rate by category"
        chart.y_axis.title = "score (0-1)"
        data = Reference(ss, min_col=3, max_col=4, min_row=1, max_row=1 + n_cats)
        cats = Reference(ss, min_col=1, min_row=2, max_row=1 + n_cats)
        chart.add_data(data, titles_from_data=True)
        chart.set_categories(cats)
        chart.y_axis.scaling.min = 0
        chart.y_axis.scaling.max = 1
        chart.height, chart.width = 9, 20
        ss.add_chart(chart, "H2")

    wb.save(args.out)
    print(f"Wrote {len(rows)} rows to {args.out}")


if __name__ == "__main__":
    main()