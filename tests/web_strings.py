"""The string literals of a TypeScript file, comments left out: the words a static guard can treat as user-visible text.

A small tokenizer (no dependency): '…', "…" and `…` (a template's static parts, each `${…}` read as one space), `//` and
`/* */` comments skipped (the literals inside a `${…}` are read as well), a regex literal skipped when a `/` follows an operator or opening bracket. shortcut: a regex
after `)` reads as division, so a quote inside such a regex could be misread; none exists in apps/web/src today, and a
misread shows up as a guard false positive (move the regex into a named const)."""
import re
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / 'apps/web/src'
_REGEX_BEFORE = set('(,=:[!&|?{};+-*%<>~^') | {''}


def literals(text):
    """[(line, value)] for every string literal in `text`."""
    out, i, n, line, last = [], 0, len(text), 1, ''
    while i < n:
        c = text[i]
        if c == '\n':
            line += 1
        if c in ' \t\r\n':
            i += 1
            continue
        two = text[i:i + 2]
        if two == '//':
            i = text.find('\n', i)
            i = n if i < 0 else i
            continue
        if two == '/*':
            end = text.find('*/', i + 2)
            end = n if end < 0 else end + 2
            line += text.count('\n', i, end)
            i = end
            continue
        if c == '/' and (last in _REGEX_BEFORE or re.search(r'\b(return|typeof|case|in|of)$', text[max(0, i - 8):i])):
            j, klass = i + 1, False
            while j < n and text[j] != '\n':
                if text[j] == '\\':
                    j += 2
                    continue
                if text[j] == '[':
                    klass = True
                elif text[j] == ']':
                    klass = False
                elif text[j] == '/' and not klass:
                    break
                j += 1
            i, last = j + 1, 'r'
            continue
        if c in '\'"':
            j, buf = i + 1, []
            while j < n and text[j] != c and text[j] != '\n':
                if text[j] == '\\':
                    buf.append(text[j:j + 2])
                    j += 2
                    continue
                buf.append(text[j])
                j += 1
            out.append((line, ''.join(buf)))
            i, last = j + 1, 's'
            continue
        if c == '`':
            j, buf, start = i + 1, [], line
            while j < n and text[j] != '`':
                if text[j] == '\\':
                    buf.append(text[j:j + 2])
                    j += 2
                    continue
                if text[j:j + 2] == '${':
                    depth, j = 1, j + 2
                    expr = j
                    while j < n and depth:
                        if text[j] == '{':
                            depth += 1
                        elif text[j] == '}':
                            depth -= 1
                        elif text[j] in '\'"`':  # a nested literal inside the expression: skip it whole
                            q, j = text[j], j + 1
                            while j < n and text[j] != q:
                                j += 2 if text[j] == '\\' else 1
                        j += 1
                    out.extend((line + ln - 1, v) for ln, v in literals(text[expr:j - 1]))  # its own literals count too
                    buf.append(' ')
                    continue
                if text[j] == '\n':
                    line += 1
                buf.append(text[j])
                j += 1
            out.append((start, ''.join(buf)))
            i, last = j + 1, 's'
            continue
        last = c
        i += 1
    return out


def web_literals():
    """{file name: [(line, value)]} for apps/web/src/*.ts (generated.ts left out: the contract's own words)."""
    return {p.name: literals(p.read_text(encoding='utf-8')) for p in sorted(SRC.glob('*.ts')) if p.name != 'generated.ts'}
