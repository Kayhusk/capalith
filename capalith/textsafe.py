"""Text for a person or an agent that may hold a path: control and direction characters never reach it as themselves."""
from __future__ import annotations

import unicodedata

# Control, direction, separator, surrogate, private-use and unassigned characters can't be allowed to end a line
# or reorder text inside an answer, an error or advice a person copies.
_UNSAFE = {"Cc", "Cf", "Zl", "Zp", "Cs", "Co", "Cn"}


def is_safe(text: object) -> bool:
    return all(unicodedata.category(char) not in _UNSAFE for char in str(text))


def visible(text: object) -> str:
    """Show an unsafe character as its code point; any record that holds the value keeps the raw text."""
    return "".join(
        char if unicodedata.category(char) not in _UNSAFE
        else (f"\\x{ord(char):02x}" if ord(char) < 256 else f"\\u{ord(char):04x}" if ord(char) < 0x10000 else f"\\U{ord(char):08x}")
        for char in str(text)
    )
