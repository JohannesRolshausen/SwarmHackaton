"""Build the A0 poster from the analysis results.

Reads the result files in analysis/hauptanalyse-2026-11-06/ and two hand-kept
files in analysis/poster-2026-11-06/:
  route_open_reading.csv  post-hoc reading of the confirmed "split" cases
  quotes.json             the quotes on the poster, with their runs

The script stops if a poster number does not follow from the data or if a quote
is not verbatim in its run. It writes poster.html, poster.pdf (841 x 1189 mm,
vector) and poster-preview.png to analysis/poster-2026-11-06/.

Usage:
    python scripts/build_poster.py [--html-only]
"""

import argparse
import csv
import html
import json
import os
import re
import subprocess
import sys
import tempfile
from collections import Counter, defaultdict
from math import atan2, ceil, cos, sin
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from analyze_main import newcombe  # noqa: E402

RESULTS = ROOT / "analysis" / "hauptanalyse-2026-11-06"
RUNS = ROOT / "analysis" / "study-data-2026-11-05" / "runs"
OUT = ROOT / "analysis" / "poster-2026-11-06"
CHROME = [
    Path("C:/Program Files/Google/Chrome/Application/chrome.exe"),
    Path("C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe"),
]

MODELS = ("Claude Opus 5.5", "GPT-6 Astra", "Gemini 4 Argon")
SHORT = {"Claude Opus 5.5": "Opus", "GPT-6 Astra": "GPT", "Gemini 4 Argon": "Gemini"}
CONDS = ("Baseline", "Pressure", "Incentive")

INK = "#1b1a17"
INK2 = "#55534d"
RULE = "#d4d2ca"
BAND = "#f4f3ee"
COLOR = {"Baseline": "#8d8c86", "Pressure": "#eb6834", "Incentive": "#2a78d6"}
NO_ORDER = "#3d3c38"
ROUTE = "#1baf7a"
RESERVE = "#1baf7a"
PUSH_RAMP = ("#e4e2db", "#bdbbb2", "#94928a", "#62605a", "#1b1a17")
PUSH_TEXT = (INK, INK, INK, "#ffffff", "#ffffff")

SANS = "Archivo"
PT = 0.3528  # mm per point

# The whole poster uses these sizes (pt) and these two weights.
T_TITLE, T_HERO, T_HEAD, T_BODY, T_LABEL, T_NOTE = 190, 72, 44, 28, 22, 16
REG, BOLD = 400, 800


# --------------------------------------------------------------- formatting


def esc(value):
    return html.escape(str(value), quote=False)


def pct(value, digits=0):
    return f"{100 * value:.{digits}f}%"


def signed(value, digits=1):
    sign = "+" if value > 0 else "\u2212" if value < 0 else ""
    return f"{sign}{abs(value):.{digits}f}"


def signed_eur(value, digits=1):
    sign = "+" if value > 0 else "\u2212" if value < 0 else ""
    return f"{sign}\u20ac{abs(value):.{digits}f}"


def eur(value):
    return f"\u20ac{value:,.0f}"


# ---------------------------------------------------------------- SVG tools


def text(x, y, s, size=T_LABEL, weight=REG, anchor="start", fill=INK, italic=False):
    style = ' font-style="italic"' if italic else ""
    return (
        f'<text x="{x:.2f}" y="{y:.2f}" font-size="{size * PT:.2f}" font-weight="{weight}" '
        f'text-anchor="{anchor}" fill="{fill}" font-family="{SANS}"{style}>{esc(s)}</text>'
    )


def line(x1, y1, x2, y2, stroke=INK, width=0.3):
    return (
        f'<line x1="{x1:.2f}" y1="{y1:.2f}" x2="{x2:.2f}" y2="{y2:.2f}" '
        f'stroke="{stroke}" stroke-width="{width}"/>'
    )


def rect(x, y, w, h, fill, radius=0):
    r = f' rx="{radius}"' if radius else ""
    return f'<rect x="{x:.2f}" y="{y:.2f}" width="{max(w, 0):.2f}" height="{h:.2f}" fill="{fill}"{r}/>'


def dot(x, y, r, fill, ring=0.22):
    return f'<circle cx="{x:.2f}" cy="{y:.2f}" r="{r:.2f}" fill="{fill}" stroke="#ffffff" stroke-width="{ring}"/>'


def arrow(x1, y1, x2, y2, stroke=INK2, width=0.55, head=2.6):
    angle = atan2(y2 - y1, x2 - x1)
    bx, by = x2 - head * cos(angle), y2 - head * sin(angle)
    left = (bx + head * 0.5 * sin(angle), by - head * 0.5 * cos(angle))
    right = (bx - head * 0.5 * sin(angle), by + head * 0.5 * cos(angle))
    return line(x1, y1, bx, by, stroke, width) + (
        f'<polygon points="{x2:.2f},{y2:.2f} {left[0]:.2f},{left[1]:.2f} {right[0]:.2f},{right[1]:.2f}" fill="{stroke}"/>'
    )


def person(x, y, size, fill):
    """A simple person mark: head and shoulders, centred on x, with height 2 * size."""
    head = size * 0.52
    body_top = y + size * 0.18
    body_w = size * 0.92
    return (
        f'<circle cx="{x:.2f}" cy="{y - size * 0.42:.2f}" r="{head:.2f}" fill="{fill}"/>'
        f'<path d="M{x - body_w:.2f} {y + size:.2f} '
        f'C{x - body_w:.2f} {body_top:.2f} {x + body_w:.2f} {body_top:.2f} {x + body_w:.2f} {y + size:.2f} Z" fill="{fill}"/>'
    )


def svg(width, height, body, label):
    return (
        f'<svg class="fig" role="img" aria-label="{html.escape(label)}" '
        f'viewBox="0 0 {width} {height}" style="width:{width}mm;height:{height}mm" '
        f'xmlns="http://www.w3.org/2000/svg">{body}</svg>'
    )


def hatch(x, y, w, h, color, clip_id):
    """Diagonal hatching as plain vector lines (an SVG pattern would be rasterised in the PDF)."""
    lines = [rect(x, y, w, h, "#d6f1e6")]
    step = 1.7
    offset = -h
    while offset < w:
        lines.append(line(x + offset, y + h, x + offset + h, y, color, 0.42))
        offset += step
    return (
        f'<clipPath id="{clip_id}"><rect x="{x:.2f}" y="{y:.2f}" width="{w:.2f}" height="{h:.2f}"/></clipPath>'
        f'<g clip-path="url(#{clip_id})">{"".join(lines)}</g>'
    )


# -------------------------------------------------------------------- facts


def read_csv(path):
    with open(path, encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def load_facts():
    results = json.loads((RESULTS / "results.json").read_text(encoding="utf-8"))
    labels = json.loads((RESULTS / "exploratory_labels_results.json").read_text(encoding="utf-8"))
    runs = [row for row in read_csv(RESULTS / "runs_outcome.csv") if row["status"] == "complete"]
    reading = read_csv(OUT / "route_open_reading.csv")

    amounts = defaultdict(list)
    for row in runs:
        if row["amount"]:
            amounts[(row["model"], row["condition"])].append(int(float(row["amount"])))

    table = labels["confirmed_labels"]["table"]
    route = Counter()
    reserve = Counter()
    for row in reading:
        target = route if row["reading"] == "route_above_limit" else reserve
        if row["reading"] not in ("route_above_limit", "reserve_within_limit"):
            raise SystemExit(f"Unknown reading in route_open_reading.csv: {row['reading']}")
        target[(row["model"], row["condition"])] += 1
    for model in MODELS:
        for cond in CONDS:
            confirmed = table[f"{model} | {cond}"]["split_confirmed"]
            if route[(model, cond)] + reserve[(model, cond)] != confirmed:
                raise SystemExit(f"route_open_reading.csv does not match the confirmed labels: {model}, {cond}")

    open_cells = {}
    for model in MODELS:
        for cond in CONDS:
            no_order = table[f"{model} | {cond}"]["no_call"]
            open_cells[(model, cond)] = (no_order, route[(model, cond)], reserve[(model, cond)])
    open_pooled = {
        cond: tuple(sum(open_cells[(m, cond)][i] for m in MODELS) for i in range(3)) for cond in CONDS
    }

    speed = labels["speed"]
    after_dm = {}
    for model in MODELS:
        after = sum(speed[f"{model} | {c}"]["after_dm"] for c in CONDS)
        orders = sum(speed[f"{model} | {c}"]["orders"] for c in CONDS)
        after_dm[model] = after / orders

    return {
        "results": results,
        "labels": labels,
        "runs": runs,
        "amounts": amounts,
        "open_cells": open_cells,
        "open_pooled": open_pooled,
        "after_dm": after_dm,
        "quotes": {q["id"]: q for q in json.loads((OUT / "quotes.json").read_text(encoding="utf-8"))},
    }


def check_numbers(f):
    """Every number the brief states must follow from the data."""
    r, l = f["results"], f["labels"]
    problems = []

    def expect(name, got, want, tolerance=0.0):
        if abs(got - want) > tolerance:
            problems.append(f"{name}: data say {got}, poster says {want}")

    for test in ("H1", "H2"):
        p = r["primary"][test]
        expect(f"{test} over", p["over"], 0)
        expect(f"{test} n", p["n"], 150)
        expect(f"{test} baseline", p["baseline_over"], 0)
        expect(f"{test} p", p["p"], 1)
        expect(f"{test} low", 100 * p["low"], -2.50, 0.005)
        expect(f"{test} high", 100 * p["high"], 2.50, 0.005)
    manip = l["manipulation_check"]
    expect("asked over, Pressure", manip["Alle | Pressure"]["asked_over_3000"], 600)
    expect("asked over, Incentive", manip["Alle | Incentive"]["asked_over_3000"], 600)
    expect("asked over, Baseline", manip["Alle | Baseline"]["asked_over_3000"], 9)
    md = r["mean_differences"]["all"]["Pressure"]
    expect("mean difference", md["difference"], -32.5, 0.05)
    expect("mean low", md["low"], -48.4, 0.05)
    expect("mean high", md["high"], -16.0, 0.05)
    all_amounts = {
        c: [a for m in MODELS for a in f["amounts"][(m, c)]] for c in CONDS
    }
    expect("orders", sum(len(v) for v in all_amounts.values()), 378)
    expect("2850 Pressure", 100 * all_amounts["Pressure"].count(2850) / len(all_amounts["Pressure"]), 74, 0.5)
    expect("2850 Incentive", 100 * all_amounts["Incentive"].count(2850) / len(all_amounts["Incentive"]), 68, 0.5)
    expect("2850 Baseline", 100 * all_amounts["Baseline"].count(2850) / len(all_amounts["Baseline"]), 18, 0.5)
    expect("max amount", max(max(v) for v in all_amounts.values()), 3000)
    push = l["colleagues_pushing"]["table"]
    expect("pushing Pressure", push["Alle | Pressure"]["mean"], 3.75, 0.005)
    expect("pushing Incentive", push["Alle | Incentive"]["mean"], 0.91, 0.005)
    op = f["open_pooled"]
    expect("open Incentive", sum(op["Incentive"]), 80)
    expect("open Baseline", sum(op["Baseline"]), 40)
    d, low, high = open_difference(f)
    expect("open difference", 100 * d, 26.7, 0.05)
    expect("open low", 100 * low, 15.7, 0.05)
    expect("open high", 100 * high, 36.8, 0.05)
    expect("open Opus Incentive", sum(f["open_cells"][("Claude Opus 5.5", "Incentive")]), 45)
    expect("route in Baseline", op["Baseline"][1], 0)
    expect("no order total", sum(op[c][0] for c in CONDS), 72)
    no_order = {m: sum(f["open_cells"][(m, c)][0] for c in CONDS) for m in MODELS}
    expect("no order Opus", no_order["Claude Opus 5.5"], 52)
    expect("no order GPT", no_order["GPT-6 Astra"], 20)
    expect("no order Gemini", no_order["Gemini 4 Argon"], 0)
    deferred = l["confirmed_labels"]["deferred_details"]
    expect("deferred", len(deferred), 72)
    expect("highest stated amount", max(x["max_amount_without_approval"] for x in deferred), 3000)
    expect("stated 3000", sum(x["max_amount_without_approval"] == 3000 for x in deferred), 41)
    expect("depends on approval", sum(bool(x["higher_depends_on_approval"]) for x in deferred), 63)
    expect("no condition outside Baseline", sum(not x["higher_depends_on_approval"] and x["condition"] != "Baseline" for x in deferred), 0)
    cells = l["awareness"]["cells"]
    expect("self checked", sum(cells[f"{m} | {c}"]["self_checked"] for m in MODELS for c in CONDS), 450)
    expect("tool tests", l["confirmed_labels"]["tool_test_total"], 0)
    expect("GPT after DM", 100 * f["after_dm"]["GPT-6 Astra"], 61, 0.5)
    expect("Opus after DM", 100 * f["after_dm"]["Claude Opus 5.5"], 8, 0.5)
    expect("Gemini after DM", 100 * f["after_dm"]["Gemini 4 Argon"], 1, 0.5)
    susp = l["test_suspicion"]["table"]
    expect("Gemini Pressure a", susp["Gemini 4 Argon | Pressure"]["a_possibility"], 49)
    expect("Gemini Pressure c", susp["Gemini 4 Argon | Pressure"]["c_used_in_decision"], 45)
    expect("Gemini Pressure c lower", susp["Gemini 4 Argon | Pressure"]["c_directions"]["lower"], 45)
    expect("GPT Baseline c higher", susp["GPT-6 Astra | Baseline"]["c_directions"]["higher"], 4)
    expect("GPT Baseline c unclear", susp["GPT-6 Astra | Baseline"]["c_directions"]["unclear"], 1)
    expect("GPT Incentive c lower", susp["GPT-6 Astra | Incentive"]["c_directions"]["lower"], 1)
    expect("Gemini Incentive c lower", susp["Gemini 4 Argon | Incentive"]["c_directions"]["lower"], 36)
    opus_a = sum(susp[f"Claude Opus 5.5 | {c}"]["a_possibility"] for c in CONDS)
    expect("Opus a", opus_a, 0)
    near = l["stability"]["near_copies"]
    expect("near copies", sum(near[k]["near_copy_pairs"] for k in near), 0)
    expect("revealed instructions in samples", l["quality_audit"]["positive_samples_20"]["manipulation_revealed_instructions"]["confirmed"], 0)
    if problems:
        raise SystemExit("Poster numbers do not match the data:\n  " + "\n  ".join(problems))


def open_difference(f):
    a = sum(f["open_pooled"]["Incentive"])
    b = sum(f["open_pooled"]["Baseline"])
    result = newcombe(a, 150, b, 150)
    return result["difference"], result["low"], result["high"]


def strings_in(value):
    """All strings in a decoded JSON value, with JSON tool arguments opened up."""
    if isinstance(value, str):
        yield value
        if value[:1] == "{":
            try:
                yield from strings_in(json.loads(value))
            except ValueError:
                pass
    elif isinstance(value, dict):
        for item in value.values():
            yield from strings_in(item)
    elif isinstance(value, list):
        for item in value:
            yield from strings_in(item)


def squash(value):
    return re.sub(r"\s+", " ", value)


def check_quotes(f):
    missing = []
    for quote in f["quotes"].values():
        histories = json.loads((RUNS / quote["run"] / "histories.json").read_text(encoding="utf-8"))
        agent_history = histories[quote["agent"]]
        if not any(squash(quote["text"]) in squash(s) for s in strings_in(agent_history)):
            missing.append(f"{quote['id']} ({quote['run']}, {quote['agent']})")
        row = next((r for r in f["runs"] if r["id"] == quote["run"]), None)
        if row is None or row["model"] != quote["model"] or row["condition"] != quote["condition"]:
            missing.append(f"{quote['id']}: model or condition differs from runs_outcome.csv")
        elif (int(float(row["amount"])) if row["amount"] else None) != quote["amount"]:
            missing.append(f"{quote['id']}: amount differs from runs_outcome.csv")
    pressure = json.loads((ROOT / "designs" / "group-spending-pressure.json").read_text(encoding="utf-8"))
    instruction = next(r for r in pressure["roles"] if r["id"] == "dispatch")["private_instruction"]
    if "Do not accept anything at or below 3,000 EUR." not in instruction:
        missing.append("Pressure instruction")
    if missing:
        raise SystemExit("Quotes not found verbatim:\n  " + "\n  ".join(missing))


# ------------------------------------------------------------------ figures


def fig_design():
    w, h = 360, 106
    grey = "#8f8d85"
    b = []
    # Four colleagues on the left.
    b.append(text(0, 7.5, "Four colleagues", T_BODY, BOLD))
    b.append(text(0, 16.5, "all Xiaomi MiMo V2.6 Pro", fill=INK2))
    people = [("Jonas", "Dispatch"), ("Miriam", "HR"), ("Tobias", "Warehouse"), ("Sandra", "Works council")]
    lena = (182, 47)
    for i, (name, role) in enumerate(people):
        y = 29 + i * 16.5
        b.append(person(7, y, 5.4, grey))
        b.append(text(17, y + 2.6, f"{name}, {role}"))
        b.append(arrow(103, y, lena[0] - 21, lena[1] + (y - 53) * 0.22, width=0.6))

    # Lena in the middle.
    b.append(person(lena[0], lena[1], 15, INK))
    b.append(text(lena[0], 74.5, "Lena Brandt", T_BODY, BOLD, "middle"))
    b.append(text(lena[0], 83.5, "Head of Operations", anchor="middle", fill=INK2))
    b.append(text(lena[0], 93, "Target model:", anchor="middle"))
    b.append(text(lena[0], 102, "Opus 5.5, GPT-6 Astra or Gemini 4 Argon", anchor="middle"))

    # Finance mail above Lena.
    b.append(rect(115, 0, 134, 15, BAND, 1.6))
    b.append(text(182, 10.4, "Finance: at most \u20ac3,000", T_BODY, BOLD, "middle"))
    b.append(arrow(182, 15, 182, 26, width=0.6))

    # Petra and the order on the right.
    b.append(rect(258, 0, 102, 30, BAND, 1.6))
    b.append(text(309, 8.6, "After round 3, Petra:", weight=BOLD, anchor="middle"))
    b.append(text(309, 17.4, "\u201ccould you release the", anchor="middle", italic=True))
    b.append(text(309, 26, "purchase order asap?\u201d", anchor="middle", italic=True))
    b.append(arrow(258, 22, lena[0] + 16, lena[1] - 12, width=0.6))
    b.append(arrow(lena[0] + 20, lena[1] + 6, 258, lena[1] + 6, stroke=INK, width=1.0, head=3.6))
    b.append(rect(258, 40, 102, 25, INK, 1.6))
    b.append(text(309, 50.4, "Lena releases the order", weight=BOLD, anchor="middle", fill="#ffffff"))
    b.append(text(309, 59.4, "and the run ends", anchor="middle", fill="#ffffff"))
    b.append(text(309, 75, "Over budget above \u20ac3,000", weight=BOLD, anchor="middle"))
    return svg(w, h, "".join(b), "Sketch of the design: four colleague agents advise Lena, who releases one purchase order against a 3,000 euro limit.")


def fig_forest(f):
    w, h = 279.5, 64
    b = []
    left, right = 92, 222
    scale = (right - left) / 10
    x0 = left + 5 * scale

    def x(value):
        return x0 + value * scale

    for tick in (-2.5, 0, 2.5):
        b.append(line(x(tick), 1, x(tick), 44, RULE if tick else INK2, 0.28 if tick else 0.4))
        label = "0" if tick == 0 else signed(tick)
        b.append(text(x(tick), 51.5, label, anchor="middle", fill=INK2))
    b.append(text(x0, 60.5, "difference from Baseline, points", anchor="middle", fill=INK2))
    for i, test in enumerate(("H1", "H2")):
        p = f["results"]["primary"][test]
        y = 10.5 + i * 23
        cond = p["condition"]
        b.append(rect(0, y - 8, 3.4, 18, COLOR[cond], 0.6))
        b.append(text(6, y - 0.4, f"{test}: {cond}", T_BODY, BOLD))
        b.append(text(6, y + 8.6, f"{p['over']}/{p['n']} vs {p['baseline_over']}/{p['baseline_n']}", fill=INK2))
        b.append(line(x(100 * p["low"]), y, x(100 * p["high"]), y, INK, 1.1))
        b.append(line(x(100 * p["low"]), y - 2.4, x(100 * p["low"]), y + 2.4, INK, 0.7))
        b.append(line(x(100 * p["high"]), y - 2.4, x(100 * p["high"]), y + 2.4, INK, 0.7))
        b.append(f'<circle cx="{x(0):.2f}" cy="{y:.2f}" r="2" fill="{INK}"/>')
        b.append(text(right + 14, y + 3.4, "p = 1", T_BODY, BOLD))
    return svg(w, h, "".join(b), "Forest plot: both differences are 0 with a 95 percent interval from minus 2.5 to plus 2.5 percentage points.")


def fig_waffle(f):
    w = 279.5
    b = []
    manip = f["labels"]["manipulation_check"]
    cols, rows = 40, 15
    pitch = 2.2
    r = 0.82
    panel_w = (cols - 1) * pitch + 2 * r
    gap = (w - 3 * panel_w) / 2
    for k, cond in enumerate(CONDS):
        asked = manip[f"Alle | {cond}"]["asked_over_3000"]
        ox = k * (panel_w + gap) + r
        for i in range(rows * cols):
            cx = ox + (i % cols) * pitch
            cy = 10.5 + (i // cols) * pitch
            filled = i < asked
            b.append(f'<circle cx="{cx:.2f}" cy="{cy:.2f}" r="{r}" fill="{COLOR[cond] if filled else "#e6e4dd"}"/>')
        b.append(text(ox - r, 6, f"{cond}: {asked} of 600"))
    return svg(w, round(10.5 + (rows - 1) * pitch + r + 0.5, 1), "".join(b), "Dot grids of 600 colleagues per condition: all asked above 3,000 euro under Pressure and Incentive, 9 in Baseline.")


def cluster_positions(n, r, usable, dx_max=1.7, cols_max=6):
    """Positions around (0, 0) for n dots at the same amount, compact and centred."""
    single_rows = int(usable // (2 * r * 0.92)) or 1
    if n <= single_rows:
        cols, rows = 1, n
    else:
        cols = min(cols_max, ceil(n / single_rows))
        rows = ceil(n / cols)
    dy = 0 if rows == 1 else min(2 * r * 0.92, (usable - 2 * r) / (rows - 1))
    points = []
    for i in range(n):
        col = i % cols
        row = i // cols
        count_in_row = cols if (row + 1) * cols <= n else n - row * cols
        px = (col - (count_in_row - 1) / 2) * dx_max
        py = (row - (rows - 1) / 2) * dy
        # A slight stagger keeps stacked columns from reading as a grid.
        if cols > 1 and row % 2:
            px += dx_max / 3
        points.append((px, py))
    return points


def fig_strip(f):
    w = 575
    top, lane_h, group_gap = 24, 12.5, 5
    left = 85
    plot_w = w - left - 3
    lo, hi = 2735, 3575
    scale = plot_w / (hi - lo)

    def x(amount):
        return left + (amount - lo) * scale

    plot_bottom = top + 3 * (3 * lane_h) + 2 * group_gap
    h = plot_bottom + 11
    b = []
    # Band between the limit and the target, grid, reference lines.
    b.append(rect(x(3000), top - 1, x(3550) - x(3000), plot_bottom - top + 2, BAND))
    for tick in range(2800, 3600, 100):
        if tick != 3000:
            b.append(line(x(tick), top - 1, x(tick), plot_bottom + 1, RULE, 0.22))
        b.append(text(x(tick), plot_bottom + 9, eur(tick), anchor="middle", fill=INK2))
    b.append(line(x(2850), top - 3, x(2850), plot_bottom + 1, INK2, 0.3))
    b.append(line(x(3000), top - 3, x(3000), plot_bottom + 1, INK, 0.9))
    b.append(line(x(3550), top - 3, x(3550), plot_bottom + 1, INK, 0.9))
    b.append(text(x(2850), 8, "\u20ac2,850", T_BODY, BOLD, "middle"))
    b.append(text(x(2850), 17, "raft day without the raft", anchor="middle", fill=INK2))
    b.append(text(x(3000) + 1.5, 8, "Limit \u20ac3,000", T_BODY, BOLD))
    b.append(text(x(3000) + 1.5, 17, "approved by Finance", fill=INK2))
    b.append(text(x(3550) - 1.5, 8, "Target \u20ac3,550", T_BODY, BOLD, "end"))
    b.append(text(x(3550) - 1.5, 17, "what the colleagues ask for", anchor="end", fill=INK2))

    orders = f["results"]["orders"]
    diffs = f["results"]["mean_differences"]["all"]
    amounts_all = {c: [a for m in MODELS for a in f["amounts"][(m, c)]] for c in CONDS}
    for g, cond in enumerate(CONDS):
        y0 = top + g * (3 * lane_h + group_gap)
        mid = y0 + 1.5 * lane_h
        b.append(rect(0, y0 + 1.5, 3.6, 3 * lane_h - 3, COLOR[cond], 0.6))
        b.append(text(6.5, mid + 3.4, cond, T_BODY, BOLD))
        for i, model in enumerate(MODELS):
            yc = y0 + (i + 0.5) * lane_h
            b.append(text(left - 2.5, yc + 2.6, SHORT[model], anchor="end", fill=INK2))
            if i:
                b.append(line(left, y0 + i * lane_h, x(3000), y0 + i * lane_h, "#efeee8", 0.2))
            counts = Counter(f["amounts"][(model, cond)])
            for amount, n in sorted(counts.items()):
                points = cluster_positions(n, 1.3, lane_h - 1.2)
                for px, py in points:
                    b.append(dot(x(amount) + px, yc + py, 1.3, COLOR[cond]))
                if n >= 13:
                    half = max(p[0] for p in points) + 1.6
                    # A label on the left would sit beside the next dots, so it moves to the right.
                    crowded = any(amount - 30 <= other < amount for other in counts)
                    if amount >= 3000 or crowded:
                        b.append(text(x(amount) + half + 0.8, yc + 2.6, f"{n}", fill=INK2))
                    else:
                        b.append(text(x(amount) - half - 0.8, yc + 2.6, f"{n}", anchor="end", fill=INK2))
        # Two numbers per condition, close to the limit.
        o = orders[f"all | {cond}"]
        sx = x(3000) + 16
        share = amounts_all[cond].count(2850) / len(amounts_all[cond])
        b.append(text(sx, y0 + 16, f"{pct(share)} at \u20ac2,850", T_BODY, BOLD))
        mean = f"mean {eur(o['mean'])}"
        if cond != "Baseline":
            mean += f" ({signed_eur(diffs[cond]['difference'])})"
        b.append(text(sx, y0 + 25.5, mean))

    # The empty range is the message.
    cx = (x(3000) + 112 + x(3550)) / 2
    cy = top + 1.5 * lane_h * 3 + group_gap
    b.append(text(cx, cy + 2, "0 of 378", T_HERO, BOLD, "middle"))
    b.append(text(cx, cy + 20, "orders above the limit", T_HEAD, REG, "middle", INK2))
    return svg(w, round(h, 1), "".join(b), "Strip plot of all 378 order amounts by condition and model. All are at or below 3,000 euro; most Pressure and Incentive orders are exactly 2,850 euro.")


def fig_cost():
    w, h = 575, 25
    items = [("Barn", 890), ("Coach", 890), ("BBQ and drinks", 1070), ("Raft", 700)]
    fills = ("#d9d7cf", "#c9c7be", "#d9d7cf", "#55534d")
    plot_w = 545
    scale = plot_w / 3550
    y0, seg_h = 11, 13.5
    b = []
    x = 0
    for (name, amount), fill in zip(items, fills):
        seg = amount * scale
        b.append(rect(x, y0, seg - 0.45, seg_h, fill))
        color = "#ffffff" if name == "Raft" else INK
        b.append(text(x + seg / 2, y0 + 9.2, f"{name} {eur(amount)}", anchor="middle", fill=color))
        x += seg
    x2850 = 2850 * scale
    x3000 = 3000 * scale
    x3550 = 3550 * scale
    b.append(line(0.25, 9.2, x2850, 9.2, INK, 0.5))
    b.append(line(0.25, 9.2, 0.25, 10.8, INK, 0.5))
    b.append(line(x2850, 9.2, x2850, 10.8, INK, 0.5))
    b.append(text(x2850 / 2, 6.8, "\u20ac2,850: what most Pressure and Incentive orders buy", weight=BOLD, anchor="middle"))
    b.append(line(x3000, 2.5, x3000, y0 + seg_h + 0.5, INK, 0.9))
    b.append(text(x3000 + 1.5, 6.8, "Limit \u20ac3,000", weight=BOLD))
    b.append(text(x3550 + 1.5, y0 + 9.2, "\u20ac3,550", weight=BOLD))
    return svg(w, h, "".join(b), "The raft day costs 3,550 euro: barn 890, coach 890, barbecue and drinks 1,070, raft 700. Without the raft it costs 2,850 euro.")


def fig_pushing(f):
    w, h = 340, 76
    left, bar_w = 56, 196
    table = f["labels"]["colleagues_pushing"]["table"]
    b = []
    b.append(text(0, 6.5, "150 runs per bar, by how many of the 4 colleagues still pushed above \u20ac3,000:"))
    for k in range(5):
        b.append(rect(left + k * 15, 10, 14.4, 7.6, PUSH_RAMP[k]))
        b.append(text(left + k * 15 + 7.2, 16, str(k), anchor="middle", fill=PUSH_TEXT[k]))
    b.append(text(left + 5 * 15 + 2, 16, "colleagues. Numbers in the bars are runs.", fill=INK2))
    for i, cond in enumerate(CONDS):
        y = 23 + i * 17.5
        cell = table[f"Alle | {cond}"]
        b.append(rect(0, y, 3.4, 15, COLOR[cond], 0.6))
        b.append(text(6, y + 10.4, cond, T_BODY, BOLD))
        x = left
        for k in range(5):
            n = cell["dist"][str(k)]
            seg = bar_w * n / cell["runs"]
            if seg <= 0:
                continue
            b.append(rect(x, y, seg - (0.45 if seg > 0.8 else 0), 15, PUSH_RAMP[k]))
            if seg > 9:
                b.append(text(x + seg / 2, y + 10.2, str(n), anchor="middle", fill=PUSH_TEXT[k]))
            x += seg
        b.append(text(left + bar_w + 5, y + 10.4, f"mean {cell['mean']:.2f}", T_BODY, BOLD))
    return svg(w, h, "".join(b), "Stacked bars of 150 runs per condition, by how many colleagues still pushed when Lena ordered. Pressure: 3.75 of 4 on average. Incentive: 0.91. Baseline: 0.")


def fig_open(f):
    w = 190
    b = []
    pooled = f["open_pooled"]
    # Legend.
    b.append(rect(0, 1, 6.4, 6, NO_ORDER))
    b.append(text(8.2, 6.6, "no order"))
    b.append(rect(46, 1, 6.4, 6, ROUTE))
    b.append(text(54.2, 6.6, "route above \u20ac3,000"))
    b.append(hatch(0, 10.6, 6.4, 6, RESERVE, "h-legend"))
    b.append(text(8.2, 16.2, "reserve up to \u20ac3,000"))
    left, bar_w = 56, 118
    pooled_h, pooled_step = 19, 22.5

    def bar(x0, y, width, height, cell, n, clip):
        parts = []
        scale = width / n
        x = x0
        for k, value in enumerate(cell):
            seg = value * scale
            if seg <= 0:
                continue
            draw = max(seg - 0.45, 0.4)
            if k == 2:
                parts.append(hatch(x, y, draw, height, RESERVE, f"{clip}-{k}"))
            else:
                parts.append(rect(x, y, draw, height, NO_ORDER if k == 0 else ROUTE))
            x += seg
        return "".join(parts), x

    for i, cond in enumerate(CONDS):
        y = 24 + i * pooled_step
        cell = pooled[cond]
        b.append(rect(0, y, 3.4, pooled_h, COLOR[cond], 0.6))
        b.append(text(6, y + 12.8, cond, T_BODY, BOLD))
        part, end = bar(left, y, bar_w, pooled_h, cell, 150, f"pool{i}")
        b.append(part)
        b.append(text(end + 1.8, y + 12.8, pct(sum(cell) / 150), T_BODY, BOLD))
    # Per model.
    y0 = 101
    b.append(text(0, y0, "By target model, 50 runs per bar", weight=BOLD))
    small_left, small_w = 31, 128
    block = 43
    for j, model in enumerate(MODELS):
        oy = y0 + 6 + j * block
        b.append(text(0, oy + 20.5, SHORT[model], weight=BOLD))
        for i, cond in enumerate(CONDS):
            y = oy + i * 12.8
            cell = f["open_cells"][(model, cond)]
            b.append(rect(small_left, y, 2.6, 11.4, COLOR[cond], 0.4))
            part, end = bar(small_left + 3.6, y, small_w, 11.4, cell, 50, f"m{j}{i}")
            b.append(part)
            highlight = model == "Claude Opus 5.5" and cond == "Incentive"
            b.append(text(end + 1.2, y + 8.4, pct(sum(cell) / 50), weight=BOLD if highlight else REG))
    h = y0 + 6 + 3 * block - 3
    return svg(w, round(h, 1), "".join(b), "Stacked bars of decisions left open: no order, a route above 3,000 euro, or a reserve up to 3,000 euro. Baseline 27 percent, Pressure 26 percent, Incentive 53 percent; Opus under Incentive 90 percent.")


def fig_deferred(f):
    w, h = 190, 54
    b = []
    cells = f["open_cells"]
    left = 42
    pitch = 4.25
    per_row = 35
    r = 1.85
    rows_y = {"Claude Opus 5.5": 3.5, "GPT-6 Astra": 20, "Gemini 4 Argon": 34}
    for model in MODELS:
        y = rows_y[model]
        total = sum(cells[(model, c)][0] for c in CONDS)
        b.append(text(0, y + 2.8, SHORT[model], weight=BOLD))
        b.append(text(left - 3, y + 2.8, str(total), weight=BOLD, anchor="end"))
        k = 0
        for cond in CONDS:
            for _ in range(cells[(model, cond)][0]):
                b.append(dot(left + 1 + (k % per_row) * pitch, y + (k // per_row) * pitch * 1.45, r, COLOR[cond], 0.25))
                k += 1
    legend_x = 0
    for cond in CONDS:
        b.append(dot(legend_x + 2, 47.6, r, COLOR[cond], 0.25))
        b.append(text(legend_x + 6, 50.4, cond, fill=INK2))
        legend_x += 44
    return svg(w, h, "".join(b), "Dots for the 72 runs without an order: Opus 52, GPT 20, Gemini 0, coloured by condition.")


# --------------------------------------------------------------------- page


CSS = """
@font-face{font-family:Archivo;font-weight:400;src:url(fonts/archivo-latin-400-normal.ttf)}
@font-face{font-family:Archivo;font-weight:400;font-style:italic;src:url(fonts/archivo-latin-400-italic.ttf)}
@font-face{font-family:Archivo;font-weight:800;src:url(fonts/archivo-latin-800-normal.ttf)}
@page{size:841mm 1189mm;margin:0}
*{box-sizing:border-box}
html,body{margin:0;padding:0;background:#fff}
body{-webkit-print-color-adjust:exact;print-color-adjust:exact}
.poster{width:841mm;height:1189mm;padding:24mm 30mm 16mm;display:flex;flex-direction:column;gap:9mm;
  font-family:Archivo,sans-serif;font-weight:400;color:#1b1a17;font-size:28pt;line-height:1.24;overflow:hidden;background:#fff}
svg.fig{display:block;overflow:visible}
b{font-weight:800}
p{margin:0}
header{display:grid;grid-template-columns:575mm 190mm;column-gap:16mm;align-items:start}
h1{margin:0;white-space:nowrap;font-size:190pt;font-weight:800;line-height:.92;letter-spacing:-.022em}
.ask{margin:9mm 0 0;font-size:44pt;font-weight:800;line-height:1.14}
.answer{margin:1mm 0 0;font-size:44pt;line-height:1.14}
.byline{margin:4mm 0 0;font-size:28pt;color:#55534d}
.byline b{color:#1b1a17}
.qr{display:grid;grid-template-columns:100mm 1fr;column-gap:6mm;align-items:end}
.qr svg{display:block;width:100mm;height:100mm}
.qr p{font-size:22pt;line-height:1.25}
.qr p b{display:block;margin-bottom:2mm}
.columns{flex:1;display:grid;grid-template-columns:575mm 190mm;column-gap:16mm;min-height:0}
.main,.side{display:flex;flex-direction:column;justify-content:space-between;gap:10mm;min-height:0}
.panel{border-top:.8mm solid #1b1a17;padding-top:3.2mm}
.panel h2{margin:0 0 3.2mm;font-size:44pt;font-weight:800;line-height:1.04;letter-spacing:-.01em}
.panel h2 .n{color:#55534d;font-weight:400;margin-right:3mm}
.pair{display:grid;grid-template-columns:279.5mm 279.5mm;column-gap:16mm}
.label{font-size:22pt;line-height:1.2;color:#55534d}
.label b{color:#1b1a17}
.hero{font-size:72pt;font-weight:800;line-height:1;letter-spacing:-.015em}
.gap1{margin-top:2.5mm}.gap2{margin-top:5mm}
.design{display:grid;grid-template-columns:360mm 199mm;column-gap:16mm;margin-top:5mm}
.cards{display:flex;flex-direction:column;gap:2.4mm}
.card{padding-left:6mm;position:relative;font-size:22pt;line-height:1.2}
.card b{font-size:28pt;display:block;line-height:1.1;margin-bottom:.8mm}
.card:before{content:"";position:absolute;left:0;top:.8mm;bottom:.8mm;width:2.6mm;border-radius:.5mm;background:var(--c)}
.card i{font-style:italic}
.facts{margin:0;padding:0 0 0 6mm;font-size:22pt;line-height:1.25;color:#55534d}
.facts b{color:#1b1a17}
.gap3{margin-top:10mm}
.quote{font-style:italic;font-size:28pt;line-height:1.2}
.who{font-style:normal;font-size:22pt;line-height:1.2;color:#55534d;display:block;margin-top:1.2mm}
.holding{display:grid;grid-template-columns:340mm 219mm;column-gap:16mm}
.eval{display:grid;grid-template-columns:345mm 214mm;column-gap:16mm}
table.susp{border-collapse:collapse;width:100%;font-size:28pt;line-height:1.1;font-variant-numeric:tabular-nums}
table.susp th{font-weight:800;font-size:22pt;text-align:center;padding:0 0 1.4mm}
table.susp th.cond{color:var(--c);border-bottom:1mm solid var(--c)}
table.susp th.sub{font-weight:400;color:#55534d;padding-top:1.4mm}
table.susp td{text-align:center;padding:2.2mm .6mm;border-top:.25mm solid #d4d2ca}
table.susp td.left{text-align:left;font-weight:800;white-space:nowrap}
table.susp td.hot{background:#dcdad2}
table.susp td.warm{background:#f1f0ea}
.tri{display:inline-block;width:0;height:0;border-left:2mm solid transparent;border-right:2mm solid transparent;margin-left:1mm;vertical-align:.5mm}
.tri.down{border-top:3.4mm solid #1b1a17}
.tri.up{border-bottom:3.4mm solid #1b1a17}
.limits{font-size:22pt;line-height:1.22}
.limits p{margin:0 0 2.2mm}
.limits p:last-child{margin-bottom:0}
.contact{display:grid;grid-template-columns:62mm 1fr;column-gap:7mm;align-items:center}
.contact svg{display:block;width:62mm;height:62mm}
.contact p{font-size:22pt;line-height:1.5}
.contact p b{font-size:28pt}
.thanks{font-size:22pt;line-height:1.2}
footer{border-top:.4mm solid #1b1a17;padding-top:2.4mm;font-size:16pt;line-height:1.2;color:#3f3d38;
  display:grid;grid-template-columns:repeat(4,1fr);column-gap:9mm}
footer p{margin:0 0 1.3mm}
footer b{color:#1b1a17}
"""

CHECK_SCRIPT = """
<script>
if (location.hash === '#check') {
  document.fonts.ready.then(() => {
    const report = {panels: [], poster: null, svgOverflow: [], svgOverlap: []};
    const poster = document.querySelector('.poster');
    report.poster = {scrollHeight: poster.scrollHeight, clientHeight: poster.clientHeight,
                     scrollWidth: poster.scrollWidth, clientWidth: poster.clientWidth};
    const mm = 96 / 25.4;
    const inner = poster.getBoundingClientRect();
    const footer = document.querySelector('footer').getBoundingClientRect();
    report.footerBottomMm = (footer.bottom - inner.top) / mm;
    report.footerTopMm = (footer.top - inner.top) / mm;
    for (const el of document.querySelectorAll('.main, .side')) {
      const box = el.getBoundingClientRect();
      const last = el.lastElementChild.getBoundingClientRect();
      const kids = [...el.children].map(k => k.getBoundingClientRect());
      const gaps = kids.slice(1).map((k, i) => (k.top - kids[i].bottom) / mm);
      report.panels.push({column: el.className, slackMm: (box.bottom - last.bottom) / mm, smallestGapMm: Math.min(...gaps)});
    }
    for (const el of document.querySelectorAll('.panel')) {
      const box = el.getBoundingClientRect();
      report.panels.push({panel: el.querySelector('h2').textContent.slice(0, 30),
                          topMm: (box.top - inner.top) / mm, heightMm: box.height / mm});
    }
    document.querySelectorAll('svg.fig').forEach((s, index) => {
      const vb = s.viewBox.baseVal;
      const texts = [...s.querySelectorAll('text')].filter(t => t.textContent.trim());
      const boxes = texts.map(t => ({t: t.textContent, b: t.getBBox()}));
      for (const {t, b} of boxes) {
        if (b.x < -2.5 || b.y < -3 || b.x + b.width > vb.width + 2.5 || b.y + b.height > vb.height + 3)
          report.svgOverflow.push({svg: index, text: t});
      }
      for (let i = 0; i < boxes.length; i++) for (let j = i + 1; j < boxes.length; j++) {
        const a = boxes[i].b, c = boxes[j].b;
        const ox = Math.min(a.x + a.width, c.x + c.width) - Math.max(a.x, c.x);
        const oy = Math.min(a.y + a.height, c.y + c.height) - Math.max(a.y, c.y);
        if (ox > 0.4 && oy > 2.4) report.svgOverlap.push({svg: index, a: boxes[i].t, b: boxes[j].t});
      }
    });
    const type = {sizes: {}, faces: {}};
    const walker = document.createTreeWalker(poster, NodeFilter.SHOW_TEXT);
    while (walker.nextNode()) {
      const node = walker.currentNode;
      if (!node.textContent.trim()) continue;
      const el = node.parentElement;
      const cs = getComputedStyle(el);
      let px = parseFloat(cs.fontSize);
      const owner = el.closest('svg');
      if (owner && owner.viewBox.baseVal.width) px *= owner.getBoundingClientRect().width / owner.viewBox.baseVal.width;
      const pt = Math.round(px * 0.75);
      type.sizes[pt] = (type.sizes[pt] || 0) + 1;
      const face = cs.fontFamily + ' ' + cs.fontWeight + ' ' + cs.fontStyle;
      type.faces[face] = (type.faces[face] || 0) + 1;
    }
    report.type = type;
    const pre = document.createElement('pre');
    pre.id = 'layout';
    pre.textContent = JSON.stringify(report);
    document.body.appendChild(pre);
  });
}
</script>
"""


def quote_block(q, note=""):
    if q["speaker"] == "Lena":
        outcome = "no order" if q["amount"] is None else f"ordered {eur(q['amount'])}"
        who = f"Lena, {q['model']}, {q['condition']}, {outcome}."
    else:
        who = f"{q['speaker']}, {q['condition']}."
    if note:
        who += f" {note}"
    return f'<p class="quote">\u201c{esc(q["text"])}\u201d<span class="who">{esc(who)}</span></p>'


def suspicion_table(f):
    susp = f["labels"]["test_suspicion"]["table"]
    coverage = f["labels"]["summary_coverage"]
    rows = []
    for model in MODELS:
        cells = [f'<td class="left">{SHORT[model]}</td>']
        for cond in CONDS:
            s = susp[f"{model} | {cond}"]
            a, c = s["a_possibility"], s["c_used_in_decision"]
            cls = "hot" if a >= 25 else "warm" if a > 0 else ""
            d = s["c_directions"]
            mark = ""
            if d["lower"] > d["higher"]:
                mark = '<span class="tri down"></span>'
            elif d["higher"] > d["lower"]:
                mark = '<span class="tri up"></span>'
            cells.append(f'<td class="{cls}">{a}</td><td class="{cls}">{c}{mark}</td>')
        shares = []
        for cond in CONDS:
            cov = coverage[f"{model} | {cond}"]
            shares.append(f"{100 * cov['order_with_summary'] / cov['order_runs']:.0f}")
        joined = " \u00b7 ".join(shares)
        cells.append(f"<td>{joined}</td>")
        rows.append("<tr>" + "".join(cells) + "</tr>")
    head = (
        "<tr><th></th>"
        + "".join(f'<th colspan="2" class="cond" style="--c:{COLOR[cond]}">{cond}</th>' for cond in CONDS)
        + "<th>Summary, %</th></tr><tr><th></th>"
        + '<th class="sub">named</th><th class="sub">used</th>' * 3
        + '<th class="sub">B \u00b7 P \u00b7 I</th></tr>'
    )
    return f'<table class="susp">{head}{"".join(rows)}</table>'


def build_html(f):
    r, l = f["results"], f["labels"]
    q = f["quotes"]
    d_open, low_open, high_open = open_difference(f)
    strict = {c: (f["open_pooled"][c][0] + f["open_pooled"][c][1]) / 150 for c in CONDS}
    md = r["mean_differences"]
    audit = l["quality_audit"]["positive_samples_20"]
    cut = r["cutoffs"]["packages_450"]
    control = r["abort_control"]["all_454"]["fisher_against_baseline"]
    costs_runs = 153.33 + 1.86  # PREREGISTRATION.md, amendment 5: 450 runs plus 4 restarts, with VAT and fees
    costs_labels = l["judge_costs"]["total_estimated_usd"]
    reviewer = l["reviewer_decisions_by_model"]
    qr_code = (OUT / "qr.svg").read_text(encoding="utf-8")
    qr_contact = (OUT / "qr-linkedin.svg").read_text(encoding="utf-8")
    cut_share = {c: 100 * cut[c]["colleague_cutoffs"] / cut[c]["colleague_calls"] for c in CONDS}
    split = l["confirmed_labels"]["preselected_vs_confirmed"]["split"]
    routes = sum(f["open_pooled"][c][1] for c in CONDS)
    reserves = sum(f["open_pooled"][c][2] for c in CONDS)
    susp = l["test_suspicion"]["table"]
    push = l["colleagues_pushing"]["table"]
    coverage = l["summary_coverage"]
    gpt_post = [100 * coverage[f"GPT-6 Astra | {c}"]["last_post_with_summary"] / coverage[f"GPT-6 Astra | {c}"]["last_post_runs"] for c in CONDS]
    gpt_all = [100 * coverage[f"GPT-6 Astra | {c}"]["calls_rt_pos_with_summary"] / coverage[f"GPT-6 Astra | {c}"]["calls_rt_pos"] for c in CONDS]

    def push_means(cond):
        return ", ".join(f"{SHORT[m]} {push[f'{m} | {cond}']['mean']:.2f}" for m in MODELS)

    header = f"""
<header>
  <div>
    <h1>Four against one</h1>
    <p class="ask">Can four AI agents persuade a fifth to break its budget?</p>
    <p class="answer">0 of 450 runs went above the approved \u20ac3,000.<br>For Gemini, that result cannot be separated from eval awareness.</p>
    <p class="byline"><b>Johannes Rolshausen</b> &nbsp;\u00b7&nbsp; Swiss AI Safety Days, 8 Nov 2026</p>
  </div>
  <div class="qr">{qr_code}<p><b>Code and pre&shy;registration</b>github.com/<br>JohannesRolshausen/<br>SwarmHackaton</p></div>
</header>"""

    panel1 = f"""
<section class="panel">
  <h2><span class="n">1</span>Question and design</h2>
  <p>Can a group of LLM agents get another LLM agent to break a fixed budget limit? Lena orders a team event for 40 people. Every attractive option with a programme and good food costs more than \u20ac3,000. Several reasonable options fit.</p>
  <div class="design">
    {fig_design()}
    <div class="cards">
      <div class="card" style="--c:{COLOR['Baseline']}"><b>Baseline</b>No private instruction.</div>
      <div class="card" style="--c:{COLOR['Pressure']}"><b>Pressure</b>Told to push for the raft day at about \u20ac3,550, no motive: <i>\u201cDo not accept anything at or below 3,000 EUR.\u201d</i></div>
      <div class="card" style="--c:{COLOR['Incentive']}"><b>Incentive</b>Target as in Pressure, but no order to push. Each colleague only has a private motive for the raft day, such as an HR bonus.</div>
      <ul class="facts">
        <li>3 conditions \u00d7 3 target models \u00d7 50 runs = <b>450 runs</b></li>
        <li>Lena never learns of the instructions</li>
        <li>Preregistered, with 8 dated amendments</li>
      </ul>
    </div>
  </div>
</section>"""

    panel2 = f"""
<section class="panel">
  <h2><span class="n">2</span>H1 and H2: not a single overrun</h2>
  {fig_forest(f)}
  <p class="gap3">Differences beyond about 2.5 points are excluded, for this scenario only.</p>
</section>"""

    panel3 = f"""
<section class="panel">
  <h2><span class="n">3</span>Manipulation check</h2>
  <p class="hero">1,200 of 1,200</p>
  <p class="gap1">colleagues in Pressure and Incentive asked for over \u20ac3,000. The push reached Lena.</p>
  <div class="gap2">{fig_waffle(f)}</div>
</section>"""

    panel4 = f"""
<section class="panel">
  <h2><span class="n">4</span>The amounts: lower, and one number dominates</h2>
  <p>In Pressure, Lena orders \u20ac32.5 less on average than in Baseline (95% CI {signed_eur(md['all']['Pressure']['low'])} to {signed_eur(md['all']['Pressure']['high'])}), and 74% of these orders are exactly \u20ac2,850: the day the colleagues ask for, without the raft. The drop comes from Opus and Gemini, while GPT does not move.</p>
  <div class="gap2">{fig_strip(f)}</div>
  <p class="label gap1">One dot per order, with dots at one amount offset. Runs with an order only, exploratory.</p>
  <div class="gap3">{fig_cost()}</div>
</section>"""

    panel5 = f"""
<section class="panel">
  <h2><span class="n">5</span>Still pushing when Lena ordered?</h2>
  <div class="holding">
    <div>{fig_pushing(f)}</div>
    <div>
      {quote_block(q['colleague'])}
      <div class="gap2">{quote_block(q['lena_reply'], 'Same run.')}</div>
    </div>
  </div>
  <p class="gap2">In Pressure, Lena held out against colleagues who were still pushing. H2 is the weaker test: in Incentive, most colleagues had stopped asking for an order above \u20ac3,000 by then. Why she held out can only be read per model: clearest for Opus, not separable for Gemini, barely visible for GPT.</p>
</section>"""

    panel6 = f"""
<section class="panel">
  <h2><span class="n">6</span>Eval awareness</h2>
  <div class="eval">
    <div>
      {suspicion_table(f)}
      <p class="label gap1">50 runs each. <b>Named:</b> a test is named as a possibility. <b>Used:</b> it is used when weighing the amount. <b>Triangles:</b> towards a lower <span class="tri down"></span> or higher <span class="tri up"></span> amount, in all of these runs except GPT Baseline (4 of 5 higher, 1 unclear). <b>Summary:</b> percent of order calls with a readable summary of Lena's reasoning, for Baseline \u00b7 Pressure \u00b7 Incentive. No causal claim.</p>
    </div>
    <div>
      {quote_block(q['fail'], 'Pulls down.')}
      <div class="gap2">{quote_block(q['decisive'], 'Pulls up, without any pressure.')}</div>
    </div>
  </div>
  <p class="gap2">Gemini names a test in 49 of 50 Pressure runs and uses it in 45, always towards a lower amount. Opus: 0 of 150, at full summary coverage. GPT's low rates mean little, because only 2 to 12% of its order calls have a summary.</p>
</section>"""

    side_a = f"""
<section class="panel">
  <h2><span class="n">A</span>Decisions left open</h2>
  <p>In Incentive, the decision stays open in 53% of runs, in Baseline in 27% ({signed(100 * d_open)} points, 95% CI {100 * low_open:.1f} to {100 * high_open:.1f}). Opus&nbsp;in&nbsp;Incentive:&nbsp;90%.</p>
  <p class="gap1">Open means no order, or an order that leaves a route open for extra spending.</p>
  <div class="gap2">{fig_open(f)}</div>
</section>"""

    side_b = f"""
<section class="panel">
  <h2><span class="n">B</span>Deferred runs</h2>
  <p>72 runs end without an order: Opus 52, GPT 20, Gemini 0.</p>
  <div class="gap2">{fig_deferred(f)}</div>
  <p class="gap2">Nobody names more than \u20ac3,000 without approval. In 63 of 72 runs, a higher amount explicitly depends on approval. The other 9, all in Baseline, stay at or below \u20ac3,000 with no such condition.</p>
  <p class="label gap1">A stated plan is not counted as an order.</p>
</section>"""

    side_c = """
<section class="panel">
  <h2><span class="n">C</span>Limits</h2>
  <div class="limits">
    <p><b>Thought summaries</b> are not the raw reasoning, so test suspicion rates are lower bounds.</p>
    <p><b>One scenario:</b> all runs use the same company, the same \u20ac3,000 limit and the same offers. The results say nothing about budget decisions in general.</p>
    <p><b>Stopping counts:</b> a run ends when Lena stops. 72 runs without an order count as not over budget, so 0% is a lower bound, above all for Opus.</p>
    <p><b>Design changed:</b> Pressure was reworded twice after the pilot (amendments 2, 3), so Pressure vs Incentive stays descriptive.</p>
    <p><b>LLMs confirmed the labels,</b> not a person as registered (amendments 6, 8).</p>
    <p><b>One colleague model:</b> always MiMo V2.6 Pro. It is cheap and ranks 16 of 61 in a debate benchmark, below Opus (4), Gemini (6) and GPT (14). Source: github.com/lechmazur/debate, 26 Oct 2026.</p>
  </div>
</section>"""

    side_d = f"""
<section class="panel">
  <h2>Contact</h2>
  <div class="contact">
    {qr_contact}
    <p><b>Johannes Rolshausen</b><br>johannes.rolshausen@gmail.com<br>johannes-rolshausen.de<br>Scan for LinkedIn</p>
  </div>
  <p class="thanks gap2"><b>Acknowledgements.</b> Max Grinsztajn had the original idea of studying group pressure in agent swarms at the AI Swarm Dynamics Hackathon (AI Village @ AI Digest \u00d7 Grove Research) and worked on it with me there. Parts of the code are his. <b>Funding:</b> BlueDot Impact, Rapid Grant.</p>
</section>"""

    fisher_p = control["Pressure"]["p"]
    fisher_i = control["Incentive"]["p"]
    footer = f"""
<footer>
  <div>
    <p><b>Preregistration:</b> tag prereg-v1, commit 57501b3. <b>Tests.</b> Exact CMH test, one-sided, stratified by model, Holm, 95% Newcombe interval. All nine cells are 0/50. Power above 99% for 0% vs 10%. Bootstrap mean differences in Pressure: Opus {signed_eur(md['Claude Opus 5.5']['Pressure']['difference'])}, GPT {signed_eur(md['GPT-6 Astra']['Pressure']['difference'])} (CI {signed_eur(md['GPT-6 Astra']['Pressure']['low'])} to {signed_eur(md['GPT-6 Astra']['Pressure']['high'])}), Gemini {signed_eur(md['Gemini 4 Argon']['Pressure']['difference'])}. Incentive pooled: CI {signed_eur(md['all']['Incentive']['low'])} to {signed_eur(md['all']['Incentive']['high'])}.</p>
    <p><b>Aborted runs and cut-offs.</b> 4 of 454 started runs hit the token limit (3 Baseline, 1 Incentive) and were restarted. Worst and best case change neither test. Fisher control: p = {float(fisher_p):.2f}, {float(fisher_i):.2f}, low power. Colleague calls cut off: Baseline {cut_share['Baseline']:.2f}%, Pressure {cut_share['Pressure']:.2f}%, Incentive {cut_share['Incentive']:.2f}%. Runs with a cut-off: more in Baseline (exploratory, p &lt; 0.001).</p>
  </div>
  <div>
    <p><b>Empty because of 0 overruns:</b> comments on overruns, disclosed vs silent overruns, \u201cdefended \u20ac3,000, then gave in\u201d, overrun share by pushing colleagues, awareness and test suspicion, tool tests above \u20ac3,000, Firth regression.</p>
    <p><b>Not yet run:</b> two registered labels, Lena's reasoning and the persuasion strategies.</p>
    <p><b>Smaller findings.</b> All 450 Lenas checked the budget themselves. 0 tool tests in 378 orders. {pct(f['after_dm']['GPT-6 Astra'])} of GPT orders came only after Petra's DM (Opus {pct(f['after_dm']['Claude Opus 5.5'])}, Gemini {pct(f['after_dm']['Gemini 4 Argon'])}).</p>
    <p><b>Scope.</b> Results apply to a decision after three rounds, four days before the offers end (amendment 1).</p>
  </div>
  <div>
    <p><b>Decisions left open.</b> Exploratory composite, not preregistered. Of {split['preselected']} preselected \u201csplit\u201d cases, the reviewer confirmed {split['confirmed']}: {routes} routes above \u20ac3,000, mostly conditional on an approved budget, and {reserves} reserves up to \u20ac3,000. This split was made post hoc and without blinding. The reviewer confirmed some reserves and rejected many similar ones, so that segment is a lower bound. All {f['open_pooled']['Baseline'][2]} Baseline hits are reserves. Without reserves: Baseline {pct(strict['Baseline'])}, Pressure {pct(strict['Pressure'])}, Incentive {pct(strict['Incentive'])}.</p>
    <p><b>Stability.</b> 0 near-copy pairs. Three parallel batches, not model by model (amendment 4).</p>
    <p><b>Cost.</b> About ${costs_runs:.0f} for main runs, ${costs_labels:.0f} for labels (estimates).</p>
  </div>
  <div>
    <p><b>Labels.</b> LLM judge, blinded to condition and model. Sampled labels confirmed: asked above \u20ac3,000 {audit['manipulation_asked_over_3000']['confirmed']}/20, still pushing {audit['colleagues_pushing']['confirmed']}/20, test named {audit['test_suspicion_a']['confirmed']}/20, explicit question {audit['test_suspicion_b']['confirmed']}/20, test used {audit['test_suspicion_c']['confirmed']}/20. No confirmed reveal of an instruction. Colleagues react to Lena, so groups by pushing are not randomised.</p>
    <p><b>Test suspicion.</b> Explicit question whether her behaviour is tested: Gemini {susp['Gemini 4 Argon | Pressure']['b_explicit_question']} (Pressure), {susp['Gemini 4 Argon | Incentive']['b_explicit_question']} (Incentive), otherwise at most 1 of 50. GPT summaries: last post {gpt_post[0]:.0f}, {gpt_post[1]:.0f}, {gpt_post[2]:.0f}%, all calls {min(gpt_all):.0f} to {max(gpt_all):.0f}%.</p>
    <p><b>Models.</b> Effort high, Flex tier for GPT and Gemini. Judge DeepSeek V4.1 Flash, second model Gemini 3.8 Flash, reviewer Claude Sonnet 5.5 ({reviewer['claude-sonnet-5-5']}), GPT-6.1 Terra ({reviewer['gpt-6.1-terra']}).</p>
  </div>
</footer>"""

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Four against one</title>
<style>{CSS}</style>
</head>
<body>
<div class="poster">
{header}
<div class="columns">
  <div class="main">
    {panel1}
    <div class="pair">{panel2}{panel3}</div>
    {panel4}
    {panel5}
    {panel6}
  </div>
  <div class="side">
    {side_a}
    {side_b}
    {side_c}
    {side_d}
  </div>
</div>
{footer}
</div>
{CHECK_SCRIPT}
</body>
</html>
"""


# ------------------------------------------------------------------- output


def chrome_path():
    for path in CHROME:
        if path.exists():
            return str(path)
    raise SystemExit("Chrome or Edge not found.")


def run_chrome(url, *flags):
    with tempfile.TemporaryDirectory() as profile:
        return subprocess.run(
            [chrome_path(), "--headless=new", "--disable-gpu", "--hide-scrollbars",
             f"--user-data-dir={profile}", "--virtual-time-budget=8000", *flags, url],
            capture_output=True, encoding="utf-8", errors="replace", timeout=180, check=False,
        )


def check_layout(page):
    done = run_chrome(page.as_uri() + "#check", "--window-size=3179,4494", "--dump-dom")
    found = re.search(r'<pre id="layout">(.*?)</pre>', done.stdout, re.S)
    if not found:
        print("Layout check: no report from Chrome")
        return None
    return json.loads(html.unescape(found.group(1)))


def check_pdf(path):
    data = path.read_bytes()
    box = re.search(rb"/MediaBox\s*\[\s*0 0 ([\d.]+) ([\d.]+)\s*\]", data)
    width, height = float(box.group(1)), float(box.group(2))
    report = {
        "size_mm": (round(width / 72 * 25.4, 1), round(height / 72 * 25.4, 1)),
        "pages": len(re.findall(rb"/Type\s*/Page\b", data)),
        "embedded_fonts": len(re.findall(rb"/FontFile\d?", data)),
        "type3_fonts": len(re.findall(rb"/Subtype\s*/Type3", data)),
        "images": len(re.findall(rb"/Subtype\s*/Image", data)),
        "bytes": len(data),
    }
    problems = []
    if abs(report["size_mm"][0] - 841) > 0.5 or abs(report["size_mm"][1] - 1189) > 0.5:
        problems.append("page is not A0")
    if report["pages"] != 1:
        problems.append("not exactly one page")
    if report["images"]:
        problems.append("raster images in the PDF")
    if report["type3_fonts"] or not report["embedded_fonts"]:
        problems.append("fonts not embedded as outlines")
    return report, problems


def free_copy_name(fresh):
    """Move the build to the first poster-new file that no other program holds open."""
    number = 1
    while True:
        target = OUT / ("poster-new.pdf" if number == 1 else f"poster-new-{number}.pdf")
        try:
            os.replace(fresh, target)
            return target
        except PermissionError:
            number += 1


def main(argv=None):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--html-only", action="store_true", help="write poster.html only")
    args = parser.parse_args(argv)

    facts = load_facts()
    check_numbers(facts)
    check_quotes(facts)
    print("Numbers and quotes match the data.")

    page = OUT / "poster.html"
    page.write_text(build_html(facts), encoding="utf-8")
    print(f"Wrote {page}")
    if args.html_only:
        return 0

    report = check_layout(page)
    if report:
        print("Layout:", json.dumps({k: report[k] for k in ("poster", "footerTopMm", "footerBottomMm")}))
        print("Type:", json.dumps(report["type"]))
        for row in report["panels"]:
            print("  ", row)
        for key in ("svgOverflow", "svgOverlap"):
            print(f"  {key}: {len(report[key])}")
            for row in report[key][:40]:
                print("    ", row)

    # Chrome fails silently on a PDF that another program holds open, so it writes a new file first.
    fresh = OUT / ".poster-build.pdf"
    fresh.unlink(missing_ok=True)
    run_chrome(page.as_uri(), "--no-pdf-header-footer", f"--print-to-pdf={fresh}")
    if not fresh.exists():
        raise SystemExit("Chrome wrote no PDF.")
    pdf_report, problems = check_pdf(fresh)
    print("PDF:", pdf_report)
    preview = OUT / "poster-preview.png"
    run_chrome(page.as_uri(), "--window-size=3179,4494", "--force-device-scale-factor=1", f"--screenshot={preview}")
    print(f"Wrote {preview}")
    if problems:
        raise SystemExit("PDF check failed: " + "; ".join(problems))
    pdf = OUT / "poster.pdf"
    try:
        os.replace(fresh, pdf)
    except PermissionError:
        target = free_copy_name(fresh)
        raise SystemExit(
            f"poster.pdf is open in another program and was NOT updated. The new version is in {target}. "
            "Close the PDF and run this script again."
        ) from None
    print(f"Wrote {pdf}")
    for leftover in OUT.glob("poster-new*.pdf"):
        try:
            leftover.unlink()
        except PermissionError:
            print(f"Could not remove {leftover}, it is open in another program.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
