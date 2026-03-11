# CI Machine Performance Analysis

Analyze perfherder data from Mozilla's Treeherder, grouped by CI machine. Detects bimodal performance splits across machine pools.

## Setup

Requires [uv](https://docs.astral.sh/uv/).

```bash
uv sync
```

## Usage

```bash
# By platform (auto-finds signature for speedometer3 score-internal)
uv run nuc_performance_analysis.py --platform windows11-64-24h2-shippable --days 30
uv run nuc_performance_analysis.py --platform macosx1470-64-shippable --days 60

# By signature ID directly (find on Treeherder perfherder)
uv run nuc_performance_analysis.py 5276830 --days 30
uv run nuc_performance_analysis.py 5153951 --days 60

# Filter to specific machines
uv run nuc_performance_analysis.py --platform windows11-64-24h2-shippable --machines nuc13-100 nuc13-108

# Export CSV
uv run nuc_performance_analysis.py --platform macosx1470-64-shippable --days 30 --csv results.csv

# Generate reports
uv run nuc_performance_analysis.py --platform windows11-64-24h2-shippable --days 30 --report report.html
uv run nuc_performance_analysis.py --platform macosx1470-64-shippable --days 60 --report report.md
```

## CLI Options

| Flag | Default | Description |
|------|---------|-------------|
| `signature` (positional) | | Perfherder signature ID, skips auto-detection |
| `--platform` | | Platform identifier (required if no signature) |
| `--days` | `14` | Time window |
| `--suite` | `speedometer3` | Test suite |
| `--test` | `score-internal` | Subtest name |
| `--application` | `firefox` | Application |
| `--repo` | `mozilla-central` | Treeherder repository |
| `--framework` | `13` | Framework ID (13=browsertime) |
| `--machines` | | Filter to specific machine names |
| `--csv` | | Export raw data to CSV |
| `--report` | | Generate `.html` (with SVG charts) or `.md` report |

## Reports

- **HTML**: Self-contained file with embedded SVG charts (time series, histogram, per-machine bar chart), summary stats, and data tables. Share as a file or host somewhere.
- **Markdown**: GFM tables with summary stats, per-machine breakdown, and machine group lists. Works in GitHub gists.

## Known Signatures

| Signature | Platform | Suite |
|-----------|----------|-------|
| `5276830` | windows11-64-24h2-shippable | speedometer3 score-internal |
| `5153951` | macosx1470-64-shippable | speedometer3 score-internal |
