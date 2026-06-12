"""Locust primary process for InferenceBenchmarker.

Launched by _burst.py (find_worker_saturation / find_rps_saturation) and find_rps.sh.
Reads all configuration from environment variables, then starts the Locust master by
manipulating sys.argv and calling locust.main.main().

Environment variables:
    LOCUST_USERS               : total number of users to spawn
    LOCUST_SPAWN_RATE          : users spawned per second
    LOCUST_WORKERS             : number of worker processes to wait for before starting
    LOCUST_PORT                : TCP port for primary/worker communication
    BENCHMARKER_CSV_PREFIX     : path prefix for CSV stats output — primary passes this as --csv
                                 (named BENCHMARKER_* to prevent workers from auto-picking it up
                                 via Locust's LOCUST_CSV→--csv env var mapping)
    BENCHMARKER_SAMPLE_HW      : set to '1' to enable client hardware sampling during the test
"""

import os
import sys

_DIR = os.path.dirname(os.path.abspath(__file__))

sys.argv = [
    'locust',
    '-f', os.path.join(_DIR, 'locust_user.py'),
    '--headless',
    '--users',           os.environ['LOCUST_USERS'],
    '--spawn-rate',      os.environ['LOCUST_SPAWN_RATE'],
    '--master',
    '--expect-workers',  os.environ['LOCUST_WORKERS'],
    '--master-bind-port', os.environ['LOCUST_PORT'],
    '--csv',             os.environ['BENCHMARKER_CSV_PREFIX'],
]

from locust.main import main  # noqa: E402  (import after sys.argv is set)

main()
