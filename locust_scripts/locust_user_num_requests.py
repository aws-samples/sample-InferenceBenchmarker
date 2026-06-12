"""Locust user definition for num_requests mode.

Same as locust_user.py except the test stops when LOCUST_TOTAL_REQUESTS is reached
(checked on the worker after each events.request.fire() call, no polling lag).

Loaded by locust_primary.py and locust_worker.py when find_rps.sh is invoked with
--num-requests. FACTORIES_PATH points to a cloudpickle file written by:
  - server_capacity/_find_rps_serialize.py  (find_rps.sh path)

payload_factory protocol — always returns:
    {'pre_computed': True,  'input': List[Any]}   — each user gets one item by index;
                                                     cycles if users > len(input),
                                                     warns if users < len(input)
    {'pre_computed': False, 'input': Callable}    —  input() called per request
"""

import os
import sys
import time
import threading

import cloudpickle
import psutil
from locust import FastHttpUser, constant, events, task

with open(os.environ['FACTORIES_PATH'], 'rb') as _f:
    _factories = cloudpickle.load(_f)

_invoke_factory  = _factories['invoke_factory']
_payload_factory = _factories['payload_factory']

_INVOKE_FN = None if '--master' in sys.argv else _invoke_factory()

_payload_config = _payload_factory()
_pre_computed   = _payload_config['pre_computed']
_payload_input  = _payload_config['input']

_requests_fired  = 0
_user_index      = -1
_TOTAL_REQUESTS  = int(os.environ.get('LOCUST_TOTAL_REQUESTS', '0'))
_env             = None


@events.init.add_listener
def on_init(environment, **kwargs):
    global _env
    _env = environment


# fires on master each time a worker reports stats (every WORKER_REPORT_INTERVAL=3s)
@events.worker_report.add_listener
def on_worker_report(client_id, data, **kwargs):
    if _env and _env.stats.total.num_requests >= _TOTAL_REQUESTS:
        _env.runner.quit()


# ---------------------------------------------------------------------------
# Client hardware sampling — only on master when BENCHMARKER_SAMPLE_HW=1
# ---------------------------------------------------------------------------
_SAMPLE_HW   = '--master' in sys.argv and os.environ.get('BENCHMARKER_SAMPLE_HW', '0') == '1'
_hw_stop     = threading.Event()
_cpu_samples = []
_mem_samples = []
_hw_thread   = None


def _hw_sample_loop():
    psutil.cpu_percent(interval=None)
    while not _hw_stop.is_set():
        _cpu_samples.append(psutil.cpu_percent(interval=None))
        _mem_samples.append(psutil.virtual_memory().percent)
        time.sleep(0.01)


@events.spawning_complete.add_listener
def on_spawning_complete(user_count, **kwargs):
    global _hw_thread
    if _SAMPLE_HW:
        _hw_thread = threading.Thread(target=_hw_sample_loop, daemon=True)
        _hw_thread.start()
    if _pre_computed:
        n = len(_payload_input)
        if user_count > n:
            print(f"   [locust_user] Warning: {user_count} users > {n} pre_computed inputs — cycling through list")
        elif user_count < n:
            print(f"   [locust_user] Warning: {n - user_count} pre_computed inputs unused ({n} inputs, {user_count} users)")


@events.quitting.add_listener
def on_quitting(environment, **kwargs):
    if _SAMPLE_HW:
        _hw_stop.set()
        if _hw_thread:
            _hw_thread.join(timeout=1)
        if _cpu_samples and _mem_samples:
            cpu_count    = psutil.cpu_count(logical=True)
            total_ram_gb = psutil.virtual_memory().total / (1024 ** 3)
            cpu_max = max(_cpu_samples);  cpu_avg = sum(_cpu_samples) / len(_cpu_samples)
            mem_max = max(_mem_samples);  mem_avg = sum(_mem_samples) / len(_mem_samples)
            print()
            print("   📊 Client Hardware:")
            print(f"   Client CPU:    max = {cpu_max:.2f}% ({cpu_max/100*cpu_count:.1f}/{cpu_count} cores) | avg = {cpu_avg:.2f}% ({cpu_avg/100*cpu_count:.1f}/{cpu_count} cores)")
            print(f"   Client Memory: max = {mem_max:.2f}% ({mem_max/100*total_ram_gb:.2f}/{total_ram_gb:.1f} GB) | avg = {mem_avg:.2f}% ({mem_avg/100*total_ram_gb:.2f}/{total_ram_gb:.1f} GB)")

    wave_dir = os.environ['LOCUST_WAVE_DIR']

    if '--master' in sys.argv:
        # write raw start/last-request epochs (TZ-safe) for postprocess (wave time + RPS)
        # and fetch_server_metrics (CloudWatch query window).
        # TODO: start_time is set when MasterRunner.start() runs (before workers connect/spawn),
        # so it includes worker-connect + user-spawn ramp. Pass --reset-stats in find_rps.sh to
        # reset start_time on spawning_complete and exclude the ramp.
        run_dir = os.path.dirname(wave_dir)  # LOCUST_WAVE_DIR is RUN_DIR/requests_fired
        wave_window_path = os.path.join(run_dir, 'wave_window.txt')
        start = environment.stats.total.start_time
        last  = environment.stats.total.last_request_timestamp
        with open(wave_window_path, 'w') as f:
            if start and last:
                f.write(f"{start} {last}")
            else:
                f.write("WARN")

    if '--worker' not in sys.argv:
        return
    worker_index = os.environ.get('WORKER_INDEX', str(os.getpid()))
    path = os.path.join(wave_dir, f'worker_{worker_index}.txt')
    try:
        with open(path, 'w') as f:
            f.write(str(_requests_fired))
    except OSError as e:
        raise RuntimeError(
            f"Failed to write requests_fired count to {path}: {e}"
        ) from e


def _get_payload(user_idx):
    if not _pre_computed:
        return _payload_input()
    return _payload_input[user_idx % len(_payload_input)]


def _invoke():
    global _requests_fired, _user_index
    _user_index += 1
    user_idx = _user_index
    start   = time.monotonic()
    exc     = None
    length  = 0
    payload = _get_payload(user_idx)

    _requests_fired += 1

    try:
        result = _INVOKE_FN(payload)
        if isinstance(result, dict) and 'Body' in result:
            length = len(result['Body'].read())
    except Exception as e:
        exc = e

    events.request.fire(
        request_type='boto3',
        name=os.environ.get('ENDPOINT_NAME', 'endpoint'),
        response_time=(time.monotonic() - start) * 1000,
        response_length=length,
        exception=exc,
        context={},
    )


class InferenceBenchmarker(FastHttpUser):
    host = 'https://amazonaws.com'
    wait_time = constant(float('inf'))

    @task
    def invoke_endpoint(self):
        _invoke()
