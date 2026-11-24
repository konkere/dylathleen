import re
from urllib.parse import urlsplit

FORGES = {'github.com': 'github', 'gitlab.com': 'gitlab', 'codeberg.org': 'codeberg'}


def repository_publisher(sources, fallback=None):
    """Prefer a repository owner, then the first usable source domain."""
    platform = None
    for source in sources:
        if not source:
            continue
        try:
            url = urlsplit(source if '://' in source else f'https://{source}')
            host = url.hostname or ''
        except ValueError:
            continue
        if host in FORGES:
            parts = url.path.strip('/').split('/')
            if len(parts) >= 2 and re.fullmatch(r'[A-Za-z0-9_.-]+', parts[0]) and parts[0] not in ('.', '..'):
                return parts[0]
            platform = platform or FORGES[host]
        elif re.fullmatch(r'[a-z0-9-]+(?:\.[a-z0-9-]+)+', host):
            platform = platform or host
    return platform or fallback
