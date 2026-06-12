"""Worker saturation — find max requests a single Locust worker can fire in 1 second."""

import os
import statistics

from . import client_metrics as _client_metrics
from ._burst import run_burst
from ._tee import Tee


def find_worker_saturation(
    factories_file,
    start_users=50,
    end_users=None,
    user_step=5,
    confidence_samples=10,
    confidence_users_scale=0.20,
    sample_interval_seconds=0.01,
    client_idle_max_wait_config=None,
):
    """Find the max requests a single Locust worker can fire in 1 second.

    Establishes client hardware baseline, then:

    Phase 1: Increase users until requests_fired < num_users (saturation).
    Phase 2: Run confidence_samples × 1-second bursts at saturation * (1 + scale).
             Collects client hardware metrics and waits for client idle between runs.

    Args:
        factories_file: Path to a factories .py file exposing invoke_factory/payload_factory
        start_users: Starting user count (default 50)
        end_users: Max user count to test — None means no limit (default None)
        user_step: Fixed increment per Phase 1 iteration (default 5)
        confidence_samples: Number of 1-second runs for Phase 2 (default 10)
        confidence_users_scale: confidence_users = saturation_users * (1 + scale) (default 0.20 = +20%)
        sample_interval_seconds: Sleep between psutil samples (default 0.01)
        client_idle_max_wait_config: max_wait_config for wait_for_client_idle

    Returns:
        dict with saturation, confidence results, and hardware metrics
    """
    from datetime import datetime
    from .client_metrics import establish_client_baseline, wait_for_client_idle, get_idle_criteria_info

    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    _root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    run_dir = os.path.join(_root, '.tmp', f'{timestamp}_worker_saturation')
    os.makedirs(run_dir, exist_ok=True)

    with Tee(os.path.join(run_dir, 'worker_saturation.log')):
        return _find_worker_saturation(
            factories_file, start_users, end_users, user_step,
            confidence_samples, confidence_users_scale, sample_interval_seconds,
            client_idle_max_wait_config, run_dir,
        )


def _find_worker_saturation(
    factories_file, start_users, end_users, user_step,
    confidence_samples, confidence_users_scale, sample_interval_seconds,
    client_idle_max_wait_config, run_dir,
):
    """Inner implementation — runs inside Tee context so all prints are captured to worker_saturation.log.

    Additional args:
        run_dir: Timestamped directory under .tmp/ for burst output and log file
    """
    from .client_metrics import establish_client_baseline, wait_for_client_idle, get_idle_criteria_info

    print("=" * 80)
    print("WORKER SATURATION — Max requests fired in 1 second (1 worker)")
    print("=" * 80)
    print(f"   Start users:       {start_users}")
    print(f"   End users:         {end_users if end_users is not None else 'unlimited'}")
    print(f"   User step:         {user_step}")
    print(f"   Confidence runs:   {confidence_samples}")
    print(f"   Confidence scale:  +{confidence_users_scale*100:.0f}% above saturation")
    print(f"   Results dir:       {run_dir}")
    print()

    # Establish client baseline
    print("🔍 Establishing client baseline...")
    establish_client_baseline()
    print()

    # ── Phase 1: Find saturation ──────────────────────────────────────────────
    print("📈 Phase 1: Finding saturation point")
    print("-" * 40)

    num_users = start_users
    saturation_users = None
    iteration = 0

    while True:
        iteration += 1
        print(f"\n🔄 Iteration {iteration} — {num_users} users")

        wave_dir = os.path.join(run_dir, f'phase1_iter{iteration}_u{num_users}')
        requests_fired, hw = run_burst(
            factories_file, num_users, 1, wave_dir,
            sample_interval_seconds=sample_interval_seconds,
        )

        cpu_count    = _client_metrics.CLIENT_BASELINES.get('cpu_count', 1)
        total_ram_gb = _client_metrics.CLIENT_BASELINES.get('total_ram_gb', 1)
        print(f"   Requests fired: {requests_fired} / {num_users}")
        print(f"   Client CPU:    max = {hw['cpu_max']:.2f}% ({hw['cpu_max']/100*cpu_count:.1f}/{cpu_count} cores) | avg = {hw['cpu_avg']:.2f}% ({hw['cpu_avg']/100*cpu_count:.1f}/{cpu_count} cores)")
        print(f"   Client Memory: max = {hw['memory_max']:.2f}% ({hw['memory_max']/100*total_ram_gb:.2f}/{total_ram_gb:.1f} GB) | avg = {hw['memory_avg']:.2f}% ({hw['memory_avg']/100*total_ram_gb:.2f}/{total_ram_gb:.1f} GB)")

        if requests_fired >= num_users:
            if end_users is not None and num_users >= end_users:
                saturation_users = requests_fired
                print(f"\n   ⚠️  Reached end_users limit ({end_users}) without saturation.")
                print(f"   ✓ Using end_users result: {saturation_users} requests/second (1 worker)")
                break
            print(f"   ✓ All {num_users} fired — not saturated, trying {num_users + user_step}")
            wait_for_client_idle(max_wait_config=client_idle_max_wait_config)
            num_users += user_step
        else:
            saturation_users = requests_fired
            print(f"\n   ✓ Saturation found: {saturation_users} requests/second (1 worker)")
            break

    # ── Phase 2: Confidence ───────────────────────────────────────────────────
    confidence_users = int(saturation_users * (1 + confidence_users_scale))
    fired_counts = []
    hw_metrics_list = []

    if confidence_samples > 0:
        print()
        print(f"📊 Phase 2: Confidence runs")
        print(f"   Users: {confidence_users} ({saturation_users} + {confidence_users_scale*100:.0f}%)")
        print(f"   Runs:  {confidence_samples} × 1 second")

        criteria_label, threshold_info, wait_limit, max_wait_seconds = get_idle_criteria_info(client_idle_max_wait_config)
        print(f"   {criteria_label}: {threshold_info}")
        print(f"   Wait limit: {max_wait_seconds}s" if wait_limit else f"   Wait limit: disabled (will wait indefinitely)")
        print("-" * 40)

        for i in range(1, confidence_samples + 1):
            print(f"\n🔄 Run {i} — {confidence_users} users")
            wave_dir = os.path.join(run_dir, f'phase2_run{i}_u{confidence_users}')
            fired, hw = run_burst(
                factories_file, confidence_users, 1, wave_dir,
                sample_interval_seconds=sample_interval_seconds,
            )
            fired_counts.append(fired)
            hw_metrics_list.append(hw)
            cpu_count    = _client_metrics.CLIENT_BASELINES.get('cpu_count', 1)
            total_ram_gb = _client_metrics.CLIENT_BASELINES.get('total_ram_gb', 1)
            print(f"   Requests fired: {fired} / {confidence_users}")
            print(f"   Client CPU:    max = {hw['cpu_max']:.1f}% ({hw['cpu_max']/100*cpu_count:.1f}/{cpu_count} cores)")
            print(f"   Client Memory: max = {hw['memory_max']:.1f}% ({hw['memory_max']/100*total_ram_gb:.2f}/{total_ram_gb:.1f} GB)")
            if i < confidence_samples:
                wait_for_client_idle(max_wait_config=client_idle_max_wait_config, print_header=False)

    mean_fired = statistics.mean(fired_counts) if fired_counts else None
    std_fired  = statistics.stdev(fired_counts) if len(fired_counts) > 1 else 0.0
    min_fired  = min(fired_counts) if fired_counts else None
    max_fired  = max(fired_counts) if fired_counts else None

    hw_agg = {
        'cpu_max_avg':    statistics.mean(h['cpu_max']    for h in hw_metrics_list) if hw_metrics_list else None,
        'cpu_max_max':    max(h['cpu_max']                for h in hw_metrics_list) if hw_metrics_list else None,
        'cpu_max_min':    min(h['cpu_max']                for h in hw_metrics_list) if hw_metrics_list else None,
        'cpu_avg_avg':    statistics.mean(h['cpu_avg']    for h in hw_metrics_list) if hw_metrics_list else None,
        'cpu_avg_max':    max(h['cpu_avg']                for h in hw_metrics_list) if hw_metrics_list else None,
        'cpu_avg_min':    min(h['cpu_avg']                for h in hw_metrics_list) if hw_metrics_list else None,
        'cpu_min_avg':    statistics.mean(h['cpu_min']    for h in hw_metrics_list) if hw_metrics_list else None,
        'cpu_min_min':    min(h['cpu_min']                for h in hw_metrics_list) if hw_metrics_list else None,
        'memory_max_avg': statistics.mean(h['memory_max'] for h in hw_metrics_list) if hw_metrics_list else None,
        'memory_max_max': max(h['memory_max']             for h in hw_metrics_list) if hw_metrics_list else None,
        'memory_max_min': min(h['memory_max']             for h in hw_metrics_list) if hw_metrics_list else None,
        'memory_avg_avg': statistics.mean(h['memory_avg'] for h in hw_metrics_list) if hw_metrics_list else None,
        'memory_avg_max': max(h['memory_avg']             for h in hw_metrics_list) if hw_metrics_list else None,
        'memory_avg_min': min(h['memory_avg']             for h in hw_metrics_list) if hw_metrics_list else None,
        'memory_min_avg': statistics.mean(h['memory_min'] for h in hw_metrics_list) if hw_metrics_list else None,
        'memory_min_min': min(h['memory_min']             for h in hw_metrics_list) if hw_metrics_list else None,
    }

    print()
    print(f"   ✓ Saturation:  {saturation_users} requests/second (1 worker)")
    if confidence_samples > 0:
        print(f"   Confidence ({confidence_users} users × {confidence_samples} runs):")
        print(f"      Mean: {mean_fired:.1f} | Std: {std_fired:.1f} | Min: {min_fired} | Max: {max_fired}")

    baseline = dict(_client_metrics.CLIENT_BASELINES)

    def _delta(base_val, hw_val):
        if base_val is None or base_val == 0 or hw_val is None:
            return {'absolute': None, 'pct_change': None}
        return {
            'absolute':   round(hw_val - base_val, 2),
            'pct_change': round((hw_val - base_val) / base_val * 100, 1),
        }

    cpu_base_max = baseline.get('cpu_max');  cpu_base_avg = baseline.get('cpu_avg');  cpu_base_min = baseline.get('cpu_min')
    mem_base_max = baseline.get('mem_max');  mem_base_avg = baseline.get('mem_avg');  mem_base_min = baseline.get('mem_min')

    hw_deltas = {
        'cpu_max_avg':    _delta(cpu_base_max, hw_agg['cpu_max_avg']),
        'cpu_max_max':    _delta(cpu_base_max, hw_agg['cpu_max_max']),
        'cpu_avg_avg':    _delta(cpu_base_avg, hw_agg['cpu_avg_avg']),
        'cpu_min_avg':    _delta(cpu_base_min, hw_agg['cpu_min_avg']),
        'memory_max_avg': _delta(mem_base_max, hw_agg['memory_max_avg']),
        'memory_max_max': _delta(mem_base_max, hw_agg['memory_max_max']),
        'memory_avg_avg': _delta(mem_base_avg, hw_agg['memory_avg_avg']),
        'memory_min_avg': _delta(mem_base_min, hw_agg['memory_min_avg']),
    }

    return {
        'saturation_users':   saturation_users,
        'confidence_users':   confidence_users,
        'confidence_samples': confidence_samples,
        'mean_fired':         mean_fired,
        'std_fired':          std_fired,
        'min_fired':          min_fired,
        'max_fired':          max_fired,
        'fired_counts':       fired_counts,
        'hw_metrics':         hw_agg,
        'hw_metrics_list':    hw_metrics_list,
        'hw_deltas':          hw_deltas,
        'baseline':           baseline,
        'run_dir':            run_dir,
    }
