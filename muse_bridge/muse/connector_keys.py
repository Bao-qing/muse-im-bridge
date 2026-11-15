"""Parse Muse's connector API-key links without treating them as vault captures."""

from __future__ import annotations

import base64
import binascii
import json
import re
from urllib.parse import parse_qs, urlparse


class ConnectorKeyRejected(ValueError):
    """A definitive rejection allows correcting the key without replay ambiguity."""


def connector_key_context(action, session_id, *, agent_id=None):
    """Return a validated single-key request template, or leave the card to web."""
    url = action.get("url")
    if not isinstance(url, str):
        return None
    try:
        parsed = urlparse(url)
        if (parsed.scheme != "https" or parsed.hostname not in {"agent.meta.ai", "muse.ai"}
                or parsed.path.rstrip("/") != "/connect/api-key" or parsed.fragment
                or parsed.username or parsed.password or parsed.port not in (None, 443)):
            return None
        params = parse_qs(parsed.query, keep_blank_values=True)
        if any(len(values) != 1 for values in params.values()):
            return None
        service = params.get("service", [""])[0]
        if not service.strip() or action.get("connectorId", action.get("connector_id", service)) != service:
            return None
        link_session = params.get("session_id", [session_id])[0]
        if not link_session or (link_session != session_id and link_session != agent_id):
            return None
        # Like the web dialog, submit the owning chat session, not the execution
        # agent ID embedded in the link. The latter only validates card ownership.
        context = {"capture": "connector_key", "page_url": url, "service_name": service,
                   "key_session_id": session_id}
        if "spec" not in params:
            context["key_request"] = {"provider": service}
            context["key_endpoint"] = "/api_key_auth/callback"
            return context
        encoded = params["spec"][0]
        raw = base64.b64decode(encoded + "=" * (-len(encoded) % 4), altchars=b"-_", validate=True)
        spec = json.loads(raw)
        if not isinstance(spec, dict) or spec.get("version") != 2:
            return None
        connector = spec.get("connector")
        if not isinstance(connector, dict) or connector.get("mode") != "api_key":
            return None
        name, hosts, proxy = (connector.get(key) for key in ("name", "hosts", "token_proxy"))
        if not isinstance(name, str) or not name.strip() or len(name) > 128:
            return None
        if (not isinstance(hosts, list) or not 1 <= len(hosts) <= 10
                or any(not isinstance(host, str) or not host.strip() for host in hosts)):
            return None
        if not isinstance(proxy, list) or len(proxy) != 1 or not isinstance(proxy[0], dict):
            return None
        field, placement = proxy[0].get("field"), proxy[0].get("placement")
        if not isinstance(field, str) or not re.fullmatch(r"[A-Za-z0-9_.-]+", field):
            return None
        if placement != "bearer_header":
            if not isinstance(placement, dict) or len(placement) != 1:
                return None
            key, value = next(iter(placement.items()))
            if (key not in {"custom_header", "url_path_segment", "query_param"}
                    or not isinstance(value, str) or not value.strip()):
                return None
        context["key_endpoint"] = "/v1/custom-connectors/register"
        context["key_request"] = {"connector": {
            "name": name, "mode": "api_key", "hosts": hosts, "token_proxy": proxy,
        }, "replace_existing": True}
        return context
    except (ValueError, UnicodeError, binascii.Error, RecursionError):
        return None
