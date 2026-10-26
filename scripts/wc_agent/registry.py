"""Port of src/agent/registry.ts — the one list of tools."""
import inspect
from typing import Any, Awaitable, Callable, Dict, List, Optional, Union

from .types import ToolContext, result

ParseResult = Union[Dict[str, Any], str]


class Tool:
    def __init__(self, name: str, description: str, parameters: Dict[str, Any], kind: str,
                 execute: Callable[..., Awaitable[Dict[str, Any]]],
                 parse: Optional[Callable[[Optional[Dict[str, Any]]], ParseResult]] = None,
                 is_available: Optional[Callable[[ToolContext], bool]] = None,
                 preview: Optional[Callable[[str, ToolContext], None]] = None,
                 markup_form: bool = False, native_on_markup: bool = False, run_last: bool = False,
                 aliases: Optional[List[str]] = None, renumbers: bool = False) -> None:
        self.name = name
        #: Earlier names a call may still use (read_and_list.md §4); never offered.
        self.aliases = list(aliases or [])
        self.description = description
        self.parameters = parameters
        self.kind = kind
        self.markup_form = markup_form
        self.native_on_markup = native_on_markup
        self.run_last = run_last
        #: Renumbers the chapters after the one it removes: runs after every other run_last call too.
        self.renumbers = renumbers
        self._execute = execute
        self._parse = parse or (lambda raw: raw if raw is not None else "its arguments could not be parsed")
        self._is_available = is_available or (lambda ctx: True)
        self.preview = preview

    def is_available(self, ctx: ToolContext) -> bool:
        return self._is_available(ctx)

    def spec(self) -> Dict[str, Any]:
        return {"name": self.name, "description": self.description, "parameters": self.parameters}

    async def invoke(self, call: Dict[str, Any], ctx: ToolContext) -> Dict[str, Any]:
        parsed = self._parse(call.get("args"))
        if isinstance(parsed, str):
            return result(False, f"{self.name} was not run: {parsed}", f"⚠️ {self.name}: {parsed}",
                          effects={"producedNothing": True} if self.kind == "write" else None)
        out = self._execute(parsed, ctx, call)
        if inspect.isawaitable(out):
            out = await out
        return out


class ToolRegistry:
    def __init__(self, tools: Optional[List[Tool]] = None) -> None:
        self._tools: List[Tool] = []
        for t in tools or []:
            self.register(t)

    def register(self, tool: Tool) -> None:
        for name in [tool.name, *tool.aliases]:
            if self.get(name):
                raise ValueError(f'Tool "{name}" is already registered')
        self._tools.append(tool)

    def get(self, name: Optional[str]) -> Optional[Tool]:
        if not name:
            return None
        return next((t for t in self._tools if t.name == name), None) or next((t for t in self._tools if name in t.aliases), None)

    def descriptor(self, name: str) -> Optional[Dict[str, Any]]:
        """What invocations.collect_step asks about a tool."""
        tool = self.get(name)
        return {"kind": tool.kind, "markupForm": tool.markup_form} if tool else None

    def available(self, ctx: ToolContext, predicate: Callable[[Tool], bool] = lambda t: True) -> List[Tool]:
        return [t for t in self._tools if predicate(t) and t.is_available(ctx)]


def to_tool_specs(tools: List[Tool]) -> List[Dict[str, Any]]:
    return [t.spec() for t in tools]
