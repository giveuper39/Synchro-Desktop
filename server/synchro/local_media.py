"""Path validation shared by desktop local-media adapters."""
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import url2pathname


def local_media_path(value: str) -> Path | None:
    """Accept existing files and file:// URIs; reject web and remote URIs."""
    if not isinstance(value, str) or not value or '\0' in value:
        return None
    try:
        parsed = urlsplit(value)
        if parsed.scheme.casefold() == 'file':
            if parsed.netloc not in {'', 'localhost'}:
                return None
            value = url2pathname(parsed.path)
        elif parsed.scheme and not (len(parsed.scheme) == 1 and value[1:3] in {':/', ':\\'}):
            return None
        path = Path(value).resolve()
        return path if path.is_file() else None
    except (OSError, ValueError):
        return None
