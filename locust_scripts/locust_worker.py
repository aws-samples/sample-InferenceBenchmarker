"""Locust worker process for InferenceBenchmarker.

Launched by _burst.py (find_worker_saturation / find_rps_saturation) and find_rps.sh.
Starts a Locust worker by manipulating sys.argv and calling locust.main.main().

Environment variables:
    LOCUST_PORT    : TCP port to connect to the primary on
    FACTORIES_PATH : path to the cloudpickle file containing invoke_factory and payload_factory
                     (read by locust_user.py when the worker imports it)
    WORKER_INDEX   : worker number (1-based) used to name the requests_fired output file
                     and as this worker's offset into a pre_computed payload list
    BENCHMARKER_WORKER_COUNT : total worker count — the stride between consecutive payload
                     indices, so workers cover disjoint slices of a pre_computed list
"""

import os
import sys

_DIR = os.path.dirname(os.path.abspath(__file__))

sys.argv = [
    'locust',
    '-f', os.path.join(_DIR, 'locust_user.py'),
    '--worker',
    '--master-host', '127.0.0.1',
    '--master-port', os.environ['LOCUST_PORT'],
]

from locust.main import main  # noqa: E402  (import after sys.argv is set)
main()
