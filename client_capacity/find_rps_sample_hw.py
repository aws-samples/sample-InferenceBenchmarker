"""Sample client hardware metrics for a given duration and print results.

Usage:
    python3 find_rps_sample_hw.py <duration_seconds>
"""

import sys
import os

if len(sys.argv) != 2:
    print("Usage: find_rps_sample_hw.py <duration_seconds>")
    sys.exit(1)

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import psutil
from InferenceBenchmarker.client_capacity.client_metrics import sample_client_metrics

hw           = sample_client_metrics(duration_seconds=float(sys.argv[1]))
cpu_count    = psutil.cpu_count(logical=True)
total_ram_gb = psutil.virtual_memory().total / (1024 ** 3)

print(f"   Client CPU:    max = {hw['cpu_max']:.2f}% ({hw['cpu_max']/100*cpu_count:.1f}/{cpu_count} cores) | avg = {hw['cpu_avg']:.2f}% ({hw['cpu_avg']/100*cpu_count:.1f}/{cpu_count} cores)")
print(f"   Client Memory: max = {hw['memory_max']:.2f}% ({hw['memory_max']/100*total_ram_gb:.2f}/{total_ram_gb:.1f} GB) | avg = {hw['memory_avg']:.2f}% ({hw['memory_avg']/100*total_ram_gb:.2f}/{total_ram_gb:.1f} GB)")
