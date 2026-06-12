"""Fetch and print server metrics from CloudWatch for a find_rps run.

Called by find_rps.sh when --endpoint-config is provided, after find_rps_postprocess.py.

All cloudwatch configs are fetched in one get_metric_data call after sleeping
max(Period + Lag) across configs (the longest wait covers all shorter ones).
Each query carries its own Period.

Query window is [start_epoch, end_epoch] (the actual load window from Locust's
master stats: start_time → last_request_timestamp), floored/ceiled to Period.
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

    cw_configs = [c for c in configs if c.get('stream') == 'cloudwatch'] # alpha only supports cloudwatch metric. beta/gamma to support parseable streams. 
    if not cw_configs:
        return

    # one wait = longest (Period + Lag) across all cloudwatch configs
    slowest   = max(cw_configs, key=lambda c: c['Period'] + c['Lag'])
    wait_time = slowest['Period'] + slowest['Lag']
    streams   = ', '.join(sorted({c['stream'] for c in cw_configs}))
    print("-" * 40)
    print()
    print(f"   ⏳ Fetching metrics from stream: {streams} "
          f"(waiting {wait_time}s (longest lag: {slowest['Lag']} + period: {slowest['Period']}) for propagation)...")
    time.sleep(wait_time)

    # one query per (metric, stat); id_map bridges the response Id back to (metric, stat)
    queries = []
    id_map = {}
    qid = 0
    for c in cw_configs:
        period     = c['Period']
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

    # floor start / ceil end to period so overlapping buckets are included
    min_period = min(c['Period'] for c in cw_configs)
    start_dt = datetime.fromtimestamp(_floor_to_period(start_epoch, min_period), tz=timezone.utc)
    end_dt   = datetime.fromtimestamp(_ceil_to_period(end_epoch,   min_period), tz=timezone.utc)

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


if __name__ == '__main__':
    if len(sys.argv) != 4:
        print("Usage: fetch_server_metrics.py <config_path> <start_epoch> <end_epoch>")
        sys.exit(1)

    fetch_server_metrics(
        config_path=sys.argv[1],
        start_epoch=float(sys.argv[2]),
        end_epoch=float(sys.argv[3]),
    )
