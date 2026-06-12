"""Client hardware metrics — baseline, sampling, and idle detection via psutil."""

import time
import threading

import psutil


# Module-level client baselines (set once by establish_client_baseline)
CLIENT_BASELINES: dict = {}


def establish_client_baseline(sample_seconds=5, sample_interval_seconds=0):
    """Sample client hardware at rest to establish baseline metrics.

    Args:
        sample_seconds: How long to sample (default 5s)
        sample_interval_seconds: Sleep between samples (default 0 = max resolution)

    Returns:
        dict: {metric_name: max_value} e.g. {'cpu': 3.2, 'memory': 45.1, 'net_bytes_sent': 0}
    """
    global CLIENT_BASELINES

    print(f"   🔍 Sampling client baseline for {sample_seconds}s...")
    psutil.cpu_percent(interval=0.1)  # discard first call to initialize delta

    cpu_samples = []
    mem_samples = []

    start = time.time()
    while time.time() - start < sample_seconds:
        cpu_samples.append(psutil.cpu_percent(interval=0.1))
        mem_samples.append(psutil.virtual_memory().percent)
        if sample_interval_seconds > 0:
            time.sleep(sample_interval_seconds)

    cpu_count    = psutil.cpu_count(logical=True)
    vm           = psutil.virtual_memory()
    total_ram_gb = vm.total / (1024 ** 3)

    CLIENT_BASELINES = {
        'cpu_max':  max(cpu_samples),
        'cpu_min':  min(cpu_samples),
        'cpu_avg':  sum(cpu_samples) / len(cpu_samples),
        'mem_max':  max(mem_samples),
        'mem_min':  min(mem_samples),
        'mem_avg':  sum(mem_samples) / len(mem_samples),
        'cpu_count':    cpu_count,
        'total_ram_gb': total_ram_gb,
    }

    def _fmt_row(label, mn, avg, mx, abs_fn):
        mx_s = f"{mx:.1f}%"
        return f"   {label} max = {mx_s} ({abs_fn(mx)})"

    cpu_abs = lambda p: f"{p/100*cpu_count:.1f}/{cpu_count} cores"
    mem_abs = lambda p: f"{p/100*total_ram_gb:.2f}/{total_ram_gb:.1f} GB"

    print(f"   📊 Client Baseline Metrics (system-wide, {len(cpu_samples)} samples):")
    print(_fmt_row("CPU:   ", CLIENT_BASELINES['cpu_min'], CLIENT_BASELINES['cpu_avg'], CLIENT_BASELINES['cpu_max'], cpu_abs))
    print(_fmt_row("Memory:", CLIENT_BASELINES['mem_min'], CLIENT_BASELINES['mem_avg'], CLIENT_BASELINES['mem_max'], mem_abs))

    return CLIENT_BASELINES


def sample_client_metrics(duration_seconds, sample_interval_seconds=0.01):
    """Sample client CPU and memory for a given duration in a background thread.

    Args:
        duration_seconds: How long to sample
        sample_interval_seconds: Sleep between samples (default 0 = max resolution)

    Returns:
        dict: {cpu_peak, cpu_avg, memory_peak, memory_avg, sample_count}
    """
    cpu_samples = []
    mem_samples = []
    stop_event  = threading.Event()

    def _sample_cpu():
        psutil.cpu_percent(interval=None)  # discard first call to initialize delta for this thread context
        while not stop_event.is_set():
            cpu_samples.append(psutil.cpu_percent(interval=None))
            if sample_interval_seconds > 0:
                time.sleep(sample_interval_seconds)  # throttle to avoid skewing the CPU metric being measured

    def _sample_mem():
        while not stop_event.is_set():
            mem_samples.append(psutil.virtual_memory().percent)
            if sample_interval_seconds > 0:
                time.sleep(sample_interval_seconds)

    t_cpu = threading.Thread(target=_sample_cpu, daemon=True)
    t_mem = threading.Thread(target=_sample_mem, daemon=True)
    t_cpu.start()
    t_mem.start()
    time.sleep(duration_seconds)
    stop_event.set()
    t_cpu.join(timeout=1)
    t_mem.join(timeout=1)

    if not cpu_samples or not mem_samples:
        return {'cpu_max': 0.0, 'cpu_avg': 0.0, 'cpu_min': 0.0,
                'memory_max': 0.0, 'memory_avg': 0.0, 'memory_min': 0.0,
                'sample_count': 0}

    return {
        'cpu_max':    max(cpu_samples),
        'cpu_avg':    sum(cpu_samples) / len(cpu_samples),
        'cpu_min':    min(cpu_samples),
        'memory_max': max(mem_samples),
        'memory_avg': sum(mem_samples) / len(mem_samples),
        'memory_min': min(mem_samples),
        'sample_count': len(cpu_samples),
    }


def get_idle_criteria_info(max_wait_config=None):
    """Return (criteria_label, threshold_info, wait_limit, max_wait_seconds) for display."""
    if not CLIENT_BASELINES:
        establish_client_baseline()
    if max_wait_config is None:
        max_wait_config = {}

    wait_limit       = max_wait_config.get('wait_limit', False)
    max_wait_seconds = max_wait_config.get('max_wait_seconds', 120)

    _METRIC_CONFIG = {
        'cpu':    ('cpu_max', 'cpu_threshold',    'cpu_tolerance',    'CPU'),
        'memory': ('mem_max', 'memory_threshold', 'memory_tolerance', 'Memory'),
    }

    has_explicit = False
    thresholds = {}
    for metric, (baseline_key, thresh_key, tol_key, _) in _METRIC_CONFIG.items():
        baseline = CLIENT_BASELINES.get(baseline_key)
        explicit = max_wait_config.get(thresh_key)
        tolerance = max_wait_config.get(tol_key)
        candidates = []
        if explicit is not None:
            candidates.append(explicit)
            has_explicit = True
        if tolerance is not None and baseline is not None:
            candidates.append(baseline * (1 + tolerance))
            has_explicit = True
        if not candidates:
            candidates.append(baseline * 1.10 if baseline is not None else 10.0)
        thresholds[metric] = max(candidates)

    if not has_explicit:
        criteria_label = "Idle criteria (baseline)"
        threshold_info = "Client baseline metrics calculated above"
    else:
        criteria_label = "Idle criteria (provided threshold/tolerance)"
        parts = [f"{_METRIC_CONFIG[m][3]}<{t:.2f}%" for m, t in thresholds.items()]
        threshold_info = ", ".join(parts)

    return criteria_label, threshold_info, wait_limit, max_wait_seconds


def wait_for_client_idle(
    max_wait_config=None,
    check_interval=1,
    print_header=True,
):
    """Wait for client hardware to return to idle state.

    Uses CLIENT_BASELINES (set by establish_client_baseline) plus optional
    thresholds/tolerances in max_wait_config:

        max_wait_config = {
            'wait_limit': True,
            'max_wait_seconds': 60,
            'cpu_threshold': 10.0,     # explicit idle threshold
            'cpu_tolerance': 0.10,     # baseline * (1 + 0.10) — 10% above baseline
            'memory_threshold': ...,
            'memory_tolerance': ...,
        }

    Effective threshold per metric:
        max(explicit_threshold, baseline * (1 + tolerance))  if both set
        explicit_threshold                                    if only threshold set
        baseline * (1 + tolerance)                           if only tolerance set
        baseline * 1.10                                       if neither set (default 10%)

    Args:
        max_wait_config: dict with wait behavior and optional thresholds/tolerances
        check_interval: seconds between checks (default 1)
    """
    if max_wait_config is None:
        max_wait_config = {}

    wait_limit       = max_wait_config.get('wait_limit', False)
    max_wait_seconds = max_wait_config.get('max_wait_seconds', 120)

    # Compute effective threshold per metric
    # _METRIC_CONFIG: metric_key → (baseline_key, threshold_key, tolerance_key, display_name)
    _METRIC_CONFIG = {
        'cpu':    ('cpu_max', 'cpu_threshold',    'cpu_tolerance',    'CPU'),
        'memory': ('mem_max', 'memory_threshold', 'memory_tolerance', 'Memory'),
    }

    has_explicit = False
    thresholds = {}
    for metric, (baseline_key, thresh_key, tol_key, _) in _METRIC_CONFIG.items():
        baseline = CLIENT_BASELINES.get(baseline_key)
        explicit = max_wait_config.get(thresh_key)
        tolerance = max_wait_config.get(tol_key)

        candidates = []
        if explicit is not None:
            candidates.append(explicit)
            has_explicit = True
        if tolerance is not None and baseline is not None:
            candidates.append(baseline * (1 + tolerance))
            has_explicit = True
        if not candidates:
            candidates.append(baseline * 1.10 if baseline is not None else 10.0)

        thresholds[metric] = max(candidates)

    if not has_explicit:
        criteria_label = "Idle criteria (baseline)"
        threshold_info = "Client baseline metrics calculated above"
    else:
        criteria_label = "Idle criteria (provided threshold/tolerance)"
        parts = [f"{_METRIC_CONFIG[m][3]}<{t:.2f}%" for m, t in thresholds.items()]
        threshold_info = ", ".join(parts)

    print(f"   ⏸️  Waiting for client to return to idle...")
    if print_header:
        print(f"     {criteria_label}: {threshold_info}")
        if not wait_limit:
            print(f"     Wait limit: disabled (will wait indefinitely)")
        else:
            print(f"     Wait limit: {max_wait_seconds}s")

    start_wait = time.time()
    check_count = 0
    psutil.cpu_percent(interval=0.1)  # discard first call to initialize delta

    while True:
        if wait_limit and (time.time() - start_wait) >= max_wait_seconds:
            print(f"   ⚠️  Client idle timeout after {time.time() - start_wait:.1f}s (proceeding anyway)")
            return

        check_count += 1
        cpu = psutil.cpu_percent(interval=0.1)
        mem = psutil.virtual_memory().percent

        current = {'cpu': cpu, 'memory': mem}
        display_names = {'cpu': 'CPU', 'memory': 'Memory'}

        status_parts = [f"{display_names[m]}={current[m]:.2f}%" for m in thresholds]
        print(f"     Check {check_count}: {', '.join(status_parts)}")

        checks_failed = []
        for metric, thresh in thresholds.items():
            val = round(current[metric], 2)
            t = round(thresh, 2)
            if val > t:
                checks_failed.append(f"{display_names[metric]} {val:.2f}% > {t:.2f}%")

        if checks_failed:
            print(f"        (Not idle: {', '.join(checks_failed)})")
        else:
            print(f"     ✓ Client idle (waited {time.time() - start_wait:.1f}s)")
            return

        time.sleep(check_interval)
