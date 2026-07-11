"""AcoustID client wire contract — lock the space-`meta` encoding (spike bug)."""

import httpx

from crate.infrastructure.acoustid.client import AcoustidClient


def test_lookup_sends_space_meta_as_plus_not_escaped() -> None:
    captured: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = request.content.decode()
        return httpx.Response(200, json={"results": []})

    client = AcoustidClient("KEY123", transport=httpx.MockTransport(handler))
    assert client.lookup(201, "AQAAB64") == {"results": []}

    body = captured["body"]
    # The meta list must arrive as `+`-joined; a literal `%2B` means the meta was
    # silently dropped and recordings/MBID come back empty (the spike regression).
    assert "meta=recordings+releases" in body
    assert "%2B" not in body
    assert "client=KEY123" in body and "fingerprint=AQAAB64" in body
