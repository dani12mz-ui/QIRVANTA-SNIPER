"""Strict wire helpers shared by the local service."""
import hashlib
import json
import math
import re
from pathlib import Path


def milliseconds(value):
    if type(value) is not int or not 0 < value < 253402300800000:
        raise ValueError("Expected UTC Unix milliseconds")
    return value


def finite(value, *, positive=False):
    if type(value) not in (int, float) or not math.isfinite(value) or (positive and value <= 0):
        raise ValueError("Expected finite number")
    return value


def identifier(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_.:|/-]{1,160}", value):
        raise ValueError("Invalid identifier")
    return value


def sha256(value):
    if not isinstance(value, str) or not re.fullmatch(r"[a-f0-9]{64}", value):
        raise ValueError("Invalid SHA256")
    return value


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def decode_json(data):
    def object_pairs(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("Duplicate JSON field")
            result[key] = value
        return result

    result = json.loads(data.decode("utf-8"), object_pairs_hook=object_pairs)
    canonical(result)
    return result


def read_json(path, limit=1048576):
    with Path(path).open("rb") as stream:
        data = stream.read(limit + 1)
    if len(data) > limit:
        raise ValueError("JSON input exceeds size limit")
    return decode_json(data)
