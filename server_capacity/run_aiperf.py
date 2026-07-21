"""Run an aiperf profile benchmark, then fetch server metrics for its window.

Called by find_rps.sh for --aiperf / --aiperf-only. Builds the aiperf command from
args mapped out of find_rps's own args, merged with optional --aiperf-args overrides,
runs it with output under <run_dir>/aiperf (console output → aiperf_console.log).

After the run it prints a RESULTS block mirroring find_rps:
    Total requests fired = completed + cancelled   (from logs/aiperf.log PhaseRecordsStats)
    Wave time            = max(request_end_ns) - min(request_start_ns)  (profile_export.jsonl)
    Server RPS           = completed / wave_time
    Total requests       = completed (success + error)
    Success rate         = success_records / completed
Then calls fetch_server_metrics.py for [min start, max end] (same --endpoint-config
behavior as find_rps).

Arg mapping (from find_rps):
    --request-rate        <- client_rps
    --input-file          <- JSONL generated from factories_file via payload_factory_to_jsonl
    --request-count       <- obs_time only: ceil(obs_time*client_rps); else num_requests
    --benchmark-duration  <- obs_time (omitted when only num_requests is given)
    --output-artifact-dir <- <run_dir>/aiperf

Fixed args: --model mock-model --endpoint-type chat
            --custom-dataset-type raw_payload --arrival-pattern constant
            --dataset-sampling-strategy sequential --streaming
            (--tokenizer omitted: defaults to --model; mock-model triggers aiperf's
             builtin-tokenizer auto-substitution. Override --model for a real tokenizer.)

--aiperf-args is a JSON object whose keys are aiperf flag names and whose values control
how each flag is emitted. Use it to override a mapped arg or add a new one. The value's
JSON type decides what lands on the command line:

    "warmup-count": 50      string/number  ->  --warmup-count 50   (flag with a value)
    "verbose": true         true (or "")    ->  --verbose           (bare on/off switch)
    "streaming": false      false           ->  (flag left off entirely)
    "header": ["a", "b"]    list            ->  --header a --header b  (flag repeated per item)
"""

import contextlib
import json
import math
import os
import re
import subprocess
import sys


# Standard AWS regional endpoint hostnames already encode both the service and
# the region (that's how boto3 builds these same URLs internally from a
# `boto3.client('sagemaker-runtime', region_name=...)` call) — so SigV4
# --aws-region/--aws-service can be read back out of --url the same way,
# instead of requiring the caller to pass them separately. Only used as a
# fallback when the caller didn't pass --aws-region/--aws-service explicitly
# (e.g. a VPC endpoint or custom domain, which doesn't match either pattern).
_AWS_SIGV4_URL_PATTERNS = (
    # https://runtime.sagemaker.<region>.amazonaws.com
    (re.compile(r"^https?://runtime\.sagemaker\.([a-z0-9-]+)\.amazonaws\.com"), "sagemaker"),
    # https://<api-id>.execute-api.<region>.amazonaws.com
    (re.compile(r"^https?://[^./]+\.execute-api\.([a-z0-9-]+)\.amazonaws\.com"), "execute-api"),
)


def _infer_sigv4_region_and_service(url):
    """Infer (region, service) from a standard AWS endpoint URL, or (None, None)."""
    for pattern, service in _AWS_SIGV4_URL_PATTERNS:
        m = pattern.match(url)
        if m:
            return m.group(1), service
    return None, None


_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_ROOT_DIR   = os.path.dirname(_SCRIPT_DIR)


@contextlib.contextmanager
def _redirect_fds(log_path):
    """Redirect fd 1 (stdout) and fd 2 (stderr) to log_path for the duration.

    Operates at the file-descriptor level (os.dup2) so output written directly to
    the fds by C extensions (e.g. HF datasets) is captured, not just sys.stdout writes.
    """
    with open(log_path, 'w') as log:
        saved_out, saved_err = os.dup(1), os.dup(2)
        sys.stdout.flush(); sys.stderr.flush()
        os.dup2(log.fileno(), 1)
        os.dup2(log.fileno(), 2)
        try:
            yield
        finally:
            sys.stdout.flush(); sys.stderr.flush()
            os.dup2(saved_out, 1); os.dup2(saved_err, 2)
            os.close(saved_out); os.close(saved_err)


def _build_aiperf_args(client_rps, obs_time, num_requests, url, api_key,
                       input_file, artifact_dir, overrides, concurrency=None,
                       auth_type='', aws_region='', aws_service='', workers=None):
    """Return an ordered dict of {flag: value} for the aiperf command.

    value semantics: str/number → '--flag value'; True/'' → bare '--flag';
    False → omitted; list → repeated flag.

    Load model (mutually exclusive, matching find_rps.sh):
      concurrency given → closed-loop: emit --concurrency, omit --request-rate/--arrival-pattern.
      otherwise         → open-loop rate: emit --request-rate + --arrival-pattern constant.

    Auth (mutually exclusive):
      auth_type set    → AWS SigV4 (e.g. SageMaker, API Gateway). aws_region/aws_service
                         are read from --url when not passed explicitly (standard AWS
                         regional endpoint hostnames encode both, the same way boto3
                         builds them internally) — see _infer_sigv4_region_and_service.
                         Credentials come from the normal boto3 chain; no flag needed.
      otherwise        → Bearer auth via --api-key, if api_key is non-empty.

    workers: mirrors find_rps.sh's --workers (locust worker *process* count) onto
      aiperf's own --workers-max (aiperf's internal client worker-process count —
      unrelated to --concurrency, which is in-flight *request* count), instead of
      aiperf silently falling back to its own min(concurrency, cpu*0.75-1) default.
    """
    # mapped + fixed defaults (insertion order preserved)
    args = {
        # placeholder model name: aiperf auto-substitutes the builtin tokenizer for
        # obvious placeholders (mock-/test-/fake-model), so we omit --tokenizer and
        # let it default to --model. Pass a real --model via --aiperf-args to get
        # that model's real HF tokenizer instead.
        'model':                    'mock-model',
        'url':                      url,
        'endpoint-type':            'chat',
        'input-file':               input_file,
        'custom-dataset-type':      'raw_payload',
        'dataset-sampling-strategy': 'sequential',
        'streaming':                True,
        'output-artifact-dir':      artifact_dir,
    }

    if workers:
        args['workers-max'] = str(workers)

    if auth_type:
        args['auth-type'] = auth_type
        if not aws_region or not aws_service:
            inferred_region, inferred_service = _infer_sigv4_region_and_service(url)
            aws_region = aws_region or inferred_region
            aws_service = aws_service or inferred_service
        if not aws_region or not aws_service:
            raise ValueError(
                f"--auth-type {auth_type} needs --aws-region and --aws-service: "
                f"couldn't infer them from --url {url!r} (only standard "
                "runtime.sagemaker.<region>.amazonaws.com / "
                "<id>.execute-api.<region>.amazonaws.com hostnames are recognized)."
            )
        args['aws-region'] = aws_region
        args['aws-service'] = aws_service
    elif api_key:
        args['api-key'] = api_key

    if concurrency is not None:
        # closed-loop: C requests in flight; no rate pacing.
        args['concurrency'] = str(concurrency)
    else:
        # open-loop: pace arrivals at client_rps with constant inter-arrival time.
        args['request-rate']    = str(client_rps)
        args['arrival-pattern'] = 'constant'

    # request-count: num_requests wins; else obs_time-only rate mode → ceil(obs_time*client_rps)
    if num_requests > 0:
        args['request-count'] = str(num_requests)
    elif obs_time > 0 and concurrency is None:
        args['request-count'] = str(math.ceil(obs_time * client_rps))

    # benchmark-duration: only when obs_time provided (omit for num_requests-only)
    if obs_time > 0:
        args['benchmark-duration'] = str(obs_time)

    # merge overrides: strip any leading '--', then override or add
    for raw_key, value in overrides.items():
        key = raw_key.lstrip('-')
        args[key] = value

    return args


def _flatten_args(args):
    """Turn {flag: value} into a flat CLI list, honoring value semantics."""
    cmd = []
    for key, value in args.items():
        flag = f'--{key}'
        if value is False:
            continue                      # omit
        if value is True or value == '':
            cmd.append(flag)              # bare switch
        elif isinstance(value, (list, tuple)):
            for v in value:               # repeated flag
                cmd += [flag, str(v)]
        else:
            cmd += [flag, str(value)]
    return cmd


def run_aiperf(factories_file, client_rps, obs_time, num_requests,
               url, api_key, run_dir, endpoint_config='', aiperf_args_json='',
               success_threshold=0.95, concurrency=None,
               auth_type='', aws_region='', aws_service='', workers=None):
    """Generate input JSONL, run aiperf profile, then fetch server metrics.

    Args:
        factories_file:  Path to factories .py exposing payload_factory. Optional
                         when --input-file is supplied via --aiperf-args (then the
                         input JSONL is used verbatim and no factory is needed).
        client_rps:      Target request rate (--request-rate), open-loop mode
        obs_time:        Observation seconds (0 if not provided)
        num_requests:    Total requests (0 if not provided)
        url:             Endpoint base URL
        api_key:         Endpoint API key (Bearer auth). Ignored when auth_type is set.
        run_dir:         Wave output dir; aiperf artifacts land in <run_dir>/aiperf
        endpoint_config: Optional path to server metrics config (CloudWatch)
        aiperf_args_json: Optional JSON dict string of override/extra aiperf args
        success_threshold: Min acceptable success rate for the pass/fail label (default 0.95)
        concurrency:     Closed-loop concurrency (--concurrency). When set, replaces
                         --request-rate/--arrival-pattern. None → open-loop rate mode.
        auth_type:       AWS SigV4 auth (e.g. 'sigv4') instead of --api-key Bearer auth.
                         Needed for SageMaker/API Gateway endpoints. Empty → Bearer auth.
        aws_region:      SigV4 region. Inferred from --url when auth_type is set and
                         this is empty (see _infer_sigv4_region_and_service).
        aws_service:     SigV4 service (e.g. 'sagemaker', 'execute-api'). Inferred from
                         --url the same way as aws_region when empty.
        workers:         find_rps.sh's --workers (locust worker *process* count),
                         mirrored onto aiperf's --workers-max (aiperf's own internal
                         worker-process count — distinct from --concurrency, which
                         controls in-flight request count). None/0 → aiperf's own
                         default formula.
    """
    sys.path.insert(0, _ROOT_DIR)
    from client_capacity.aiperf_extension.payload_factory_to_jsonl import (
        payload_factory_to_jsonl,
    )

    artifact_dir = os.path.join(run_dir, 'aiperf')
    os.makedirs(artifact_dir, exist_ok=True)

    # 1. parse overrides
    overrides = json.loads(aiperf_args_json) if aiperf_args_json else {}
    if not isinstance(overrides, dict):
        raise ValueError(f"--aiperf-args must be a JSON object, got: {aiperf_args_json}")

    # 2. generate the input JSONL from the factory's payload_factory — UNLESS the caller
    # supplied their own --input-file via --aiperf-args, in which case use that verbatim.
    # Redirect factory-side console noise (e.g. HF datasets warnings) to a log file, at the
    # fd level so output written directly to fd 1/2 (not just sys.stdout) is captured.
    input_file_override = next(
        (v for k, v in overrides.items() if k.lstrip('-') == 'input-file'), None
    )
    if input_file_override is not None:
        input_file = input_file_override
    elif not factories_file:
        raise ValueError(
            "no input for aiperf: pass --factories-file, or supply an existing input "
            "JSONL via --aiperf-args '{\"input-file\": \"/path.jsonl\"}'"
        )
    else:
        input_file = os.path.join(artifact_dir, 'input.jsonl')
        gen_log = os.path.join(artifact_dir, 'input_gen.log')
        with _redirect_fds(gen_log):
            payload_factory_to_jsonl(factories_file, input_file)

    # 3. build + run the aiperf command — console output to a log file, not the terminal
    args = _build_aiperf_args(client_rps, obs_time, num_requests, url, api_key,
                              input_file, artifact_dir, overrides, concurrency=concurrency,
                              auth_type=auth_type, aws_region=aws_region,
                              aws_service=aws_service, workers=workers)
    cmd = ['aiperf', 'profile'] + _flatten_args(args)

    console_log = os.path.join(artifact_dir, 'aiperf_console.log')
    with open(console_log, 'w') as log:
        log.write(" ".join(cmd) + "\n\n")
        # don't check=True: aiperf exits non-zero when all requests fail, but it still
        # writes profile_export.jsonl + the log stats we summarize below.
        subprocess.run(cmd, stdout=log, stderr=subprocess.STDOUT)

    # 4. summarize + (optional) server metrics over aiperf's actual request window
    stats = _parse_aiperf_log(artifact_dir)
    window = _aiperf_window(artifact_dir)          # (start_epoch, end_epoch) or None
    _print_aiperf_results(stats, window, artifact_dir, success_threshold=success_threshold)

    if endpoint_config and window:
        sys.path.insert(0, _ROOT_DIR)
        from server_capacity.fetch_server_metrics import fetch_server_metrics
        fetch_server_metrics(endpoint_config, window[0], window[1])


def _parse_aiperf_log(artifact_dir):
    """Extract completed/cancelled/errors/success counts from aiperf's PhaseRecordsStats line.

    Returns dict with completed, cancelled, errors, success, or None if not found.
    """
    log_path = os.path.join(artifact_dir, 'logs', 'aiperf.log')
    if not os.path.exists(log_path):
        print(f"   ⚠️ aiperf log not found ({log_path}) — request counts unavailable")
        return None

    text = open(log_path).read()
    matches = re.findall(r'PhaseRecordsStats\(([^)]*)\)', text)
    if not matches:
        print(f"   ⚠️ no PhaseRecordsStats in {log_path} — request counts unavailable")
        return None
    fields = matches[-1]   # last (final) stats line

    def _grab(name):
        m = re.search(rf'{name}=(\d+)', fields)
        return int(m.group(1)) if m else 0

    # success_records lags on this line (validation runs later); derive success = completed - errors
    return {
        'completed': _grab('final_requests_completed'),
        'cancelled': _grab('final_requests_cancelled'),
        'errors':    _grab('final_request_errors'),
    }


def _aiperf_window(artifact_dir):
    """Return (start_epoch, end_epoch) from profile_export.jsonl request timestamps, or None.

    Uses min(request_start_ns) and max(request_end_ns) across all completed records —
    robust even when the summary JSON is absent (e.g. all-failed runs).
    """
    records_path = os.path.join(artifact_dir, 'profile_export.jsonl')
    if not os.path.exists(records_path):
        print(f"   ⚠️ aiperf records not found ({records_path}) — wave window unavailable")
        return None

    starts, ends = [], []
    with open(records_path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            meta = json.loads(line).get('metadata', {})
            if 'request_start_ns' in meta:
                starts.append(meta['request_start_ns'])
            if 'request_end_ns' in meta:
                ends.append(meta['request_end_ns'])

    if not starts or not ends:
        print(f"   ⚠️ no request timestamps in {records_path} — wave window unavailable")
        return None
    return (min(starts) / 1e9, max(ends) / 1e9)


def _print_aiperf_results(stats, window, artifact_dir, success_threshold):
    """Print the RESULTS — aiperf block, mirroring find_rps's output."""
    print()
    print("RESULTS — aiperf")
    print("-" * 16)

    if stats is None:
        print("   ⚠️  Could not parse aiperf stats from logs/aiperf.log")
        return

    completed = stats['completed']
    cancelled = stats['cancelled']
    errors    = stats['errors']
    success   = completed - errors   # aiperf's own metric: error rate = errors / completed
    fired     = completed + cancelled
    wave_time = (window[1] - window[0]) if window else 0.0
    rps       = round(completed / wave_time, 1) if wave_time > 0 else 0.0
    success_rate = (success / completed) if completed > 0 else 0.0
    passed    = success_rate >= success_threshold

    wave_time_str = f"{wave_time:.1f}s" if window else "⚠️ unavailable"

    print(f"   Total requests fired: {fired}")
    print()
    print(f"   Duration:         {wave_time_str}")
    print(f"   Server RPS:       {rps} req/s")
    print(f"   Total requests:   {completed}")
    print(f"   Success rate:     {success_rate*100:.1f}% "
          f"({'✓ passed' if passed else '❌ failed'}, {success_threshold*100:.0f}% target)")

    # csv_path = os.path.join(artifact_dir, 'profile_export_aiperf.csv')
    # csv_str  = os.path.relpath(csv_path, _ROOT_DIR) if os.path.exists(csv_path) else "⚠️ unavailable"
    # print()
    # print(f"   Full metrics:     {csv_str}")


if __name__ == '__main__':
    # argv: factories_file client_rps obs_time num_requests url api_key run_dir
    #       endpoint_config aiperf_args_json success_threshold concurrency
    #       auth_type aws_region aws_service workers
    # All 15 are positional. factories_file may be empty ('') when an --input-file
    # is supplied via aiperf_args_json; pass '' to hold the position. concurrency is ''
    # for open-loop rate mode, or an integer for closed-loop concurrency mode.
    # auth_type/aws_region/aws_service are '' for Bearer (--api-key) auth; when
    # auth_type is set, aws_region/aws_service may also be '' to infer from --url.
    # workers is '' to let aiperf pick its own default, or an integer to mirror
    # find_rps.sh's --workers onto aiperf's --workers-max.
    if len(sys.argv) != 16:
        print("Usage: run_aiperf.py <factories_file|''> <client_rps> <obs_time> "
              "<num_requests> <url> <api_key> <run_dir> <endpoint_config> "
              "<aiperf_args_json> <success_threshold> <concurrency|''> "
              "<auth_type|''> <aws_region|''> <aws_service|''> <workers|''>")
        sys.exit(1)

    sys.path.insert(0, os.path.dirname(_ROOT_DIR))

    run_aiperf(
        factories_file    = sys.argv[1],
        client_rps        = float(sys.argv[2]),
        obs_time          = float(sys.argv[3]),
        num_requests      = int(sys.argv[4]),
        url               = sys.argv[5],
        api_key           = sys.argv[6],
        run_dir           = sys.argv[7],
        endpoint_config   = sys.argv[8],
        aiperf_args_json  = sys.argv[9],
        success_threshold = float(sys.argv[10]),
        concurrency       = int(sys.argv[11]) if sys.argv[11] else None,
        auth_type         = sys.argv[12],
        aws_region        = sys.argv[13],
        aws_service       = sys.argv[14],
        workers           = int(sys.argv[15]) if sys.argv[15] else None,
    )
