"""Shared burst helper — runs a single Locust burst via find_rps.sh."""

import glob
# import json  # used when reading hw_metrics.json from locust_user.py path (see commented block below)
import os
import subprocess
import threading

from .client_metrics import sample_client_metrics

_SCRIPT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def run_burst(factories_file, num_users, num_workers, wave_dir,
              run_time=1, sample_interval_seconds=0.01):
    """Run a single Locust burst via find_rps.sh.

    Calls find_rps.sh with --parent-dir wave_dir so all locust output lands
    under wave_dir (locust_logs/, locust_stats/, requests_fired/).
    Runs sample_client_metrics in parallel to capture hw metrics.

    Client-side only — no server metrics (--no-postprocess passed).

    Args:
        factories_file: Path to a factories .py file exposing invoke_factory/payload_factory
        num_users: Total users (= spawn_rate for burst mode)
        num_workers: Number of Locust worker processes
        wave_dir: Directory to write all output under
        run_time: Burst duration in seconds (default 1)
        sample_interval_seconds: psutil sampling interval (default 0.01)

    Returns:
        tuple: (requests_fired, hardware_metrics)
    """
    os.makedirs(wave_dir, exist_ok=True)

    script = os.path.join(_SCRIPT_DIR, 'server_capacity', 'find_rps.sh')
    parent_dir = os.path.dirname(_SCRIPT_DIR)

    cmd = [
        'bash', script,
        '--factories-file',   factories_file,
        '--client-rps',       str(num_users),
        '--obs-time',         str(run_time),
        '--workers',          str(num_workers),
        '--parent-dir',       wave_dir,
        '--no-postprocess',
    ]

    env = os.environ.copy()
    env['PYTHONPATH'] = parent_dir + os.pathsep + env.get('PYTHONPATH', '')

    # Alternative: use locust_user.py hw sampling (triggered via spawning_complete/quitting events).
    # Samples only during active test window. To enable:
    #   1. Uncomment below
    #   2. Comment out the sample_client_metrics thread block
    #   3. Uncomment the hw_metrics.json read block at the bottom
    # env['BENCHMARKER_SAMPLE_HW'] = '1'

    sample_duration = num_workers * 0.5 + run_time + 3
    hw_metrics = [None]

    def _sample():
        hw_metrics[0] = sample_client_metrics(
            duration_seconds=sample_duration,
            sample_interval_seconds=sample_interval_seconds,
        )

    t = threading.Thread(target=_sample, daemon=True)
    t.start()

    with open(os.path.join(wave_dir, 'find_rps.log'), 'w') as log:
        subprocess.run(cmd, env=env, check=True, stdout=log, stderr=log)

    t.join()

    total = 0
    for path in glob.glob(os.path.join(wave_dir, 'requests_fired', 'worker_*.txt')):
        try:
            with open(path) as f:
                total += int(f.read().strip())
        except (ValueError, OSError) as e:
            raise RuntimeError(
                f"Failed to read requests_fired count from {path}: {e}"
            ) from e

    # Alternative: read hw metrics written by locust_user.py when BENCHMARKER_SAMPLE_HW=1.
    # locust_user.py writes hw metrics to: requests_fired/hw_metrics.json
    # import json
    # hw_metrics_path = os.path.join(wave_dir, 'requests_fired', 'hw_metrics.json')
    # if os.path.exists(hw_metrics_path):
    #     with open(hw_metrics_path) as f:
    #         hw_metrics[0] = json.load(f)

    return total, hw_metrics[0]
