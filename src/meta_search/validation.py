import math
import re
from urllib.parse import urlsplit


class ValidationError(ValueError):
    pass


class PipelineError(RuntimeError):
    pass


def valid_url(url):
    try:
        parsed = urlsplit(url)
        return parsed.scheme in ("http", "https") and bool(parsed.hostname)
    except ValueError:
        return False


def valid_key(key):
    return bool(re.fullmatch(r"[a-z][a-z0-9_]*", key))


def clean_value(value, kind):
    valid = {
        "string": isinstance(value, str) and bool(value.strip()),
        "number": type(value) in (int, float) and math.isfinite(value),
        "boolean": type(value) is bool,
        "string_list": isinstance(value, list)
        and bool(value)
        and all(isinstance(v, str) and bool(v.strip()) for v in value),
    }
    if not valid.get(kind, False):
        raise ValidationError(f"{kind}: 값 타입/내용 불일치: {value!r}")
    if kind == "string":
        return value.strip()
    if kind == "string_list":
        return sorted(set(v.strip() for v in value))
    return value
