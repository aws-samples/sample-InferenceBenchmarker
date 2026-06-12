"""Serialize invoke_factory and payload_factory from a factories file to a cloudpickle file.

Called by find_rps.sh when --factories-file is provided. Writes a .pkl file to
FACTORIES_PKL path which locust_user.py loads via FACTORIES_PATH env var.

The factories file is loaded by file path (not a dotted module name), so paths
with hyphens or arbitrary locations work.
"""

import sys
import importlib.util
import cloudpickle

if len(sys.argv) != 3:
    print("Usage: _find_rps_serialize.py <factories_file> <output_pkl_path>")
    sys.exit(1)

factories_file = sys.argv[1]
output_path    = sys.argv[2]

spec = importlib.util.spec_from_file_location('_factories', factories_file)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)

invoke_factory  = getattr(module, 'invoke_factory')
payload_factory = getattr(module, 'payload_factory')

with open(output_path, 'wb') as f:
    cloudpickle.dump({'invoke_factory': invoke_factory, 'payload_factory': payload_factory}, f)
