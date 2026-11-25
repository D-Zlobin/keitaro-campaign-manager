"""Recorded Keitaro response fixtures (plan stage 0).

Shapes verified against the official OpenAPI reference and read-only
inspection of the target instance (keitaro-for-tests.etolegko.com) on
2026-11-25. Values are synthetic; field names and types mirror the target.

STREAM_REDIRECT_JSON is the documented redirect representation
(``action_payload`` as a URL string); the target version has no redirect
stream to observe read-only, so this remains the one payload mapping pending
write-verification on a disposable campaign.
"""

CAMPAIGN_JSON = {
    "id": 223,
    "alias": "x7k2p9ab",
    "name": "Example DE",
    "type": "weight",
    "uniqueness_method": "ip",
    "cookies_ttl": 24,
    "position": 1,
    "cost_type": "CPC",
    "cost_value": 0,
    "cost_currency": "USD",
    "bind_visitors": "",
    "token": "3f6f1c2e-0000-4000-8000-abcdefabcdef",
    "cost_auto": False,
    "notes": "",
    "parameters": {},
    "uniqueness_use_cookies": True,
    "traffic_loss": 0,
    "bypass_cache": False,
    "uniqueness_param": "",
    "state": "active",
    "group_id": 10332,
    "traffic_source_id": 836,
    "domain_id": 1813,
    "created_at": "2026-11-24 22:30:00",
    "updated_at": "2026-11-24 22:35:00",
    "domain": "kt-emulator.test",
    "postbacks": [],
    "group": "FORTESTS",
}

STREAM_LANDINGS_JSON = {
    "id": 425,
    "campaign_id": 223,
    "name": "Offer — 18001",
    "type": "regular",
    "schema": "landings",
    "state": "active",
    "position": 1,
    "weight": 50,
    "action_type": "http",
    "action_payload": None,
    "action_options": None,
    "collect_clicks": False,
    "filter_or": False,
    "comments": "",
    "filters": [
        {
            "id": 51,
            "stream_id": 425,
            "name": "country",
            "mode": "reject",
            "payload": ["DE"],
        }
    ],
    "triggers": [],
    "landings": [],
    "offers": [
        {
            "id": 693,
            "stream_id": 425,
            "offer_id": 18001,
            "state": "active",
            "share": 100,
            "created_at": "2026-11-24 22:30:47",
            "updated_at": None,
        }
    ],
    "offer_selection": "before_click",
    "created_at": "2026-11-24 22:30:47",
    "updated_at": None,
}

STREAM_REDIRECT_JSON = {
    "id": 424,
    "campaign_id": 223,
    "name": "Google — DE",
    "type": "regular",
    "schema": "redirect",
    "state": "active",
    "position": 0,
    "weight": 50,
    "action_type": "redirect",
    "action_payload": "https://www.google.com/",
    "action_options": None,
    "collect_clicks": False,
    "filter_or": False,
    "comments": "",
    "filters": [
        {
            "id": 50,
            "stream_id": 424,
            "name": "country",
            "mode": "accept",
            "payload": ["DE"],
        }
    ],
    "triggers": [],
    "landings": [],
    "offers": [],
    "offer_selection": "before_click",
    "created_at": "2026-11-24 22:30:40",
    "updated_at": None,
}

OFFER_JSON = {
    "id": 18001,
    "name": "Example Offer",
    "group_id": 900,
    "action_payload": "",
    "affiliate_network_id": 12,
    "payout_value": 1.5,
    "payout_currency": "USD",
    "payout_type": "CPA",
    "state": "active",
    "created_at": "2026-11-20 10:00:00",
    "updated_at": "2026-11-24 12:00:00",
    "payout_auto": False,
    "payout_upsell": False,
    "country": ["CL"],
    "notes": "",
    "action_options": {},
    "action_type": "redirect",
    "offer_type": "url",
    "values": "",
}

DOMAIN_JSON = {
    "id": 1813,
    "name": "kt-emulator.test",
    "is_ssl": True,
    "network_status": "active",
    "default_campaign_id": None,
    "state": "active",
    "created_at": "2026-11-20 10:00:00",
    "updated_at": "2026-11-20 10:00:00",
    "catch_not_found": False,
    "notes": "",
    "ssl_redirect": True,
    "allow_indexing": False,
    "group_id": None,
    "status": "active",
}

GROUP_JSON = {"id": 10332, "name": "FORTESTS", "position": 0, "type": "campaigns"}

TRAFFIC_SOURCE_JSON = {
    "id": 836,
    "name": "FORTESTS",
    "postback_url": "",
    "postback_statuses": [],
    "template_name": "",
    "accept_parameters": True,
    "parameters": {},
    "state": "active",
    "created_at": "2026-11-20 10:00:00",
    "updated_at": "2026-11-20 10:00:00",
    "notes": "",
    "traffic_loss": 0,
}
