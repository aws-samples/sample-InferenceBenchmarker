"""Fetch and print server metrics from CloudWatch for a find_rps run.

Called by find_rps.sh when --endpoint-config is provided, after find_rps_postprocess.py.

CloudWatch stamps each datapoint at the START of its Period bucket and only publishes it
once that bucket has closed (+ a publish/propagation lag). So for each config:

  Window  = [floor(start, Period), ceil(end, Period)] — pad the load window out to whole
            Period buckets using THAT config's Period. A datapoint at 12:03:30 with
            Period=300 lives in the [12:00:00, 12:05:00) bucket, so the window is
            12:00:00 → 12:05:00 (full 5 min); with Period=60 it would be 12:03:00 → 12:04:00.
  Wait    = until the end bucket closes — ceil(end, Period) — PLUS an additional Lag for
            propagation. Computed against wall-clock, not a flat Period+Lag sleep.

Configs are grouped by (Period, Lag): each distinct combination gets its own wait and its own
get_metric_data call (one call shares one StartTime/EndTime, which can't satisfy two different
Periods). Groups are processed shortest-wait-first.
"""

import json
import sys
import time
import math


def _floor_to_period(ts, period):
    return math.floor(ts / period) * period


def _ceil_to_period(ts, period):
    return math.ceil(ts / period) * period


def fetch_server_metrics(config_path, start_epoch, end_epoch):
    """Fetch CloudWatch metrics as per config and print metric–values.

    Args:
        config_path: Path to JSON config file (list of source dicts)
        start_epoch: Wave start (unix epoch) — stats.total.start_time
        end_epoch:   Wave end (unix epoch) — stats.total.last_request_timestamp
    """
    import boto3
    from datetime import datetime, timezone

    cloudwatch = boto3.client('cloudwatch')

    with open(config_path) as f:
        configs = json.load(f)

    cw_configs = [c for c in configs if c.get('stream') == 'cloudwatch']  # alpha only supports cloudwatch metric. beta/gamma to support parseable streams.
    if not cw_configs:
        return

    streams = ', '.join(sorted({c['stream'] for c in cw_configs}))
    print("-" * 40)
    print()

    # Group configs by (Period, Lag): the window depends on Period and the wait depends on
    # Period+Lag, and one get_metric_data call shares a single StartTime/EndTime — so each
    # distinct (Period, Lag) needs its own call.
    groups = {}
    for c in cw_configs:
        groups.setdefault((c['Period'], c['Lag']), []).append(c)

    # When each group becomes queryable = its end bucket closing (ceil end to Period) + Lag.
    plans = []
    for (period, lag), gconfigs in groups.items():
        queryable_at = _ceil_to_period(end_epoch, period) + lag
        plans.append((queryable_at, period, lag, gconfigs))
    plans.sort()  # pay the shortest wait first

    for queryable_at, period, lag, gconfigs in plans:
        # build this group's queries (one per metric, stat)
        queries = []
        id_map = {}
        qid = 0
        for c in gconfigs:
            namespace  = c['Namespace']
            dimensions = c['Dimensions']
            for metric_name, stats in zip(c['Metrics'], c['Statistics']):
                for stat in stats:
                    key = f'q{qid}'
                    queries.append({
                        'Id': key,
                        'MetricStat': {
                            'Metric': {
                                'Namespace':  namespace,
                                'MetricName': metric_name,
                                'Dimensions': dimensions,
                            },
                            'Period': period,
                            'Stat':   stat,
                        },
                        'ReturnData': True,
                    })
                    id_map[key] = (metric_name, stat)
                    qid += 1

        # wait until this group's end bucket has closed + lag (measured against wall clock)
        wait = max(0.0, queryable_at - time.time())
        print(f"   ⏳ Fetching {streams} metrics (period {period}s, lag {lag}s) — "
              f"waiting {wait:.0f}s for the {period}s bucket to close + propagate...")
        if wait > 0:
            time.sleep(wait)

        # window: pad the load interval out to whole Period buckets (this group's Period)
        start_dt = datetime.fromtimestamp(_floor_to_period(start_epoch, period), tz=timezone.utc)
        end_dt   = datetime.fromtimestamp(_ceil_to_period(end_epoch,   period), tz=timezone.utc)

        resp = cloudwatch.get_metric_data(
            MetricDataQueries=queries,
            StartTime=start_dt,
            EndTime=end_dt,
        )

        # collate results by metric in input order: {metric_name: [(stat, value), ...]}
        by_metric = {}
        for result in resp['MetricDataResults']:
            metric_name, stat = id_map[result['Id']]
            values = result.get('Values', [])
            # aggregate per-bucket values across the window:
            # Sum→sum, Maximum→max, Minimum→min, anything else (Average etc.)→mean
            if not values:
                agg = None
            elif stat == 'Sum':
                agg = sum(values)
            elif stat == 'Maximum':
                agg = max(values)
            elif stat == 'Minimum':
                agg = min(values)
            else:
                agg = sum(values) / len(values)
            by_metric.setdefault(metric_name, []).append((stat, agg))

        print()
        for metric_name, stat_vals in by_metric.items():
            parts = [f"{stat}={val:.4g}" if val is not None else f"{stat}=N/A"
                     for stat, val in stat_vals]
            print(f"   {metric_name}: {', '.join(parts)}")
        print()


if __name__ == '__main__':
    if len(sys.argv) != 4:
        print("Usage: fetch_server_metrics.py <config_path> <start_epoch> <end_epoch>")
        sys.exit(1)

    fetch_server_metrics(
        config_path=sys.argv[1],
        start_epoch=float(sys.argv[2]),
        end_epoch=float(sys.argv[3]),
    )
