"""Explicit central-manager access policy. No settings discovery or I/O."""


def normalize_manager_allowed_user_ids(value):
    """Normalize to None (legacy access) or a tuple; an empty tuple denies all."""
    if value is None:
        return None
    if not isinstance(value, (tuple, list)) or len(value) > 100:
        raise ValueError("Некорректный список доступа управляющего кабинета.")
    result = tuple(value)
    if (
        any(type(user_id) is not int or not 0 < user_id < 2**63 for user_id in result)
        or len(set(result)) != len(result)
    ):
        raise ValueError("Некорректный список доступа управляющего кабинета.")
    return result
