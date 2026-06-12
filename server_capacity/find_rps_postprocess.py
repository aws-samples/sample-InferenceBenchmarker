"""Post-processing for find_rps.sh — reads locust_stats.csv and prints wave results.

Called by find_rps.sh after the Locust wave completes. Reads Request Count and
Failure Count from locust_stats/locust_stats.csv (Aggregated row), and the actual
load window from wave_window.txt (start/last-request epochs written by the locust
master). Prints Wave time, Server RPS, Total requests, and Success rate.


Usage:
    python find_rps_postprocess.py <wave_dir> <success_threshold>

Args (positional):
    wave_dir:          Path to the wave output directory (contains locust_stats/)
    success_threshold: Minimum acceptable success rate e.g. 0.95
"""

import csv
import sys
import os

if len(sys.argv) != 3:
    print("Usage: find_rps_postprocess.py <wave_dir> <success_threshold>")
    sys.exit(1)

wave_dir          = sys.argv[1]
success_threshold = float(sys.argv[2])

# Read Request Count and Failure Count from locust_stats.csv (Aggregated row)
stats_csv = os.path.join(wave_dir, 'locust_stats', 'locust_stats.csv')
total_requests = 0
failure_count  = 0
with open(stats_csv) as f:
    for row in csv.DictReader(f):
        if row.get('Name') == 'Aggregated':
            total_requests = int(float(row['Request Count']))
            failure_count  = int(float(row['Failure Count']))
            break

success_rate = (total_requests - failure_count) / total_requests if total_requests > 0 else 0.0
passed       = success_rate >= success_threshold

# Read actual load window (raw epochs) written by the locust master
wave_window_path = os.path.join(wave_dir, 'wave_window.txt')
raw = open(wave_window_path).read().strip() if os.path.exists(wave_window_path) else ''
if raw == 'WARN' or not raw:
    wave_time_str = "⚠️ unavailable — no requests completed"
    rps = 0.0
else:
    start_epoch, end_epoch = (float(x) for x in raw.split())
    elapsed = end_epoch - start_epoch
    wave_time_str = f"{elapsed:.1f}s"
    rps = round(total_requests / elapsed, 1) if elapsed > 0 else 0.0

print()
print(f"   Duration:         {wave_time_str}")
print(f"   Server RPS:       {rps} req/s")
print(f"   Total requests:   {total_requests}")
print(f"   Success rate:     {success_rate*100:.1f}% ({'✓ passed' if passed else '❌ failed'}, {success_threshold*100:.0f}% target)")
