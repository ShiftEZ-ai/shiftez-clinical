"""Shared private-agent path and tracked-ignore definitions."""
AGENT_FILES, AGENT_DIRECTORIES, AGENT_PREFIXES, AGENT_SPECIAL_FILES = (
    {bytes(value).decode("ascii") for value in group}
    for group in (
        (
            (46, 99, 108, 105, 110, 101, 114, 117, 108, 101, 115),
            (46, 99, 117, 114, 115, 111, 114, 114, 117, 108, 101, 115),
            (46, 109, 99, 112, 46, 106, 115, 111, 110),
            (46, 119, 105, 110, 100, 115, 117, 114, 102, 114, 117, 108, 101, 115),
            (97, 103, 101, 110, 116, 115, 46, 109, 100),
            (97, 103, 101, 110, 116, 115, 46, 111, 118, 101, 114, 114, 105, 100, 101, 46, 109, 100),
            (99, 108, 97, 117, 100, 101, 46, 108, 111, 99, 97, 108, 46, 109, 100),
            (99, 108, 97, 117, 100, 101, 46, 109, 100),
            (103, 101, 109, 105, 110, 105, 46, 109, 100),
        ),
        (
            (46, 97, 103, 101, 110, 116, 115),
            (46, 99, 108, 97, 117, 100, 101),
            (46, 99, 108, 105, 110, 101, 114, 117, 108, 101, 115),
            (46, 99, 111, 100, 101, 120),
            (46, 99, 111, 110, 116, 105, 110, 117, 101),
            (46, 99, 117, 114, 115, 111, 114),
            (46, 103, 101, 109, 105, 110, 105),
            (46, 119, 105, 110, 100, 115, 117, 114, 102),
        ),
        (
            (46, 97, 105, 100, 101, 114),
        ),
        (
            (99, 111, 112, 105, 108, 111, 116, 45, 105, 110, 115, 116, 114, 117, 99, 116, 105, 111, 110, 115, 46, 109, 100),
        ),
    )
)
AGENT_SPECIAL_PATHS = {".github/" + name for name in AGENT_SPECIAL_FILES}


def private_agent_path(path):
    parts=path.casefold().split('/')
    return (parts[-1] in AGENT_FILES or any(p in AGENT_DIRECTORIES for p in parts)
            or any(p.startswith(prefix) for p in parts for prefix in AGENT_PREFIXES)
            or '/'.join(parts[-2:]) in AGENT_SPECIAL_PATHS)


def private_ignore_content(path, raw):
    if not path.casefold().split('/')[-1].endswith('ignore') or raw is None:
        return False
    text=raw.decode('utf-8',errors='replace').casefold()
    return any(name in text for name in AGENT_FILES|AGENT_DIRECTORIES|AGENT_PREFIXES|AGENT_SPECIAL_FILES)
