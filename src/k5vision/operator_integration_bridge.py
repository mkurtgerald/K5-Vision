"""Native local consumer client for K5's versioned downstream lab handoff.

An admitted host supplies the actual lab provider. This library neither imports
private research implementations nor creates an autonomous execution authority.
"""

from __future__ import annotations

import http.client
import ipaddress
import json
import socket
import threading
from urllib.parse import urlsplit
from uuid import UUID

from k5vision.operator_integration_contracts import (
    IntegrationContractError,
    bounded_document,
    k5_lab_response,
    negotiate_k5_lab,
)


class K5LabBridgeClient:
    """Bounded credential-scoped HTTP client for a colocated native provider host.

    The existing desktop gateway is loopback-only. Remote service deployment is
    deliberately not silently opened by a user-supplied URL or this helper.
    """

    def __init__(self, origin: str, connection_id: UUID, token: str):
        try:
            value = urlsplit(origin)
            address = ipaddress.ip_address(value.hostname or "")
            if (
                value.scheme != "http"
                or not address.is_loopback
                or value.path not in ("", "/")
                or value.query
                or value.fragment
                or value.username is not None
                or value.password is not None
                or type(token) is not str
                or not 32 <= len(token) <= 128
                or any(
                    c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-"
                    for c in token
                )
            ):
                raise ValueError
            self._host, self._port = str(address), value.port or 80
            if not 1 <= self._port <= 65535:
                raise ValueError
            self._prefix = "/api/v1/operator/integrations/" + str(UUID(str(connection_id)))
            self._token = token
        except (ValueError, TypeError):
            raise IntegrationContractError("integration_invalid_request") from None

    def _request(self, method, suffix, payload=None):
        connection = http.client.HTTPConnection(self._host, self._port, timeout=3)
        timer = None
        active_socket = None
        expired = threading.Event()

        def abort():
            expired.set()
            try:
                if active_socket is not None:
                    active_socket.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            connection.close()

        try:
            # Numeric loopback has no external DNS lookup. Start the elapsed
            # watchdog before connect; shutdown bounds slow headers and bodies.
            timer = threading.Timer(3, abort)
            timer.daemon = True
            timer.start()
            raw = (
                None
                if payload is None
                else json.dumps(
                    bounded_document(payload), allow_nan=False, separators=(",", ":")
                ).encode("utf-8")
            )
            if raw is not None and len(raw) > 64 * 1024:
                raise IntegrationContractError("integration_invalid_request")
            connection.connect()
            active_socket = connection.sock
            if expired.is_set():
                raise IntegrationContractError("integration_provider_unavailable")
            connection.request(
                method,
                self._prefix + suffix,
                body=raw,
                headers={
                    "X-Integration-Token": self._token,
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                    "Connection": "close",
                },
            )
            response = connection.getresponse()
            body = response.read(256 * 1024 + 1)
            if (
                len(body) > 256 * 1024
                or response.getheader("Content-Type", "").split(";", 1)[0] != "application/json"
            ):
                raise IntegrationContractError("integration_provider_unavailable")
            result = json.loads(body)
            if type(result) is not dict:
                raise ValueError
            if response.status != 200:
                code = result.get("detail")
                if type(code) is str and code in {
                    "integration_unauthorized",
                    "integration_handshake_required",
                    "integration_source_clock_ahead",
                    "integration_time_inconsistent",
                    "integration_incompatible",
                    "integration_camera_forbidden",
                    "integration_capacity",
                    "integration_correlation_mismatch",
                    "integration_event_conflict",
                    "integration_response_conflict",
                }:
                    raise IntegrationContractError(code)
                raise IntegrationContractError("integration_provider_unavailable")
            return result
        except IntegrationContractError:
            raise
        except Exception:
            raise IntegrationContractError("integration_provider_unavailable") from None
        finally:
            if timer is not None:
                timer.cancel()
            abort()

    def handshake(self, supported_versions=("2",)):
        selected = negotiate_k5_lab(list(supported_versions))
        result = self._request(
            "POST", "/handshake", {"supported_versions": list(supported_versions)}
        )
        if (
            result.get("accepted") is not True
            or result.get("selected_version") != selected
            or result.get("authority") != "observe_only"
        ):
            raise IntegrationContractError("integration_incompatible")
        return result

    def observations(self):
        return self._request("GET", "/observations")

    def respond(self, request, response):
        k5_lab_response(request, response)
        result = self._request("POST", "/responses", {"request": request, "response": response})
        if result.get("accepted") is not True or result.get("authorized") is not False:
            raise IntegrationContractError("integration_invalid_response")
        return result

    def ingest_analytic(self, event):
        """Send Analytics Lab0.1 metadata after an explicit0.1 handshake."""
        return self._request("POST", "/events", event)

    def analytics_handshake(self):
        result = self._request("POST", "/handshake", {"supported_versions": ["0.1"]})
        if result.get("accepted") is not True or result.get("selected_version") != "0.1":
            raise IntegrationContractError("integration_incompatible")
        return result
