"""Adapter for the Wangyue pool (望月号池) third-party video API.

Contract (partner guide ``DOUBAO_POOL_THIRD_PARTY_API_GUIDE``, API v1):

* every ``/v1/pool/api/...`` call is HMAC-SHA256 signed per request
  (:mod:`.signing`); the upstream key is stored as ``keyId:secret``
* create ``POST /v1/pool/api/tasks`` with a caller-chosen ``taskId`` and an
  ``X-Idempotency-Key`` (same key + same body returns the original task)
* reference images are uploaded as private assets first (:mod:`.assets`)
* poll ``GET /v1/pool/api/tasks/{id}``
* download: ``GET /v1/pool/api/tasks/{id}/result`` returns a short-lived
  signed URL, fetched afresh for every download
* models come from the public ``GET /v1/pool/api/models``

Everything outside this package imports from here only.
"""

from __future__ import annotations

from .assets import WangyueAssetError, upload_images
from .catalog import (
    MODELS_PATH,
    PROFILE,
    PROFILE_DEFINITIONS,
    PROTOCOL,
    discovered_models,
    family_for_model,
    is_wangyue_base_url,
    suggest_route,
)
from .payload import (
    WangyueRequestError,
    attach_assets,
    normalize_idempotency_key,
    task_id_for,
    transform_create_payload,
)
from .signing import WangyueCredentialError, encode_body, parse_credentials, signed_headers
from .tasks import (
    CREATE_PATH,
    extract_create_status,
    extract_create_task_id,
    extract_download,
    extract_task_fields,
    is_credential_rejection,
    normalize_status,
    result_path,
    task_path,
)

__all__ = [
    "CREATE_PATH",
    "MODELS_PATH",
    "PROFILE",
    "PROFILE_DEFINITIONS",
    "PROTOCOL",
    "WangyueAssetError",
    "WangyueCredentialError",
    "WangyueRequestError",
    "attach_assets",
    "discovered_models",
    "encode_body",
    "extract_create_status",
    "extract_create_task_id",
    "extract_download",
    "extract_task_fields",
    "family_for_model",
    "is_credential_rejection",
    "is_wangyue_base_url",
    "normalize_idempotency_key",
    "normalize_status",
    "parse_credentials",
    "result_path",
    "signed_headers",
    "suggest_route",
    "task_id_for",
    "transform_create_payload",
    "upload_images",
]
