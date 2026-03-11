#!/usr/bin/env -S uv run
# /// script
# requires-python = ">=3.12"
# dependencies = ["aiohttp", "matplotlib"]
# ///
"""
Fetch perfherder data from Treeherder, grouped by CI machine.

Usage:
    uv run nuc_performance_analysis.py --platform windows11-64-24h2-shippable --days 30
    uv run nuc_performance_analysis.py --platform macosx1470-64-shippable --days 60
    uv run nuc_performance_analysis.py 5276830 --days 30
    uv run nuc_performance_analysis.py 5276830 --days 14 --report report.html
"""

import argparse
import asyncio
import csv
import io
import math
from collections import defaultdict
from datetime import datetime
from html import escape as html_escape

import aiohttp
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.dates as mdates

TREEHERDER = "https://treeherder.mozilla.org"


async def fetch_json(session, url):
    async with session.get(url) as resp:
        if resp.status != 200:
            raise Exception(f"HTTP {resp.status} for {url}")
        return await resp.json()


async def find_signatures(session, repo, framework, suite, platform, application,
                          test="score-internal"):
    """Find parent and subtest signature IDs."""
    url = f"{TREEHERDER}/api/project/{repo}/performance/signatures/"
    url += f"?framework={framework}&platform={platform}"

    data = await fetch_json(session, url)

    parent_id = None
    test_id = None

    for _, sig in data.items():
        if sig.get("suite") != suite or sig.get("application") != application:
            continue
        if sig.get("test") is None:
            parent_id = sig["id"]
        elif sig.get("test") == test:
            test_id = sig["id"]

    if not test_id:
        raise Exception(
            f"No '{test}' signature for {suite}/{application} on {platform}"
        )

    return parent_id, test_id


async def fetch_perf_data(session, repo, framework, sig_id, days):
    """Fetch perfherder data points for a signature over a time window."""
    url = f"{TREEHERDER}/api/project/{repo}/performance/data/"
    url += f"?framework={framework}&interval={days * 86400}&signature_id={sig_id}"

    data = await fetch_json(session, url)
    for _, points in data.items():
        return points
    return []


async def fetch_jobs_bulk(session, repo, job_ids):
    """Fetch job details in batches to get machine names."""
    jobs = {}
    ids = list(job_ids)
    for i in range(0, len(ids), 100):
        batch = ids[i : i + 100]
        ids_str = ",".join(str(j) for j in batch)
        url = f"{TREEHERDER}/api/project/{repo}/jobs/?id__in={ids_str}&count={len(batch)}"
        data = await fetch_json(session, url)
        for job in data.get("results", []):
            jobs[job["id"]] = job
    return jobs


def stdev(values):
    if len(values) < 2:
        return 0.0
    avg = sum(values) / len(values)
    return math.sqrt(sum((v - avg) ** 2 for v in values) / (len(values) - 1))


def classify_groups(nuc_data):
    """Detect bimodal grouping by finding the sparsest region in the data."""
    all_values = sorted(
        d["value"] for data in nuc_data.values() for d in data
    )
    if len(all_values) < 4:
        return None

    data_range = all_values[-1] - all_values[0]
    if data_range < 0.5:
        return None

    # Slide a window across the sorted values and find the region with fewest
    # points relative to its width -- this finds the "sparse zone" between modes
    window = data_range * 0.15
    best_score = float("inf")
    best_center = None
    step = data_range / 200
    lo = all_values[0] + window / 2
    hi = all_values[-1] - window / 2
    if lo >= hi:
        return None

    pos = lo
    while pos <= hi:
        count = sum(1 for v in all_values if pos - window / 2 <= v <= pos + window / 2)
        density = count / len(all_values)
        below = sum(1 for v in all_values if v < pos - window / 2)
        above = sum(1 for v in all_values if v > pos + window / 2)
        if below >= len(all_values) * 0.1 and above >= len(all_values) * 0.1:
            if density < best_score:
                best_score = density
                best_center = pos
        pos += step

    if best_center is None or best_score > 0.15:
        return None

    split = best_center
    below_split = [v for v in all_values if v < split]
    above_split = [v for v in all_values if v >= split]
    gap = above_split[0] - below_split[-1] if below_split and above_split else 0

    low, high, mixed = [], [], []
    for machine, data in sorted(nuc_data.items()):
        values = [d["value"] for d in data]
        avg = sum(values) / len(values)
        below = [v for v in values if v < split]
        above = [v for v in values if v >= split]
        entry = {"machine": machine, "avg": avg, "n": len(values),
                 "n_low": len(below), "n_high": len(above)}
        if below and above:
            mixed.append(entry)
        elif avg < split:
            low.append(entry)
        else:
            high.append(entry)

    low.sort(key=lambda x: x["avg"])
    high.sort(key=lambda x: x["avg"])
    mixed.sort(key=lambda x: x["avg"])

    low_vals = [v for m in low for d in nuc_data[m["machine"]] for v in [d["value"]]]
    high_vals = [v for m in high for d in nuc_data[m["machine"]] for v in [d["value"]]]
    low_mean = sum(low_vals) / len(low_vals) if low_vals else 0
    high_mean = sum(high_vals) / len(high_vals) if high_vals else 0

    n_low_pts = sum(1 for v in all_values if v < split)
    n_high_pts = len(all_values) - n_low_pts

    return {
        "split": split,
        "gap": gap,
        "low": low,
        "high": high,
        "mixed": mixed,
        "low_mean": low_mean,
        "high_mean": high_mean,
        "n_low_pts": n_low_pts,
        "n_high_pts": n_high_pts,
    }


def compute_stats(nuc_data):
    all_values = []
    all_timestamps = []
    for data in nuc_data.values():
        all_values.extend(d["value"] for d in data)
        all_timestamps.extend(d["timestamp"] for d in data)

    overall_avg = sum(all_values) / len(all_values)
    overall_sd = stdev(all_values)
    overall_mn = min(all_values)
    overall_mx = max(all_values)
    date_min = min(all_timestamps)
    date_max = max(all_timestamps)

    groups = classify_groups(nuc_data)

    machine_stats = []
    for machine, data in sorted(nuc_data.items()):
        values = [d["value"] for d in data]
        avg = sum(values) / len(values)
        sd = stdev(values) if len(values) > 1 else 0.0
        mn, mx = min(values), max(values)
        note = ""
        if overall_sd > 0 and abs(avg - overall_avg) > 2 * overall_sd:
            note = "OUTLIER"
        group = ""
        if groups:
            if any(e["machine"] == machine for e in groups["mixed"]):
                group = "MIXED"
            elif avg < groups["split"]:
                group = "LOW"
            else:
                group = "HIGH"
        machine_stats.append({
            "machine": machine, "n": len(values), "avg": avg,
            "min": mn, "max": mx, "stdev": sd, "note": note, "group": group,
        })
    machine_stats.sort(key=lambda x: x["avg"])

    return {
        "n_points": len(all_values),
        "n_machines": len(nuc_data),
        "mean": overall_avg,
        "stdev": overall_sd,
        "min": overall_mn,
        "max": overall_mx,
        "date_min": date_min,
        "date_max": date_max,
        "machines": machine_stats,
        "groups": groups,
    }


def print_analysis(nuc_data, label):
    stats = compute_stats(nuc_data)

    print(f"\n{'=' * 70}")
    print(label)
    print(f"{'=' * 70}")
    print(f"\n  Overall ({stats['n_points']} points across {stats['n_machines']} machines)")
    print(f"    mean:    {stats['mean']:.2f}")
    print(f"    stdev:   {stats['stdev']:.2f} ({stats['stdev'] / stats['mean'] * 100:.1f}%)")
    print(f"    min:     {stats['min']:.2f}")
    print(f"    max:     {stats['max']:.2f}")
    print(f"    range:   {stats['date_min']:%Y-%m-%d} to {stats['date_max']:%Y-%m-%d}")

    groups = stats["groups"]
    if groups:
        print(f"\n  Bimodal group analysis (gap={groups['gap']:.2f}, split at {groups['split']:.2f}):")
        print(f"    LOW  group: {len(groups['low']):>3} machines ({groups['n_low_pts']} pts), mean={groups['low_mean']:.2f}")
        print(f"    HIGH group: {len(groups['high']):>3} machines ({groups['n_high_pts']} pts), mean={groups['high_mean']:.2f}")
        if groups["mixed"]:
            print(f"    MIXED:      {len(groups['mixed']):>3} machines (points in both groups)")
            for e in groups["mixed"]:
                print(f"      {e['machine']:<14} {e['n_low']} low, {e['n_high']} high")
        else:
            print(f"    No machines cross between groups.")

    print(f"\n  Per-machine breakdown (sorted by avg):")
    print(f"  {'machine':<14} {'n':>3}  {'avg':>7}  {'min':>7}  {'max':>7}  {'stdev':>7}  {'group':>5}  note")
    print(f"  {'-'*14} {'-'*3}  {'-'*7}  {'-'*7}  {'-'*7}  {'-'*7}  {'-'*5}  {'-'*10}")

    for m in stats["machines"]:
        print(
            f"  {m['machine']:<14} {m['n']:>3}  {m['avg']:>7.2f}  {m['min']:>7.2f}"
            f"  {m['max']:>7.2f}  {m['stdev']:>7.2f}  {m['group']:>5}  {m['note']}"
        )


def print_time_series(nuc_data):
    all_points = []
    for machine, data in nuc_data.items():
        for d in data:
            all_points.append((d["timestamp"], machine, d["value"]))
    all_points.sort(key=lambda x: x[0])

    print(f"\n  Time series ({len(all_points)} points, chronological):")
    print(f"  {'date':>19}  {'machine':<14} {'score':>7}")
    print(f"  {'-'*19}  {'-'*14} {'-'*7}")
    for ts, machine, val in all_points:
        print(f"  {ts:%Y-%m-%d %H:%M:%S}  {machine:<14} {val:>7.2f}")


# -- Chart generation --

def _machine_colors(machines):
    cmap = plt.get_cmap("tab20")
    return {m: cmap(i % 20) for i, m in enumerate(sorted(machines))}


def generate_time_series_chart(nuc_data, groups=None):
    fig, ax = plt.subplots(figsize=(12, 5))
    group_colors = {"LOW": "#1f77b4", "HIGH": "#d62728", "MIXED": "#9467bd"}
    machine_color = _machine_colors(nuc_data.keys())
    for machine in sorted(nuc_data.keys()):
        data = nuc_data[machine]
        dates = [d["timestamp"] for d in data]
        values = [d["value"] for d in data]
        if groups:
            avg = sum(d["value"] for d in data) / len(data)
            if any(e["machine"] == machine for e in groups["mixed"]):
                c, label_prefix = group_colors["MIXED"], "MIXED"
            elif avg < groups["split"]:
                c, label_prefix = group_colors["LOW"], "LOW"
            else:
                c, label_prefix = group_colors["HIGH"], "HIGH"
            ax.scatter(dates, values, color=c, s=20, alpha=0.7)
        else:
            ax.scatter(dates, values, label=machine, color=machine_color[machine], s=20, alpha=0.7)
    if groups:
        ax.axhline(groups["split"], color="gray", linestyle=":", alpha=0.6, label=f"split={groups['split']:.1f}")
        for label, color in group_colors.items():
            ax.scatter([], [], color=color, s=20, label=label)
    ax.set_xlabel("Date")
    ax.set_ylabel("score-internal")
    ax.set_title("Speedometer3 score-internal Over Time by Machine")
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%m-%d"))
    ax.xaxis.set_major_locator(mdates.AutoDateLocator())
    ax.legend(fontsize=8, loc="upper left", bbox_to_anchor=(0, -0.12))
    fig.tight_layout()
    fig.subplots_adjust(bottom=0.2)
    return _fig_to_svg(fig)


def generate_histogram(nuc_data):
    fig, ax = plt.subplots(figsize=(10, 4))
    all_values = []
    for data in nuc_data.values():
        all_values.extend(d["value"] for d in data)
    ax.hist(all_values, bins=30, edgecolor="black", alpha=0.7)
    ax.set_xlabel("score-internal")
    ax.set_ylabel("Count")
    ax.set_title("Distribution of score-internal Values")
    ax.axvline(sum(all_values) / len(all_values), color="red", linestyle="--", label="mean")
    ax.legend()
    fig.tight_layout()
    return _fig_to_svg(fig)


def generate_bar_chart(nuc_data):
    stats = compute_stats(nuc_data)
    groups = stats["groups"]
    machines = [m["machine"] for m in stats["machines"]]
    avgs = [m["avg"] for m in stats["machines"]]

    fig, ax = plt.subplots(figsize=(max(8, len(machines) * 0.6), 5))
    group_colors = {"LOW": "#1f77b4", "HIGH": "#d62728", "MIXED": "#9467bd", "": "#888888"}
    bar_colors = [group_colors.get(m.get("group", ""), "#888888") for m in stats["machines"]]
    ax.bar(range(len(machines)), avgs, color=bar_colors, alpha=0.7, zorder=2)

    for i, machine in enumerate(machines):
        values = [d["value"] for d in nuc_data[machine]]
        ax.scatter([i] * len(values), values, color="black", s=10, alpha=0.4, zorder=3)

    ax.set_xticks(range(len(machines)))
    ax.set_xticklabels(machines, rotation=45, ha="right", fontsize=8)
    ax.set_ylabel("score-internal")
    ax.set_title("Per-Machine Average (with individual data points)")
    ax.grid(axis="y", alpha=0.3)
    fig.tight_layout()
    return _fig_to_svg(fig)


def _fig_to_svg(fig):
    buf = io.StringIO()
    fig.savefig(buf, format="svg")
    plt.close(fig)
    return buf.getvalue()


# -- Report generation --

def generate_html_report(nuc_data, label, days):
    stats = compute_stats(nuc_data)
    groups = stats["groups"]
    svg_timeseries = generate_time_series_chart(nuc_data, groups)
    svg_histogram = generate_histogram(nuc_data)
    svg_bar = generate_bar_chart(nuc_data)
    group_section = ""
    if groups:
        mixed_rows = ""
        if groups["mixed"]:
            mixed_items = ", ".join(
                f"<strong>{html_escape(e['machine'])}</strong> ({e['n_low']} low, {e['n_high']} high)"
                for e in groups["mixed"]
            )
            mixed_rows = f"<p>Mixed machines: {mixed_items}</p>"
        else:
            mixed_rows = "<p>No machines cross between groups.</p>"
        low_list = ", ".join(html_escape(e["machine"]) for e in groups["low"])
        high_list = ", ".join(html_escape(e["machine"]) for e in groups["high"])
        mixed_list = ", ".join(
            f"{html_escape(e['machine'])} ({e['n_low']} low, {e['n_high']} high)"
            for e in groups["mixed"]
        ) if groups["mixed"] else "(none)"
        group_section = f"""
<h2>Bimodal Group Analysis</h2>
<p>Largest gap: <strong>{groups['gap']:.2f}</strong> (split at {groups['split']:.2f})</p>
<div class="summary">
  <div class="stat"><div class="value">{len(groups['low'])}</div><div class="label">LOW machines (mean {groups['low_mean']:.2f})</div></div>
  <div class="stat"><div class="value">{len(groups['high'])}</div><div class="label">HIGH machines (mean {groups['high_mean']:.2f})</div></div>
  <div class="stat"><div class="value">{len(groups['mixed'])}</div><div class="label">MIXED machines</div></div>
</div>
<p><strong>LOW:</strong> {low_list}</p>
<p><strong>HIGH:</strong> {high_list}</p>
<p><strong>MIXED:</strong> {mixed_list}</p>"""

    machine_rows = ""
    for m in stats["machines"]:
        cls_parts = []
        if m["note"]:
            cls_parts.append("outlier")
        if m.get("group") == "MIXED":
            cls_parts.append("mixed")
        cls_attr = f' class="{" ".join(cls_parts)}"' if cls_parts else ""
        machine_rows += (
            f"<tr{cls_attr}>"
            f"<td>{html_escape(m['machine'])}</td>"
            f"<td>{m['n']}</td>"
            f"<td>{m['avg']:.2f}</td>"
            f"<td>{m['min']:.2f}</td>"
            f"<td>{m['max']:.2f}</td>"
            f"<td>{m['stdev']:.2f}</td>"
            f"<td>{html_escape(m.get('group', ''))}</td>"
            f"<td>{html_escape(m['note'])}</td>"
            f"</tr>\n"
        )

    raw_rows = ""
    all_points = []
    for machine, data in sorted(nuc_data.items()):
        for d in data:
            all_points.append((d["timestamp"], machine, d["value"], d["revision"]))
    all_points.sort(key=lambda x: x[0])
    for ts, machine, val, rev in all_points:
        raw_rows += (
            f"<tr>"
            f"<td>{ts:%Y-%m-%d %H:%M}</td>"
            f"<td>{html_escape(machine)}</td>"
            f"<td>{val:.2f}</td>"
            f"<td><code>{html_escape(rev[:12])}</code></td>"
            f"</tr>\n"
        )

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>CI Machine Performance Report</title>
<style>
body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; margin: 2em; color: #333; }}
h1 {{ border-bottom: 2px solid #333; padding-bottom: 0.3em; }}
h2 {{ margin-top: 2em; color: #555; }}
.summary {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 1em; margin: 1em 0; }}
.stat {{ background: #f5f5f5; padding: 1em; border-radius: 8px; text-align: center; }}
.stat .value {{ font-size: 1.5em; font-weight: bold; }}
.stat .label {{ color: #777; font-size: 0.85em; }}
table {{ border-collapse: collapse; width: 100%; margin: 1em 0; }}
th, td {{ border: 1px solid #ddd; padding: 6px 10px; text-align: right; }}
th {{ background: #f0f0f0; position: sticky; top: 0; cursor: pointer; }}
td:first-child, th:first-child {{ text-align: left; }}
tr.outlier {{ background: #fff3cd; }}
tr.mixed {{ background: #cce5ff; }}
.chart {{ margin: 1.5em 0; overflow-x: auto; }}
.chart svg {{ max-width: 100%; height: auto; }}
.raw-data {{ max-height: 500px; overflow-y: auto; }}
</style>
</head>
<body>
<h1>CI Machine Performance Report</h1>
<p>{html_escape(label)} | Last {days} days</p>

<div class="summary">
  <div class="stat"><div class="value">{stats['n_points']}</div><div class="label">Data Points</div></div>
  <div class="stat"><div class="value">{stats['n_machines']}</div><div class="label">Machines</div></div>
  <div class="stat"><div class="value">{stats['mean']:.2f}</div><div class="label">Mean</div></div>
  <div class="stat"><div class="value">{stats['stdev']:.2f}</div><div class="label">Stdev ({stats['stdev'] / stats['mean'] * 100:.1f}%)</div></div>
  <div class="stat"><div class="value">{stats['min']:.2f}</div><div class="label">Min</div></div>
  <div class="stat"><div class="value">{stats['max']:.2f}</div><div class="label">Max</div></div>
  <div class="stat"><div class="value">{stats['date_min']:%Y-%m-%d}</div><div class="label">From</div></div>
  <div class="stat"><div class="value">{stats['date_max']:%Y-%m-%d}</div><div class="label">To</div></div>
</div>

<h2>Time Series</h2>
<div class="chart">{svg_timeseries}</div>

<h2>Score Distribution</h2>
<div class="chart">{svg_histogram}</div>

<h2>Per-Machine Average</h2>
<div class="chart">{svg_bar}</div>

{group_section}

<h2>Per-Machine Table</h2>
<table>
<thead><tr><th>Machine</th><th>N</th><th>Avg</th><th>Min</th><th>Max</th><th>Stdev</th><th>Group</th><th>Note</th></tr></thead>
<tbody>
{machine_rows}</tbody>
</table>

<h2>Raw Data</h2>
<div class="raw-data">
<table>
<thead><tr><th>Date</th><th>Machine</th><th>Score</th><th>Revision</th></tr></thead>
<tbody>
{raw_rows}</tbody>
</table>
</div>

</body>
</html>"""
    return html


def generate_md_report(nuc_data, label, days):
    stats = compute_stats(nuc_data)

    lines = []
    lines.append(f"# CI Machine Performance Report\n")
    lines.append(f"{label} | Last {days} days\n")

    lines.append(f"## Summary\n")
    lines.append(f"| Metric | Value |")
    lines.append(f"|--------|-------|")
    lines.append(f"| Data Points | {stats['n_points']} |")
    lines.append(f"| Machines | {stats['n_machines']} |")
    lines.append(f"| Mean | {stats['mean']:.2f} |")
    lines.append(f"| Stdev | {stats['stdev']:.2f} ({stats['stdev'] / stats['mean'] * 100:.1f}%) |")
    lines.append(f"| Min | {stats['min']:.2f} |")
    lines.append(f"| Max | {stats['max']:.2f} |")
    lines.append(f"| Date Range | {stats['date_min']:%Y-%m-%d} to {stats['date_max']:%Y-%m-%d} |")
    lines.append("")

    groups = stats["groups"]
    if groups:
        lines.append(f"## Bimodal Group Analysis\n")
        lines.append(f"Largest gap: **{groups['gap']:.2f}** (split at {groups['split']:.2f})\n")
        lines.append(f"| Group | Machines | Mean |")
        lines.append(f"|-------|--------:|-----:|")
        lines.append(f"| LOW | {len(groups['low'])} | {groups['low_mean']:.2f} |")
        lines.append(f"| HIGH | {len(groups['high'])} | {groups['high_mean']:.2f} |")
        lines.append(f"| MIXED | {len(groups['mixed'])} | -- |")
        lines.append("")
        low_list = ", ".join(e["machine"] for e in groups["low"])
        high_list = ", ".join(e["machine"] for e in groups["high"])
        mixed_list = ", ".join(
            f"{e['machine']} ({e['n_low']} low, {e['n_high']} high)" for e in groups["mixed"]
        ) if groups["mixed"] else "(none)"
        lines.append(f"**LOW:** {low_list}  ")
        lines.append(f"**HIGH:** {high_list}  ")
        lines.append(f"**MIXED:** {mixed_list}")
        lines.append("")

    lines.append(f"## Per-Machine Breakdown\n")
    lines.append(f"| Machine | N | Avg | Min | Max | Stdev | Group | Note |")
    lines.append(f"|---------|--:|----:|----:|----:|------:|-------|------|")
    for m in stats["machines"]:
        lines.append(
            f"| {m['machine']} | {m['n']} | {m['avg']:.2f} | {m['min']:.2f}"
            f" | {m['max']:.2f} | {m['stdev']:.2f} | {m.get('group', '')} | {m['note']} |"
        )
    lines.append("")

    lines.append(f"## Raw Data\n")
    lines.append(f"| Date | Machine | Score | Revision |")
    lines.append(f"|------|---------|------:|----------|")
    all_points = []
    for machine, data in sorted(nuc_data.items()):
        for d in data:
            all_points.append((d["timestamp"], machine, d["value"], d["revision"]))
    all_points.sort(key=lambda x: x[0])
    for ts, machine, val, rev in all_points:
        lines.append(f"| {ts:%Y-%m-%d %H:%M} | {machine} | {val:.2f} | `{rev[:12]}` |")
    lines.append("")

    return "\n".join(lines)


async def run(args):
    async with aiohttp.ClientSession() as session:
        if args.signature:
            sig_id = args.signature
            label = f"signature {sig_id}"
            print(f"Using signature {sig_id}")
        else:
            print(f"Finding signatures for {args.suite}/{args.application} on {args.platform}...")
            parent_id, sig_id = await find_signatures(
                session, args.repo, args.framework,
                args.suite, args.platform, args.application,
                args.test,
            )
            label = f"{args.test}: {args.suite} ({args.application}) on {args.platform}"
            print(f"  {args.test} signature: {sig_id}")

        print(f"Fetching {args.days} days of data...")
        points = await fetch_perf_data(
            session, args.repo, args.framework, sig_id, args.days,
        )
        print(f"  {len(points)} data points")
        if not points:
            print("No data found!")
            return

        job_ids = {p["job_id"] for p in points}
        print(f"Fetching job details for {len(job_ids)} jobs...")
        jobs = await fetch_jobs_bulk(session, args.repo, job_ids)

        nuc_data = defaultdict(list)
        for point in points:
            job = jobs.get(point["job_id"])
            if not job:
                continue
            machine = job.get("machine_name", "unknown")
            if args.machines and machine not in args.machines:
                continue
            nuc_data[machine].append({
                "timestamp": datetime.fromtimestamp(point["push_timestamp"]),
                "value": point["value"],
                "revision": point["revision"],
                "push_id": point["push_id"],
                "job_id": point["job_id"],
            })

        for machine in nuc_data:
            nuc_data[machine].sort(key=lambda x: x["timestamp"])

        if not nuc_data:
            print("No data matched the specified machines!")
            return

        print_analysis(nuc_data, label)

        if not args.report:
            print_time_series(nuc_data)

        if args.csv:
            with open(args.csv, "w", newline="") as f:
                w = csv.writer(f)
                w.writerow(["machine", "timestamp", "score_internal", "revision", "push_id", "job_id"])
                for machine, data in sorted(nuc_data.items()):
                    for d in data:
                        w.writerow([
                            machine,
                            d["timestamp"].isoformat(),
                            d["value"],
                            d["revision"],
                            d["push_id"],
                            d["job_id"],
                        ])
            print(f"\nExported to {args.csv}")

        if args.report:
            ext = args.report.rsplit(".", 1)[-1].lower()
            if ext == "html":
                content = generate_html_report(nuc_data, label, args.days)
            elif ext == "md":
                content = generate_md_report(nuc_data, label, args.days)
            else:
                print(f"Unknown report format '.{ext}', use .html or .md")
                return
            with open(args.report, "w") as f:
                f.write(content)
            print(f"\nReport written to {args.report}")


def main():
    parser = argparse.ArgumentParser(
        description="Perfherder score analysis by CI machine"
    )
    parser.add_argument("signature", nargs="?", type=int,
                        help="Perfherder signature ID (skip auto-detection)")
    parser.add_argument("--repo", default="mozilla-central")
    parser.add_argument("--framework", type=int, default=13, help="13=browsertime")
    parser.add_argument("--suite", default="speedometer3")
    parser.add_argument("--platform", help="Platform identifier (required unless signature given)")
    parser.add_argument("--test", default="score-internal", help="Subtest name")
    parser.add_argument("--application", default="firefox")
    parser.add_argument("--days", type=int, default=14)
    parser.add_argument("--machines", nargs="+", help="Filter to specific machines")
    parser.add_argument("--csv", help="Export results to CSV")
    parser.add_argument("--report", help="Generate report (.html or .md)")
    args = parser.parse_args()

    if not args.signature and not args.platform:
        parser.error("either provide a signature ID or --platform")

    asyncio.run(run(args))


if __name__ == "__main__":
    main()
