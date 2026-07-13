<a id="readme-top"></a>

<!--
*** README based on othneildrew/Best-README-Template
*** Reference-style links are used for readability; definitions live at the bottom.
-->

<!-- PROJECT TITLE -->
# InferenceBenchmarker

Model-, platform-, and payload-agnostic load testing and capacity planning for inference endpoints.

* **Guaranteed client RPS** – Customizes and wraps [`Locust`][locust-url] to pace requests from
  the client so a target **client requests-per-second (RPS)** is sustained with 5-8x throughput-token gains.
* **Client-side bottleneck diagnostics** – Detects when the client sending requests is the limiting factor.
* **Any inference endpoint** – Traditional ML, GenAI, or any other HTTPS endpoint.
* **Defined in plain Python** – Endpoint characteristics — invocation logic, payload
  generation, endpoint creation — are expressed as simple Python function definitions.
* **Configurable server metrics** – Latches to configurable server metrics to correlate
  hardware utilization (or any available metric) — to plan capacity
  management across multiple endpoints, autoscaling, and cost extrapolations.
* **aiperf integration** – Integrates with NVIDIA [`aiperf`][aiperf-url] for token usage metrics from client(server can emit metrics enough for locust run only).
* **Comparison visualizations** – Basic bar-plot reports for comparing configurations side by side.

### Built With

[![Python][python-shield]][python-url]
[![Locust][locust-shield]][locust-url]
[![AWS][aws-shield]][aws-url]
[![Plotly][plotly-shield]][plotly-url]
[![NVIDIA aiperf][aiperf-shield]][aiperf-url]

<!-- USAGE -->
## Usage

### Installation

1. Clone the repo
   ```sh
   git clone https://github.com/aws-samples/sample-InferenceBenchmarker.git && cd sample-InferenceBenchmarker
   ```
2. Run the one-time setup
   ```sh
   ./benchmark --init
   ```
   ```text
   Use an existing virtual env? Enter its path, or press return to create a dedicated .venvIB:
     /path/to/your/venv   → installs dependencies into it
     <return>             → creates .venvIB and installs into it
   ```
3. Confirm it resolves
   ```sh
   benchmark
   ```
   (Use `./benchmark` from the repo root if you skipped adding to PATH.)


### 1. Describe your endpoint

InferenceBenchmarker takes your invocation logic defined in Python functions in a file.

1. **`invoke_factory(endpoint_name)`** runs **once per worker process** its body is shared by all users on that worker, so put reusable code there. It returns a callable `invoke(payload)`, which runs **per request**. 

    ```python
    from typing import Any, Callable

    Payload = Any   # whatever your endpoint accepts

    def invoke_factory(endpoint_name: str | None = None) -> Callable[[Payload], None]:
        # WORKER-LEVEL setup: this body runs ONCE per worker process and is shared by every
        # user on that worker. Put expensive, reusable client state here — e.g. the boto3
        # client and its connection pool — so it isn't rebuilt per request.
        import json, boto3
        client = boto3.client('sagemaker-runtime')

        def invoke(payload: Payload) -> None:
            # PER-REQUEST: called once for each request a user fires.
            resp = client.invoke_endpoint(
                EndpointName=endpoint_name or 'my-endpoint',
                ContentType='application/json',
                Body=json.dumps(payload).encode('utf-8'),
            )
            resp['Body'].read()

        return invoke            # the per-request callable
    ```

2. `payload_factory()` is called **once per worker** and has two modes.

    **`pre_computed=True`** —  payload pre-built, at request time a payload is just picked from the list, so payload-build cost is **not** included. Use this mode if payload generation logic is compute heavy – causing CPU to be a client bottleneck. Might cause higher memory usage during benchmarking.

    ```python
    def payload_factory() -> dict[str, bool | list[Payload]]:
        return {
            'pre_computed': True,
            'input': [{'instances': [[...]]}],   # list[Payload]
        }
    ```
    Requests cycle through the pre-computed inputs in order and wrap back to the start, so the list is never exhausted or cut off — fire more requests than there are inputs and it simply loops.

    **`pre_computed=False`** — payload built per user request by returning a Python callable that is called on each inference request. payload-build cost **is** included. Use this mode to introduce dynamism in payload_generation and if maintaining a pre-computed payload is heavy on memory causing memory to be a client bottleneck. Might cause higher compute usage during benchmarking:

    ```python
    def payload_factory() -> dict[str, bool | Callable[[], Payload]]:
        return {
            'pre_computed': False,
            'input': lambda: {'instances': [[...]]},   # Callable[[], Payload]
        }
    ```

### 2. Run a wave

```sh
benchmark \
  --factories-file   factories/sagemakerai_realtime/factories_cnn.py \
  --endpoint-config  server_capacity/server_metrics_configs/sagemakerai_realtime.json \
  --client-rps       10 \
  --obs-time         60 \
  --workers          5
```

```
RESULTS – InferenceBenchmarker
------------------------------
   Total requests fired: 600

   Duration:         59.9s
   Server RPS:       10.0 req/s
   Total requests:   599
   Success rate:     99.8% (✓ passed, 95% target)
```

Detailed reports land under `.tmp/<timestamp>_benchmark/`.

#### Bounding a wave: `--obs-time` and `--num-requests`

`--client-rps` sets the rate (how many requests per second are fired). `--obs-time` and `--num-requests`
decide how long the wave runs — pass at least one:

| You pass | Wave ends when |
|---|---|
| **`--obs-time S` only** | `S` seconds elapse |
| **`--num-requests N` only** | `N` requests have **fired & completed** |
| **both** | `S` seconds elapse **or** `N` requests complete — whichever first |


### 3. Add server-side metrics

Pass `--endpoint-config <file.json>` — currently supporting CloudWatch only. Add a **lag** if server publishes with a delay after wave ends. **Metrics and Statistics** are paired by position — the i-th metric uses the i-th list of statistics — and each (metric, statistic) is queried under the block's **Namespace, Dimensions, Period, and Lag**.

```json
[
  {
    "stream": "cloudwatch",
    "Namespace": "/aws/sagemaker/Endpoints",
    "Dimensions": [{"Name": "EndpointName", "Value": "my-endpoint"},
                   {"Name": "VariantName", "Value": "AllTraffic"}],
    "Period": 60,
    "Lag": 30,
    "Metrics": ["CPUUtilization", "MemoryUtilization", "GPUUtilization", "GPUMemoryUtilization"],
    "Statistics": [["Average","Maximum"], ["Average","Maximum"], ["Average","Maximum"], ["Average","Maximum"]]
  }
]
```

The metrics for the wave window print after the wave results:

```
----------------------------------------

   ⏳ Fetching metrics from stream: cloudwatch (waiting 90s (longest lag: 30 + period: 60) for propagation)...

   CPUUtilization: Average=312.4, Maximum=394.1
   MemoryUtilization: Average=18.7, Maximum=21.3
   GPUUtilization: Average=82.5, Maximum=97.0
   GPUMemoryUtilization: Average=44.2, Maximum=51.8
```


### 4. Get Token metrics with aiperf

```sh
benchmark --factories-file factories/sagemakerai_realtime/factories_llm_textgeneration.py \
  --client-rps 10 \ 
  --obs-time 60 \
  --url https://my-endpoint/v1/chat/completions --api-key "$KEY" \
  --aiperf
```

* `--aiperf` runs the Locust wave first, pauses to confirm the server is at a
  baseline (purposed for utilization metrics), then runs aiperf.
* `--aiperf-only` skips the InferenceBenchmarker wave via locust and runs aiperf directly.
* `--aiperf-args '{"warmup-count": 50, "streaming": false}'` overrides or adds any aiperf flag.

aiperf's input JSONL is auto-generated from the same `payload_factory`; to skip generation
and supply your own, pass it via `--aiperf-args '{"input-file": "/path/to/inputs.jsonl"}'`.

```
RESULTS — aiperf
----------------
   Total requests fired: 599

   Duration:         59.8s
   Server RPS:       0.9 req/s
   Total requests:   53
   Success rate:     100.0% (✓ passed, 95% target)
```

Detailed token metrics land under `.tmp/<timestamp>_benchmark/aiperf/`.

### 5. Bar plots

```sh
benchmark --plot .tmp/<run1> .tmp/<run2>
```

<a href="https://raw.githack.com/aws-samples/sample-InferenceBenchmarker/main/visualization/sample_benchmark_plots.html" target="_blank" rel="noopener noreferrer">sample plot</a>

**Label runs and attach hover info** with `--plot-metadata` — a JSON object keyed by run-dir
basename, passed either inline or as a path to a `.json` file. For each run, `legend` renames
it in the shared legend; every other key/value is shown as a hover line on that run's bars:

```sh
benchmark --plot .tmp/<run1> .tmp/<run2> .tmp/<run3> \
  --plot-metadata visualization/hover_configs/example.json
```

The [sample plot](https://raw.githack.com/aws-samples/sample-InferenceBenchmarker/main/visualization/sample_benchmark_plots.html)
above is rendered with this metadata.


### All flags

```
--factories-file FILE     Python file exposing invoke_factory / payload_factory (required for a wave)
--endpoint-config FILE    enables server telemetry, purposed for hardware utilization
--client-rps R            Target requests per second R to send from client
--obs-time S              Run a wave with client-rps for S seconds
--num-requests N          Run a wave with N requests, behavior with --obs-time refer Bounding a wave section
--workers N               N Locust worker processes (default 1; ~1 per available core is recommend)
--success-threshold F     Min acceptable success rate F, 0-1 (default 0.95)
--sample-client-hw        Record client CPU/memory during the wave
--port P                  Locust primary worker port (default 5557)
--locust-file FILE        Use a self-contained Locust file instead of factories (debugging)
--debug                   Verbose tracing (debugging)

--aiperf                  Run the wave, pause, then run aiperf            (needs --url, --api-key)
--aiperf-only             Skip the wave, run aiperf directly             (needs --url, --api-key)
--url URL                 Endpoint URL for aiperf
--api-key KEY             API key for aiperf URL
--aiperf-args JSON        Override/add `aiperf profile` flags; e.g. '{"model": "Qwen/Qwen2.5-0.5B"}' to estimate token counts with that HF model's tokenizer

--plot DIR [DIR ...]      Build a comparison report from existing run dirs (no wave); e.g. --plot <dir1> <dir2>
--plot-output-dir DIR     Output dir for the report (default: first --plot dir; e.g. --plot <dir1> <dir2> -> <dir1>)
--plot-fields JSON        Restrict plotted metrics per source; e.g. '{"locust": ["Latency (ms)"], "aiperf": ["Server RPS"]}'
--plot-metadata JSON|FILE Per-run legend rename + hover info, keyed by dir basename; inline JSON or a .json path (see Bar plots)
```

<!-- CLIENT DIAGNOSTICS -->
## Client Diagnostics

InferenceBenchmarker detects client bottleneck and alerts you (when you run the Locust wave) — after
every wave it scans the Locust logs for CPU / heartbeat saturation and prints a warning if the
client was overloaded:

```
   ⚠️ CLIENT BOTTLENECK detected in locust executions, test results might be unstable. Monitor client hardware. Pass --sample-client-hw to have InferenceBenchmarker benchmark client usage. Use diagnostic tools to find worker and rps saturation—worker_saturation.py/rps_saturation.py. Try pre-computed inputs in payload_factory if payload computation is a bottleneck. Use a client with higher cores and/or memory.
```

A simple manual check: **correlate requests fired with the wave duration.** If `requests fired
/ duration` falls short of your `--client-rps`, the client couldn't keep up — treat the run as
client-limited. `--sample-client-hw` records client CPU/memory during the wave to confirm. Or use own client telemetry tools to co-relate.

Diagnostic tools help you find where the client saturates:

* **`find_worker_saturation(factories_file)`** — the max requests a **single Locust worker**
  can fire per second. 
* **`find_rps_saturation(factories_file, saturation_users)`** — how many **workers** to run
  before total requests fired plateaus (adding workers stops helping). This is the client's
  ceiling: the `--workers` setting beyond which you need a bigger / additional load-gen host.
* **`find_file_descriptors_limit()`** — reports the client's file-descriptor limits, which cap number of requests(client_rps).

See [client_capacity/README.md](client_capacity/README.md) for usage.


<!-- UPCOMING IMPROVEMENTS -->
## Upcoming Improvements

- [ ] **endpoint_factory** – Add endpoint creation code in factories — create a latch for Automatic RPS. [tracking issues](https://github.com/aws-samples/sample-InferenceBenchmarker/issues)
- [ ] **Hydrate w Examples** – EKS, Hyperpod, EC2, OCP(on-prem) etc. examples. [tracking issues](https://github.com/aws-samples/sample-InferenceBenchmarker/issues)
- [ ] **Interactive CLI** – Add traces while running benchmarks in the current dry benchmark tool. [tracking issues](https://github.com/aws-samples/sample-InferenceBenchmarker/issues)
- [ ] **Automatic RPS** – Automate trial and error server rps supported at success threshold when --endpoint-config for hardware telemetry provided. [tracking issue](https://github.com/aws-samples/sample-InferenceBenchmarker/issues/3)
- [ ] **Distribute as a package** – enough said. [tracking issue](https://github.com/aws-samples/sample-InferenceBenchmarker/issues/9)
- [x] **Plot metadata** – Provide a JSON (inline or file) to set per-run legend names and hover info in plots.


<!-- SECURITY -->
## Security

See [CONTRIBUTING](CONTRIBUTING.md#security-issue-notifications) for more information.

<!-- LICENSE -->
## License

This library is licensed under the MIT-0 License. See the LICENSE file.

<!-- MARKDOWN LINKS & IMAGES -->
[locust-url]: https://locust.io/
[aiperf-url]: https://github.com/ai-dynamo/aiperf
[aiperf-shield]: https://img.shields.io/badge/aiperf-76B900?style=for-the-badge&logoColor=white
[plotly-url]: https://plotly.com/python/
[aws-url]: https://aws.amazon.com/sagemaker/
[python-url]: https://www.python.org/
[python-shield]: https://img.shields.io/badge/python-3670A0?style=for-the-badge&logo=python&logoColor=ffdd54
[locust-shield]: https://img.shields.io/badge/Locust-2D9D5A?style=for-the-badge&logo=locust&logoColor=white
[aws-shield]: https://img.shields.io/badge/AWS-232F3E?style=for-the-badge&logo=amazon-web-services&logoColor=white
[plotly-shield]: https://img.shields.io/badge/Plotly-3F4F75?style=for-the-badge&logo=plotly&logoColor=white
