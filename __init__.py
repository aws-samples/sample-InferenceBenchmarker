"""
InferenceBenchmarker - Client capacity testing for SageMaker endpoints.
"""

from .client_capacity import find_worker_saturation, find_rps_saturation

__all__ = [
    'find_worker_saturation',
    'find_rps_saturation',
]

__version__ = '2.0.0'
