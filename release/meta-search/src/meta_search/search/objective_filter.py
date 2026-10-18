from mcp.server.fastmcp.exceptions import ToolError

from ..textnorm import norm_text
from ..validation import ValidationError, clean_value

OPS = {
    "string": ["eq", "ne", "in", "not_in", "exists"],
    "number": ["eq", "ne", "in", "not_in", "gte", "lte", "between", "exists"],
    "boolean": ["eq", "ne", "exists"],
    "string_list": ["contains_any", "contains_all", "exists"],
}


def validate_filter(condition, definitions):
    definitions = {d["key"]: d for d in definitions}
    guide = {key: definition["value_type"] for key, definition in definitions.items()}
    if condition.key not in definitions:
        raise ToolError(f"없는 속성 키: {condition.key}. 사용 가능 키/타입: {guide}")
    kind = definitions[condition.key]["value_type"]
    if condition.op not in OPS[kind]:
        raise ToolError(f"{condition.key}: 연산자 {condition.op} 불가. 허용: {OPS[kind]}. 키/타입: {guide}")
    value = condition.value
    try:
        if condition.op == "exists":
            if type(value) is not bool:
                raise ValidationError("exists value는 boolean이어야 합니다")
        elif condition.op in ("in", "not_in", "contains_any", "contains_all", "between"):
            if not isinstance(value, list) or not value:
                raise ValidationError("비어 있지 않은 배열 필요")
            item_kind = "string" if kind == "string_list" else kind
            for item in value:
                clean_value(item, item_kind)
            if condition.op == "between" and (len(value) != 2 or value[0] > value[1]):
                raise ValidationError("between에는 [min,max] 필요")
        else:
            clean_value(value, kind)
    except ValidationError as error:
        raise ToolError(f"{condition.key}: {error}. 키/타입: {guide}") from error


def evaluate(condition, value):
    op, expected = condition.op, condition.value
    if op == "exists":
        passed = (value is not None) == expected
    elif value is None:
        return "UNKNOWN", f"{condition.key}: 값 미확인"
    else:

        def normalize(v):
            return norm_text(v) if isinstance(v, str) else v

        v = normalize(value)
        e = normalize(expected)
        if op in ("eq", "ne"):
            passed = (v == e) if op == "eq" else (v != e)
        elif op in ("in", "not_in"):
            passed = v in [normalize(item) for item in expected]
            if op == "not_in":
                passed = not passed
        elif op == "gte":
            passed = value >= expected
        elif op == "lte":
            passed = value <= expected
        elif op == "between":
            passed = expected[0] <= value <= expected[1]
        else:
            stored = {norm_text(item) for item in value}
            wanted = {norm_text(item) for item in expected}
            passed = bool(stored & wanted) if op == "contains_any" else wanted <= stored
    return ("PASS" if passed else "FAIL"), f"{condition.key} {op} {expected}: {value}"
