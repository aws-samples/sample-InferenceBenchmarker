"""Entry point for `benchmark --plot` — builds one combined aiperf + locust plot file.

Invoked by find_rps.sh when --plot is passed. Produces a single HTML file with both the
aiperf and the InferenceBenchmarker (locust) figures stacked (plotly.js loaded once), each
keeping its own subplots and Stat/Theme dropdowns. Written under the output dir
(--plot-output-dir, else the first run dir), comparing all the run dirs given. Prints where
the file was written and which dirs lacked a source file.

Args (positional, passed by find_rps.sh):
    out_dir         output dir, or '' to use the first run dir
    theme           'light' or 'dark'
    fields_json     JSON string {'locust': [...], 'aiperf': [...]} or '' for all fields
    metadata_json   optional per-run overrides — either inline JSON or a path to a .json file:
                        {"<dir_basename>": {"legend": "<name>", "<key>": "<val>", ...}}
                    For a run, "legend" replaces its name in the shared legend, and every
                    other key/value is shown as a hover line on that run's bars. An empty
                    string means no overrides.
    run_dir...      one or more run directories
"""

import json
import os
import sys

from _plot_common import assign_colors, write_combined_html
from plot_aiperf import make_aiperf_figure
from plot_locust import make_locust_figure


def main(argv):
    if len(argv) < 4:
        print("Usage: plot.py <out_dir|''> <theme> <fields_json|''> <run_dir> [run_dir ...]")
        return 1

    out_dir       = argv[1]
    theme         = argv[2] if argv[2] in ('light', 'dark') else 'light'
    fields_json   = argv[3]
    metadata_json = argv[4] if len(argv) > 4 else ''
    run_dirs      = argv[5:]

    if not run_dirs:
        print("Error: at least one run dir is required")
        return 1

    # validate dirs (warn, drop nonexistent)
    valid = []
    for d in run_dirs:
        if os.path.isdir(d):
            valid.append(d)
        else:
            print(f"   ⚠️ not a directory, skipping: {d}")
    if not valid:
        print("Error: no valid run dirs")
        return 1

    # output dir: --plot-output-dir, else first run dir
    target = out_dir if out_dir else valid[0]
    os.makedirs(target, exist_ok=True)

    # optional field filters
    locust_fields = aiperf_fields = None
    if fields_json:
        try:
            spec = json.loads(fields_json)
            locust_fields = spec.get('locust')
            aiperf_fields = spec.get('aiperf')
        except (ValueError, AttributeError):
            print(f"   ⚠️ --plot-fields is not valid JSON, ignoring: {fields_json}")

    # optional per-run metadata, keyed by dir basename:
    #   {"<basename>": {"legend": "<name>", "<key>": <val>, ...}}
    # "legend" → display label; all other keys → hover lines on that run's bars.
    # The arg may be inline JSON or a path to a .json file (a path is more convenient for the
    # nested object and avoids shell-quoting the whole thing).
    metadata = {}
    if metadata_json:
        raw = metadata_json
        if os.path.isfile(metadata_json):
            try:
                with open(metadata_json) as f:
                    raw = f.read()
            except OSError as e:
                print(f"   ⚠️ --plot-metadata file unreadable ({e}), ignoring: {metadata_json}")
                raw = ''
        if raw:
            try:
                parsed = json.loads(raw)
                if isinstance(parsed, dict):
                    metadata = parsed
                else:
                    print(f"   ⚠️ --plot-metadata must be a JSON object, ignoring: {metadata_json}")
            except ValueError:
                print(f"   ⚠️ --plot-metadata is not valid JSON, ignoring: {metadata_json}")

    print("=" * 80)
    print("PLOTS")
    print("=" * 80)

    # Build a per-dir descriptor, one entry per run dir in order. label = legend override or
    # basename; hover = ordered [(key, value)] of the non-'legend' metadata keys.
    # The display label is the canonical run identity everywhere (color, legend chip, trace
    # name, toggle): two runs sharing a label would merge into one (same color, one legend
    # chip, the toggle hiding both). So we force every label unique — disambiguating with a
    # numeric suffix when a legend override or an identical basename would otherwise collide.
    # dir_meta is keyed by the run dir's full normalized path (basenames can repeat across
    # different parent dirs), so lookups never alias.
    dir_meta = {}      # normpath -> {'label': str, 'hover': [(k, v), ...]}
    ordered_labels = []
    seen = set()
    for d in valid:
        key = os.path.normpath(d)
        base = os.path.basename(key)
        entry = metadata.get(base, {}) if isinstance(metadata.get(base), dict) else {}
        label = str(entry.get('legend', base)) or base
        if label in seen:
            orig, n = label, 2
            while label in seen:
                label = f"{orig} ({n})"
                n += 1
            print(f"   ⚠️ run label '{orig}' already in use; showing '{label}' to keep runs distinct")
        seen.add(label)
        ordered_labels.append(label)
        hover = [(k, v) for k, v in entry.items() if k != 'legend']
        dir_meta[key] = {'label': label, 'hover': hover}

    # shared label→color map, in run-dir order, so the same run is the same color in both
    # figures and the one common legend. Labels are now guaranteed unique → one color each.
    run_color = assign_colors(ordered_labels)

    # build both figures (no per-figure files)
    aiperf_fig, a_missing = make_aiperf_figure(valid, fields_filter=aiperf_fields,
                                               default_theme=theme, run_color=run_color,
                                               dir_meta=dir_meta)
    locust_fig, l_missing = make_locust_figure(valid, fields_filter=locust_fields,
                                               default_theme=theme, run_color=run_color,
                                               dir_meta=dir_meta)

    if aiperf_fig is None and locust_fig is None:
        print("   ⚠️ no run dir had aiperf/profile_export_aiperf.csv or "
              "locust_stats/locust_stats.csv — nothing to plot")
        for m in a_missing:
            print(f"      ⚠️ no aiperf CSV in: {m}")
        return 1

    # one combined file — InferenceBenchmarker (Locust) first, then aiperf
    out_path = os.path.join(target, 'benchmark_plots.html')
    write_combined_html(
        [('Locust', locust_fig),
         ('aiperf', aiperf_fig)],
        out_path,
        default_theme=theme,
        run_color=run_color,
    )
    print(f"   Combined plot:  {out_path}")

    if aiperf_fig is None:
        print("   ⚠️ aiperf section empty — no run dir had aiperf/profile_export_aiperf.csv")
    for m in a_missing:
        print(f"      ⚠️ no aiperf CSV in: {m}")
    if locust_fig is None:
        print("   ⚠️ InferenceBenchmarker section empty — no run dir had locust_stats/locust_stats.csv")
    for m in l_missing:
        print(f"      ⚠️ no locust_stats.csv in: {m}")

    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv))
