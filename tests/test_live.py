from __future__ import annotations

from factory.events import LiveDelta, LiveFinal
from factory.gateway.hub import ANY_STREAM, EventHub
from factory.gateway.live import STALE_SECONDS, LiveCache, PartialMessage

STREAM = "agent_run:ar_1"
THREAD = "github:acme/web#issue-12"


def delta(text: str, local_seq: int = 1, part: int = 0, *, message_id: str = "m1", stream: str = STREAM) -> LiveDelta:
    return LiveDelta(stream=stream, thread=THREAD, message_id=message_id, local_seq=local_seq, part=part, text=text)


def test_the_cache_appends_parts_in_order_and_ignores_repeated_or_older_ones() -> None:
    cache = LiveCache()
    assert cache.apply(delta("Hel", 1, 0), now=0) is not None
    assert cache.apply(delta("lo", 1, 1), now=0) is not None
    assert cache.apply(delta("lo", 1, 1), now=0) is None
    assert cache.apply(delta("Hel", 1, 0), now=0) is None
    assert cache.apply(delta(" there", 2, 0), now=0) is not None
    assert cache.apply(delta("late", 1, 2), now=0) is None
    assert cache.partials(STREAM) == [PartialMessage(STREAM, THREAD, "m1", "Hello there")]


def test_a_final_notice_deletes_the_message() -> None:
    cache = LiveCache()
    cache.apply(delta("Hi"), now=0)
    cache.apply(delta("Work", message_id="m2"), now=0)
    assert cache.apply(LiveFinal(stream=STREAM, message_id="m1"), now=0) is None
    assert [p.message_id for p in cache.partials(STREAM)] == ["m2"]


def test_partials_match_a_stream_or_a_thread() -> None:
    cache = LiveCache()
    cache.apply(delta("a"), now=0)
    cache.apply(delta("b", stream="agent_run:ar_2"), now=0)
    assert [p.text for p in cache.partials(STREAM)] == ["a"]
    assert [p.text for p in cache.partials(THREAD)] == ["a", "b"]
    assert cache.partials("github:acme/web#issue-13") == []


def test_messages_untouched_for_fifteen_minutes_are_swept() -> None:
    cache = LiveCache()
    cache.apply(delta("old"), now=0)
    cache.apply(delta("new", message_id="m2"), now=STALE_SECONDS)
    assert [p.message_id for p in cache.partials(STREAM)] == ["m1", "m2"]
    cache.apply(delta("newer", message_id="m3"), now=STALE_SECONDS + 1)
    assert [p.message_id for p in cache.partials(STREAM)] == ["m2", "m3"]


def test_the_hub_wakes_subscribers_of_the_stream_and_of_every_stream() -> None:
    hub = EventHub()
    calls: list[str] = []
    stop = hub.on_events(STREAM, lambda: calls.append("stream"))
    hub.on_events(ANY_STREAM, lambda: calls.append("any"))
    hub.on_events("run:other", lambda: calls.append("other"))
    hub.receive("events", f"{STREAM} 3")
    assert calls == ["stream", "any"]
    stop()
    hub.receive("events", f"{STREAM} 4")
    assert calls == ["stream", "any", "any"]


def test_the_hub_relays_new_deltas_by_stream_and_thread_and_keeps_partials() -> None:
    hub = EventHub()
    by_stream: list[str] = []
    by_thread: list[str] = []
    hub.on_delta(STREAM, lambda d: by_stream.append(d.text))
    stop = hub.on_delta(THREAD, lambda d: by_thread.append(d.text))
    hub.receive("live", delta("Hel", 1, 0).model_dump_json())
    hub.receive("live", delta("Hel", 1, 0).model_dump_json())
    hub.receive("live", "not json")
    hub.receive("live", '{"type": "delta", "stream": "x"}')
    hub.receive("live", delta("lo", 1, 1).model_dump_json())
    assert by_stream == by_thread == ["Hel", "lo"]
    assert hub.partials(THREAD) == [PartialMessage(STREAM, THREAD, "m1", "Hello")]

    stop()
    hub.receive("live", LiveFinal(stream=STREAM, message_id="m1").model_dump_json())
    assert hub.partials(STREAM) == []
    hub.receive("live", delta("Next", 2, 0, message_id="m2").model_dump_json())
    assert by_stream == ["Hel", "lo", "Next"]
    assert by_thread == ["Hel", "lo"]
