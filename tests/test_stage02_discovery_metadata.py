import asyncio
from types import SimpleNamespace

from k5vision.adapters.stage02_client import Stage02Adapter


def test_discovery_retains_only_source_safe_xaddr_metadata() -> None:
    def discovery_factory(**kwargs):
        return SimpleNamespace(
            discover=lambda: [
                {
                    "host": "10.0.0.9",
                    "port": 8899,
                    "xaddrs": [
                        (
                            "https://admin:secret@10.0.0.9:8899/service"
                            "?transport=tcp&access_token=AUDIT_QUERY_TOKEN"
                            "#AUDIT_FRAGMENT_TOKEN"
                        ),
                        "not-a-uri",
                    ],
                }
            ]
        )

    result = asyncio.run(Stage02Adapter(discovery_factory=discovery_factory).discover())[0]

    assert result.secure is True
    assert result.xaddrs == ["https://10.0.0.9:8899/service?transport=tcp"]
    serialized = result.model_dump_json()
    for forbidden in (
        "admin",
        "secret",
        "AUDIT_QUERY_TOKEN",
        "AUDIT_FRAGMENT_TOKEN",
        "access_token",
    ):
        assert forbidden not in serialized
