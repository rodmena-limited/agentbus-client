from __future__ import annotations

import json
import os

import httpx
import pytest

from agentbus_client import sealing
from agentbus_client.client import AgentBus, AgentBusError, TransportError

BASE = "https://bus.round2.test"
READER = "reader-a-1"


class Recorder:
    def __init__(self, responder) -> None:
        self.responder = responder
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self.responder(request)


def _client(recorder: Recorder, agent: str | None = READER) -> AgentBus:
    bus = AgentBus(api_key="ab_live_round2", base_url=BASE, agent=agent)
    bus._client.close()
    bus._client = httpx.Client(transport=httpx.MockTransport(recorder), base_url=BASE)
    return bus


def _json(payload, status=200):
    return lambda request: httpx.Response(status, json=payload)


CLAIM = {
    "claim": {"kind": "repro", "command": "pytest -q", "expected_exit": 0},
    "verdicts": [{"verdict_id": "v-1", "result": "reproduced", "attested": True}],
    "unverified": False,
}


def test_get_claim_returns_the_servers_claim_unchanged() -> None:
    recorder = Recorder(_json(CLAIM))
    assert _client(recorder).get_claim("msg-77") == CLAIM
    (request,) = recorder.requests
    assert request.method == "GET"
    assert request.url.path == "/v1/messages/msg-77/claim"
    assert request.headers["X-AgentBus-Agent"] == READER
    assert request.headers["Authorization"] == "Bearer ab_live_round2"


def test_get_claim_acts_as_the_explicit_agent() -> None:
    recorder = Recorder(_json(CLAIM))
    _client(recorder).get_claim("msg-77", agent="other-agent-9")
    assert recorder.requests[0].headers["X-AgentBus-Agent"] == "other-agent-9"


@pytest.mark.parametrize("malformed", [[], ["claim"], "text", 7])
def test_get_claim_marks_a_non_object_response_malformed(malformed) -> None:
    recorder = Recorder(_json(malformed))
    assert _client(recorder).get_claim("msg-77") == {"claim": None, "note": "malformed"}
    assert len(recorder.requests) == 1


def test_get_claim_surfaces_a_server_error() -> None:
    recorder = Recorder(_json({"code": "not_found", "detail": "no claim"}, status=404))
    with pytest.raises(AgentBusError):
        _client(recorder).get_claim("msg-77")


def test_record_verdict_posts_every_observation_and_returns_the_servers_answer() -> None:
    answer = {"verdict_id": "v-42", "result": "reproduced", "attested": True}
    recorder = Recorder(_json(answer))
    result = _client(recorder).record_verdict(
        "msg-77",
        result="reproduced",
        observed_exit=0,
        observed_output="",
        client_version="0.9.102",
        env_note="linux x86_64",
    )
    assert result == answer
    (request,) = recorder.requests
    assert request.method == "POST"
    assert request.url.path == "/v1/messages/msg-77/claim/verdict"
    assert request.headers["X-AgentBus-Agent"] == READER
    assert request.headers["content-type"] == "application/json"
    assert json.loads(request.content) == {
        "result": "reproduced",
        "observed_exit": 0,
        "observed_output": "",
        "client_version": "0.9.102",
        "env_note": "linux x86_64",
    }


def test_record_verdict_sends_only_the_result_when_nothing_was_observed() -> None:
    recorder = Recorder(_json({"verdict_id": "v-43", "result": "not_reproduced"}))
    _client(recorder).record_verdict("msg-77", result="not_reproduced")
    assert json.loads(recorder.requests[0].content) == {"result": "not_reproduced"}


@pytest.mark.parametrize(
    "field,value",
    [
        ("observed_exit", 3),
        ("observed_output", "boom"),
        ("client_version", "1.0"),
        ("env_note", "freebsd"),
    ],
)
def test_record_verdict_sends_each_observation_alone(field, value) -> None:
    recorder = Recorder(_json({"verdict_id": "v-44", "result": "reproduced"}))
    _client(recorder).record_verdict("msg-77", result="reproduced", **{field: value})
    assert json.loads(recorder.requests[0].content) == {"result": "reproduced", field: value}


def test_record_verdict_acts_as_the_explicit_agent() -> None:
    recorder = Recorder(_json({"verdict_id": "v-45", "result": "reproduced"}))
    _client(recorder).record_verdict("msg-77", result="reproduced", agent="other-agent-9")
    assert recorder.requests[0].headers["X-AgentBus-Agent"] == "other-agent-9"


def test_record_verdict_on_a_non_object_response_does_not_invent_an_id() -> None:
    recorder = Recorder(_json([]))
    result = _client(recorder).record_verdict("msg-77", result="reproduced")
    assert result == {"verdict_id": "", "result": "reproduced"}
    assert len(recorder.requests) == 1


def test_record_verdict_surfaces_a_refusal() -> None:
    recorder = Recorder(_json({"code": "forbidden", "detail": "not yours"}, status=403))
    with pytest.raises(AgentBusError):
        _client(recorder).record_verdict("msg-77", result="reproduced")


def _bytes(content: bytes, status=200):
    return lambda request: httpx.Response(status, content=content)


def test_attachment_defaults_to_the_first_attachment_and_returns_its_bytes() -> None:
    payload = bytes(range(256)) * 4
    recorder = Recorder(_bytes(payload))
    assert _client(recorder).attachment("dlv-9") == payload
    (request,) = recorder.requests
    assert request.method == "GET"
    assert request.url.path == "/v1/deliveries/dlv-9/attachments/0"
    assert request.headers["X-AgentBus-Agent"] == READER
    assert request.headers["Authorization"] == "Bearer ab_live_round2"
    assert "idempotency-key" not in request.headers


def test_attachment_fetches_the_index_asked_for() -> None:
    recorder = Recorder(_bytes(b"third"))
    assert _client(recorder).attachment("dlv-9", 2) == b"third"
    assert recorder.requests[0].url.path == "/v1/deliveries/dlv-9/attachments/2"


def test_attachment_acts_as_the_explicit_agent() -> None:
    recorder = Recorder(_bytes(b"x"))
    _client(recorder).attachment("dlv-9", agent="other-agent-9")
    assert recorder.requests[0].headers["X-AgentBus-Agent"] == "other-agent-9"


def test_a_multi_chunk_attachment_comes_back_byte_for_byte() -> None:
    payload = os.urandom(64 * 1024 * 3 + 777)
    recorder = Recorder(_bytes(payload))
    assert _client(recorder).attachment("dlv-9") == payload


class CountingStream(httpx.SyncByteStream):
    def __init__(self, chunk: bytes, count: int) -> None:
        self.chunk = chunk
        self.count = count
        self.served = 0

    def __iter__(self):
        for _ in range(self.count):
            self.served += 1
            yield self.chunk


def test_the_cap_trips_while_streaming_before_the_body_is_read(monkeypatch) -> None:
    monkeypatch.setenv("AGENTBUS_MAX_ATTACHMENT_BYTES", "100000")
    stream = CountingStream(b"z" * 16 * 1024, 64)
    recorder = Recorder(lambda request: httpx.Response(200, stream=stream))
    with pytest.raises(AgentBusError) as exc:
        _client(recorder).attachment("dlv-9")
    assert "150,000 byte cap" in str(exc.value)
    assert "AGENTBUS_MAX_ATTACHMENT_BYTES" in str(exc.value)
    assert stream.served < 64


def test_the_cap_counts_bytes_across_chunks(monkeypatch) -> None:
    monkeypatch.setenv("AGENTBUS_MAX_ATTACHMENT_BYTES", "100000")
    recorder = Recorder(_bytes(b"q" * 200_000))
    with pytest.raises(AgentBusError) as exc:
        _client(recorder).attachment("dlv-9")
    assert "150,000 byte cap" in str(exc.value)


def test_a_body_of_exactly_the_cap_is_accepted_and_one_more_byte_is_not(monkeypatch) -> None:
    monkeypatch.setenv("AGENTBUS_MAX_ATTACHMENT_BYTES", "100000")
    exact = b"e" * 150_000
    assert _client(Recorder(_bytes(exact))).attachment("dlv-9") == exact
    with pytest.raises(AgentBusError):
        _client(Recorder(_bytes(exact + b"!"))).attachment("dlv-9")


def test_the_default_cap_is_one_and_a_half_times_fifty_megabytes(monkeypatch) -> None:
    monkeypatch.delenv("AGENTBUS_MAX_ATTACHMENT_BYTES", raising=False)
    stream = CountingStream(b"d" * (1024 * 1024), 200)
    recorder = Recorder(lambda request: httpx.Response(200, stream=stream))
    with pytest.raises(AgentBusError) as exc:
        _client(recorder).attachment("dlv-9")
    assert f"{int(1.5 * 50 * 1024 * 1024):,} byte cap" in str(exc.value)
    assert stream.served == 76


def test_an_attachment_error_status_raises() -> None:
    recorder = Recorder(lambda request: httpx.Response(404, json={"code": "not_found"}))
    with pytest.raises(AgentBusError) as exc:
        _client(recorder).attachment("dlv-9", 5)
    assert exc.value.status == 404


def test_a_transport_failure_is_a_typed_transport_error() -> None:
    def refuse(request):
        raise httpx.ConnectError("connection refused", request=request)

    with pytest.raises(TransportError):
        _client(Recorder(refuse)).attachment("dlv-9")


def test_a_sealed_attachment_is_opened_with_the_clients_own_key() -> None:
    _, public = sealing.ensure_keypair(READER)
    raw = os.urandom(5000)
    armored = sealing.seal_for_bytes(raw, [public])
    assert armored.startswith(b"-----BEGIN AGE ENCRYPTED FILE-----")
    assert _client(Recorder(_bytes(armored))).attachment("dlv-9") == raw


def test_sealed_armor_after_leading_whitespace_is_still_opened() -> None:
    _, public = sealing.ensure_keypair(READER)
    raw = b"leading whitespace must not hide the armor"
    armored = b"\n\n   " + sealing.seal_for_bytes(raw, [public])
    assert _client(Recorder(_bytes(armored))).attachment("dlv-9") == raw


def test_a_sealed_attachment_on_a_keyless_machine_is_refused_by_name() -> None:
    _, stranger_public = sealing.generate_keypair()
    armored = sealing.seal_for_bytes(b"secret", [stranger_public])
    with pytest.raises(AgentBusError) as exc:
        _client(Recorder(_bytes(armored))).attachment("dlv-9")
    assert "holds no sealing key" in str(exc.value)


def test_a_sealed_attachment_for_someone_else_is_not_called_corrupt() -> None:
    sealing.ensure_keypair(READER)
    _, stranger_public = sealing.generate_keypair()
    armored = sealing.seal_for_bytes(b"secret", [stranger_public])
    with pytest.raises(AgentBusError) as exc:
        _client(Recorder(_bytes(armored))).attachment("dlv-9")
    assert "sealed to keys this machine does not hold" in str(exc.value)


def test_another_local_agents_key_does_not_open_this_agents_attachment() -> None:
    sealing.ensure_keypair(READER)
    _, other_public = sealing.ensure_keypair("neighbour-b-2")
    armored = sealing.seal_for_bytes(b"for the neighbour only", [other_public])
    with pytest.raises(AgentBusError) as exc:
        _client(Recorder(_bytes(armored))).attachment("dlv-9")
    assert "sealed to keys this machine does not hold" in str(exc.value)


def test_a_damaged_sealed_attachment_is_called_damaged() -> None:
    sealing.ensure_keypair(READER)
    damaged = (
        b"-----BEGIN AGE ENCRYPTED FILE-----\n!!!not base64!!!\n-----END AGE ENCRYPTED FILE-----\n"
    )
    with pytest.raises(AgentBusError) as exc:
        _client(Recorder(_bytes(damaged))).attachment("dlv-9")
    assert "this attachment is damaged" in str(exc.value)


def test_plain_bytes_that_mention_the_armor_later_are_returned_untouched() -> None:
    sealing.ensure_keypair(READER)
    payload = b"x" * 80 + b"-----BEGIN AGE ENCRYPTED FILE-----"
    assert _client(Recorder(_bytes(payload))).attachment("dlv-9") == payload
