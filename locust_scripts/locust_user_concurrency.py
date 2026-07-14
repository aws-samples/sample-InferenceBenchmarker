"""Locust user definition for concurrency mode.

Same as locust_user.py except each user loops (wait_time = constant(0)) instead of firing
once, so a fixed user count == a fixed number of concurrent in-flight requests. Bounded by
--run-time (obs-time); used when --concurrency is given without --num-requests.

Loaded by locust_primary.py and locust_worker.py for each benchmark wave.
FACTORIES_PATH points to a cloudpickle file written by:
  - server_capacity/_find_rps_serialize.py  (find_rps.sh path)
  - client_capacity/_burst.py               (find_worker_saturation / find_rps_saturation path)

payload_factory protocol — always returns:
    {'pre_computed': True,  'input': List[Any]}   — each request gets one item by index;
                                                     cycles across requests
    {'pre_computed': False, 'input': Callable}    —  input() called per request
"""

import os
import sys
import time
import threading

import gevent

import cloudpickle
import psutil
from locust import FastHttpUser, constant, events, task
import numpy as np

with open(os.environ['FACTORIES_PATH'], 'rb') as _f:
    _factories = cloudpickle.load(_f)

_invoke_factory  = _factories['invoke_factory']
_payload_factory = _factories['payload_factory']

_INVOKE_FN = None if '--master' in sys.argv else _invoke_factory()

_payload_config = _payload_factory()
_pre_computed   = _payload_config['pre_computed']
_payload_input  = _payload_config['input']   # List when pre_computed, callable otherwise

_requests_fired = 0
_user_index     = -1   # incremented per request; gevent cooperative — no lock needed

# ---------------------------------------------------------------------------
# Client hardware sampling — only on master when BENCHMARKER_SAMPLE_HW=1
# ---------------------------------------------------------------------------
_SAMPLE_HW   = '--master' in sys.argv and os.environ.get('BENCHMARKER_SAMPLE_HW', '0') == '1'
_hw_stop     = threading.Event()
_cpu_samples = []
_mem_samples = []
_hw_thread   = None


def _hw_sample_loop():
    psutil.cpu_percent(interval=None)  # discard first call to initialize delta
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
            print(f"   [locust_user] Note: concurrency {user_count} > {n} pre_computed inputs — inputs cycle across requests")


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
        # To show response size in locust results (the "Average Content Size" column),
        # derive a length from your invoke()'s return value here and assign it to `length`
        # below.
        # if isinstance(result, dict) and 'Body' in result:
        #     length = len(result['Body'].read())
    except Exception as e:
        exc = e

    events.request.fire(
        request_type='boto3',  # feed into locust_stats.csv
        name=os.environ.get('ENDPOINT_NAME', 'endpoint'), # feed into locust_stats.csv
        response_time=(time.monotonic() - start) * 1000,  # feed into locust_stats_history.csv for latency stats
        response_length=length,
        exception=exc,
        context={},
    )


class InferenceBenchmarker(FastHttpUser):
    host = 'https://amazonaws.com'
    # Concurrency mode: fire → wait for completion → fire again immediately. constant(0)
    # keeps exactly --users (= C) requests in flight for the whole wave (bounded by --run-time).
    wait_time = constant(0)

    @task
    def invoke_endpoint(self):
        _invoke()
