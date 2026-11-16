"""Source scan: an error notice or toast built from a caught error passes that error, so "Report this problem" can
decide on the error itself (apps/web/src/api.ts unexpectedError). A message alone never offers the report, so a
catch that drops the error would hide the report for a server fault or a lost connection."""
import re
from pathlib import Path

WEB = Path(__file__).resolve().parents[1] / 'apps' / 'web' / 'src'
# `x.message` or `(x as Error).message` inside the call's message.
# Rule check issues carry a message too; they are findings, not caught errors.
NOT_ERRORS = {'issue'}
MESSAGE = re.compile(r'\(?\b(\w+)(?:\s+as\s+\w+\))?\)?\.message\b')


def calls(text, name):
    """Each call of `name(` with its full argument text (brackets and strings balanced)."""
    for m in re.finditer(r'(?<![\w.])' + name + r'\(', text):
        i = j = m.end()
        depth, quote = 1, None
        while depth and j < len(text):
            c = text[j]
            if quote:
                if c == '\\':
                    j += 1
                elif c == quote:
                    quote = None
            elif c in '\'"`':
                quote = c
            elif c in '([{':
                depth += 1
            elif c in ')]}':
                depth -= 1
            j += 1
        yield m.start(), text[i:j - 1]


def missing():
    out = []
    for path in sorted(WEB.glob('*.ts')):
        if path.name == 'generated.ts':
            continue
        text = path.read_text(encoding='utf-8')
        for name in ('note', 'notify'):
            for pos, args in calls(text, name):
                if "'error'" not in args:
                    continue
                # A name bound inside the call (`errors.map(e=>e.message)`) is a list item, not a caught error.
                bound = set(re.findall(r'\(?(\w+)(?::\s*\w+)?\)?\s*=>', args))
                for var in set(MESSAGE.findall(args)) - bound - NOT_ERRORS:
                    if not re.search(r",\s*'error'\s*(?:,[\s\S]*)?,\s*" + re.escape(var) + r'\s*$', args):
                        out.append(f'{path.name}:{text.count(chr(10), 0, pos) + 1} {name}(… {var}.message …) without passing {var}')
    return out


def test_every_error_notice_from_a_caught_error_passes_the_error():
    assert missing() == []


if __name__ == '__main__':
    print('\n'.join(missing()))
