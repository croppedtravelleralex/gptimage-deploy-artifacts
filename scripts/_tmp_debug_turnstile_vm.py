#!/usr/bin/env python3
from __future__ import annotations

import base64
import json
import random
import time
from pathlib import Path

from utils.turnstile import OrderedMap, _turnstile_to_str, _xor_string

HAR = Path("docs/captures/spa/spa-image-20260721T144019Z.har")


def decode_result(token: str, key):
    if not token:
        return None
    raw = base64.b64decode(token).decode()
    return json.loads(_xor_string(raw, _turnstile_to_str(key)))


def diff_values(actual, expected, path="$"):
    diffs = []
    if type(actual) is not type(expected):
        return [
            (
                path,
                f"{type(actual).__name__}:{repr(actual)[:100]}",
                f"{type(expected).__name__}:{repr(expected)[:100]}",
            )
        ]
    if isinstance(actual, dict):
        for key in sorted(actual.keys() | expected.keys()):
            child = f"{path}.{key}"
            if key not in actual:
                diffs.append((child, "<missing>", repr(expected[key])[:120]))
            elif key not in expected:
                diffs.append((child, repr(actual[key])[:120], "<missing>"))
            else:
                diffs.extend(diff_values(actual[key], expected[key], child))
        return diffs
    if isinstance(actual, list):
        if len(actual) != len(expected):
            diffs.append((path + ".length", len(actual), len(expected)))
        for index, (left, right) in enumerate(zip(actual, expected)):
            diffs.extend(diff_values(left, right, f"{path}[{index}]"))
        return diffs
    if actual != expected:
        diffs.append((path, repr(actual)[:120], repr(expected)[:120]))
    return diffs


def main() -> None:
    har = json.loads(HAR.read_text(encoding="utf-8", errors="replace"))
    preps: list[tuple[str, str]] = []
    recorded_tokens: list[str] = []
    for e in har["log"]["entries"]:
        url = e["request"]["url"]
        text = ((e["request"].get("postData") or {}).get("text")) or ""
        if not text:
            continue
        try:
            q = json.loads(text)
        except Exception:
            continue
        if url.endswith("/chat-requirements/prepare"):
            r = json.loads((e["response"].get("content") or {}).get("text") or "{}")
            preps.append((q["p"], r["turnstile"]["dx"]))
        elif url.endswith("/chat-requirements/finalize"):
            token = str(q.get("turnstile") or q.get("turnstile_token") or "")
            if token:
                recorded_tokens.append(token)

    p, dx = preps[0]
    token_list = json.loads(_xor_string(base64.b64decode(dx).decode(), p))

    start_time = time.time()
    process_map: dict = {}
    result_box = {"value": ""}
    errors: list = []
    misses: list = []
    nested_programs: list = []
    nested_program_calls: list = []

    def read_property(target, key):
        key_text = str(key)
        if isinstance(target, OrderedMap):
            return target.get(key_text)
        if isinstance(target, dict):
            return target.get(key_text)
        if isinstance(target, str):
            value = f"{target}.{key_text}"
            return "https://chatgpt.com/" if value == "window.document.location" else value
        return None

    def make_pseudo_element():
        element = OrderedMap()
        element.add("style", OrderedMap())

        def get_bounding_client_rect():
            rect = OrderedMap()
            for key, value in (
                ("x", 0),
                ("y", 1129),
                ("width", 28.300003051757812),
                ("height", 27),
                ("top", 1129),
                ("right", 28.300003051757812),
                ("bottom", 1156),
                ("left", 0),
            ):
                rect.add(key, value)
            return rect

        element.add("getBoundingClientRect", get_bounding_client_rect)
        return element

    def func_1(e, t):
        process_map[e] = _xor_string(
            _turnstile_to_str(process_map[e]), _turnstile_to_str(process_map[t])
        )

    def func_2(e, t):
        process_map[e] = t

    def func_3(e: str):
        result_box["value"] = base64.b64encode(e.encode()).decode()

    def func_5(e, t):
        current = process_map[e]
        incoming = process_map[t]
        if isinstance(current, (list, tuple)):
            process_map[e] = list(current) + [incoming]
        elif isinstance(current, (str, float)) or isinstance(incoming, (str, float)):
            process_map[e] = _turnstile_to_str(current) + _turnstile_to_str(incoming)
        else:
            process_map[e] = "NaN"

    def func_6(e, t, n):
        process_map[e] = read_property(process_map.get(t), process_map.get(n))

    def func_7(e, *args):
        target = process_map[e]
        values = [process_map[a] for a in args]
        if isinstance(target, str) and target == "window.Reflect.set":
            obj, key_name, val = values
            if isinstance(obj, OrderedMap):
                obj.add(str(key_name), val)
            elif isinstance(obj, dict):
                obj[str(key_name)] = val
        elif callable(target):
            target(*values)

    def func_8(e, t):
        process_map[e] = process_map[t]

    def func_14(e, t):
        process_map[e] = json.loads(process_map[t])

    def to_jsonable(value):
        if isinstance(value, OrderedMap):
            return {key: to_jsonable(value.values[key]) for key in value.keys}
        if isinstance(value, list):
            return [to_jsonable(item) for item in value]
        return value

    def func_15(e, t):
        process_map[e] = json.dumps(
            to_jsonable(process_map[t]),
            ensure_ascii=False,
            separators=(",", ":"),
        )

    def func_17(e, t, *args):
        call_args = [process_map[a] for a in args]
        target = process_map[t]
        if target == "window.performance.now":
            elapsed_ns = time.time_ns() - int(start_time * 1e9)
            process_map[e] = (elapsed_ns + random.random()) / 1e6
        elif target == "window.Object.create":
            process_map[e] = OrderedMap()
        elif target == "window.document.createElement":
            process_map[e] = make_pseudo_element()
        elif target == "window.navigator.storage.estimate":
            estimate = OrderedMap()
            estimate.add("quota", 10 * 1024**3)
            estimate.add("usage", 16 * 1024)
            process_map[e] = estimate
        elif target == "window.Object.keys":
            if call_args and call_args[0] == "window.localStorage":
                process_map[e] = [
                    "STATSIG_LOCAL_STORAGE_INTERNAL_STORE_V4",
                    "STATSIG_LOCAL_STORAGE_STABLE_ID",
                    "client-correlated-secret",
                    "oai/apps/capExpiresAt",
                    "oai-did",
                    "STATSIG_LOCAL_STORAGE_LOGGING_REQUEST",
                    "UiState.isNavigationCollapsed.1",
                ]
            elif call_args and isinstance(call_args[0], OrderedMap):
                process_map[e] = list(call_args[0].keys)
            elif call_args and isinstance(call_args[0], dict):
                process_map[e] = list(call_args[0])
        elif target == "window.Math.random":
            process_map[e] = random.random()
        elif callable(target):
            process_map[e] = target(*call_args)

    def func_18(e):
        process_map[e] = base64.b64decode(_turnstile_to_str(process_map[e])).decode()

    def func_19(e):
        process_map[e] = base64.b64encode(_turnstile_to_str(process_map[e]).encode()).decode()

    def func_20(e, t, n, *args):
        if process_map.get(e) == process_map.get(t):
            target = process_map.get(n)
            if callable(target):
                target(*args)

    def func_21(*_):
        return

    def func_23(e, t, *args):
        if process_map.get(e) is not None and callable(process_map.get(t)):
            process_map[t](*args)

    def func_13(e, t, *args):
        if process_map.get(e) is not None and callable(process_map.get(t)):
            process_map[t](*args)

    def func_22(e, program):
        process_map[e] = None
        nested_programs.append(program)
        nested_program_calls.append({"e": e, "program_len": len(program) if isinstance(program, list) else None})
        execute_program(program, stage=-1)

    def func_34(e, t):
        process_map[e] = process_map.get(t)

    def func_24(e, t, n):
        process_map[e] = read_property(process_map.get(t), process_map.get(n))

    process_map.update(
        {
            1: func_1,
            2: func_2,
            3: func_3,
            5: func_5,
            6: func_6,
            7: func_7,
            8: func_8,
            9: token_list,
            10: "window",
            13: func_13,
            14: func_14,
            15: func_15,
            16: p,
            17: func_17,
            18: func_18,
            19: func_19,
            20: func_20,
            21: func_21,
            22: func_22,
            23: func_23,
            24: func_24,
            34: func_34,
        }
    )

    def execute_program(program, *, stage):
        for idx, token in enumerate(program):
            try:
                fn = process_map.get(token[0])
                if callable(fn):
                    fn(*token[1:])
                else:
                    misses.append((stage, idx, token[0], token[:4]))
            except Exception as exc:  # noqa: BLE001
                arg_values = {
                    str(arg): repr(process_map.get(arg, "<missing>"))[:240]
                    for arg in token[1:]
                }
                errors.append(
                    (
                        stage,
                        idx,
                        token[0],
                        type(exc).__name__,
                        str(exc)[:160],
                        token[:5],
                        arg_values,
                    )
                )

    programs = [token_list]
    program_lengths: list[int] = []
    stage = 0
    while stage < len(programs):
        program = programs[stage]
        program_lengths.append(len(program))
        execute_program(program, stage=stage)
        next_program = process_map.get(9)
        if len(programs) < 8 and isinstance(next_program, list) and next_program is not program:
            programs.append(next_program)
        stage += 1

    recorded = recorded_tokens[0] if recorded_tokens else ""
    result_key = process_map.get(45.42)
    actual_payload = decode_result(result_box["value"], result_key)
    recorded_payload = decode_result(recorded, result_key)
    payload_diffs = diff_values(actual_payload, recorded_payload)
    recorded_rect = None
    try:
        recorded_rect = json.loads(
            _xor_string(
                base64.b64decode(recorded_payload["85.13"]).decode(),
                _turnstile_to_str(result_key),
            )
        )
    except Exception:
        pass
    common_prefix_len = 0
    for actual, expected in zip(result_box["value"], recorded):
        if actual != expected:
            break
        common_prefix_len += 1

    print(
        json.dumps(
            {
                "tokens": len(token_list),
                "program_lengths": program_lengths,
                "inner_program_slice_300_370": programs[1][300:370] if len(programs) > 1 else [],
                "nested_programs": nested_programs,
                "nested_program_calls": nested_program_calls,
                "func13_instructions": [
                    (index, token)
                    for index, token in enumerate(programs[1] if len(programs) > 1 else [])
                    if token and token[0] in (27.84, 51.41)
                ],
                "register_61_6_instructions": [
                    (index, token)
                    for index, token in enumerate(programs[1] if len(programs) > 1 else [])
                    if 61.6 in token
                ],
                "func22_aliases": [
                    key for key, value in process_map.items() if value is func_22
                ],
                "func22_instructions": [
                    (index, token)
                    for index, token in enumerate(programs[1] if len(programs) > 1 else [])
                    if token and process_map.get(token[0]) is func_22
                ],
                "func13_condition_values": {
                    str(key): repr(process_map.get(key, "<missing>"))[:200]
                    for key in (94.27, 1.91, 37.44, 61.6)
                },
                "last_program": programs[-1] if programs else None,
                "result_len": len(result_box["value"]),
                "recorded_len": len(recorded),
                "common_prefix_len": common_prefix_len,
                "actual_payload_type": type(actual_payload).__name__,
                "recorded_payload_type": type(recorded_payload).__name__,
                "payload_diff_count": len(payload_diffs),
                "first_payload_diffs": payload_diffs[:50],
                "recorded_rect": recorded_rect,
                "storage_registers": {
                    str(key): repr(process_map.get(key, "<missing>"))
                    for key in (61.6, 59.98, 68.65, 22.65, 5.84, 48.53, 68.56)
                },
                "misses": len(misses),
                "errors": len(errors),
                "first_misses": misses[:6],
                "first_errors": errors[:50],
                "has_81_2": 81.2 in process_map,
                "type_81_2": type(process_map.get(81.2)).__name__,
                "result_prefix": result_box["value"][:48] if result_box["value"] else None,
            },
            ensure_ascii=False,
            indent=2,
            default=str,
        )
    )


if __name__ == "__main__":
    main()
