"""Compare generated identities before hashing, while checking the original issued evidence."""
from packages.domain.input_manifest import manifest


def canonical_validation(check, data, replace):
    original = manifest(data)
    assert check['fingerprint'] == original['token']
    assert check['input_manifest'] == {key: original[key] for key in check['input_manifest']}
    normalized = manifest(replace(data))
    return {**replace(check), 'fingerprint': normalized['token'],
            'input_manifest': {key: normalized[key] for key in check['input_manifest']}}, normalized
