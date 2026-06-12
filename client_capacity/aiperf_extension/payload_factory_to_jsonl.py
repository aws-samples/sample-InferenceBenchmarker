"""Convert a factories file's payload_factory() output into a JSONL file.

Loads payload_factory from a file path (not a dotted module name) and writes one
JSON object per line, per the payload_factory protocol:

    {'pre_computed': True,  'input': List[Any]}  → one JSONL line per list item
    {'pre_computed': False, 'input': Callable}   → call input() once, write one line
"""

import importlib.util
import json


def payload_factory_to_jsonl(factories_file, output_path):
    """Write payload_factory() output to a JSONL file (one JSON object per line).

    Args:
        factories_file: Path to a factories .py file exposing payload_factory()
        output_path:    Path to write the JSONL file (mandatory)
    """
    spec = importlib.util.spec_from_file_location('_factories', factories_file)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    payload_factory = getattr(module, 'payload_factory')

    config       = payload_factory()
    pre_computed = config['pre_computed']
    payload_in   = config['input']

    if pre_computed:
        items = payload_in            # list of JSON objects
    else:
        items = [payload_in()]        # callable → one JSON object

    with open(output_path, 'w') as f:
        for item in items:
            f.write(json.dumps(item) + '\n')

    return output_path
