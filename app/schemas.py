from __future__ import annotations

from decimal import Decimal
from typing import Literal
from urllib.parse import urlparse

from pydantic import BaseModel, Field, field_validator

from .model_profiles import MAX_DURATION_SECONDS, PROFILE_DEFINITIONS


class RouteInput(BaseModel):
    model: str = Field(min_length=1, max_length=160)
    upstream_model: str = Field(default="", max_length=160)
    # 请求协议是运输层的封闭集合：每加一个都要在 proxy.py 里接一条 endpoint
    # 与转换分支，所以这里保留枚举。
    protocol: Literal[
        "videos", "seedance", "ark-v3", "o10-grok", "sub2api-video", "mai-token", "funai",
        "autodl-comfyui", "rolldek"
    ] = "videos"
    # 请求格式必须是渠道适配器真的声明过的 profile。这里刻意不写死清单：
    # 渠道新增 profile 时只要在它自己的 ``PROFILE_DEFINITIONS`` 里登记，
    # 后台保存与模型发现立刻可用，不会出现「下拉里有、保存 422」的错位。
    profile: str = "default"
    durations: list[int] = Field(default_factory=list, max_length=MAX_DURATION_SECONDS)
    resolutions: list[str] = Field(default_factory=list, max_length=20)
    # 三个数量是该模型能接受的参考媒体上限，也是唯一事实来源：留空或 0 都表示
    # 不支持这类参考媒体，代理会直接拒绝而不是静默丢弃。下面的布尔字段只是旧
    # 前端（勾选式能力）的兼容入口，仅在数量没提交时作为回退值使用。
    image_count: int | None = Field(default=None, ge=0, le=50)
    video_count: int | None = Field(default=None, ge=0, le=50)
    audio_count: int | None = Field(default=None, ge=0, le=50)
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

    @field_validator("profile")
    @classmethod
    def validate_profile(cls, value: str) -> str:
        if value not in PROFILE_DEFINITIONS:
            raise ValueError(f"unknown request format: {value}")
        return value

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
    cost_per_request: Decimal = Field(default=Decimal("0"), ge=0, le=100000)

    @field_validator("public_model", "upstream_model")
    @classmethod
    def normalize_image_model(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("image model names cannot be empty")
        return normalized

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
    api_format: Literal["openai", "gemini"] = "openai"
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
