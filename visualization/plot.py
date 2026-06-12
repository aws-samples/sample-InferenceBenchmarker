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

    out_dir     = argv[1]
    theme       = argv[2] if argv[2] in ('light', 'dark') else 'light'
    fields_json = argv[3]
    run_dirs    = argv[4:]

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

    print("=" * 80)
    print("PLOTS")
    print("=" * 80)

    # shared run→color map (run labels = dir basenames) so the same run is the same color
    # in both figures and the one common legend
    run_labels = [os.path.basename(os.path.normpath(d)) for d in valid]
    run_color = assign_colors(run_labels)

    # build both figures (no per-figure files)
    aiperf_fig, a_missing = make_aiperf_figure(valid, fields_filter=aiperf_fields,
                                               default_theme=theme, run_color=run_color)
    locust_fig, l_missing = make_locust_figure(valid, fields_filter=locust_fields,
                                               default_theme=theme, run_color=run_color)

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
