from typing import TypeGuard


def is_json_object(value: object) -> TypeGuard[dict[str, object]]:
    """Whether ``value`` is a JSON object or TOML table: a dict whose keys are all strings."""
    return isinstance(value, dict) and all(isinstance(key, str) for key in value)
