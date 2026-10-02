"""A minimal reader for KiCad's s-expression file format.

Only what the schematic reader needs: nested lists of atoms, with quoted-string
escapes honoured. Deliberately dependency-free and forgiving: a malformed tail
yields whatever parsed cleanly rather than raising, because a partially readable
schematic is more useful to a validator than none.
"""

from __future__ import annotations

SExpr = str | list["SExpr"]

_ESCAPES = {"n": "\n", "r": "\r", "t": "\t", '"': '"', "\\": "\\"}

_DELIMITERS = set('()"\r\n\t ')


def parse(text: str) -> SExpr | None:
    """Parse the first complete s-expression in ``text``.

    Returns ``None`` if no balanced expression is found.
    """
    stack: list[list[SExpr]] = []
    current: list[SExpr] | None = None
    index = 0
    length = len(text)

    while index < length:
        char = text[index]

        if char == "(":
            current = []
            stack.append(current)
            index += 1
            continue

        if char == ")":
            index += 1
            if current is None:
                continue
            # `current` is itself the last entry on the stack; drop it, then the
            # new stack top (if any) is this list's parent.
            stack.pop()
            if not stack:
                # Outermost expression closed; that is the whole document.
                return current
            parent = stack[-1]
            parent.append(current)
            current = parent
            continue

        if char == '"':
            value, index = _read_quoted(text, index)
            if current is not None:
                current.append(value)
            continue

        if char in _DELIMITERS:
            index += 1
            continue

        atom, index = _read_atom(text, index)
        if current is not None:
            current.append(atom)

    return None


def _read_quoted(text: str, start: int) -> tuple[str, int]:
    out: list[str] = []
    index = start + 1
    length = len(text)

    while index < length:
        char = text[index]
        if char == "\\":
            if index + 1 < length:
                out.append(_ESCAPES.get(text[index + 1], text[index + 1]))
                index += 2
                continue
            index += 1
            continue
        if char == '"':
            return "".join(out), index + 1
        out.append(char)
        index += 1

    return "".join(out), index


def _read_atom(text: str, start: int) -> tuple[str, int]:
    index = start
    length = len(text)
    while index < length and text[index] not in _DELIMITERS:
        index += 1
    return text[start:index], index


def head(node: SExpr) -> str:
    """Return the leading keyword of a list node, or '' for an atom."""
    if isinstance(node, list) and node and isinstance(node[0], str):
        return node[0]
    return ""


def children(node: SExpr | None, keyword: str) -> list[list[SExpr]]:
    """Return direct child lists whose leading keyword is ``keyword``."""
    if not isinstance(node, list):
        return []
    return [child for child in node if isinstance(child, list) and head(child) == keyword]


def child(node: SExpr | None, keyword: str) -> list[SExpr] | None:
    """Return the first direct child list with ``keyword``, or None."""
    found = children(node, keyword)
    return found[0] if found else None


def value_of(node: list[SExpr] | None, index: int = 1, default: str = "") -> str:
    """Return the nth string atom of a list node, or ``default``."""
    if node is None or index >= len(node):
        return default
    candidate = node[index]
    return candidate if isinstance(candidate, str) else default
