"""JavaScript string semantics the ports need and Python lacks."""

JS_WS = "\t\n\v\f\r                  　﻿"


def js_trim(s: str) -> str:
    """String.prototype.trim: JavaScript's whitespace set, which has U+FEFF and Python's does not."""
    return s.strip(JS_WS)


def js_is_space(ch: str) -> bool:
    """The `\\s` class of a JavaScript regex, one character at a time."""
    return ch in JS_WS


def js_replace(s: str, needle: str, repl: str, count: int = -1) -> str:
    """String.prototype.replace with a STRING replacement: `$$`, `$&`, `` $` ``
    and `$'` in the replacement are patterns, not text. `count` -1 replaces
    every occurrence (a global regex); 1 replaces the first (a string needle)."""
    out = []
    pos = 0
    done = 0
    while count < 0 or done < count:
        i = s.find(needle, pos)
        if i == -1:
            break
        out.append(s[pos:i])
        expanded = []
        j = 0
        while j < len(repl):
            if repl[j] == "$" and j + 1 < len(repl):
                nxt = repl[j + 1]
                if nxt == "$":
                    expanded.append("$"); j += 2; continue
                if nxt == "&":
                    expanded.append(needle); j += 2; continue
                if nxt == "`":
                    expanded.append(s[:i]); j += 2; continue
                if nxt == "'":
                    expanded.append(s[i + len(needle):]); j += 2; continue
            expanded.append(repl[j]); j += 1
        out.append("".join(expanded))
        pos = i + len(needle)
        done += 1
        if not needle:
            break
    out.append(s[pos:])
    return "".join(out)
