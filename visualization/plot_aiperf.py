"""Build a Plotly bar plot from aiperf results across one or more run dirs.

For each run dir, reads <dir>/aiperf/profile_export_aiperf.csv:
  - the stat table (Metric, avg, min, max, sum, p1..p99, std) — one bar per metric, with
    a global Stat dropdown to switch which statistic every stat-bearing bar shows;
  - the scalar table (Metric, Value);
and augments with four cross-cut scalars: Duration, Client RPS, Server RPS, Total Requests
Fired. All HTTP * metrics are dropped except HTTP Total Time, which is renamed "Latency".

Dirs missing the aiperf CSV are skipped.
"""

import csv
import json
import os
import re

from _plot_common import make_bar_figure


# stat columns aiperf writes, in the order we offer them in the dropdown
_STAT_KEYS = ['avg', 'min', 'max', 'sum', 'p1', 'p5', 'p10', 'p25', 'p50',
              'p75', 'p90', 'p95', 'p99', 'std']
_DEFAULT_STAT = 'avg'


def _unit_of(display_name):
    """Pull the unit out of an aiperf display name's trailing parenthetical, else 'value'."""
    m = re.search(r'\(([^)]*)\)\s*$', display_name)
    return m.group(1) if m else 'value'


def _parse_aiperf_csv(csv_path):
    """Return (stat_metrics, scalar_metrics) parsed from profile_export_aiperf.csv.

    stat_metrics:   [{'name','unit','stats':{stat:val}}]  from the first (stat) table
    scalar_metrics: {name: value}                          from the 'Metric,Value' table
    Only the first two CSV sections are read (the third is GPU/endpoint telemetry).
    """
    stat_metrics, scalar_metrics = [], {}
    section = None
    with open(csv_path, newline='') as f:
        for row in csv.reader(f):
            if not row or not row[0].strip():
                section = None
                continue
            head = row[0].strip()
            if head == 'Metric' and len(row) > 2:      # stat-table header
                section = 'stats'; continue
            if head == 'Metric' and len(row) == 2:     # scalar-table header
                section = 'scalar'; continue
            if head in ('Endpoint',):                  # telemetry header — stop
                section = None; continue

            if section == 'stats':
                name = head
                vals = {}
                for key, cell in zip(_STAT_KEYS, row[1:]):
                    try:
                        vals[key] = float(cell)
                    except (ValueError, TypeError):
                        vals[key] = None
                stat_metrics.append({'name': name, 'unit': _unit_of(name), 'stats': vals})
            elif section == 'scalar':
                try:
                    scalar_metrics[head] = float(row[1])
                except (ValueError, TypeError, IndexError):
                    print(f"   ⚠️ non-numeric scalar '{head}' in {csv_path} — bar omitted")
    return stat_metrics, scalar_metrics


def _client_rps_from_json(aiperf_dir):
    """Target request rate from profile_export_aiperf.json input_config.phases[].rate."""
    jp = os.path.join(aiperf_dir, 'profile_export_aiperf.json')
    if not os.path.exists(jp):
        print(f"   ⚠️ {jp} not found — Client RPS bar omitted")
        return None
    try:
        d = json.load(open(jp))
        phases = d.get('input_config', {}).get('phases', [])
        for ph in phases:
            if ph.get('rate') is not None:
                return float(ph['rate'])
    except (ValueError, KeyError, TypeError) as e:
        print(f"   ⚠️ {jp} unreadable ({e}) — Client RPS bar omitted")
        return None
    print(f"   ⚠️ no phase rate in {jp} — Client RPS bar omitted")
    return None


def _fired_from_log(aiperf_dir):
    """Total requests fired = completed + cancelled, from logs/aiperf.log PhaseRecordsStats."""
    lp = os.path.join(aiperf_dir, 'logs', 'aiperf.log')
    if not os.path.exists(lp):
        print(f"   ⚠️ {lp} not found — Total Requests Fired bar omitted")
        return None
    text = open(lp, errors='ignore').read()
    matches = re.findall(r'PhaseRecordsStats\(([^)]*)\)', text)
    if not matches:
        print(f"   ⚠️ no PhaseRecordsStats in {lp} — Total Requests Fired bar omitted")
        return None
    fields = matches[-1]

    def grab(name):
        m = re.search(rf'{name}=(\d+)', fields)
        return int(m.group(1)) if m else 0

    return grab('final_requests_completed') + grab('final_requests_cancelled')


def _build_run_series(run_dir, fields_filter):
    """Return (metrics_list, found_bool) for one run dir, applying HTTP rules + filter."""
    aiperf_dir = os.path.join(run_dir, 'aiperf')
    csv_path = os.path.join(aiperf_dir, 'profile_export_aiperf.csv')
    if not os.path.exists(csv_path):
        return None, False

    stat_metrics, scalar = _parse_aiperf_csv(csv_path)

    metrics = []
    for m in stat_metrics:
        name = m['name']
        if name == 'HTTP Total Time (ms)':
            m = {**m, 'name': 'Latency (ms)'}          # keep + rename
        elif name.startswith('HTTP '):
            continue                                    # drop other HTTP
        metrics.append(m)

    # cross-cut scalars
    duration  = scalar.get('Benchmark Duration (sec)')
    req_count = scalar.get('Request Count')
    client_rps = _client_rps_from_json(aiperf_dir)
    server_rps = (req_count / duration) if (req_count and duration) else None
    fired = _fired_from_log(aiperf_dir)

    extras = [
        ('Duration (sec)',        'sec',   duration),
        ('Client RPS (req/s)',    'req/s', client_rps),
        ('Server RPS (req/s)',    'req/s', server_rps),
        ('Total Requests Fired',  'count', fired),
    ]
    for nm, unit, val in extras:
        if val is not None:
            metrics.append({'name': nm, 'unit': unit, 'value': val})

    # also expose scalar-table entries (counts/tokens) as bars
    for nm, val in scalar.items():
        if nm == 'Benchmark Duration (sec)':
            continue   # already surfaced as Duration
        metrics.append({'name': nm, 'unit': _unit_of(nm), 'value': val})

    if fields_filter is not None:
        wanted = set(fields_filter)
        metrics = [m for m in metrics if m['name'] in wanted]

    return metrics, True


def make_aiperf_figure(run_dirs, fields_filter=None, default_theme='light', run_color=None,
                       dir_meta=None):
    """Build the aiperf figure (no file). Returns (fig or None, missing_dirs).

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
    fig = make_bar_figure(series_by_run, _STAT_KEYS, _DEFAULT_STAT,
                          title='aiperf results', default_theme=default_theme,
                          run_color=run_color)
    return fig, missing


def plot_aiperf(run_dirs, out_path, fields_filter=None, default_theme='light'):
    """Build the aiperf bar plot at its own HTML file. Returns (out_path or None, missing_dirs).

    Args:
        run_dirs:      list of run directories to compare
        out_path:      HTML file to write
        fields_filter: optional list of display names to restrict to (from --plot-fields)
        default_theme: 'light' or 'dark'
    """
    import plotly.io as pio
    fig, missing = make_aiperf_figure(run_dirs, fields_filter, default_theme)
    if fig is None:
        return None, missing
    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    pio.write_html(fig, out_path, include_plotlyjs='cdn', full_html=True)
    return out_path, missing
