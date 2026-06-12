"""Shared plotting helpers for the InferenceBenchmarker / aiperf bar plots.

build_bar_figure() turns a per-run list of metric series into one Plotly figure:
  - metrics are split into subplots by unit (ms / tokens / req-s / count / ...), each
    subplot a grouped bar chart with one colored bar per run dir;
  - a "Stat" dropdown re-points every stat-bearing bar at the chosen statistic
    (avg / p50 / p99 / ... for aiperf, or Average/Median/95% / ... for locust) at once,
    while scalar bars (counts, RPS, duration) stay fixed;
  - a "Theme" dropdown switches the whole figure between light and dark;
  - every bar shows its value as a label and a full hover breakdown.

The data contract (see SERIES below) is identical for both plot modules, so this file
holds all the Plotly wiring and the modules only parse their own artifacts.
"""

import math
import os
import re

import plotly.graph_objects as go
import plotly.io as pio
from plotly.subplots import make_subplots


# ---------------------------------------------------------------------------
# Data contract
# ---------------------------------------------------------------------------
# build_bar_figure receives `series_by_run`: an ordered list of (run_label, metrics),
# where metrics is an ordered list of dicts, each one of:
#     {'name': 'Latency (ms)', 'unit': 'ms', 'stats': {'avg': .., 'p50': .., ...}}   # stat-bearing
#     {'name': 'Client RPS',   'unit': 'req/s', 'value': 10.0}                        # scalar
# `stat_options` is the ordered list of stat keys offered in the Stat dropdown and
# `default_stat` the one shown first; scalar metrics ignore both.

_PALETTE = ['#4C78A8', '#F58518', '#54A24B', '#E45756', '#72B7B2',
            '#EECA3B', '#B279A2', '#FF9DA6', '#9D755D', '#BAB0AC']


def assign_colors(run_labels):
    """Map run labels → palette colors in order. Shared across figures so the same run
    is the same color in both the Locust and aiperf plots (and the one common legend)."""
    return {label: _PALETTE[i % len(_PALETTE)] for i, label in enumerate(run_labels)}

_THEMES = {
    'light': dict(paper='#ffffff', plot='#ffffff', font='#222222', grid='#dddddd'),
    'dark':  dict(paper='#111418', plot='#111418', font='#e8e8e8', grid='#333a42'),
}


def _short(name):
    """Drop a trailing unit parenthetical for a compact x-axis label."""
    return re.sub(r'\s*\([^)]*\)\s*$', '', name).strip()


def _val(metric, stat):
    """Value of a metric at the selected stat: scalar metrics ignore stat."""
    if 'value' in metric:
        return metric['value']
    return metric.get('stats', {}).get(stat)


def theme_patch(fig, theme):
    """Relayout dict that recolors an existing figure to the given theme.

    Generic over the figure's actual axes/annotations (read from its layout), so it works
    for any subplot count. Keys are Plotly relayout paths — usable both by fig.update_layout
    (the non-bracket keys) and by JS Plotly.relayout in the combined page (all keys, incl.
    'annotations[i].font.color').
    """
    t = _THEMES[theme]
    patch = {
        'paper_bgcolor': t['paper'],
        'plot_bgcolor':  t['plot'],
        'font.color':    t['font'],
        'legend.font.color': t['font'],
    }
    for key in fig.to_plotly_json().get('layout', {}):
        if key.startswith('xaxis') or key.startswith('yaxis'):
            patch[f'{key}.gridcolor']      = t['grid']
            patch[f'{key}.linecolor']      = t['grid']
            patch[f'{key}.tickfont.color'] = t['font']
    for i in range(len(fig.layout.annotations)):
        patch[f'annotations[{i}].font.color'] = t['font']
    return patch


def make_bar_figure(series_by_run, stat_options, default_stat, title,
                    subtitle='', default_theme='light', run_color=None):
    """Build and return the grouped bar Plotly figure (no file written).

    Args:
        series_by_run: ordered [(run_label, [metric, ...]), ...] per the contract above
        stat_options:  ordered stat keys for the Stat dropdown (e.g. avg/p50/.../std)
        default_stat:  stat shown initially
        title:         figure title
        subtitle:      optional line under the title (e.g. which dirs lacked the file)
        default_theme: 'light' or 'dark'
    """
    # 1. collect metric names in first-seen order across runs, plus each metric's unit.
    # One subplot per metric (independently scaled), so wildly different magnitudes are
    # each readable instead of sharing one axis.
    metric_order, metric_unit = [], {}
    for _label, metrics in series_by_run:
        for m in metrics:
            if m['name'] not in metric_unit:
                metric_unit[m['name']] = m['unit']
                metric_order.append(m['name'])

    if not metric_order:
        raise ValueError("no metrics to plot")

    # 2. subplot grid — 4 per row, one metric each. Subplot title = metric name.
    n = len(metric_order)
    cols = min(4, n)
    rows = math.ceil(n / cols)

    # Fixed-pixel panel sizing so every subplot is the same physical size in BOTH the locust
    # and aiperf figures (vertical_spacing is a per-gap fraction, so a fraction that looks fine
    # at 2 rows eats most of the height at 7 rows — pinning pixels avoids that). PANEL_PX is the
    # drawing height of one subplot; GAP_PX the gap between rows.
    _PANEL_PX, _GAP_PX, _TOP_M, _BOT_M = 320, 90, 50, 40
    _plot_area = rows * _PANEL_PX + max(0, rows - 1) * _GAP_PX
    _total_height = _plot_area + _TOP_M + _BOT_M
    _vspacing = (_GAP_PX / _plot_area) if rows > 1 else 0.0

    fig = make_subplots(rows=rows, cols=cols,
                        subplot_titles=[_short(nm) for nm in metric_order],
                        horizontal_spacing=0.05,
                        vertical_spacing=_vspacing)

    # 3. one bar trace per (metric, run); within a subplot the runs sit side by side.
    # trace_meta is parallel to fig.data: (run_label, metric-dict-or-None) — one metric each.
    # Colors come from a shared map (run → color, consistent across both figures); fall back
    # to local palette order if not supplied. The legend is always hidden here — the combined
    # page renders one shared legend under the title.
    if run_color is None:
        run_color = assign_colors([label for label, _m in series_by_run])
    trace_meta = []

    for mi, name in enumerate(metric_order):
        r, c = divmod(mi, cols)
        unit = metric_unit[name]
        for label, metrics in series_by_run:
            metric = next((m for m in metrics if m['name'] == name), None)
            y = _val(metric, default_stat) if metric else None
            fig.add_trace(go.Bar(
                x=[label],
                y=[y],
                name=label,
                legendgroup=label,
                showlegend=False,
                marker_color=run_color.get(label, _PALETTE[0]),
                texttemplate='%{y:.4g}',
                textposition='outside',
                cliponaxis=False,
                # hover shows only the current value (+ unit)
                hovertemplate=f'%{{y:.4g}} {unit}<extra></extra>',
            ), row=r + 1, col=c + 1)
            trace_meta.append((label, metric))

    # x ticks (run labels) are redundant with the legend and crowd the narrow panels — hide
    fig.update_xaxes(showticklabels=False)

    # y headroom so the outside value label clears the title. The range must track the
    # CURRENTLY DISPLAYED stat — not the max over all stats (aiperf's 'sum' is ~40x its 'avg',
    # which would crush the avg bar to a sliver). We compute a per-axis range for every stat
    # and (a) set the default now, (b) hand the rest to the Stat dropdown via stat_ranges so JS
    # relayouts the axis alongside the y restyle.
    def _axis_key(mi):
        return 'yaxis' if mi == 0 else f'yaxis{mi + 1}'

    def _range_for(name, stat):
        vals = []
        for _label, metrics in series_by_run:
            metric = next((m for m in metrics if m['name'] == name), None)
            if metric is None:
                continue
            v = _val(metric, stat)
            if v is not None:
                vals.append(v)
        top = max(vals) if vals else 0
        return [0, top * 1.2] if top > 0 else [0, 1]

    for mi, name in enumerate(metric_order):
        fig.layout[_axis_key(mi)].range = _range_for(name, default_stat)

    # 4. Stat restyle data — per stat: the y-array per trace AND the axis range per subplot.
    # Stashed on layout.meta so the combined page drives both from an HTML <select> (the JS
    # does Plotly.restyle for the bar heights + Plotly.relayout for the matching axis ranges).
    stat_restyle = {stat: [[_val(metric, stat) if metric else None]
                           for _label, metric in trace_meta]
                    for stat in stat_options}
    stat_ranges = {stat: {_axis_key(mi): _range_for(name, stat)
                          for mi, name in enumerate(metric_order)}
                   for stat in stat_options}
    fig.layout.meta = {
        'stat_options': list(stat_options),
        'default_stat': default_stat,
        'stat_restyle': stat_restyle,
        'stat_ranges': stat_ranges,
    }

    # 5. Layout. No in-figure title (the combined page supplies a section heading per figure),
    # no per-figure Theme/Stat menus (the page renders both controls in the heading row).
    subtitle_ann = []
    if subtitle:
        subtitle_ann = [dict(text=f'<sup>{subtitle}</sup>', xref='paper', yref='paper',
                             x=0.5, y=1.08, showarrow=False, xanchor='center')]

    fig.update_layout(
        barmode='group',
        height=_total_height,
        margin=dict(t=_TOP_M, b=_BOT_M),
        showlegend=False,   # one shared legend is rendered by the combined page
    )
    for ann in subtitle_ann:
        fig.add_annotation(**ann)

    # apply default theme to the initial layout (non-bracket keys via update_layout;
    # annotation colors set directly since their relayout-path keys can't pass as kwargs)
    fig.update_layout(**{k: v for k, v in theme_patch(fig, default_theme).items()
                         if '[' not in k})
    t = _THEMES[default_theme]
    for ann in fig.layout.annotations:
        ann.font.color = t['font']

    return fig


def build_bar_figure(series_by_run, stat_options, default_stat, title,
                     out_path, subtitle='', default_theme='light'):
    """Build the figure and write it to its own HTML file. Returns out_path."""
    fig = make_bar_figure(series_by_run, stat_options, default_stat, title,
                          subtitle=subtitle, default_theme=default_theme)
    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    pio.write_html(fig, out_path, include_plotlyjs='cdn', full_html=True)
    return out_path


def write_combined_html(figures, out_path, page_title='InferenceBenchmarker plots',
                        default_theme='light', run_color=None):
    """Write several figures into ONE HTML file, plotly.js loaded once.

    Each figure keeps its own subplots, Stat dropdown and hover. They are stacked vertically
    with a heading before each. A single page-level Theme toggle (in the title row) recolors
    every figure at once via Plotly.relayout — there is no per-figure Theme menu. The figures'
    own legends are hidden; one shared legend (run_color) is rendered under the title row.

    Args:
        figures:  ordered [(heading, fig_or_None), ...]; None entries render a note
        out_path: HTML file to write
        page_title: <title> / top heading of the page
        default_theme: 'light' or 'dark' the figures were built with
        run_color: ordered {run_label: color} for the one shared legend (run order = insertion)
    """
    import json

    blocks = []
    theme_patches = {'light': {}, 'dark': {}}
    stat_data = {}               # div_id -> {options, default, restyle:{stat:[y...]}}
    first = True
    for idx, (heading, fig) in enumerate(figures):
        if fig is None:
            blocks.append(f'<h2>{heading}</h2><p style="color:#b00">⚠️ not available</p>')
            continue
        div_id = f'plot{idx}'
        meta = dict(fig.layout.meta or {})
        # full_html=False → just the <div> + script; load plotly.js only on the first
        frag = pio.to_html(fig, include_plotlyjs=('cdn' if first else False),
                           full_html=False, default_width='100%', div_id=div_id)
        first = False
        theme_patches['light'][div_id] = theme_patch(fig, 'light')
        theme_patches['dark'][div_id] = theme_patch(fig, 'dark')

        # per-figure Stat <select> next to the heading, driving Plotly.restyle
        opts = meta.get('stat_options', [])
        stat_sel = ''
        if opts:
            stat_data[div_id] = {'restyle': meta['stat_restyle'],
                                 'ranges': meta.get('stat_ranges', {})}
            default = meta.get('default_stat', opts[0])
            options_html = ''.join(
                f'<option value="{o}"{" selected" if o == default else ""}>{o}</option>'
                for o in opts)
            stat_sel = (f'<select class="statSel" data-plot="{div_id}">{options_html}</select>')
        blocks.append(
            f'<div class="sec"><h2>{heading}</h2>{stat_sel}</div>\n{frag}')

    # JS: one shared Theme toggle (Plotly.relayout) + per-figure Stat selects (Plotly.restyle)
    toggle_js = f'''
<script>
const _patches = {json.dumps(theme_patches)};
const _stat = {json.dumps(stat_data)};
function _applyTheme(theme) {{
    const dark = theme === 'dark';
    document.body.style.background = dark ? '#111418' : '#ffffff';
    document.body.style.color = dark ? '#e8e8e8' : '#222222';
    for (const [id, patch] of Object.entries(_patches[theme])) {{
        const el = document.getElementById(id);
        if (el) Plotly.relayout(el, patch);
    }}
}}
function _applyStat(divId, stat) {{
    const d = _stat[divId];
    const el = document.getElementById(divId);
    if (!d || !el) return;
    Plotly.restyle(el, {{y: d.restyle[stat]}});
    // also rescale each subplot's y-axis to the chosen stat (avg vs sum differ ~40x)
    const ranges = d.ranges && d.ranges[stat];
    if (ranges) {{
        const patch = {{}};
        for (const [axis, rng] of Object.entries(ranges)) patch[axis + '.range'] = rng;
        Plotly.relayout(el, patch);
    }}
}}
// shared legend click → hide/show that run in EVERY figure (one trace per panel, matched
// by trace name) and dim the chip. Replaces Plotly's native per-figure legend toggle.
const _runOff = {{}};
function _toggleRun(label) {{
    _runOff[label] = !_runOff[label];
    const vis = _runOff[label] ? false : true;
    for (const id of Object.keys(_patches.light)) {{
        const el = document.getElementById(id);
        if (!el) continue;
        const idx = [];
        el.data.forEach((t, i) => {{ if (t.name === label) idx.push(i); }});
        if (idx.length) Plotly.restyle(el, {{visible: vis}}, idx);
    }}
    document.querySelectorAll('.lgi').forEach(c => {{
        if (c.dataset.run === label) c.classList.toggle('off', _runOff[label]);
    }});
}}
document.addEventListener('DOMContentLoaded', () => {{
    const t = document.getElementById('themeSel');
    t.addEventListener('change', e => _applyTheme(e.target.value));
    _applyTheme(t.value);
    document.querySelectorAll('.statSel').forEach(sel => {{
        sel.addEventListener('change', e =>
            _applyStat(e.target.dataset.plot, e.target.value));
    }});
    document.querySelectorAll('.lgi').forEach(c => {{
        c.addEventListener('click', () => _toggleRun(c.dataset.run));
    }});
}});
</script>'''

    dark_sel = ' selected' if default_theme == 'dark' else ''
    light_sel = ' selected' if default_theme != 'dark' else ''
    header = (
        '<div class="hdr">'
        f'<h1>{page_title}</h1>'
        '<label class="theme">Theme: '
        '<select id="themeSel">'
        f'<option value="light"{light_sel}>Light</option>'
        f'<option value="dark"{dark_sel}>Dark</option>'
        '</select></label>'
        '</div>'
    )

    # one shared legend (swatch + run label), directly under the title row. Each chip is
    # clickable: toggles that run's visibility across every figure (see _toggleRun in JS).
    legend = ''
    if run_color:
        items = ''.join(
            f'<span class="lgi" data-run="{label}">'
            f'<span class="sw" style="background:{c}"></span>{label}</span>'
            for label, c in run_color.items())
        legend = f'<div class="legend">{items}</div>'

    html = (
        '<!DOCTYPE html><html><head><meta charset="utf-8">'
        f'<title>{page_title}</title>'
        '<style>body{font-family:system-ui,Arial,sans-serif;margin:24px;}'
        '.hdr{display:flex;align-items:center;gap:24px;}'
        '.hdr h1{margin:0;} .theme{font-size:15px;margin-left:auto;}'
        '.legend{display:flex;flex-wrap:wrap;gap:20px;margin:10px 0 4px;font-size:14px;}'
        '.lgi{display:flex;align-items:center;gap:7px;cursor:pointer;user-select:none;}'
        '.lgi.off{opacity:0.35;}'
        '.sw{display:inline-block;width:14px;height:14px;border-radius:3px;}'
        '.sec{display:flex;align-items:center;gap:16px;'
        'margin:32px 0 4px;border-top:1px solid #ccc;padding-top:16px;}'
        '.sec h2{margin:0;border:0;padding:0;} .statSel{font-size:15px;}'
        '</style></head><body>'
        + header
        + legend
        + '\n'.join(blocks)
        + toggle_js
        + '</body></html>'
    )
    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    with open(out_path, 'w') as f:
        f.write(html)
    return out_path
