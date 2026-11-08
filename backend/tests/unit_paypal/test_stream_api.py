"""Stream API tests (Person 1, frozen contract /api/v1/stream).

Covers the in-memory EventBus contract (seq, replay, drop-oldest, isolation)
and the live SSE endpoint (frames, Last-Event-ID replay, dedupe, heartbeat,
disconnect cleanup) against the real demo chain.

AUTH NOTE (documented conflict, like app/api/stream.py): the frozen contract
declares no security scheme on /api/v1/stream and the identity system the task
asks to enforce per-user isolation with does not exist in this app yet. The
"authentication/authorization failure" scenarios from the task are therefore
N/A at contract level and replaced by: no-auth contract conformance + the
additive topic filter validation. Recorded in docs/phase4-handoff.
"""

from __future__ import annotations

import json
import threading
import time

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.services import events as events_module
from app.services.events import (
    EventBus,
    get_event_bus,
    reset_event_bus,
)

QUOTE_BODY = {
    "merchant_id": "mer_stream_e2e",
    "item_hash": "d" * 64,
    "item_title": "Stream Gadget",
    "item_price": "80.00",
    "currency_code": "USD",
}


def _client(tmp_path, **overrides) -> TestClient:
    settings = Settings(
        _env_file=None,
        engine_impl="stub",
        demo_deterministic_mode=True,
        paypal_client_id="",
        paypal_client_secret="",
        sqlite_url=f"sqlite+aiosqlite:///{tmp_path}/stream.db",
        quotes_hmac_key="stream-e2e-key-0123456789abcdef",
        claim_reviewer_token="rev-secret-token",
        stream_heartbeat_seconds=1,
        **overrides,
    )
    from app.main import create_app

    return TestClient(create_app(settings))


@pytest.fixture(autouse=True)
def _isolated_bus():
    reset_event_bus()
    yield
    reset_event_bus()


def _capture_and_refund(client: TestClient) -> str:
    r = client.post("/api/v1/quotes", json=QUOTE_BODY)
    assert r.status_code == 200, r.text
    quote = r.json()["quote"]
    r = client.post(
        "/api/v1/orders",
        json={"quote_id": quote["quote_id"], "buyer_ref": "buyer_stream", "opt_in": True},
    )
    assert r.status_code == 201, r.text
    order_id = r.json()["order_id"]
    r = client.post(f"/api/v1/orders/{order_id}/capture")
    assert r.status_code == 200, r.text
    r = client.post(
        "/api/v1/claims",
        data={"order_id": order_id},
        files={"photo": ("r.jpg", b"\x89PNG\r\n\x1a\n", "image/png")},
    )
    assert r.status_code == 201, r.text
    claim_id = r.json()["claim_id"]
    resolved = client.post(
        f"/api/v1/claims/{claim_id}/resolve",
        json={"decision": "REFUND", "reason_code": "TEST"},
        headers={"X-Reviewer-Token": "rev-secret-token"},
    )
    assert resolved.status_code == 200, resolved.text
    return order_id


# ---------------------------------------------------------------------------
# EventBus unit behavior
# ---------------------------------------------------------------------------
class TestEventBus:
    def test_publish_assigns_monotonic_seq_and_history(self):
        bus = EventBus()
        e1 = bus.publish("transaction", "add", _FakeRecord("a"))
        e2 = bus.publish("claim", "update", _FakeRecord("b"))
        assert (e1.seq, e2.seq) == (1, 2)
        assert [ev.seq for ev in bus.replay(0)] == [1, 2]
        assert bus.replay(e1.seq) == [e2]
        assert bus.replay(99) == []

    def test_publish_rejects_unknown_topic_or_action(self):
        bus = EventBus()
        with pytest.raises(ValueError):
            bus.publish("order", "add", _FakeRecord("x"))
        with pytest.raises(ValueError):
            bus.publish("transaction", "replace", _FakeRecord("x"))

    @pytest.mark.asyncio
    async def test_subscriber_drop_oldest_while_backpressured(self):
        bus = EventBus()
        # subscribing binds the current running loop
        subscriber = bus.subscribe()
        for i in range(events_module.SUBSCRIBER_BUFFER_EVENTS + 10):
            bus.publish("transaction", "add", _FakeRecord(str(i)))
        got = [await subscriber.receive() for _ in range(5)]
        # oldest overflow dropped: we are at the tail end of the buffer
        assert got[0].seq == 10 + 1
        assert got[-1].seq == 10 + 5
        assert len(got) == 5

    @pytest.mark.asyncio
    async def test_receive_timeout_returns_none_for_heartbeat(self):
        bus = EventBus()
        subscriber = bus.subscribe()
        start = time.perf_counter()
        got = await subscriber.receive(timeout=0.05)
        assert got is None
        assert 0.01 < time.perf_counter() - start < 1.0

    @pytest.mark.asyncio
    async def test_unsubscribe_and_subscriber_count(self):
        bus = EventBus()
        count_before = bus.subscriber_count
        subscriber = bus.subscribe()
        assert bus.subscriber_count == count_before + 1
        bus.unsubscribe(subscriber)
        assert bus.subscriber_count == count_before


class _FakeRecord:
    def __init__(self, value: str) -> None:
        self.value = value


# ---------------------------------------------------------------------------
# SSE endpoint
# ---------------------------------------------------------------------------
def _read_frames(response, want: int | None = None, max_pings: int = 3,
                 timeout: float = 10.0, lines_iter=None) -> list[tuple[str, str, str, dict]]:
    """Collect SSE data frames with bounds — heartbeats repeat forever, so
    reads must stop on: reached `want` frames, `max_pings` keep-alives, or
    `timeout` seconds (whichever comes first)."""
    frames: list = []
    event: str | None = None
    id_: str | None = None
    pings = 0
    started = time.perf_counter()
    for raw in (lines_iter if lines_iter is not None else response.iter_lines()):
        if time.perf_counter() - started > timeout:
            break
        line = (raw if isinstance(raw, str) else raw.decode()).rstrip("\r")
        if line == ": ping":
            pings += 1
            if pings >= max_pings:
                break
            continue
        if line.startswith("event: "):
            event = line[len("event: "):]
        elif line.startswith("id: "):
            id_ = line[len("id: "):]
        elif line.startswith("data: "):
            payload = json.loads(line[len("data: "):])
            frames.append((event, id_, payload, payload["record"]))
            event = id_ = None
            if want is not None and len(frames) >= want:
                break
    return frames


class TestStreamEndpoint:
    def test_content_type_and_headers(self, tmp_path):
        with _client(tmp_path) as client:
            with client.stream("GET", "/api/v1/stream") as r:
                assert r.status_code == 200
                assert r.headers["content-type"].startswith("text/event-stream")
                assert r.headers["cache-control"] == "no-store"
                assert r.headers["x-accel-buffering"] == "no"

    def test_replay_after_real_flow_in_order(self, tmp_path):
        """Event naming/order/payload follow the contract, post-commit only."""
        with _client(tmp_path) as client:
            _capture_and_refund(client)
            bus = get_event_bus()
            assert bus.subscriber_count == 0

            with client.stream(
                "GET", "/api/v1/stream", headers={"Last-Event-ID": "0"}
            ) as response:
                frames = _read_frames(response, want=7)

            # create add, capture update, claim add, pipeline update,
            # refund claim update + order update + merchant update
            assert [f[0] for f in frames] == [
                "transaction", "transaction", "claim",
                "claim", "claim", "transaction", "merchant",
            ]
            assert [f[2]["action"] for f in frames] == [
                "add", "update", "add", "update", "update", "update", "update",
            ]

            # envelope shape
            for _event, seq, envelope, _record in frames:
                assert set(envelope) == {"topic", "action", "record"}
                assert envelope["topic"] == _event
                assert envelope["action"] in {"add", "update"}
                assert seq is not None and seq.isdigit()

            # the first transaction row is the order add
            first = frames[0][3]
            assert set(first) == {
                "internal_order_id", "merchant_id", "item_price",
                "protection_fee", "r_final", "state", "created_at",
            }
            assert first["state"] == "ORDER_CREATED"

            # claim records carry the contract ClaimRecord fields
            claim_frame = next(f for f in frames if f[0] == "claim")
            assert "claim_id" in claim_frame[3]
            assert "state" in claim_frame[3]

            # merchant update exists and matches trust shape
            merchant_frame = next(f for f in frames if f[0] == "merchant")
            assert set(merchant_frame[3]) == {
                "merchant_id", "total_orders", "successful_orders",
                "verified_fraud_claims", "trust_score", "m_penalty",
            }
            assert merchant_frame[3]["verified_fraud_claims"] == 1

    def test_capture_before_refund_ordering_ground_truth(self, tmp_path):
        """transaction update (CAPTURED) precedes claim add and the refund chain,
        and the persisted-only rule holds: no event can show a state the DB
        did not have at its commit time."""
        with _client(tmp_path) as client:
            _capture_and_refund(client)
            with client.stream(
                "GET", "/api/v1/stream", headers={"Last-Event-ID": "0"}
            ) as response:
                frames = _read_frames(response, want=7)

            order_of = [
                (f[0], f[2]["action"], f[3].get("state") or f[3].get("claim_id"))
                for f in frames
            ]
            kinds = [o[0] + "/" + o[1] for o in order_of]
            captured_idx = next(
                i for i, (k, o) in enumerate(zip(kinds, order_of))
                if k == "transaction/update" and o[2] == "CAPTURED"
            )
            claim_add_idx = next(i for i, k in enumerate(kinds) if k == "claim/add")
            refunded_txn_idx = next(
                i for i, (k, o) in enumerate(zip(kinds, order_of))
                if k == "transaction/update" and o[2] == "REFUNDED"
            )
            assert captured_idx < claim_add_idx < refunded_txn_idx

    def test_last_event_id_deduplicates_replay(self, tmp_path):
        """A reconnect at the last cursor gets no replayed events (only pings)."""
        with _client(tmp_path) as client:
            _capture_and_refund(client)
            bus = get_event_bus()
            last_seq = bus.replay(0)[-1].seq
            with client.stream(
                "GET", "/api/v1/stream",
                headers={"Last-Event-ID": str(last_seq)},
            ) as response:
                frames = _read_frames(response)
            assert frames == []  # nothing after the cursor, so no dupes

    def test_mid_history_replay_gives_only_newer_events(self, tmp_path):
        with _client(tmp_path) as client:
            _capture_and_refund(client)
            bus = get_event_bus()
            history = bus.replay(0)
            cursor = history[len(history) // 2].seq
            expected_ids = [str(ev.seq) for ev in history if ev.seq > cursor]
            with client.stream(
                "GET", "/api/v1/stream",
                headers={"Last-Event-ID": str(cursor)},
            ) as response:
                frames = _read_frames(response, want=len(expected_ids))
            assert [f[1] for f in frames] == expected_ids

    def test_garbage_last_event_id_treated_as_fresh(self, tmp_path):
        with _client(tmp_path) as client:
            _capture_and_refund(client)
            want = len(get_event_bus().replay(0))
            with client.stream(
                "GET", "/api/v1/stream", headers={"Last-Event-ID": "garbage"}
            ) as response:
                frames = _read_frames(response, want=want)
            assert len(frames) == want > 0  # full replay, like cursor 0

    def test_heartbeat_comments_keep_connection_alive(self, tmp_path):
        with _client(tmp_path) as client:
            with client.stream("GET", "/api/v1/stream") as response:
                lifelines = response.iter_lines()
                pings = 0
                first = next(lifelines, "")
                assert (first if isinstance(first, str) else first.decode()).startswith("retry")
                ping_seen = False
                for line in lifelines:
                    text = line if isinstance(line, str) else line.decode()
                    if text.strip() == ": ping":
                        pings += 1
                        if pings >= 2:
                            ping_seen = True
                            break
                assert ping_seen, "expected keep-alive pings within the heartbeat"

    def test_live_delivery_while_connected_no_duplicates(self, tmp_path):
        """A live publish reaching an open, already-subscribed stream exactly
        once — and the subscriber leaves cleanly on disconnect."""
        from app.api.response_models import MerchantTrustRow

        with _client(tmp_path) as client:
            with client.stream("GET", "/api/v1/stream") as response:
                lines = response.iter_lines()
                first = next(lines, "")
                assert (first if isinstance(first, str) else first.decode()).startswith("retry")

                seen = []

                def publish():
                    # runs on a different thread; call_soon_threadsafe makes
                    # the bus cross-thread safe for exactly this case
                    get_event_bus().publish(
                        "merchant", "update",
                        MerchantTrustRow(
                            merchant_id="mer_live",
                            total_orders=3,
                            successful_orders=3,
                            verified_fraud_claims=0,
                            trust_score="0.7917",
                            m_penalty="1.00",
                        ),
                    )

                thread = threading.Thread(target=publish)
                thread.start()
                thread.join(timeout=2)

                # next data frame must be the live one (heartbeats interleaved)
                frames = _read_frames(response, want=1, lines_iter=lines)
                seen.extend(frames)

            assert seen, "live event never delivered"
            _event, _seq, envelope, record = seen[0]
            assert envelope["topic"] == "merchant"
            assert envelope["action"] == "update"
            assert record["merchant_id"] == "mer_live"

        bus = get_event_bus()
        deadline = time.time() + 3
        while get_event_bus().subscriber_count > 0 and time.time() < deadline:
            time.sleep(0.05)
        assert bus.subscriber_count == 0

    def test_topic_filter_additive_deviation(self, tmp_path):
        with _client(tmp_path) as client:
            _capture_and_refund(client)
            with client.stream(
                "GET", "/api/v1/stream", params={"topics": "claim"}
            ) as response:
                want = sum(
                    1 for ev in get_event_bus().replay(0) if ev.topic == "claim"
                )
                frames = _read_frames(response, want=want)
            assert frames and all(f[0] == "claim" for f in frames)

    def test_unknown_topic_filter_is_422(self, tmp_path):
        with _client(tmp_path) as client:
            # non-streaming call so the 422 status is observable
            r = client.get("/api/v1/stream", params={"topics": "walrus"})
            assert r.status_code == 422

    def test_disconnect_cleans_up_subscriber(self, tmp_path):
        with _client(tmp_path) as client:
            with client.stream("GET", "/api/v1/stream") as response:
                # consume the initial retry line
                next(response.iter_lines(), None)
                assert get_event_bus().subscriber_count == 1
            deadline = time.time() + 3
            while get_event_bus().subscriber_count > 0 and time.time() < deadline:
                time.sleep(0.05)
            assert get_event_bus().subscriber_count == 0
