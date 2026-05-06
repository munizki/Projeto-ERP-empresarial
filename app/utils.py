import ipaddress
import os
import re
from datetime import datetime, time, timedelta, timezone
from typing import Any
from urllib.parse import unquote
from zoneinfo import ZoneInfo


APP_TIMEZONE = os.getenv("APP_TIMEZONE", "America/Sao_Paulo")
try:
    LOCAL_TIMEZONE = ZoneInfo(APP_TIMEZONE)
except Exception:
    LOCAL_TIMEZONE = timezone(timedelta(hours=-3), name="America/Sao_Paulo")


def utc_now() -> datetime:
    """Return a naive UTC datetime to match the legacy PostgreSQL columns."""
    return datetime.now(timezone.utc).replace(microsecond=0, tzinfo=None)


def ensure_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def to_localtime(value: datetime | None) -> datetime | None:
    value_utc = ensure_utc(value)
    if value_utc is None:
        return None
    return value_utc.astimezone(LOCAL_TIMEZONE)


def format_datetime(value: datetime | None, with_seconds: bool = False) -> str:
    local_value = to_localtime(value)
    if local_value is None:
        return "-"
    fmt = "%d/%m/%Y %H:%M:%S" if with_seconds else "%d/%m/%Y %H:%M"
    return local_value.strftime(fmt)


def parse_date_start(value: str | None) -> datetime | None:
    if not value:
        return None
    local_start = datetime.combine(datetime.strptime(value, "%Y-%m-%d").date(), time.min, LOCAL_TIMEZONE)
    return local_start.astimezone(timezone.utc).replace(tzinfo=None)


def parse_date_end(value: str | None) -> datetime | None:
    if not value:
        return None
    local_end = datetime.combine(datetime.strptime(value, "%Y-%m-%d").date() + timedelta(days=1), time.min, LOCAL_TIMEZONE)
    return local_end.astimezone(timezone.utc).replace(tzinfo=None)


def normalize_text(value: str | None, *, upper: bool = False) -> str:
    cleaned = " ".join((value or "").strip().split())
    return cleaned.upper() if upper else cleaned


def normalize_digits(value: str | None) -> str:
    return "".join(ch for ch in (value or "") if ch.isdigit())


def parse_bool_form(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    return text in {"1", "true", "on", "yes", "sim"}


def safe_jsonable(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, dict):
        return {str(key): safe_jsonable(val) for key, val in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [safe_jsonable(item) for item in value]
    if isinstance(value, datetime):
        return format_datetime(value, with_seconds=True)
    return value


INFRA_PROXY_NETWORKS = tuple(
    ipaddress.ip_network(network)
    for network in os.getenv(
        "AUDIT_INFRA_PROXY_CIDRS",
        "127.0.0.0/8,::1/128,169.254.0.0/16,172.16.0.0/12,192.168.65.0/24",
    ).split(",")
    if network.strip()
)


def _clean_ip_candidate(value: str | None) -> str | None:
    text = unquote(str(value or "").strip().strip('"').strip("'"))
    if not text or text.lower() in {"unknown", "localhost", "none", "null", "-"}:
        return None
    if text.startswith("_"):
        return None

    if text.startswith("[") and "]" in text:
        text = text[1:text.index("]")]
    elif text.count(":") == 1 and text.rsplit(":", 1)[1].isdigit():
        text = text.rsplit(":", 1)[0]

    if "%" in text:
        text = text.split("%", 1)[0]

    try:
        return str(ipaddress.ip_address(text))
    except ValueError:
        return None


def _forwarded_for_candidates(value: str | None) -> list[str]:
    candidates: list[str] = []
    for entry in str(value or "").split(","):
        for part in entry.split(";"):
            key, _, raw_value = part.partition("=")
            if key.strip().lower() == "for":
                cleaned = _clean_ip_candidate(raw_value)
                if cleaned:
                    candidates.append(cleaned)
                break
    return candidates


def _header_ip_candidates(value: str | None) -> list[str]:
    candidates: list[str] = []
    for piece in str(value or "").split(","):
        cleaned = _clean_ip_candidate(piece)
        if cleaned:
            candidates.append(cleaned)
    return candidates


def _is_infra_proxy_ip(value: str) -> bool:
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        return False
    return any(address in network for network in INFRA_PROXY_NETWORKS)


def _ip_quality(value: str) -> int:
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        return 100
    if address.is_unspecified:
        return 90
    if address.is_loopback:
        return 80
    if address.is_link_local:
        return 70
    if _is_infra_proxy_ip(value):
        return 20
    return 0


def choose_client_ip(candidates: list[str | None]) -> str | None:
    cleaned: list[str] = []
    for candidate in candidates:
        ip = _clean_ip_candidate(candidate)
        if ip and ip not in cleaned:
            cleaned.append(ip)
    if not cleaned:
        return None
    return sorted(enumerate(cleaned), key=lambda item: (_ip_quality(item[1]), item[0]))[0][1]


def get_request_ip(request) -> str | None:
    if not request:
        return None
    candidates: list[str | None] = []
    candidates.extend(_forwarded_for_candidates(request.headers.get("forwarded")))
    candidates.extend(_header_ip_candidates(request.headers.get("x-forwarded-for")))
    candidates.extend(_header_ip_candidates(request.headers.get("x-original-forwarded-for")))
    candidates.extend(_header_ip_candidates(request.headers.get("x-real-ip")))
    candidates.extend(_header_ip_candidates(request.headers.get("true-client-ip")))
    candidates.extend(_header_ip_candidates(request.headers.get("cf-connecting-ip")))
    if getattr(request, "client", None):
        candidates.append(request.client.host)
    return choose_client_ip(candidates)


def get_request_audit_meta(request) -> dict[str, str | None]:
    if not request:
        return {
            "ip_cliente": "unknown",
            "ip_conexao": "unknown",
            "x_forwarded_for": None,
            "x_real_ip": None,
            "user_agent": None,
        }

    forwarded_header = (request.headers.get("forwarded") or "").strip() or None
    forwarded_for = (request.headers.get("x-forwarded-for") or "").strip() or None
    original_forwarded_for = (request.headers.get("x-original-forwarded-for") or "").strip() or None
    real_ip = (request.headers.get("x-real-ip") or "").strip() or None
    user_agent = (request.headers.get("user-agent") or "").strip() or None
    connection_ip = request.client.host if getattr(request, "client", None) else "unknown"

    candidates: list[str | None] = []
    candidates.extend(_forwarded_for_candidates(forwarded_header))
    candidates.extend(_header_ip_candidates(forwarded_for))
    candidates.extend(_header_ip_candidates(original_forwarded_for))
    candidates.extend(_header_ip_candidates(real_ip))
    candidates.extend(_header_ip_candidates(request.headers.get("true-client-ip")))
    candidates.extend(_header_ip_candidates(request.headers.get("cf-connecting-ip")))
    candidates.append(connection_ip)

    interpreted = choose_client_ip(candidates) or "unknown"

    return {
        "ip_cliente": interpreted,
        "ip_conexao": connection_ip or "unknown",
        "x_forwarded_for": forwarded_for,
        "x_real_ip": real_ip,
        "user_agent": user_agent,
    }


def summarize_user_agent(user_agent: str | None) -> str:
    ua = str(user_agent or "").strip()
    if not ua:
        return "Nao informado"

    browser_patterns = (
        ("Microsoft Edge", r"(?:Edg|Edge|EdgA|EdgiOS)/([0-9.]+)"),
        ("Opera", r"(?:OPR|Opera)/([0-9.]+)"),
        ("Firefox", r"Firefox/([0-9.]+)"),
        ("Chrome", r"(?:Chrome|CriOS)/([0-9.]+)"),
        ("Safari", r"Version/([0-9.]+).*Safari/"),
    )
    browser = "Navegador desconhecido"
    for name, pattern in browser_patterns:
        match = re.search(pattern, ua)
        if match:
            major = match.group(1).split(".", 1)[0]
            browser = f"{name} {major}" if major else name
            break

    platform = "sistema nao identificado"
    if "Windows NT 10.0" in ua:
        platform = "Windows 10/11"
    elif "Windows NT 6.3" in ua:
        platform = "Windows 8.1"
    elif "Windows NT 6.1" in ua:
        platform = "Windows 7"
    elif "Android" in ua:
        match = re.search(r"Android\s+([0-9.]+)", ua)
        platform = f"Android {match.group(1)}" if match else "Android"
    elif "iPhone" in ua or "iPad" in ua:
        match = re.search(r"OS\s+([0-9_]+)", ua)
        version = match.group(1).replace("_", ".") if match else ""
        platform = f"iOS {version}" if version else "iOS"
    elif "Mac OS X" in ua:
        match = re.search(r"Mac OS X\s+([0-9_]+)", ua)
        version = match.group(1).replace("_", ".") if match else ""
        platform = f"macOS {version}" if version else "macOS"
    elif "Linux" in ua:
        platform = "Linux"

    return f"{browser} no {platform}"
