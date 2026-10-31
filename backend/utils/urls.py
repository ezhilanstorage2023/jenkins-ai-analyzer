import posixpath
from urllib.parse import urlsplit


def url_under_base(url: str, base: str) -> bool:
    """True when url points inside base: same scheme and host, path below.

    Compares parsed components rather than string prefixes so hosts like
    "ci.example.com.evil.net" or "ci.example.com@evil" never pass, and
    "/jenkins/../x" cannot escape a "/jenkins/" base.
    """

    try:
        target = urlsplit(url)
        root = urlsplit(base)
    except ValueError:
        return False

    if target.scheme.lower() not in ("http", "https"):
        return False

    if (target.scheme.lower(), target.netloc.lower()) != (
        root.scheme.lower(),
        root.netloc.lower(),
    ):
        return False

    root_path = root.path.rstrip("/") + "/"
    target_path = posixpath.normpath("/" + target.path.lstrip("/"))

    return (target_path.rstrip("/") + "/").startswith(root_path)
