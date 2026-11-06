"""Preserve SQLite integer evidence across JavaScript's JSON number boundary."""
MAX_SAFE_INTEGER = 2**53 - 1


def browser_safe(value):
    if type(value) is int and abs(value) > MAX_SAFE_INTEGER:
        return str(value)
    if isinstance(value, dict):
        return {key: browser_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [browser_safe(item) for item in value]
    return value
