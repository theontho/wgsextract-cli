from __future__ import annotations

import json
from typing import Any


def loads_json_with_comments(source: str) -> Any:
    return json.loads(strip_json_comments(source))


def strip_json_comments(source: str) -> str:
    output: list[str] = []
    in_string = False
    escaped = False
    in_line_comment = False
    in_block_comment = False
    index = 0

    while index < len(source):
        char = source[index]
        next_char = source[index + 1] if index + 1 < len(source) else ""

        if in_line_comment:
            if char in "\r\n":
                in_line_comment = False
                output.append(char)
            else:
                output.append(" ")
            index += 1
            continue

        if in_block_comment:
            if char == "*" and next_char == "/":
                output.append("  ")
                index += 2
                in_block_comment = False
            else:
                output.append(char if char in "\r\n" else " ")
                index += 1
            continue

        if in_string:
            output.append(char)
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            index += 1
            continue

        if char == '"':
            in_string = True
            output.append(char)
        elif char == "/" and next_char == "/":
            output.append("  ")
            index += 1
            in_line_comment = True
        elif char == "/" and next_char == "*":
            output.append("  ")
            index += 1
            in_block_comment = True
        else:
            output.append(char)
        index += 1

    return "".join(output)
