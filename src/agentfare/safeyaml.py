"""Guards for YAML that somebody else may have written (a shared config, a price table from a download).

PyYAML builds an alias as a reference to the anchored object, so a few lines of anchors (a "billion laughs" document) load
instantly into a structure whose *expansion* has billions of values. Loading is safe; the first ``str()``, ``copy`` or
``json.dumps`` of it is not. ``check_expansion`` measures the expanded size by walking each shared object once.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any

import yaml

MAX_EXPANDED_VALUES = 100_000  # values in the document once every alias is expanded; real files have a few hundred
MAX_DEPTH = 100


class YamlBombError(ValueError):
    """The document expands to too many values, is nested too deeply or contains itself."""


def check_expansion(doc: Any, *, limit: int = MAX_EXPANDED_VALUES) -> None:
    """Raise ``YamlBombError`` unless ``doc`` has at most ``limit`` values after aliases are expanded, no cycle and a sane depth."""
    sizes: dict[int, int] = {}  # id of a container -> its expanded size (capped at limit + 1)
    active: set[int] = set()  # containers being walked now: meeting one again is a cycle
    # Iterative post-order walk: (node, children iterator); depth is the length of the stack.
    if not isinstance(doc, (dict, list, tuple)):
        return

    def children(node: Any) -> list[Any]:
        if isinstance(node, dict):
            return [x for kv in node.items() for x in kv]
        return list(node)

    stack: list[tuple[Any, list[Any], int]] = []
    active.add(id(doc))
    stack.append((doc, children(doc), 0))
    totals: list[int] = [1]
    while stack:
        node, kids, at = stack[-1]
        if len(stack) > MAX_DEPTH:
            raise YamlBombError(f"nested more than {MAX_DEPTH} levels deep")
        if at < len(kids):
            stack[-1] = (node, kids, at + 1)
            kid = kids[at]
            if not isinstance(kid, (dict, list, tuple)):
                totals[-1] += 1
            elif id(kid) in active:
                raise YamlBombError("a value contains itself (a recursive alias)")
            elif id(kid) in sizes:
                totals[-1] += sizes[id(kid)]
            else:
                active.add(id(kid))
                stack.append((kid, children(kid), 0))
                totals.append(1)
            if totals[-1] > limit:
                raise YamlBombError(f"aliases expand to more than {limit:,} values")
        else:
            stack.pop()
            active.discard(id(node))
            size = totals.pop()
            sizes[id(node)] = size
            if stack:
                totals[-1] += size
                if totals[-1] > limit:
                    raise YamlBombError(f"aliases expand to more than {limit:,} values")


def shown(value: Any) -> str:
    """``value`` for an error message: a scalar as written (cut short), anything else by its type. Never ``str()`` of a
    structure: one built by YAML aliases can be billions of characters long."""
    if isinstance(value, int) and not isinstance(value, bool) and value.bit_length() > 200:
        return "a very large number"
    if isinstance(value, (str, int, float, Decimal, bool, date)):
        text = repr(value)
        return text if len(text) <= 40 else text[:37] + "..."
    return f"a {type(value).__name__}"


class DuplicateKeyError(yaml.YAMLError):
    """A mapping that holds the same key twice: ``duplicate key input at line 5``."""


class UniqueKeyLoader(yaml.SafeLoader):
    """``yaml.SafeLoader`` that refuses a mapping with the same key twice.

    PyYAML keeps the last value and says nothing, so a second ``input:`` (a price copied in and never removed) silently replaces
    the first one. The error names the key and the line of the second one. A key that a merge (``<<``) brings in and the mapping
    then sets itself is an override, as YAML defines it, and is not a duplicate."""

    def construct_mapping(self, node: yaml.MappingNode, deep: bool = False) -> dict[Any, Any]:  # type: ignore[override]
        seen: set[Any] = set()
        for key_node, _ in node.value:
            if not isinstance(key_node, yaml.ScalarNode) or key_node.tag == "tag:yaml.org,2002:merge":
                continue
            key = self.construct_object(key_node, deep=True)  # the scalar as the mapping will hold it: 1 and 1.0 are one key
            try:
                if key in seen:
                    name = key if isinstance(key, str) else str(key)
                    name = name if len(name) <= 40 else name[:37] + "..."
                    raise DuplicateKeyError(f"duplicate key {name} at line {key_node.start_mark.line + 1}")
                seen.add(key)
            except TypeError:  # an unhashable key: PyYAML reports it itself
                continue
        return super().construct_mapping(node, deep=deep)
