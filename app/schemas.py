from __future__ import annotations

from decimal import Decimal
from typing import Literal
from urllib.parse import urlparse

from pydantic import BaseModel, Field, field_validator

from .model_profiles import MAX_DURATION_SECONDS


class RouteInput(BaseModel):
    model: str = Field(min_length=1, max_length=160)
    upstream_model: str = Field(default="", max_length=160)
    protocol: Literal["videos", "seedance", "ark-v3", "o10-grok"] = "videos"
    profile: Literal[
        "default",
        "gemini-omni",
        "sora2",
        "veo31-fast",
        "manxue-900",
        "manxue-933",
        "ark-seedance-2",
        "grok-auto",
        "grok-fast",
        "pro666-video-v1",
        "pro666-video-900",
        "pro666-sd2-431",
        "pro666-sd2-5",
        "pro666-firefly-480p",
        "pro666-firefly-720p",
        "pro666-firefly-1080p",
        "pro666-veo-omni",
    ] = "default"
    durations: list[int] = Field(default_factory=list, max_length=MAX_DURATION_SECONDS)
    resolutions: list[str] = Field(default_factory=list, max_length=20)
    image_count: int | None = Field(default=None, ge=0, le=50)
    enabled: bool = True
    supports_image: bool = True
    supports_video: bool = True
    supports_audio: bool = True
    forward_resolution: bool = True
    duration_override: int | None = Field(default=None, ge=1, le=60)

    @field_validator("model", "upstream_model")
    @classmethod
    def normalize_model(cls, value: str) -> str:
        return value.strip()

    @field_validator("durations")
    @classmethod
    def validate_durations(cls, value: list[int]) -> list[int]:
        if any(duration < 1 or duration > MAX_DURATION_SECONDS for duration in value):
            raise ValueError(f"durations must be between 1 and {MAX_DURATION_SECONDS} seconds")
        return sorted(set(value))

    @field_validator("resolutions")
    @classmethod
    def normalize_resolutions(cls, value: list[str]) -> list[str]:
        normalized: list[str] = []
        for resolution in value:
            item = resolution.strip()
            if not item:
                continue
            if len(item) > 30:
                raise ValueError("each resolution must be at most 30 characters")
            if item not in normalized:
                normalized.append(item)
        return normalized


class UpstreamInput(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    base_url: str = Field(min_length=8, max_length=500)
    api_key: str = Field(default="", max_length=1000)
    enabled: bool = True
    priority: int = Field(default=100, ge=0, le=9999)
    routes: list[RouteInput] = Field(min_length=1, max_length=200)

    @field_validator("name")
    @classmethod
    def normalize_name(cls, value: str) -> str:
        return value.strip()

    @field_validator("base_url")
    @classmethod
    def validate_base_url(cls, value: str) -> str:
        normalized = value.strip().rstrip("/")
        parsed = urlparse(normalized)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.username or parsed.password:
            raise ValueError("base_url must be a valid HTTP or HTTPS URL without embedded credentials")
        return normalized

    @field_validator("routes")
    @classmethod
    def unique_models(cls, routes: list[RouteInput]) -> list[RouteInput]:
        models = [route.model for route in routes]
        if len(models) != len(set(models)):
            raise ValueError("model routes must be unique")
        upstream_models = [route.upstream_model or route.model for route in routes]
        if len(upstream_models) != len(set(upstream_models)):
            raise ValueError("upstream model mappings must be unique")
        return routes


class ModelDiscoveryInput(BaseModel):
    upstream_id: int | None = Field(default=None, ge=1)
    base_url: str = Field(min_length=8, max_length=500)
    api_key: str = Field(default="", max_length=1000)

    @field_validator("base_url")
    @classmethod
    def validate_base_url(cls, value: str) -> str:
        normalized = value.strip().rstrip("/")
        parsed = urlparse(normalized)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.username or parsed.password:
            raise ValueError("base_url must be a valid HTTP or HTTPS URL without embedded credentials")
        return normalized


class LoginInput(BaseModel):
    username: str = Field(max_length=100)
    password: str = Field(max_length=500)


class PublicTaskInput(BaseModel):
    public_task_id: str = Field(default="", max_length=191, pattern=r"^(|task_[A-Za-z0-9_-]+)$")

    @field_validator("public_task_id")
    @classmethod
    def normalize_public_task_id(cls, value: str) -> str:
        normalized = value.strip()
        if normalized and not normalized.startswith("task_"):
            raise ValueError("public_task_id must start with task_")
        return normalized


class PublicLinkSettingsInput(BaseModel):
    public_base_url: Literal[
        "https://media.yyapi.cloud",
        "https://www.yyapi.cloud",
        "https://zl.yyapi.cloud",
    ]


class PublicVideoDownloadSettingsInput(BaseModel):
    download_limit: int = Field(ge=1, le=10000)


class ImageRouteInput(BaseModel):
    public_model: str = Field(min_length=1, max_length=160)
    upstream_model: str = Field(min_length=1, max_length=160)
    sizes: list[str] = Field(default_factory=lambda: ["*"], max_length=50)
    qualities: list[str] = Field(default_factory=lambda: ["*"], max_length=50)
    operations: list[Literal["generation", "edit"]] = Field(
        default_factory=lambda: ["generation"], min_length=1, max_length=2
    )
    cost_per_request: Decimal = Field(default=Decimal("0"), ge=0, le=100000)

    @field_validator("public_model", "upstream_model")
    @classmethod
    def normalize_image_model(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("image model names cannot be empty")
        return normalized

    @field_validator("sizes", "qualities")
    @classmethod
    def normalize_constraints(cls, values: list[str]) -> list[str]:
        normalized = list(dict.fromkeys(value.strip().lower() for value in values if value.strip()))
        if any(len(value) > 64 for value in normalized):
            raise ValueError("image route constraints must not exceed 64 characters")
        return normalized or ["*"]

    @field_validator("operations")
    @classmethod
    def unique_operations(cls, values: list[str]) -> list[str]:
        return list(dict.fromkeys(values))

    @field_validator("cost_per_request")
    @classmethod
    def finite_cost(cls, value: Decimal) -> Decimal:
        if not value.is_finite():
            raise ValueError("cost_per_request must be finite")
        return value


class ImageUpstreamInput(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    base_url: str = Field(min_length=8, max_length=500)
    api_key: str = Field(default="", max_length=1000)
    enabled: bool = True
    priority: int = Field(default=100, ge=0, le=9999)
    routes: list[ImageRouteInput] = Field(min_length=1, max_length=200)

    @field_validator("name")
    @classmethod
    def normalize_image_upstream_name(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("image upstream name cannot be empty")
        return normalized

    @field_validator("api_key")
    @classmethod
    def normalize_image_api_key(cls, value: str) -> str:
        return value.strip()

    @field_validator("base_url")
    @classmethod
    def validate_image_base_url(cls, value: str) -> str:
        normalized = value.strip().rstrip("/")
        parsed = urlparse(normalized)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.netloc
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("base_url must be a valid HTTP or HTTPS URL without embedded credentials")
        return normalized
