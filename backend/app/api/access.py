"""Origin and content-type checks for this same-origin JSON API."""

from urllib.parse import urlsplit

from fastapi import HTTPException, Request


COOKIE_NAME = "apexmedic_session"
DEFAULT_ORIGIN = "http://127.0.0.1:8000"


def validate_public_origin(value: str) -> str:
    parsed = urlsplit(value)
    if (
        parsed.scheme not in ("http", "https")
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in ("", "/")
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("APEX_PUBLIC_ORIGIN must be one HTTP(S) origin.")

    host = parsed.hostname.lower()
    if parsed.scheme == "http" and host not in ("localhost", "127.0.0.1", "::1"):
        raise ValueError("Public origins must use HTTPS; HTTP is for loopback only.")

    port = parsed.port
    authority = f"[{host}]" if ":" in host else host
    default_port = 443 if parsed.scheme == "https" else 80
    if port is not None and port != default_port:
        authority += f":{port}"
    return f"{parsed.scheme}://{authority}"


def mutation_guard(public_origin: str):
    """Browser writes need an exact origin and non-simple JSON content type.

    Non-browser clients may omit Origin, but must still send JSON and possess
    the cookie for protected cases. Cross-origin CORS is intentionally absent.
    """
    def check(request: Request) -> None:
        if request.method in ("GET", "HEAD", "OPTIONS"):
            return

        origins = request.headers.getlist("origin")
        if origins and (len(origins) != 1 or origins[0] != public_origin):
            raise HTTPException(403, "Request origin is not allowed.")

        if request.headers.get("sec-fetch-site") == "cross-site":
            raise HTTPException(403, "Cross-site writes are not allowed.")

        content_types = request.headers.getlist("content-type")
        if (
            len(content_types) != 1
            or content_types[0].split(";", 1)[0].strip().lower() != "application/json"
        ):
            raise HTTPException(415, "Send application/json.")

    return check
