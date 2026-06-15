# Client Diagnostics

Tools to measure the load-generating client's capacity. Importable from the package:

```python
from client_capacity.worker_saturation import find_worker_saturation
from client_capacity.rps_saturation import find_rps_saturation
```

Each runs short Locust bursts against the endpoint described by your `factories_file` and writes a timestamped log + per-burst
output under `.tmp/<timestamp>_<tool>/`.

---

## `find_worker_saturation`

Finds the max requests a **single Locust worker** can fire in one second.

1. Establishes a client CPU/memory baseline.
2. **Phase 1:** runs 1-second bursts, adding `user_step` users each round, until fewer requests
   fire than users requested — that requests-fired count is the worker's per-second ceiling
   (`saturation_users`).
3. **Phase 2:** re-runs `confidence_samples` bursts at `saturation × (1 + confidence_users_scale)`,
   sampling client hardware and waiting for the client to idle between runs.

| Arg | Default | Meaning |
|---|---|---|
| `factories_file` | — | factories `.py` exposing `invoke_factory` / `payload_factory` |
| `start_users` | `50` | users in the first Phase-1 burst |
| `end_users` | `None` | cap on users to try (`None` = no cap) |
| `user_step` | `5` | users added each Phase-1 round |InferenceBenchmarker/models
| `confidence_samples` | `10` | number of Phase-2 confirmation bursts |
| `confidence_users_scale` | `0.20` | Phase-2 users = saturation × (1 + this) |

Returns a dict including `saturation_users` (the per-worker ceiling). Feed it into
`find_rps_saturation`.

```python
{
    'saturation_users':   240,        # the per-worker per-second ceiling
    'confidence_users':   288,        # saturation × (1 + confidence_users_scale)
    'confidence_samples': 10,
    'mean_fired':         238.6,      # mean requests fired across confidence runs
    'std_fired':          4.2,
    'min_fired':          231,
    'max_fired':          245,
    'fired_counts':       [240, 237, 241, ...],   # one per confidence run
    'hw_metrics':         {'cpu_max_avg': 92.1, 'memory_max_avg': 18.7, ...},  # client CPU/mem aggregates
    'hw_deltas':          {'cpu_max_avg': {'absolute': 71.3, 'pct_change': 340.5}, ...},  # vs idle baseline
    'baseline':           {'cpu_avg': 20.8, 'mem_avg': 16.1, ...},
    'run_dir':            '.tmp/20260614_…_worker_saturation',
}
```

---

## `find_rps_saturation`

Finds how many **workers** to run before total requests fired plateaus (adding workers stops
helping). Uses `saturation_users` as the users-per-worker, then adds `worker_step` workers each
round until the gain drops below `plateau_threshold`. A confidence phase repeats runs at the
saturation point.

| Arg | Default | Meaning |
|---|---|---|
| `factories_file` | — | factories `.py` |
| `saturation_users` | — | users per worker, from `find_worker_saturation` |
| `start_workers` | `1` | workers in the first round |
| `end_workers` | `None` | cap on workers to try (`None` = no cap) |
| `worker_step` | `1` | workers added each round |
| `plateau_threshold` | `1` | stop when added workers gain fewer than this many requests |
| `confidence_samples` | `10` | number of confirmation runs |

Returns a dict including `saturation_workers` and `saturation_requests` (the host's total
per-second ceiling), per-round `results`, and confidence stats.

```python
{
    'saturation_workers':  6,         # last round BEFORE the plateau
    'saturation_requests': 1378,      # requests/second at saturation_workers (round 6 above)
    'users_per_worker':    240,       # = saturation_users passed in
    'results': [                      # one entry per round
        {'num_workers': 1, 'total_users': 240,  'requests_fired': 238,  'gain': None, 'hw_metrics': {...}},
        {'num_workers': 2, 'total_users': 480,  'requests_fired': 470,  'gain': 232,  'hw_metrics': {...}},
        # ... rounds 3–5 ...
        {'num_workers': 6, 'total_users': 1440, 'requests_fired': 1378, 'gain': 18,   'hw_metrics': {...}},
        {'num_workers': 7, 'total_users': 1680, 'requests_fired': 1379, 'gain': 1,    'hw_metrics': {...}},  # gain < plateau_threshold → stop
    ],
    'confidence_workers':  7,         # int(saturation_workers × (1 + confidence_users_scale)) = int(6 × 1.2)
    'confidence_samples':  10,
    'confidence_fired':    [1377, 1380, 1376, ...],   # one per confidence run
    'confidence_mean':     1378.2,
    'confidence_std':      4.8,
    'run_dir':             '.tmp/20260614_…_rps_saturation',
}
```

Use `saturation_workers` to set a wave's `--workers`.

---

## `find_file_descriptors_limit`

**(Locust waves only.)** Reports the client's file-descriptor limits. A request holds an open
socket (one fd) only **while it is in flight**, so the **per-process soft limit caps how many
requests a worker can have outstanding at once**.

Actual fd usage depends on the `invoke_factory` logic. e.g. An fd is freed when the last reference to its socket is dropped. e.g. Pooled connections will reuse the client(and the fd) for multiple requests up to the pool limit.


Raise the limits:

```sh
# inspect current limits
ulimit -Sn                 # per-worker soft limit — caps concurrent in-flight requests per worker
ulimit -Hn                 # per-worker hard limit
cat /proc/sys/fs/file-max  # system-wide total across all workers and other processes

# per-worker, no sudo — raise soft up to the existing hard limit (this shell + workers it spawns)
ulimit -n 65535            # 65535 = new soft limit (must be <= hard limit)

# per-worker, raise both hard and soft, then launch benchmark in the same shell
sudo prlimit --pid $$ --nofile=1048576:1048576   # --nofile=SOFT:HARD; 1048576 = 2^20 fds

# persist per-worker limits across logins (edit, then re-login)
echo '* soft nofile 1048576' | sudo tee -a /etc/security/limits.conf   # 1048576 = new soft limit
echo '* hard nofile 1048576' | sudo tee -a /etc/security/limits.conf   # 1048576 = new hard limit

# system-wide cap, with sudo — only when total fds across all workers exhaust file-max
sudo sysctl -w fs.file-max=2097152   # 2097152 = 2^21 total fds across all processes
```
