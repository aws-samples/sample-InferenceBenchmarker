"""
Standalone vLLM server-side metrics over an arbitrary time window.

get_server_metrics(endpoint_name, start, end) -> dict

Give it a SageMaker endpoint name and a [start, end] window (Unix seconds or
ISO-8601 UTC) and it returns every vLLM metric the endpoint exposes, summarized
over that window.

  * HISTOGRAMS (latencies, per-request token counts, etc.)
      vLLM exposes these as cumulative *native histograms*. We take the DELTA of
      the histogram between `start` and `end` (buckets@end - buckets@start) and
      derive count / mean / min / p50 / p90 / p99 / max from that delta. This
      gives the distribution of ONLY the observations recorded in the window.

  * COUNTERS (…_total): reported as the delta over the window (total in window)
      plus a per-second rate. Multi-series counters (e.g. request_success_total
      by finished_reason) are summed, with a per-label breakdown.

  * GAUGES (num_requests_running, kv_cache_usage_perc, DCGM GPU metrics, …):
      point-in-time values. We pull the raw samples in the window via a range
      selector and report avg / min / max / last (aggregated across series, e.g.
      across the 4 GPUs for DCGM).

  * DERIVED (spec-decode): mean acceptance length and draft acceptance rate,
      computed from the spec_decode counters. mean_acceptance_length is the
      average number of tokens committed per engine step (= 1 + accepted/drafts);
      >1 means multi-token steps are actually happening.

Notes / caveats:
  * inter_token_latency_seconds is per ENGINE STEP, not per token. On spec-decode
    / MTP endpoints one step commits a VARIABLE 1..(num_speculative_tokens+1)
    tokens, so this metric over-reports true per-token latency and can't be fixed
    by a constant divisor. For a true per-token latency use
    request_time_per_output_token_seconds (= decode_time/(gen_tokens-1) per
    request). On a vanilla model step==token and inter_token_latency_seconds is
    already a correct per-token ITL.
  * Histogram percentiles are interpolated within vLLM's coarse buckets, so they
    are indicative, not exact. Counts and means are exact.
  * end_pad_s (default 0): optionally extend the end snapshot to catch requests
    still in flight at `end` (only meaningful for request-completion histograms;
    only safe when no other traffic hits the endpoint just after the window).

Auth: uses the default boto3 session (region + creds from the environment),
SigV4-signed against the SageMaker Detailed Observability PromQL endpoint.

CLI:
    python vllm_server_metrics.py --endpoint EP --start 2026-07-15T15:29:53 --end 2026-07-15T15:32:40
    python vllm_server_metrics.py --endpoint EP --start 1784129393 --end 1784129560 --json
"""

import argparse
import json
import math
import urllib.error
import urllib.request
from datetime import datetime, timezone

import boto3
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest

# ----------------------------------------------------------------------------------
# Metric catalog (vLLM v0.24.0). kind: "histogram" | "counter" | "gauge".
# scale: multiply the raw value by this for the reported unit. unit: label.
# ----------------------------------------------------------------------------------
S = 1000.0  # seconds -> milliseconds
HISTOGRAMS = {
    # latency histograms (seconds -> ms)
    "e2e_request_latency_seconds":        (S, "ms"),
    "time_to_first_token_seconds":        (S, "ms"),
    "inter_token_latency_seconds":        (S, "ms"),   # PER ENGINE STEP (see module docstring)
    "request_time_per_output_token_seconds": (S, "ms"),  # true per-token latency per request
    "request_queue_time_seconds":         (S, "ms"),
    "request_inference_time_seconds":     (S, "ms"),
    "request_prefill_time_seconds":       (S, "ms"),
    "request_decode_time_seconds":        (S, "ms"),
    # token / param histograms (unitless counts)
    "request_prompt_tokens":              (1.0, "tokens"),
    "request_generation_tokens":          (1.0, "tokens"),
    "request_max_num_generation_tokens":  (1.0, "tokens"),
    "request_prefill_kv_computed_tokens": (1.0, "tokens"),
    "iteration_tokens_total":             (1.0, "tokens"),   # batch tokens per engine iteration
    "request_params_n":                   (1.0, "n"),
    "request_params_max_tokens":          (1.0, "tokens"),
}

COUNTERS = {
    "prompt_tokens_total":                    "tokens",
    "generation_tokens_total":                "tokens",
    "num_preemptions_total":                  "count",
    "prefix_cache_queries_total":             "queries",
    "prefix_cache_hits_total":                "hits",
    "request_success_total":                  "requests",   # labeled by finished_reason
    "spec_decode_num_accepted_tokens_total":  "tokens",
    "spec_decode_num_draft_tokens_total":     "tokens",
    "spec_decode_num_drafts_total":           "drafts",
    # --- added for vLLM 0.25.x (absent on older endpoints; missing metrics
    # --- are skipped silently, so the catalog is backward-compatible) ---
    "spec_decode_num_accepted_tokens_per_pos_total": "tokens",  # labeled by position
    "estimated_flops_per_gpu_total":          "flops",
    "estimated_read_bytes_per_gpu_total":     "bytes",
    "estimated_write_bytes_per_gpu_total":    "bytes",
    "prompt_tokens_cached_total":             "tokens",
    "prompt_tokens_by_source_total":          "tokens",     # labeled by source
    "external_prefix_cache_queries_total":    "queries",
    "external_prefix_cache_hits_total":       "hits",
}

# gauge -> unit. DCGM GPU gauges are not vllm:-prefixed.
GAUGES = {
    "vllm:num_requests_running":   "requests",
    "vllm:num_requests_waiting":   "requests",
    "vllm:num_requests_waiting_by_reason": "requests",   # labeled by reason
    "vllm:kv_cache_usage_perc":    "ratio",
    "DCGM_FI_DEV_GPU_UTIL":        "%",
    "DCGM_FI_DEV_FB_USED":         "MiB",
    # --- hardware depth (instance-level agent; exists on ALL runs) ---
    "DCGM_FI_DEV_FB_FREE":         "MiB",
    "DCGM_FI_PROF_SM_ACTIVE":      "ratio",   # SM occupancy — finer than GPU_UTIL
    "DCGM_FI_DEV_MEM_COPY_UTIL":   "%",       # memory-bandwidth utilization
    "DCGM_FI_DEV_GPU_TEMP":        "C",
    "DCGM_FI_DEV_MEMORY_TEMP":     "C",
    "node_load1":                  "load",
    "node_memory_MemAvailable_bytes": "bytes",
}


# ----------------------------------------------------------------------------------
# PromQL plumbing (SigV4-signed instant queries)
# ----------------------------------------------------------------------------------
def _make_session():
    """
    Build a working boto3 Session by inferring creds without depending on a clean
    env. Handles two failure modes seen in this environment:
      * AWS_PROFILE points at a profile that doesn't exist (e.g. "default" with no
        ~/.aws/config) -> boto3 raises ProfileNotFound. We retry ignoring the profile.
      * Credentials still come from the SageMaker container endpoint / instance role,
        which boto3 finds automatically once the bad profile is out of the way.
    """
    from botocore.exceptions import ProfileNotFound

    try:
        session = boto3.Session()
        session.get_credentials()  # force resolution now so a bad profile fails here
        return session
    except ProfileNotFound:
        # Retry with the profile stripped from botocore's view for this session only.
        import os
        saved = {k: os.environ.pop(k, None) for k in ("AWS_PROFILE", "AWS_DEFAULT_PROFILE")}
        try:
            session = boto3.Session(profile_name=None)
            session.get_credentials()
            return session
        finally:
            for k, v in saved.items():
                if v is not None:
                    os.environ[k] = v


def _resolve_region(session, endpoint_name):
    """
    Pick the region the endpoint actually lives in, not whatever ambiguous env var
    happens to be set. Strategy:
      1. Ask SageMaker (in each candidate region) to describe the endpoint; the first
         region that knows it is authoritative.
      2. Fall back to the session's configured region.
    Candidates are tried in a sensible order and de-duplicated.
    """
    import os

    candidates = []
    for r in (session.region_name,
              os.environ.get("AWS_REGION"),
              os.environ.get("AWS_DEFAULT_REGION"),
              os.environ.get("REGION_NAME"),
              "us-east-1", "us-east-2", "us-west-2"):
        if r and r not in candidates:
            candidates.append(r)

    for region in candidates:
        try:
            sm = session.client("sagemaker", region_name=region)
            sm.describe_endpoint(EndpointName=endpoint_name)
            return region  # this region knows the endpoint -> it's the right one
        except Exception:
            continue

    if session.region_name:
        return session.region_name
    if candidates:
        return candidates[0]
    raise RuntimeError(
        "Could not determine an AWS region. Pass region=... to get_server_metrics()."
    )


def _session_bits(endpoint_name=None, region=None):
    """Return (region, creds), inferring both without relying on a clean env."""
    session = _make_session()
    if region is None:
        region = _resolve_region(session, endpoint_name) if endpoint_name else session.region_name
    if not region:
        raise RuntimeError("No AWS region resolved; pass region=... to get_server_metrics().")
    creds = session.get_credentials().get_frozen_credentials()
    return region, creds


def _query(query, at_unix, region, creds):
    """PromQL instant query at a specific time. Returns data.result (list), or []."""
    url = f"https://monitoring.{region}.amazonaws.com/api/v1/query"
    req = AWSRequest(method="GET", url=url, params={"query": query, "time": str(at_unix)})
    SigV4Auth(creds, "monitoring", region).add_auth(req)
    prepared = req.prepare()
    urlreq = urllib.request.Request(prepared.url, headers=dict(prepared.headers), method="GET")
    try:
        with urllib.request.urlopen(urlreq) as resp:
            body = json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"PromQL failed ({e.code}) for {query!r}: {e.read().decode()[:300]}") from None
    return body.get("data", {}).get("result", [])


def _selector(metric, endpoint):
    return f'{metric}{{"aws.sagemaker.endpoint.name"="{endpoint}"}}'


# ----------------------------------------------------------------------------------
# Histograms: delta of cumulative native histogram + distribution stats
# ----------------------------------------------------------------------------------
def _native_hist_at(metric, endpoint, at_unix, region, creds):
    """Fetch one native histogram instant. -> ({(lo,hi): count}, count, sum) or None."""
    result = _query(f'vllm:{_selector(metric, endpoint)}', at_unix, region, creds)
    if not result or "histogram" not in result[0]:
        return None
    hist = result[0]["histogram"][1]
    buckets = {}
    for b in hist.get("buckets", []):
        lo, hi, cnt = round(float(b[1]), 9), round(float(b[2]), 9), float(b[3])
        buckets[(lo, hi)] = buckets.get((lo, hi), 0.0) + cnt
    return buckets, float(hist["count"]), float(hist["sum"])


def _hist_quantile(buckets, total, p):
    if total <= 0:
        return float("nan")
    target, cum = p * total, 0.0
    for lo, hi, cnt in buckets:
        if cum + cnt >= target:
            return hi if cnt == 0 else lo + (target - cum) / cnt * (hi - lo)
        cum += cnt
    return buckets[-1][1] if buckets else float("nan")


def _histogram_window(metric, endpoint, start, end, region, creds, scale, unit):
    """Delta of the histogram over [start, end] -> distribution dict, or None."""
    h0 = _native_hist_at(metric, endpoint, start, region, creds)
    h1 = _native_hist_at(metric, endpoint, end, region, creds)
    if h1 is None:
        return None
    if h0 is None:
        h0 = ({}, 0.0, 0.0)
    b0, c0, s0 = h0
    b1, c1, s1 = h1
    keys = sorted(set(b0) | set(b1))
    delta = [(lo, hi, max(0.0, b1.get((lo, hi), 0.0) - b0.get((lo, hi), 0.0))) for (lo, hi) in keys]
    count, total_sum = c1 - c0, s1 - s0
    nonzero = [(lo, hi, c) for lo, hi, c in delta if c > 0]
    return {
        "count": count,
        "avg": (total_sum / count * scale) if count > 0 else float("nan"),
        "min": (nonzero[0][0] * scale) if nonzero else float("nan"),
        "p50": _hist_quantile(delta, count, 0.50) * scale,
        "p90": _hist_quantile(delta, count, 0.90) * scale,
        "p99": _hist_quantile(delta, count, 0.99) * scale,
        "max": (nonzero[-1][1] * scale) if nonzero else float("nan"),
        "unit": unit,
    }


# ----------------------------------------------------------------------------------
# Counters: delta over window (+ per-second rate, + per-label breakdown)
# ----------------------------------------------------------------------------------
def _label_key(metric_labels):
    """Human label for a counter series: the non-infra labels (e.g. finished_reason)."""
    infra = {"__name__", "__type__", "__temporality__", "__monotonicity__",
             "model_name", "engine",
             "aws.sagemaker.endpoint.name", "aws.sagemaker.variant.name",
             "aws.sagemaker.container.id", "aws.sagemaker.container.name",
             "aws.sagemaker.inference_framework"}
    extra = {k: v for k, v in metric_labels.items()
             if not k.startswith("@") and not k.startswith("__") and k not in infra}
    return ",".join(f"{k}={v}" for k, v in sorted(extra.items())) or None


def _counter_window(metric, endpoint, start, end, region, creds, unit):
    """Delta of a (possibly multi-series) counter over [start, end]."""
    r0 = _query(f'vllm:{_selector(metric, endpoint)}', start, region, creds)
    r1 = _query(f'vllm:{_selector(metric, endpoint)}', end, region, creds)
    if not r1:
        return None
    start_by = {_label_key(s["metric"]): float(s["value"][1]) for s in r0}
    dur = max(end - start, 1e-9)
    total_delta, by_label = 0.0, {}
    for s in r1:
        lk = _label_key(s["metric"])
        d = max(0.0, float(s["value"][1]) - start_by.get(lk, 0.0))
        total_delta += d
        if lk is not None:
            by_label[lk] = by_label.get(lk, 0.0) + d
    out = {"delta": total_delta, "rate_per_s": total_delta / dur, "unit": unit}
    if by_label:
        out["by_label"] = by_label
    return out


# ----------------------------------------------------------------------------------
# Gauges: raw samples in the window via range selector -> avg/min/max/last
# ----------------------------------------------------------------------------------
def _gauge_window(metric, endpoint, start, end, region, creds, unit):
    """avg/min/max/last of a gauge over [start, end], aggregated across all series."""
    window = max(int(round(end - start)) + 1, 1)
    q = f'{_selector(metric, endpoint)}[{window}s]'
    result = _query(q, end, region, creds)
    if not result:
        return None
    samples = []  # (ts, value) across all series (e.g. all GPUs)
    for s in result:
        for ts, val in s.get("values", []):
            if start <= float(ts) <= end:
                samples.append((float(ts), float(val)))
    if not samples:
        return None
    vals = [v for _, v in samples]
    last = max(samples, key=lambda x: x[0])[1]
    return {
        "avg": sum(vals) / len(vals),
        "min": min(vals),
        "max": max(vals),
        "last": last,
        "n_series": len(result),
        "n_samples": len(samples),
        "unit": unit,
    }


# ----------------------------------------------------------------------------------
# Public API
# ----------------------------------------------------------------------------------
def _to_unix(value):
    """Unix seconds (int/float/str) or ISO-8601 (naive treated as UTC) -> float."""
    try:
        return float(value)
    except (TypeError, ValueError):
        pass
    dt = datetime.fromisoformat(str(value))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def get_server_metrics(endpoint_name, start, end, end_pad_s=0.0, region=None):
    """
    All vLLM server metrics for `endpoint_name` over [start, end].

    start/end: Unix seconds or ISO-8601 (UTC). end_pad_s: extend the end snapshot
    for request-completion histograms to catch in-flight requests (default 0).
    region: override the AWS region; if None, it is inferred (creds and region are
    resolved automatically — no need to pre-set AWS_PROFILE/AWS_REGION env vars).

    Returns:
      {
        "endpoint", "start", "end", "window_s",
        "histograms": {name: {count, avg, min, p50, p90, p99, max, unit}},
        "counters":   {name: {delta, rate_per_s, unit, by_label?}},
        "gauges":     {name: {avg, min, max, last, n_series, n_samples, unit}},
        "derived":    {mean_acceptance_length, mean_tokens_per_step,
                       draft_acceptance_rate_pct, ...},
      }
    Metrics not exposed by the endpoint are omitted.
    """
    start_u, end_u = _to_unix(start), _to_unix(end)
    region, creds = _session_bits(endpoint_name, region)
    hist_end = end_u + end_pad_s

    out = {
        "endpoint": endpoint_name,
        "start": start_u,
        "end": end_u,
        "window_s": end_u - start_u,
        "histograms": {},
        "counters": {},
        "gauges": {},
        "derived": {},
    }

    for name, (scale, unit) in HISTOGRAMS.items():
        d = _histogram_window(name, endpoint_name, start_u, hist_end, region, creds, scale, unit)
        if d is not None:
            out["histograms"][name] = d

    for name, unit in COUNTERS.items():
        d = _counter_window(name, endpoint_name, start_u, end_u, region, creds, unit)
        if d is not None:
            out["counters"][name] = d

    for full_name, unit in GAUGES.items():
        # gauges include non-vllm DCGM names; _selector adds the endpoint label to any name
        window = max(int(round(end_u - start_u)) + 1, 1)
        result = _query(f'{full_name}{{"aws.sagemaker.endpoint.name"="{endpoint_name}"}}[{window}s]',
                        end_u, region, creds)
        samples = [(float(ts), float(v)) for s in result for ts, v in s.get("values", [])
                   if start_u <= float(ts) <= end_u]
        if samples:
            vals = [v for _, v in samples]
            out["gauges"][full_name] = {
                "avg": sum(vals) / len(vals), "min": min(vals), "max": max(vals),
                "last": max(samples, key=lambda x: x[0])[1],
                "n_series": len(result), "n_samples": len(samples), "unit": unit,
            }

    # Derived spec-decode stats (real tokens committed per engine step)
    c = out["counters"]
    acc = c.get("spec_decode_num_accepted_tokens_total", {}).get("delta")
    drafts = c.get("spec_decode_num_drafts_total", {}).get("delta")
    draft_toks = c.get("spec_decode_num_draft_tokens_total", {}).get("delta")
    if acc is not None and drafts:
        # mean acceptance length = 1 bonus token + accepted drafts per drafting step
        out["derived"]["mean_acceptance_length"] = 1.0 + acc / drafts
        out["derived"]["mean_tokens_per_step"] = 1.0 + acc / drafts
    if acc is not None and draft_toks:
        out["derived"]["draft_acceptance_rate_pct"] = acc / draft_toks * 100.0
    gen = c.get("generation_tokens_total", {}).get("delta")
    if gen and drafts:
        out["derived"]["generation_tokens_in_window"] = gen

    return out


# ----------------------------------------------------------------------------------
# CLI / pretty printing
# ----------------------------------------------------------------------------------
def _fmt(x):
    if x is None or (isinstance(x, float) and math.isnan(x)):
        return "-"
    return f"{x:,.2f}"


def print_report(m):
    s0 = datetime.fromtimestamp(m["start"], timezone.utc)
    s1 = datetime.fromtimestamp(m["end"], timezone.utc)
    print(f"endpoint : {m['endpoint']}")
    print(f"window   : {m['start']:.0f} -> {m['end']:.0f} "
          f"({s0:%Y-%m-%d %H:%M:%S} -> {s1:%H:%M:%S} UTC, {m['window_s']:.0f}s)")

    if m["histograms"]:
        cols = ["count", "avg", "min", "p50", "p90", "p99", "max"]
        print(f"\n== HISTOGRAMS (distribution over window) ==")
        print(f"{'metric':40}{'unit':7}" + "".join(f"{c:>12}" for c in cols))
        print("-" * (47 + 12 * len(cols)))
        for name, d in m["histograms"].items():
            print(f"{name:40}{d['unit']:7}" + "".join(f"{_fmt(d.get(c)):>12}" for c in cols))

    if m["counters"]:
        print(f"\n== COUNTERS (total in window / per-second) ==")
        print(f"{'metric':44}{'unit':10}{'delta':>16}{'rate/s':>14}")
        print("-" * 84)
        for name, d in m["counters"].items():
            print(f"{name:44}{d['unit']:10}{_fmt(d['delta']):>16}{_fmt(d['rate_per_s']):>14}")
            for lk, v in d.get("by_label", {}).items():
                print(f"    {lk:40}{'':10}{_fmt(v):>16}")

    if m["gauges"]:
        print(f"\n== GAUGES (avg/min/max/last over window) ==")
        print(f"{'metric':32}{'unit':8}{'avg':>12}{'min':>12}{'max':>12}{'last':>12}{'series':>8}")
        print("-" * 96)
        for name, d in m["gauges"].items():
            print(f"{name:32}{d['unit']:8}{_fmt(d['avg']):>12}{_fmt(d['min']):>12}"
                  f"{_fmt(d['max']):>12}{_fmt(d['last']):>12}{d['n_series']:>8}")

    if m["derived"]:
        print(f"\n== DERIVED ==")
        for k, v in m["derived"].items():
            print(f"  {k:32}{_fmt(v)}")
        print("  (mean_tokens_per_step > 1 => multi-token engine steps; "
              "inter_token_latency_seconds is per-step, not per-token)")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--endpoint", required=True)
    ap.add_argument("--start", required=True, help="Unix seconds or ISO-8601 (UTC)")
    ap.add_argument("--end", required=True, help="Unix seconds or ISO-8601 (UTC)")
    ap.add_argument("--end-pad", type=float, default=0.0,
                    help="Extend end snapshot (s) to catch in-flight request completions (default 0)")
    ap.add_argument("--region", default=None, help="AWS region override (inferred if omitted)")
    ap.add_argument("--json", action="store_true", help="Emit raw JSON instead of a table")
    args = ap.parse_args()

    m = get_server_metrics(args.endpoint, args.start, args.end, args.end_pad, region=args.region)
    if args.json:
        print(json.dumps(m, indent=2))
    else:
        print_report(m)


if __name__ == "__main__":
    main()
