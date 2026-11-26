#!/usr/bin/env python3
"""Stage 0 read-only Keitaro contract verification.

Calls only safe GET endpoints on the target Keitaro instance and prints a
structural summary (field names, types, enum values) needed to finalize the
adapter payload contracts. It never prints credentials and redacts the API
key from any output.

Usage (from backend/):
    .venv/bin/python scripts/verify_keitaro_readonly.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import httpx
from pydantic_settings import BaseSettings, SettingsConfigDict

EXPECTED_ENV_NAMES = [
    "KEITARO_BASE_URL",
    "KEITARO_API_KEY",
    "KEITARO_DOMAIN_ID",
    "KEITARO_GROUP_ID",
    "KEITARO_TRAFFIC_SOURCE_ID",
    "GOOGLE_REDIRECT_URL",
    "DATABASE_URL",
]

# Same single dotenv file the application reads (app.config.ENV_FILE),
# anchored so the launch directory does not matter.
ENV_FILE = Path(__file__).resolve().parents[1] / ".env"


class VerifySettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=ENV_FILE, extra="ignore")

    keitaro_base_url: str = "https://keitaro-for-tests.etolegko.com/admin_api/v1"
    keitaro_api_key: str = ""
    keitaro_domain_id: int | None = None
    keitaro_group_id: int | None = None
    keitaro_traffic_source_id: int | None = None


def shape(value: Any, depth: int = 0) -> Any:
    """Reduce a JSON value to its structure: types and key names only."""
    if depth > 4:
        return "..."
    if isinstance(value, dict):
        return {k: shape(v, depth + 1) for k, v in value.items()}
    if isinstance(value, list):
        if not value:
            return "list[empty]"
        return [f"list[{len(value)}]", shape(value[0], depth + 1)]
    if isinstance(value, str):
        return f"str({len(value)})"
    return type(value).__name__


class Redactor:
    def __init__(self, *secrets: str) -> None:
        self._secrets = [s for s in secrets if s and len(s) >= 6]

    def text(self, value: Any) -> str:
        out = value if isinstance(value, str) else json.dumps(value, indent=1, default=str)
        for secret in self._secrets:
            out = out.replace(secret, "***")
        return out


def main() -> int:
    settings = VerifySettings()
    if not settings.keitaro_api_key:
        print("KEITARO_API_KEY is not set; cannot verify. Fill it in the local .env.")
        return 1

    redact = Redactor(settings.keitaro_api_key)
    base_url = settings.keitaro_base_url.rstrip("/")
    print(f"Target: {base_url}")

    client = httpx.Client(
        base_url=base_url,
        headers={"Api-Key": settings.keitaro_api_key},
        timeout=15.0,
    )

    def get(path: str, **params: Any) -> Any:
        resp = client.get(path, params=params or None)
        print(f"\n### GET {path} -> {resp.status_code}")
        if resp.status_code != 200:
            print(redact.text(resp.text[:300]))
            return None
        try:
            return resp.json()
        except ValueError:
            print("!! non-JSON response")
            return None

    actions = get("/streams_actions")
    if isinstance(actions, list):
        print(redact.text([a.get("key") for a in actions if isinstance(a, dict)]))
        print(redact.text(shape(actions[0]) if actions else "empty"))

    schemas = get("/stream_schemas")
    if isinstance(schemas, list):
        print(redact.text([s.get("key") for s in schemas if isinstance(s, dict)]))

    filters = get("/stream_filters")
    if isinstance(filters, list):
        names = [f.get("value") for f in filters if isinstance(f, dict)]
        print(f"filter names ({len(names)}): {redact.text(names)}")
        country = next(
            (f for f in filters if isinstance(f, dict) and f.get("value") == "country"), None
        )
        if country:
            print("country filter entry:", redact.text(shape(country)))

    for path, params in [
        ("/domains", {}),
        ("/groups", {"type": "campaigns"}),
        ("/traffic_sources", {}),
        ("/offers", {}),
    ]:
        data = get(path, **params)
        if isinstance(data, list):
            print(f"count={len(data)}")
            if data:
                print("item shape:", redact.text(shape(data[0])))

    campaigns = get("/campaigns", limit=5)
    streams_seen = False
    if isinstance(campaigns, list):
        print(f"count(returned)={len(campaigns)}")
        if campaigns:
            print("campaign shape:", redact.text(shape(campaigns[0])))
            first_id = campaigns[0].get("id")
            streams = get(f"/campaigns/{first_id}/streams")
            if isinstance(streams, list) and streams:
                streams_seen = True
                for stream in streams:
                    summary = {
                        k: stream.get(k)
                        for k in (
                            "id",
                            "schema",
                            "type",
                            "state",
                            "position",
                            "weight",
                            "action_type",
                        )
                    }
                    summary["action_payload"] = shape(stream.get("action_payload"))
                    summary["action_options"] = shape(stream.get("action_options"))
                    summary["keys"] = sorted(stream.keys())
                    summary["filters"] = [
                        {"name": f.get("name"), "mode": f.get("mode"), "payload": f.get("payload")}
                        for f in stream.get("filters", [])
                        if isinstance(f, dict)
                    ]
                    summary["offers"] = [
                        {k: o.get(k) for k in ("id", "offer_id", "state", "share")}
                        for o in stream.get("offers", [])
                        if isinstance(o, dict)
                    ]
                    print(redact.text(summary))
    if not streams_seen:
        print("\n(no existing streams found to inspect redirect representation)")

    for label, value, path_tpl in [
        ("domain", settings.keitaro_domain_id, "/domains/{id}"),
        ("group", settings.keitaro_group_id, None),
        ("traffic_source", settings.keitaro_traffic_source_id, "/traffic_sources/{id}"),
    ]:
        if value is None:
            print(f"\n(configured {label} id not set in .env; skipped)")
            continue
        if path_tpl is None:
            groups = get("/groups", type="campaigns")
            found = (
                [g for g in groups if isinstance(g, dict) and g.get("id") == value]
                if isinstance(groups, list)
                else []
            )
            print(f"configured {label} id={value}: {'FOUND' if found else 'NOT FOUND'}")
        else:
            data = get(path_tpl.format(id=value))
            if isinstance(data, dict):
                print(
                    f"configured {label} id={value}: "
                    f"state={data.get('state')!r} keys={sorted(data.keys())}"
                )

    missing = [name for name in EXPECTED_ENV_NAMES if name not in VerifySettings.model_fields]
    print("\n### .env variable presence (names only)")
    import os

    from dotenv import dotenv_values

    file_values = dotenv_values(ENV_FILE)
    for name in EXPECTED_ENV_NAMES:
        present = name in os.environ or name in file_values
        print(f"  {name}: {'set' if present else 'MISSING'}")
    _ = missing

    client.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
