"""Check a Wangyue pool key against the live gateway without creating anything.

Sends one signed ``GET /v1/pool/api/tasks/{random id}``. The task does not
exist, so nothing is created or billed; the answer only says whether the
signature was accepted:

* 404 (or 200)      -> key and signature are good
* 401 / 403 / 409   -> rejected; the gateway's error code is printed

The key is read from ``WANGYUE_KEY`` (``keyId:secret``) or prompted for, so it
never lands in shell history.

    python tools/wangyue_check.py [base_url]
"""
from __future__ import annotations

import getpass
import os
import sys
import uuid

import httpx

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.channels.wangyue import signing, tasks  # noqa: E402

DEFAULT_BASE_URL = "https://1456719883-78ndn27wdv.ap-guangzhou.tencentscf.com"


def main() -> int:
    base_url = (sys.argv[1] if len(sys.argv) > 1 else DEFAULT_BASE_URL).rstrip("/")
    api_key = os.environ.get("WANGYUE_KEY") or getpass.getpass("keyId:secret: ")
    try:
        signing.parse_credentials(api_key)
    except signing.WangyueCredentialError as error:
        print(error)
        return 2
    url = base_url + tasks.task_path(str(uuid.uuid4()))
    response = httpx.get(url, headers=signing.signed_headers(api_key, "GET", url), timeout=30)
    try:
        body = response.json()
    except ValueError:
        body = {"message": response.text[:300]}
    code = body.get("error") if isinstance(body, dict) else None
    message = body.get("message") if isinstance(body, dict) else None
    print(f"HTTP {response.status_code} {code or ''} {message or ''}".strip())
    if response.status_code in (200, 404):
        print("签名通过：Key 可用")
        return 0
    print("签名未通过：检查 keyId:secret、服务器时间（允许偏差 300 秒）")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
