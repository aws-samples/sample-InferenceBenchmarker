import json
import numpy as np
import boto3
from sagemaker.serve.spec.inference_base import CustomOrchestrator

instance_type = 'ml.g5.48xlarge'
instance_code = 'g548x'

def invoke_factory(endpoint_name=None):
    from botocore.config import Config

    _endpoint_name = endpoint_name or 'IC-ep-{instance_code}'

    client = boto3.client(
        'sagemaker-runtime',
        config=Config(retries={'max_attempts': 0}, max_pool_connections=500),
    )

    def invoke(payload):
        response = client.invoke_endpoint(
            EndpointName=_endpoint_name,
            InferenceComponentName=f'ic-orchestrator-{instance_code}',
            ContentType='application/json',
            Accept='application/json',
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
