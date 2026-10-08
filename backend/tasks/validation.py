"""Input checks shared by the task repository and queue."""


def has_control(value: str) -> bool:
    return any(ord(c) < 32 and c not in "\n\t" or ord(c) == 127 for c in value)


def bounded_text(value: object, name: str, limit: int) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise ValueError(f"{name} must be nonblank text of at most {limit} characters")
    if has_control(value):
        raise ValueError(f"{name} must not contain control characters")
    return value


def optional_text(value: object, name: str, limit: int) -> str | None:
    return None if value is None else bounded_text(value, name, limit)
