"""Public rule definitions at storage and strict compiler boundaries."""
from packages.contracts.schemas import PolicyDefinition


def stored_definition(value):
    """Copy without derived private metadata; unknown public fields still fail validation."""
    if isinstance(value, PolicyDefinition):
        value = value.model_dump(mode='json')
    if isinstance(value, dict):
        return {key: stored_definition(item) for key, item in value.items()
                if not isinstance(key, str) or not key.startswith('_')}
    if isinstance(value, list):
        return [stored_definition(item) for item in value]
    return value
