"""Disposable end-to-end server: the real application with a fake Keitaro.

Used only by the Playwright suite (``frontend/e2e``). It serves the built
frontend (``frontend/dist``) and the application API on one origin, backed
by the in-memory Keitaro fake from ``tests.fakes`` — no real Keitaro
instance, credentials, or network access is involved.

Extra ``/e2e/*`` control endpoints let the suite reset all state and
simulate external remote changes (drift) between the fingerprint preflight
and upload. They exist only in this harness, never in the deployed
application.

Run from anywhere:

    backend/.venv/bin/python backend/scripts/e2e_server.py --port 8787
"""

from __future__ import annotations

import argparse
import sys
import tempfile
import uuid
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[1]
FRONTEND_DIST = BACKEND_ROOT.parent / "frontend" / "dist"
sys.path.insert(0, str(BACKEND_ROOT))

import uvicorn  # noqa: E402
from alembic.config import Config  # noqa: E402
from fastapi import Request  # noqa: E402
from fastapi.staticfiles import StaticFiles  # noqa: E402

from alembic import command as alembic_command  # noqa: E402
from app.config import Settings  # noqa: E402
from app.db.engine import session_scope  # noqa: E402
from app.db.models import Base, SyncState  # noqa: E402
from app.keitaro.models import KeitaroOffer  # noqa: E402
from app.main import create_app  # noqa: E402
from app.repositories.campaigns import CampaignRepository  # noqa: E402
from tests.fakes import FakeKeitaroAdapter  # noqa: E402

# Deterministic catalog shared with the Playwright specs.
E2E_OFFERS = [
    (18001, "Alpha Diet Offer"),
    (18002, "Beta Finance Offer"),
    (18003, "Gamma Sweep Offer"),
    (18004, "Delta Nutra Offer"),
]
IMPORT_KEITARO_ID = 4242
IMPORT_STREAM_IDS = (9101, 9102, 9103)


class E2EKeitaroAdapter(FakeKeitaroAdapter):
    """Fake Keitaro seeded with the E2E offers and one importable campaign."""

    def reset(self) -> None:
        self.__init__()
        for offer_id, name in E2E_OFFERS:
            self.offers[offer_id] = KeitaroOffer(id=offer_id, name=name, state="active")
        campaign = self.seed_campaign(keitaro_id=IMPORT_KEITARO_ID, name="Imported Remote Campaign")
        google_id, offer_id, disabled_id = IMPORT_STREAM_IDS
        self.seed_stream(
            campaign_id=campaign.id,
            keitaro_id=google_id,
            name="Google — DE",
            schema="redirect",
            action_type="redirect",
            action_payload="https://www.google.com/",
            position=0,
            weight=50,
            filters=[{"name": "country", "mode": "accept", "payload": ["DE"]}],
        )
        self.seed_stream(
            campaign_id=campaign.id,
            keitaro_id=offer_id,
            name="Offer — 18001",
            schema="landings",
            action_type="http",
            position=1,
            weight=50,
            filters=[{"name": "country", "mode": "reject", "payload": ["DE"]}],
            offers=[{"offer_id": 18001, "state": "active", "share": 100}],
        )
        self.seed_stream(
            campaign_id=campaign.id,
            keitaro_id=disabled_id,
            name="Legacy Stream",
            schema="landings",
            action_type="http",
            state="disabled",
            position=2,
            weight=0,
            offers=[{"offer_id": 18002, "state": "disabled", "share": 0}],
        )


def build_e2e_app(database_url: str):
    settings = Settings(
        keitaro_base_url="https://keitaro-e2e.invalid/admin_api/v1",
        keitaro_api_key="e2e-dummy-key",  # noqa: S106 - fake, never leaves the harness
        keitaro_domain_id=1813,
        keitaro_group_id=10332,
        keitaro_traffic_source_id=836,
        google_redirect_url="https://www.google.com/",
        database_url=database_url,
    )
    keitaro = E2EKeitaroAdapter()
    keitaro.reset()
    app = create_app(settings, keitaro=keitaro)

    @app.post("/e2e/reset")
    def e2e_reset() -> dict[str, str]:
        keitaro.reset()
        with session_scope(app.state.session_factory) as session:
            for table in reversed(Base.metadata.sorted_tables):
                session.execute(table.delete())
        return {"status": "reset"}

    @app.post("/e2e/campaigns/{campaign_id}/syncing")
    def e2e_mark_syncing(campaign_id: uuid.UUID) -> dict[str, str]:
        with session_scope(app.state.session_factory) as session:
            CampaignRepository().transition_sync_state(
                session, campaign_id=campaign_id, to_state=SyncState.SYNCING
            )
        return {"status": "syncing"}

    @app.post("/e2e/remote/drift")
    async def e2e_remote_drift(request: Request) -> dict[str, object]:
        """Simulate an external remote edit: bump a stream weight like another
        operator would, changing the remote fingerprint."""
        payload = await request.json()
        campaign_id = int(payload["keitaro_campaign_id"])
        stream = next(s for s in keitaro.streams.values() if s.campaign_id == campaign_id)
        updated = keitaro.touch_stream(stream.id, weight=(int(stream.weight) + 1) % 100)
        return {"status": "drifted", "stream_id": updated.id, "weight": updated.weight}

    @app.get("/e2e/remote/state")
    def e2e_remote_state(keitaro_campaign_id: int) -> dict[str, object]:
        """Inspect fake remote state so specs can assert upload outcomes."""
        streams = [
            stream.model_dump(mode="json")
            for stream in keitaro.streams.values()
            if stream.campaign_id == keitaro_campaign_id
        ]
        return {"streams": streams}

    if not FRONTEND_DIST.is_dir():
        raise RuntimeError(
            f"Frontend build not found at {FRONTEND_DIST}. Run `npm run build` first."
        )
    app.mount("/", StaticFiles(directory=FRONTEND_DIST, html=True), name="frontend")
    return app


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8787)
    args = parser.parse_args()

    database_path = Path(tempfile.mkdtemp(prefix="kcm-e2e-")) / "e2e.db"
    database_url = f"sqlite:///{database_path}"
    alembic_config = Config(str(BACKEND_ROOT / "alembic.ini"))
    alembic_config.set_main_option("script_location", str(BACKEND_ROOT / "alembic"))
    alembic_config.set_main_option("sqlalchemy.url", database_url)
    alembic_command.upgrade(alembic_config, "head")

    app = build_e2e_app(database_url)
    uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
