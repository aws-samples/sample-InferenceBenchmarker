"""Streaming variant of factories_llm_textgeneration.py.

Same payloads, but invoke() uses invoke_endpoint_with_response_stream and parses the
streamed response (decoding each PayloadPart and reading the SSE `data:` chunks) the way
a real client would consume token-by-token output — rather than draining and discarding.

Parsing errors are raised, not swallowed: during a benchmark a malformed/garbled stream is
a real failure (it should show up in the success rate and logs), so we surface it instead
of silently skipping the chunk.
"""

def invoke_factory(endpoint_name=None):
    import json
    import boto3
    from botocore.config import Config

    _endpoint_name = endpoint_name or 'qwen3-5-0-8b-ge62x'

    client = boto3.client(
        'sagemaker-runtime',
        config=Config(retries={'max_attempts': 3}, max_pool_connections=500, read_timeout=3600),
    )

    def invoke(payload):
        response = client.invoke_endpoint_with_response_stream(
            EndpointName=_endpoint_name,
            ContentType='application/json',
            Body=json.dumps(payload).encode('utf-8'),
        )

        # response['Body'] is an EventStream of PayloadPart events. Bytes can split an SSE
        # line across events, so buffer and split on newlines. 
        buffer = ""
        text = []
        usage = None
        for event in response['Body']:
            part = event.get('PayloadPart')
            if not part:
                # an event with no PayloadPart is unexpected for this endpoint —
                # surface it (could be an error/exception event in the stream)
                raise RuntimeError(f"unexpected stream event without PayloadPart: {event!r}")
            buffer += part['Bytes'].decode('utf-8')

            # process complete lines; keep any trailing partial line in the buffer
            while '\n' in buffer:
                line, buffer = buffer.split('\n', 1)
                line = line.strip()
                if not line or not line.startswith('data:'):
                    continue
                data = line[len('data:'):].strip()
                if data == '[DONE]':
                    continue
                chunk = json.loads(data)   # raises JSONDecodeError on a malformed chunk
                # Some servers signal failure in-band as a 200 stream carrying an
                # `error` chunk (e.g. data: {"error": {...}}). json.loads succeeds on it,
                # so it would otherwise be counted as success. Raise so it propagates to
                # locust_user._invoke and is recorded as a FAILED request.
                if isinstance(chunk, dict) and chunk.get('error'):
                    raise RuntimeError(f"server returned error in stream: {chunk['error']}")
                for choice in chunk.get('choices', []):
                    piece = choice.get('delta', {}).get('content')
                    if piece:
                        text.append(piece)
                if chunk.get('usage'):
                    usage = chunk['usage']

        return_dict = {'text': ''.join(text), 'usage': usage}

    return invoke


def payload_factory():
    from datasets import load_dataset
    test_ds_prompts = load_dataset("openai/gsm8k", "main")["test"]["question"][:100]
    return {
        'pre_computed': True,
        'input': [
            {
                "messages": [
                    {
                        "content": input_prompt,
                        "role": "user"
                    }
                ],
                "stream": True,
                "stream_options": {"include_usage": True},
                "min_tokens": 4000,
                "max_tokens": 4000,
                "temperature": 0.0,
                "ignore_eos": True,

            }
            for input_prompt in test_ds_prompts
        ]
    }
