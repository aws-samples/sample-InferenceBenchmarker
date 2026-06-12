def invoke_factory(endpoint_name=None):
    import json
    import boto3
    from botocore.config import Config

    _endpoint_name = endpoint_name or 'qwen3-5-0-8b-ge62x'

    client = boto3.client(
        'sagemaker-runtime',
        region_name='us-east-1',
        config=Config(retries={'max_attempts': 3}, max_pool_connections=500, read_timeout=3600),
    )

    def invoke(payload):
        response = client.invoke_endpoint(
            EndpointName=_endpoint_name,
            ContentType='application/json',
            Body=json.dumps(payload).encode('utf-8'),
        )
        response['Body'].read()

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
                "stream_options": {"include_usage": True}
            }
            for input_prompt in test_ds_prompts
        ]
    }


# # Example non-pre-computed factory:
# def payload_factory():
#     return {
#         'pre_computed': False,
#         'input': lambda: {
#             "messages": [
#                 {
#                     "content": "What is deep learning?",
#                     "role": "user"
#                 }
#             ],
#             "stream": True,
#             "stream_options": {"include_usage": True}
#         }
#     }