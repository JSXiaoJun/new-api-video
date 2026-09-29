"""Adapter for the Fuyao API (扶摇 API) OpenAI-compatible video gateway.

Documented contract (https://fuyao47.xyz/tutorial/api-media/#openai-video):

* create ``POST /v1/videos`` (JSON URL requests; multipart is only for local
  files, which this relay never forwards)
* poll ``GET /v1/videos/{id}``
* download ``GET /v1/videos/{id}/content``
* models come from ``GET /v1/models`` for the configured token

One gateway fronts several model families whose rules differ, so the package
is split by responsibility:

* :mod:`.catalog` -- host detection, model family recognition, profiles and
  route suggestions
* :mod:`.payload` -- the per-family request body
* :mod:`.tasks` -- task paths, create/poll response parsing

Everything outside this package imports from here only.
"""

from __future__ import annotations

from .catalog import (
    DEFAULT_PROFILE,
    PROFILE,
    PROFILE_DEFINITIONS,
    PROTOCOL,
    family_for_model,
    is_fuyao_base_url,
    is_video_model,
    suggest_route,
)
from .payload import FuyaoRequestError, transform_create_payload
from .tasks import (
    CREATE_PATH,
    content_path,
    extract_create_task_id,
    extract_task_fields,
    normalize_status,
    task_path,
)

__all__ = [
    "CREATE_PATH",
    "DEFAULT_PROFILE",
    "FuyaoRequestError",
    "PROFILE",
    "PROFILE_DEFINITIONS",
    "PROTOCOL",
    "content_path",
    "extract_create_task_id",
    "extract_task_fields",
    "family_for_model",
    "is_fuyao_base_url",
    "is_video_model",
    "normalize_status",
    "suggest_route",
    "task_path",
    "transform_create_payload",
]
