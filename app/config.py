from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parent.parent
PUBLIC_LINK_BASE_URLS = (
    "https://media.yyapi.cloud",
    "https://www.yyapi.cloud",
    "https://zl.yyapi.cloud",
)
DEFAULT_PUBLIC_LINK_BASE_URL = "https://zl.yyapi.cloud"


def load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if key and key not in os.environ:
            os.environ[key] = value.strip().strip('"').strip("'")


load_dotenv(ROOT_DIR / ".env")


def env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    app_version: str
    admin_username: str
    admin_password: str
    session_secret: str
    adapter_api_key: str
    encryption_key: str
    host: str
    port: int
    api_public_base_url: str
    public_base_url: str
    new_api_public_base_url: str
    workbench_origin: str
    cookie_secure: bool
    session_ttl_seconds: int
    upstream_timeout_seconds: float
    image_upstream_timeout_seconds: float
    data_dir: Path


def load_settings() -> Settings:
    required = {
        "ADMIN_USERNAME": os.getenv("ADMIN_USERNAME", ""),
        "ADMIN_PASSWORD": os.getenv("ADMIN_PASSWORD", ""),
        "SESSION_SECRET": os.getenv("SESSION_SECRET", ""),
        "ADAPTER_API_KEY": os.getenv("ADAPTER_API_KEY", ""),
        "ENCRYPTION_KEY": os.getenv("ENCRYPTION_KEY", ""),
    }
    missing = [name for name, value in required.items() if not value]
    if missing:
        raise RuntimeError(f"Missing required environment variables: {', '.join(missing)}")
    if len(required["SESSION_SECRET"]) < 32:
        raise RuntimeError("SESSION_SECRET must contain at least 32 characters")

    try:
        session_ttl_days = int(os.getenv("SESSION_TTL_DAYS", "30"))
    except ValueError as exc:
        raise RuntimeError("SESSION_TTL_DAYS must be an integer") from exc
    if not 1 <= session_ttl_days <= 365:
        raise RuntimeError("SESSION_TTL_DAYS must be between 1 and 365")

    data_dir = Path(os.getenv("DATA_DIR", "./data"))
    if not data_dir.is_absolute():
        data_dir = ROOT_DIR / data_dir

    return Settings(
        app_version=os.getenv("APP_VERSION", "dev"),
        admin_username=required["ADMIN_USERNAME"],
        admin_password=required["ADMIN_PASSWORD"],
        session_secret=required["SESSION_SECRET"],
        adapter_api_key=required["ADAPTER_API_KEY"],
        encryption_key=required["ENCRYPTION_KEY"],
        host=os.getenv("HOST", "0.0.0.0"),
        port=int(os.getenv("PORT", "8787")),
        api_public_base_url=os.getenv("API_PUBLIC_BASE_URL", DEFAULT_PUBLIC_LINK_BASE_URL).rstrip("/"),
        public_base_url=os.getenv("PUBLIC_BASE_URL", "http://127.0.0.1:8787").rstrip("/"),
        new_api_public_base_url=os.getenv(
            "NEW_API_PUBLIC_BASE_URL",
            DEFAULT_PUBLIC_LINK_BASE_URL,
        ).rstrip("/"),
        workbench_origin=os.getenv("WORKBENCH_ORIGIN", "https://image.yyapi.cloud").rstrip("/"),
        cookie_secure=env_bool("COOKIE_SECURE"),
        session_ttl_seconds=session_ttl_days * 24 * 60 * 60,
        upstream_timeout_seconds=float(os.getenv("UPSTREAM_TIMEOUT_SECONDS", "60")),
        image_upstream_timeout_seconds=float(os.getenv("IMAGE_UPSTREAM_TIMEOUT_SECONDS", "360")),
        data_dir=data_dir,
    )


settings = load_settings()
