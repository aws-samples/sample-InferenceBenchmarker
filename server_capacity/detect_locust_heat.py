"""Detect a client-side bottleneck after a find_rps wave.

detect_locust_heat scans the locust logs for the CPU / heartbeat warnings Locust emits
when a worker or master is overloaded.
"""

import glob
import os

# Warnings Locust logs when a worker/master is overloaded (verified against locust 2.43.3
# runners.py).
_HEAT_PHRASES = (
    'failed to send heartbeat',
    'CPU usage above',
    'CPU usage was too high',
)

_WARNING = (
    "   ⚠️ CLIENT BOTTLENECK detected in locust executions, test results might be unstable. "
    "Monitor client hardware. Pass --sample-client-hw to have InferenceBenchmarker "
    "benchmark client usage. Use diagnostic tools to find worker and rps saturation—"
    "worker_saturation.py/rps_saturation.py. Try pre-computed inputs in payload_factory "
    "if payload computation is a bottleneck. Use a client with higher cores and/or memory."
)


def detect_locust_heat(wave_dir):
    """Print a bottleneck warning if any locust log contains a CPU/heartbeat warning.

    Args:
        wave_dir: Wave output dir (contains locust_logs/)
    """
    for path in glob.glob(os.path.join(wave_dir, 'locust_logs', '*.log')):
        text = open(path, errors='ignore').read()
        if any(phrase in text for phrase in _HEAT_PHRASES):
            print(_WARNING)
            return


if __name__ == '__main__':
    import sys
    detect_locust_heat(sys.argv[1])
