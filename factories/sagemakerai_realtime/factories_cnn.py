import numpy as np


def invoke_factory(endpoint_name=None):
    import json
    import boto3
    from botocore.config import Config

    _endpoint_name = endpoint_name or 'InferenceBenchmarker-CNN'

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
    return {
        'pre_computed': True,
        'input': [
            {'instances': np.random.randn(1, 200, 200).tolist()}
            for _ in range(1)
        ],
    }


# # Example non-pre-computed factory:
# def payload_factory():
#     return {
#         'pre_computed': False,
#         'input': lambda: {'instances': np.random.randn(1, 200, 200).tolist()},
#     }
