"""RPS saturation — find how many workers needed before requests/s stops increasing."""

import os

from . import client_metrics as _client_metrics
from ._burst import run_burst
from ._tee import Tee


def find_rps_saturation(
    factories_file,
    saturation_users,
    start_workers=1,
    end_workers=None,
    worker_step=1,
    plateau_threshold=1,
    confidence_samples=10,
    confidence_users_scale=0.20,
    sample_interval_seconds=0.01,
    client_idle_max_wait_config=None,
):
    """Find how many workers are needed before requests_fired stops increasing.

    Uses saturation_users (from find_worker_saturation) as users per worker.
    Starts with worker_step workers, adds worker_step each iteration until
    requests_fired gain < plateau_threshold.

    Args:
        factories_file: Path to a factories .py file exposing invoke_factory/payload_factory
        saturation_users: Users per worker (from find_worker_saturation result['saturation_users'])
        start_workers: Starting worker count (default 1)
        end_workers: Max worker count to test — None means no limit (default None)
        worker_step: Workers added per iteration (default 1)
        plateau_threshold: Stop when gain < this many requests (default 1)
        confidence_samples: Number of runs for confidence phase (default 10)
        confidence_users_scale: confidence workers = saturation_workers * (1 + scale) (default 0.20)
        sample_interval_seconds: Sleep between psutil samples (default 0.01)
        client_idle_max_wait_config: max_wait_config for wait_for_client_idle

    Returns:
        dict with per-worker results and saturation point
    """
    from datetime import datetime
    from .client_metrics import wait_for_client_idle, get_idle_criteria_info

    users_per_worker = saturation_users

    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    _root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    run_dir = os.path.join(_root, '.tmp', f'{timestamp}_rps_saturation')
    os.makedirs(run_dir, exist_ok=True)

    with Tee(os.path.join(run_dir, 'rps_saturation.log')):
        return _find_rps_saturation(
            factories_file, users_per_worker, start_workers, end_workers,
            worker_step, plateau_threshold, confidence_samples, confidence_users_scale,
            sample_interval_seconds, client_idle_max_wait_config, run_dir,
            wait_for_client_idle, get_idle_criteria_info,
        )


def _find_rps_saturation(
    factories_file, users_per_worker, start_workers, end_workers,
    worker_step, plateau_threshold, confidence_samples, confidence_users_scale,
    sample_interval_seconds, client_idle_max_wait_config, run_dir,
    wait_for_client_idle, get_idle_criteria_info,
):
    """Inner implementation — runs inside Tee context so all prints are captured to rps_saturation.log.

    Additional args:
        run_dir: Timestamped directory under .tmp/ for burst output and log file
        wait_for_client_idle: Bound callable from client_metrics (passed to avoid re-importing)
        get_idle_criteria_info: Bound callable from client_metrics (passed to avoid re-importing)
    """
    import statistics

    print("=" * 80)
    print("RPS SATURATION — Find worker count where requests/s stops increasing")
    print("=" * 80)
    print(f"   Users per worker:  {users_per_worker} (from worker saturation)")
    print(f"   Start workers:     {start_workers}")
    print(f"   End workers:       {end_workers if end_workers is not None else 'unlimited'}")
    print(f"   Worker step:       {worker_step}")
    print(f"   Plateau threshold: {plateau_threshold} requests")
    print(f"   Confidence runs:   {confidence_samples}")
    print(f"   Confidence scale:  +{confidence_users_scale*100:.0f}% above saturation workers")
    print(f"   Results dir:       {run_dir}")
    print()

    criteria_label, threshold_info, wait_limit, max_wait_seconds = get_idle_criteria_info(client_idle_max_wait_config)
    print(f"   {criteria_label}: {threshold_info}")
    print(f"   Wait limit: {max_wait_seconds}s" if wait_limit else f"   Wait limit: disabled")
    print("-" * 40)

    results = []
    num_workers = start_workers
    prev_fired = None
    saturation_workers = None

    while True:
        if end_workers is not None and num_workers > end_workers:
            print(f"\n   ⚠️  Reached end_workers limit ({end_workers}) without plateau.")
            saturation_workers = num_workers - worker_step
            break
        total_users = num_workers * users_per_worker
        print(f"\n🔄 {num_workers} workers × {users_per_worker} users = {total_users} total users")

        wave_dir = os.path.join(run_dir, f'workers_{num_workers}')
        requests_fired, hw = run_burst(
            factories_file, total_users, num_workers, wave_dir,
            sample_interval_seconds=sample_interval_seconds,
        )

        cpu_count    = _client_metrics.CLIENT_BASELINES.get('cpu_count', 1)
        total_ram_gb = _client_metrics.CLIENT_BASELINES.get('total_ram_gb', 1)
        gain = requests_fired - prev_fired if prev_fired is not None else None

        print(f"   Requests fired: {requests_fired}" + (f" (gain: +{gain})" if gain is not None else ""))
        print(f"   Client CPU:    max = {hw['cpu_max']:.2f}% ({hw['cpu_max']/100*cpu_count:.1f}/{cpu_count} cores) | avg = {hw['cpu_avg']:.2f}% ({hw['cpu_avg']/100*cpu_count:.1f}/{cpu_count} cores)")
        print(f"   Client Memory: max = {hw['memory_max']:.2f}% ({hw['memory_max']/100*total_ram_gb:.2f}/{total_ram_gb:.1f} GB) | avg = {hw['memory_avg']:.2f}% ({hw['memory_avg']/100*total_ram_gb:.2f}/{total_ram_gb:.1f} GB)")

        results.append({
            'num_workers':    num_workers,
            'total_users':    total_users,
            'requests_fired': requests_fired,
            'gain':           gain,
            'hw_metrics':     hw,
        })

        if gain is not None and gain < plateau_threshold:
            saturation_workers = num_workers - worker_step
            print(f"\n   ✓ Plateau detected: gain {gain} < threshold {plateau_threshold}")
            print(f"   ✓ RPS saturated at {saturation_workers} workers → {prev_fired} requests/second")
            break

        prev_fired = requests_fired
        num_workers += worker_step

        wait_for_client_idle(max_wait_config=client_idle_max_wait_config, print_header=False)

    # ── Confidence phase ──────────────────────────────────────────────────────
    confidence_workers = int(saturation_workers * (1 + confidence_users_scale)) if saturation_workers else 1
    confidence_total_users = confidence_workers * users_per_worker
    conf_fired_counts = []
    conf_hw_list = []

    if confidence_samples > 0:
        print()
        print(f"📊 Confidence runs")
        print(f"   Workers: {confidence_workers} ({saturation_workers} + {confidence_users_scale*100:.0f}%)")
        print(f"   Users:   {confidence_total_users} ({confidence_workers} × {users_per_worker})")
        print(f"   Runs:    {confidence_samples} × 1 second")

        criteria_label, threshold_info, wait_limit, max_wait_seconds = get_idle_criteria_info(client_idle_max_wait_config)
        print(f"   {criteria_label}: {threshold_info}")
        print(f"   Wait limit: {max_wait_seconds}s" if wait_limit else f"   Wait limit: disabled")
        print("-" * 40)

        for i in range(1, confidence_samples + 1):
            print(f"\n🔄 Run {i} — {confidence_workers} workers × {users_per_worker} users")
            wave_dir = os.path.join(run_dir, f'confidence_run{i}_w{confidence_workers}')
            fired, hw = run_burst(
                factories_file, confidence_total_users, confidence_workers, wave_dir,
                sample_interval_seconds=sample_interval_seconds,
            )
            conf_fired_counts.append(fired)
            conf_hw_list.append(hw)
            cpu_count    = _client_metrics.CLIENT_BASELINES.get('cpu_count', 1)
            total_ram_gb = _client_metrics.CLIENT_BASELINES.get('total_ram_gb', 1)
            print(f"   Requests fired: {fired}")
            print(f"   Client CPU:    max = {hw['cpu_max']:.1f}% ({hw['cpu_max']/100*cpu_count:.1f}/{cpu_count} cores)")
            print(f"   Client Memory: max = {hw['memory_max']:.1f}% ({hw['memory_max']/100*total_ram_gb:.2f}/{total_ram_gb:.1f} GB)")
            if i < confidence_samples:
                wait_for_client_idle(max_wait_config=client_idle_max_wait_config, print_header=False)

    mean_fired = statistics.mean(conf_fired_counts) if conf_fired_counts else None
    std_fired  = statistics.stdev(conf_fired_counts) if len(conf_fired_counts) > 1 else 0.0

    saturation_requests = prev_fired or 0

    print()
    print("RESULTS")
    print("=" * 7)
    print(f"   {'Workers':<10} {'Requests/s':<14} {'Gain'}")
    print(f"   {'-'*10} {'-'*14} {'-'*8}")
    for r in results:
        gain_str = f"+{r['gain']}" if r['gain'] is not None else "—"
        marker = " ◀ saturation" if r['num_workers'] == saturation_workers else ""
        print(f"   {r['num_workers']:<10} {r['requests_fired']:<14} {gain_str}{marker}")
    if confidence_samples > 0:
        print()
        print(f"   Confidence ({confidence_workers} workers × {confidence_samples} runs):")
        print(f"      Mean: {mean_fired:.1f} | Std: {std_fired:.1f} | Min: {min(conf_fired_counts)} | Max: {max(conf_fired_counts)}")
    print("=" * 80)

    return {
        'saturation_workers':          saturation_workers,
        'saturation_requests':         saturation_requests,
        'users_per_worker':            users_per_worker,
        'results':                     results,
        'confidence_workers':          confidence_workers,
        'confidence_samples':          confidence_samples,
        'confidence_fired':            conf_fired_counts,
        'confidence_mean':             mean_fired,
        'confidence_std':              std_fired,
        'confidence_hw_list':          conf_hw_list,
        'run_dir':                     run_dir,
    }
