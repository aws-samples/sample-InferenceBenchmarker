"""Build a Plotly bar plot from InferenceBenchmarker (Locust) results across run dirs.

For each run dir, reads <dir>/locust_stats/locust_stats.csv (Aggregated row). The
response-time columns become a single "Latency" bar whose value is driven by a global
Stat dropdown (Average Response Time default; Median/Min/Max and the percentile columns
50%..100% as alternatives). Augmented with four cross-cut scalars: Duration, Client RPS,
Server RPS, Total Requests Fired.

Dirs missing locust_stats/locust_stats.csv are skipped.
"""

import csv
import os

from _plot_common import make_bar_figure


# Response-time columns in locust_stats.csv → the Latency stat dropdown.
# Order = dropdown order; first entry is the default.
_LATENCY_COLS = [
    'Average Response Time', 'Median Response Time', 'Min Response Time', 'Max Response Time',
    '50%', '66%', '75%', '80%', '90%', '95%', '98%', '99%', '99.9%', '99.99%', '100%',
]
_DEFAULT_STAT = 'Average Response Time'


def _read_aggregated(csv_path):
    """Return the Aggregated row of locust_stats.csv as a dict, or None."""
    with open(csv_path, newline='') as f:
        for row in csv.DictReader(f):
            if row.get('Name') == 'Aggregated':
                return row
    return None


def _fired_from_workers(run_dir):
    """Total requests fired = sum of requests_fired/worker_*.txt, or None."""
    import glob
    total, found = 0, False
    for fp in glob.glob(os.path.join(run_dir, 'requests_fired', 'worker_*.txt')):
        try:
            total += int(open(fp).read().strip())
            found = True
        except (ValueError, OSError) as e:
            print(f"   ⚠️ unreadable worker count {fp} ({e}) — Total Requests Fired undercounted")
    if not found:
        print(f"   ⚠️ no requests_fired/worker_*.txt in {run_dir} — Total Requests Fired bar omitted")
        return None
    return total


def _wave_seconds(run_dir):
    """Wave duration from wave_window.txt (last - start epochs), or None."""
    wp = os.path.join(run_dir, 'wave_window.txt')
    if not os.path.exists(wp):
        print(f"   ⚠️ {wp} not found — Duration / Server RPS bars omitted")
        return None
    raw = open(wp).read().strip()
    if not raw or raw == 'WARN':
        print(f"   ⚠️ {wp} has no window ('{raw}') — Duration / Server RPS bars omitted")
        return None
    try:
        start, last = (float(x) for x in raw.split())
        return last - start
    except (ValueError, TypeError) as e:
        print(f"   ⚠️ {wp} unparseable ({e}) — Duration / Server RPS bars omitted")
        return None


def _client_rps(run_dir):
    """Target client RPS parsed from find_rps.log header ('Client RPS: N req/s')."""
    lp = os.path.join(run_dir, 'find_rps.log')
    if not os.path.exists(lp):
        print(f"   ⚠️ {lp} not found — Client RPS bar omitted")
        return None
    import re
    m = re.search(r'Client RPS:\s*([\d.]+)', open(lp, errors='ignore').read())
    if not m:
        print(f"   ⚠️ no 'Client RPS' line in {lp} — Client RPS bar omitted")
        return None
    return float(m.group(1))


def _build_run_series(run_dir, fields_filter):
    """Return (metrics_list, found_bool) for one run dir."""
    csv_path = os.path.join(run_dir, 'locust_stats', 'locust_stats.csv')
    if not os.path.exists(csv_path):
        return None, False
    agg = _read_aggregated(csv_path)
    if agg is None:
        print(f"   ⚠️ no Aggregated row in {csv_path} — run skipped")
        return None, False

    # Latency: one stat-bearing metric whose stats are the response-time columns (ms).
    # A missing/blank column is left as None silently (benign per-stat gap; the bar still
    # plots for the stats that are present) — same policy as the aiperf stat-cell parse.
    lat_stats = {}
    for col in _LATENCY_COLS:
        try:
            lat_stats[col] = float(agg[col])
        except (ValueError, TypeError, KeyError):
            lat_stats[col] = None
    metrics = [{'name': 'Latency (ms)', 'unit': 'ms', 'stats': lat_stats}]

    # cross-cut scalars
    try:
        total_requests = int(float(agg.get('Request Count', 0)))
    except (ValueError, TypeError):
        print(f"   ⚠️ non-numeric 'Request Count' in {csv_path} — Total Requests bar omitted")
        total_requests = None
    wave = _wave_seconds(run_dir)
    server_rps = (total_requests / wave) if (total_requests and wave) else None
    fired = _fired_from_workers(run_dir)

    extras = [
        ('Duration (sec)',       'sec',   wave),
        ('Client RPS (req/s)',   'req/s', _client_rps(run_dir)),
        ('Server RPS (req/s)',   'req/s', server_rps),
        ('Total Requests',       'count', total_requests),
        ('Total Requests Fired', 'count', fired),
    ]
    for nm, unit, val in extras:
        if val is not None:
            metrics.append({'name': nm, 'unit': unit, 'value': val})

    if fields_filter is not None:
        wanted = set(fields_filter)
        metrics = [m for m in metrics if m['name'] in wanted]

    return metrics, True


def make_locust_figure(run_dirs, fields_filter=None, default_theme='light', run_color=None,
                       dir_meta=None):
    """Build the locust figure (no file). Returns (fig or None, missing_dirs).

    dir_meta: optional {dir_basename: {'label': str, 'hover': [(k, v), ...]}} — 'label' is the
    display name (legend override or basename); 'hover' adds per-run hover lines.
    """
    dir_meta = dir_meta or {}
    series_by_run, missing = [], []
    for d in run_dirs:
        metrics, found = _build_run_series(d, fields_filter)
        if not found:
            missing.append(d)
            continue
        if metrics:
            key = os.path.normpath(d)
            base = os.path.basename(key)
            meta = dir_meta.get(key, {})
            label = meta.get('label', base)
            hover = meta.get('hover', [])
            series_by_run.append((label, metrics, hover))

    if not series_by_run:
        return None, missing

    # missing dirs are reported in the console log by plot.py, not on the figure
    fig = make_bar_figure(series_by_run, _LATENCY_COLS, _DEFAULT_STAT,
                          title='InferenceBenchmarker results', default_theme=default_theme,
                          run_color=run_color)
    return fig, missing


def plot_locust(run_dirs, out_path, fields_filter=None, default_theme='light'):
    """Build the locust bar plot at its own HTML file. Returns (out_path or None, missing_dirs).

    Args:
        run_dirs:      list of run directories to compare
        out_path:      HTML file to write
        fields_filter: optional list of display names to restrict to (from --plot-fields)
        default_theme: 'light' or 'dark'
    """
    import plotly.io as pio
    fig, missing = make_locust_figure(run_dirs, fields_filter, default_theme)
    if fig is None:
        return None, missing
    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    pio.write_html(fig, out_path, include_plotlyjs='cdn', full_html=True)
    return out_path, missing
