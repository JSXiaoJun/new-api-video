from __future__ import annotations

from typing import Literal
from urllib.parse import urlparse

from pydantic import BaseModel, Field, field_validator


class RouteInput(BaseModel):
    model: str = Field(min_length=1, max_length=160)
    protocol: Literal["videos", "seedance"] = "videos"

    @field_validator("model")
    @classmethod
    def normalize_model(cls, value: str) -> str:
        return value.strip()


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
        return routes


class LoginInput(BaseModel):
    username: str = Field(max_length=100)
    password: str = Field(max_length=500)

