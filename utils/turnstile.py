import base64
import json
import random
import time
from typing import Any, Dict, Optional


class OrderedMap:
    def __init__(self) -> None:
        self.keys: list[str] = []
        self.values: dict[str, Any] = {}

    def add(self, key: str, value: Any) -> None:
        if key not in self.values:
            self.keys.append(key)
        self.values[key] = value

    def to_dict(self) -> dict[str, Any]:
        return {key: _turnstile_json_value(self.values[key]) for key in self.keys}

    def get(self, key: Any, default: Any = None) -> Any:
        return self.values.get(str(key), default)


def _turnstile_json_value(value: Any) -> Any:
    if isinstance(value, OrderedMap):
        return value.to_dict()
    if isinstance(value, dict):
        return {str(key): _turnstile_json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_turnstile_json_value(item) for item in value]
    return value


def _turnstile_to_str(value: Any) -> str:
    if value is None:
        return "undefined"
    if isinstance(value, float):
        return str(value)
    if isinstance(value, str):
        special = {
            "window.Math": "[object Math]",
            "window.Reflect": "[object Reflect]",
            "window.performance": "[object Performance]",
            "window.localStorage": "[object Storage]",
            "window.Object": "function Object() { [native code] }",
            "window.Reflect.set": "function set() { [native code] }",
            "window.performance.now": "function () { [native code] }",
            "window.Object.create": "function create() { [native code] }",
            "window.Object.keys": "function keys() { [native code] }",
            "window.Math.random": "function random() { [native code] }",
        }
        return special.get(value, value)
    if isinstance(value, list) and all(isinstance(item, str) for item in value):
        return ",".join(value)
    return str(value)


def _xor_string(text: str, key: str) -> str:
    if not key:
        return text
    return "".join(chr(ord(ch) ^ ord(key[i % len(key)])) for i, ch in enumerate(text))


def solve_turnstile_token(dx: str, p: str) -> Optional[str]:
    if not isinstance(dx, str) or not isinstance(p, str) or not dx or len(dx) > 512_000:
        return None
    try:
        decoded = base64.b64decode(dx, validate=True).decode()
        token_list = json.loads(_xor_string(decoded, p))
    except Exception:
        return None
    if not isinstance(token_list, list):
        return None

    process_map: Dict[Any, Any] = {}
    start_time = time.time()
    result = ""

    def read_property(target: Any, key: Any) -> Any:
        key_text = str(key)
        if isinstance(target, OrderedMap):
            return target.get(key_text)
        if isinstance(target, dict):
            return target.get(key_text)
        if isinstance(target, str):
            value = f"{target}.{key_text}"
            return "https://chatgpt.com/" if value == "window.document.location" else value
        return None

    def make_pseudo_element() -> OrderedMap:
        element = OrderedMap()
        element.add("style", OrderedMap())

        def get_bounding_client_rect() -> OrderedMap:
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

    def func_1(e: float, t: float) -> None:
        process_map[e] = _xor_string(_turnstile_to_str(process_map[e]), _turnstile_to_str(process_map[t]))

    def func_2(e: float, t: Any) -> None:
        process_map[e] = t

    def func_3(e: str) -> None:
        nonlocal result
        result = base64.b64encode(e.encode()).decode()

    def func_5(e: float, t: float) -> None:
        current = process_map[e]
        incoming = process_map[t]
        if isinstance(current, (list, tuple)):
            process_map[e] = list(current) + [incoming]
            return
        if isinstance(current, (str, float)) or isinstance(incoming, (str, float)):
            process_map[e] = _turnstile_to_str(current) + _turnstile_to_str(incoming)
            return
        process_map[e] = "NaN"

    def func_6(e: float, t: float, n: float) -> None:
        process_map[e] = read_property(process_map.get(t), process_map.get(n))

    def func_7(e: float, *args: float) -> None:
        target = process_map[e]
        values = [process_map[arg] for arg in args]
        if isinstance(target, str) and target == "window.Reflect.set":
            obj, key_name, val = values
            if isinstance(obj, OrderedMap):
                obj.add(str(key_name), val)
            elif isinstance(obj, dict):
                obj[str(key_name)] = val
        elif callable(target):
            target(*values)

    def func_8(e: float, t: float) -> None:
        process_map[e] = process_map[t]

    def func_14(e: float, t: float) -> None:
        process_map[e] = json.loads(process_map[t])

    def func_15(e: float, t: float) -> None:
        process_map[e] = json.dumps(
            _turnstile_json_value(process_map[t]),
            ensure_ascii=False,
            separators=(",", ":"),
        )

    def func_17(e: float, t: float, *args: float) -> None:
        call_args = [process_map[arg] for arg in args]
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

    def func_18(e: float) -> None:
        process_map[e] = base64.b64decode(_turnstile_to_str(process_map[e])).decode()

    def func_19(e: float) -> None:
        process_map[e] = base64.b64encode(_turnstile_to_str(process_map[e]).encode()).decode()

    def func_20(e: float, t: float, n: float, *args: float) -> None:
        if process_map.get(e) == process_map.get(t):
            target = process_map.get(n)
            if callable(target):
                target(*args)

    def func_21(*_: Any) -> None:
        return

    def func_23(e: float, t: float, *args: float) -> None:
        if process_map.get(e) is not None and callable(process_map.get(t)):
            process_map[t](*args)

    def func_24(e: float, t: float, n: float) -> None:
        process_map[e] = read_property(process_map.get(t), process_map.get(n))

    def func_13(e: float, t: float, *args: float) -> None:
        if process_map.get(e) is not None and callable(process_map.get(t)):
            process_map[t](*args)

    def func_22(e: float, program: Any) -> None:
        process_map[e] = None
        if isinstance(program, list):
            _execute_program(program)

    def func_34(e: float, t: float) -> None:
        process_map[e] = process_map.get(t)

    process_map.update({
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
    })

    instruction_count = 0

    def _execute_program(program: list[Any]) -> None:
        nonlocal instruction_count
        for token in program:
            instruction_count += 1
            if instruction_count > 10_000:
                raise RuntimeError("turnstile instruction limit exceeded")
            if not isinstance(token, list) or not token:
                continue
            try:
                fn = process_map.get(token[0])
                if callable(fn):
                    fn(*token[1:])
            except Exception:
                continue

    programs: list[list[Any]] = [token_list]
    seen_program_ids: set[int] = set()
    try:
        for _ in range(8):
            program = programs[-1]
            program_id = id(program)
            if program_id in seen_program_ids:
                break
            seen_program_ids.add(program_id)
            _execute_program(program)
            next_program = process_map.get(9)
            if not isinstance(next_program, list) or id(next_program) in seen_program_ids:
                break
            programs.append(next_program)
    except RuntimeError:
        return None

    if not result or len(result) < 512:
        return None
    return result
