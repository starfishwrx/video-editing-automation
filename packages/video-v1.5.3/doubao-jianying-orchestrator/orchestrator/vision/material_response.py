"""Validate the two material response shapes and identify the field to repair."""
from functools import partial

from .vision_client import ResponseShapeError


def object_validator(check, **bindings):
    validator = partial(check, **bindings)
    validator.expects_json_object = True
    return validator


def validate_shots(data, expected):
    rows = data.get("shots") if isinstance(data, dict) else data
    if not isinstance(rows, list) or len(rows) != len(expected):
        raise ResponseShapeError(f"shots 必须是数组，按原图的镜头编号返回 {len(expected)} 条记录。")
    seen = set()
    for index, row in enumerate(rows, 1):
        if not isinstance(row, dict):
            raise ResponseShapeError(f"shots 第 {index} 项必须是对象。")
        sid = str(row.get("shot_id"))
        if sid not in expected or sid in seen:
            raise ResponseShapeError(f"shots 第 {index} 项的 shot_id 错误或重复，请逐一照抄原图编号。")
        seen.add(sid)
        if not isinstance(row.get("description"), str) or not row["description"].strip():
            raise ResponseShapeError(f"shots 第 {index} 项缺少非空字符串 description。")
        if type(row.get("action_complete")) is not bool:
            raise ResponseShapeError(f"shots 第 {index} 项的 action_complete 必须为 true 或 false。")
    return True


def validate_text(data, shot_id):
    if isinstance(data, list) and len(data) == 1:
        data = data[0]
    if not isinstance(data, dict) or data.get("shot_id") != shot_id:
        raise ResponseShapeError("请照抄原输入的 shot_id，并返回 readable_claims 和 pointed_text 两个字符串数组。")
    for field in ("readable_claims", "pointed_text"):
        values = data.get(field)
        if not isinstance(values, list) or not all(isinstance(value, str) for value in values):
            raise ResponseShapeError(f"{field} 必须是字符串数组；请重新看原图，确实没有可抄文字时填 []。")
    return True
