"""Tool registry: the @tool decorator turns a function into an Atenea tool in a few lines.

    from atenea import tool

    @tool
    def weather(city: str) -> str:
        "Return the current weather in a city."
        ...

The JSON schema comes from the signature and type hints; the description from the docstring.
"""
from __future__ import annotations

import inspect
import typing
from dataclasses import dataclass, field
from typing import Any, Callable

_PY2JSON = {str: "string", int: "integer", float: "number", bool: "boolean", list: "array", dict: "object"}


@dataclass
class Tool:
    name: str
    description: str
    parameters: dict
    fn: Callable[..., Any]
    dangerous: bool = False           # needs user confirmation when a permission handler is set
    tags: set = field(default_factory=set)

    def schema(self) -> dict:
        return {"type": "function", "function": {"name": self.name, "description": self.description,
                                                 "parameters": self.parameters}}

    def __call__(self, **kw):
        return self.fn(**kw)


def _json_type(ann) -> dict:
    origin = typing.get_origin(ann)
    if origin is typing.Union or str(origin) == "types.UnionType":
        args = [a for a in typing.get_args(ann) if a is not type(None)]
        return _json_type(args[0]) if args else {"type": "string"}
    if origin in (list, typing.List):
        (inner,) = typing.get_args(ann) or (str,)
        return {"type": "array", "items": _json_type(inner)}
    return {"type": _PY2JSON.get(ann, "string")}


def tool(fn: Callable | None = None, *, name: str | None = None, dangerous: bool = False, tags=()):
    """Turn a function into an Atenea tool."""
    def wrap(f):
        sig = inspect.signature(f)
        hints = typing.get_type_hints(f)
        props, req = {}, []
        for p in sig.parameters.values():
            props[p.name] = _json_type(hints.get(p.name, str))
            if p.default is inspect.Parameter.empty:
                req.append(p.name)
        return Tool(name or f.__name__, inspect.getdoc(f) or f.__name__,
                    {"type": "object", "properties": props, "required": req}, f, dangerous, set(tags))
    return wrap(fn) if fn else wrap
