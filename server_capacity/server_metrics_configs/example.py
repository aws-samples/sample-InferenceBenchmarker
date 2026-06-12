[
    {
        'stream': 'cloudwatch',
        'Namespace': str,
        'Dimensions': [{"Name": str, "Value": str}],
        'Period': int,
        'Lag': int,
        # Collated parameters to unfold in query
        'Metrics': list[str],
        'Statistics': listlist[[str]],
    },
    {
        'stream': 'cloudwatch',
        'Namespace': str,
        'Dimensions': [{"Name": str, "Value": str}],
        'Period': int,
        'Lag': int,
        # Collated parameters to unfold in query
        'Metrics': list[str],
        'Statistics': listlist[[str]],
    },
]