---
status: active
role: implementation
date: 2026-11-20
last_reviewed: 2026-11-21
superseded_by: null
topic: akosha-event-pipeline-recovery
plan_kind: repair
replaces: null
related_decisions:
  - mahavishnu/.claude/decisions/wire-up-contract.md
  - mahavishnu/.claude/decisions/mcp-backend-wiring-discipline.md
related_plans:
  - docs/plans/2026-11-17-unified-pgvector-warm-tier.md
  - docs/plans/2026-09-09-akosha-otel-feed-recovery.md
cross_repo_plans:
  - mahavishnu/docs/plans/2026-11-19-fleet-standardisation.md
  - mahavishnu/docs/plans/2026-11-20-fleet-memory-retrieval-quality.md
---

# Akosha Event-Pipeline Recovery Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use
> superpowers:subagent-driven-development (recommended) or
> superpowers:executing-plans to implement this plan task-by-task. Steps use
> checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the `bodai:events` -> Akosha ingestion path actually produce
HotStore rows, so the knowledge graph stops being empty.

**Verification status: the code is verified AND the live run has
happened.** The code is complete and the automated gates are green: the
full suite is **3351 passed / 10 skipped** (exit 0), and the
crackerjack gate is green — **18/18 fast hooks**, **12/12 comprehensive
hooks**, **58 tests, 0 errors** in the gate's own test phase. `ruff` and
`ty` are clean.

**Task 6 — the live end-to-end verification — RAN on 2026-11-21.** The
service was restarted onto the merged code, the leaked consumers were
cleaned up, the backlog was replayed, and the observed results are in
"Verification runbook" at the end of this document. The run PASSED its
primary criteria and exposed three defects that the code-level fixes
had not covered; those three are now fixed and merged
(`665bbe6`). One step remains outstanding: the live service still runs
the pre-fix code until it is restarted onto `665bbe6`, so the three
fixes are verified by tests but not yet by a second live run.

**Which store each number belongs to — read this before writing the success
check.** The `conversations` table lives in the **hot** tier (DuckDB). The
subscriber writes to it, and the knowledge-graph refresh reads it from
there. The warm pgvector tier's `vectors_conversations` table is written by
**exactly one** writer: `AgingService._migrate_batch`
(`akosha/storage/aging.py:125,238`), the hot→warm migration. So:

- **The fix's observable effect** is on the hot tier and the knowledge
  graph: `knowledge_graph_feed.entities_count` goes above 0.
- **`vectors_conversations` stays at 0** until aging is enabled, which is
  Phase 4 of the warm-tier plan, not this one. Checking it as the success
  criterion would report a false failure for a correctly working fix.

**Architecture:** Two independent defects sit on the same path. The
`BodaiToolInvocationSubscriber` mints a fresh `uuid4` consumer name on every
`XREADGROUP` call, so the consumer group accumulated 446,049 consumers and
`XINFO CONSUMERS` on it took 158-203 seconds. Separately, `_decode_envelope`
accepts two wire shapes that no producer emits, so every real message decodes
to `None` and is XACKed and dropped — silently, as "poison". The fix pins the
decoder to the shape `oneiric`'s Redis Streams adapter actually writes, makes
the consumer name stable per process, and stops the health feed from
reporting a permanent stall as `warming_up`.

Three further defects surfaced while fixing those two — the resume path that
could never deliver a message, a watermark write that was neither atomic nor
monotonic, and a websocket flag that vetoed the only producer. They are
recorded in "Round two" at the end of this document, and they are part of the
same fix: without them the pipeline would have looked healthy and delivered
nothing after the first restart.

**Tech Stack:** Python 3.14, `redis-py` asyncio, DuckDB (hot tier),
pgvector (warm tier), pytest, `pytest-asyncio` (auto mode).

**Spec:** none — this is a defect-recovery plan. The wire contract it
enforces is pinned by observation in Task 2 and is not negotiable.

## Global Constraints

- **Do not touch the embedding model, embedding width, embedding provider, or
  any embedding setting.** Those belong to a separate live session
  (Indigo Hearth's width decision, Lark Bridge's Session-Buddy PR). This plan
  restores the ingestion plumbing and reuses whatever embedding
  configuration is already committed. If re-ingestion needs a different
  width, that is a follow-up, not this plan.
- **Do not touch `settings/models.yaml`, `settings/local.yaml`, or
  `~/.config/akosha/local.yaml`.** The overlay that enabled the subscriber
  lives outside the repo; the repo default stays `false`.
- **Do not regenerate or commit `docs/plans/PLAN_INDEX.md` from this lane.**
  The index belongs to the session that owns the mahavishnu plan index.
- **Pre-1.0 hard-break policy.** Where a wire shape has no producer, remove
  it rather than keep a compatibility shim.
- Python 3.14; `from __future__ import annotations` first non-comment line;
  `X | None` not `Optional[X]`; `pathlib.Path` not `os.path`; no `print()` in
  library code; `logger.exception(...)` in `except` blocks; line length 88.
- Tests live under `tests/`, use the repo's pytest markers, and need no
  `@pytest.mark.asyncio` (`asyncio_mode = "auto"`).

## Review Focus

The spec here is a wire contract, and its silence is not permission for the
program to break. These are the inputs and conditions most likely to bite,
most likely first. Each line gets a test added to the task that owns the
code.

1. **A message from a producer that emits the flat `enqueue` shape**
   (`channel` plus one stream field per payload key, no nested `payload`
   field) must decode, not drop. The decoder has two producers to serve and
   only one of them nests its body under `payload`.
2. **A message whose `payload` is not valid JSON** must not raise out of
   `_decode_envelope`; it must be handled as undecodable and XACKed, with a
   WARNING, exactly as the current `envelope=` path does.
3. **A message whose `headers` field is the empty string** must yield `{}`,
   not a JSON decode error.
4. **A message on an unrecognised channel** must be XACKed and skipped
   without inserting a row and without bumping the watermark.
5. **A payload with no `version` field** must be accepted (the live producer
   emits none). A payload with an explicitly *different* `version` must be
   rejected. Only the second is a mismatch.
6. **A payload whose tool name lives in `tool_name` rather than `tool`** must
   still render non-placeholder content, so the embedding is not built from a
   row of `?` characters.
7. **Two subscriber processes running at once** must not fight over one
   consumer name; each gets its own, and each is removed on stop.

---

### Task 1: One consumer per process, removed on stop

**Files:**
- Modify: `akosha/ingestion/bodai_event_subscriber.py` (`__init__` ~L145,
  `start` ~L215, `stop` ~L228, `_run_loop` ~L351, `_auto_consumer_name`
  ~L407)
- Test: `tests/unit/test_bodai_event_subscriber.py`

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces: `BodaiToolInvocationSubscriber._consumer_name` is a `str` once
  `start()` has completed and before `_run_loop` begins. `stop()` issues
  `XGROUP DELCONSUMER` for that name against
  `(STREAM_NAME, self._consumer_group)`.

- [ ] **Step 1: Write the failing test**

Add to the existing `TestRunLoop` class in
`tests/unit/test_bodai_event_subscriber.py`:

```python
async def test_run_loop_uses_one_stable_consumer_name(self, subscriber) -> None:
    """Every xreadgroup in the loop must use the SAME consumer name."""
    seen: list[str] = []

    async def fake_xreadgroup(*, groupname, consumername, streams, count, block):
        seen.append(consumername)
        if len(seen) >= 3:
            subscriber._running = False
        return []

    subscriber._redis_client = MagicMock()
    subscriber._redis_client.xreadgroup = fake_xreadgroup
    subscriber._running = True
    subscriber._consumer_name = None

    await subscriber._run_loop(">")

    assert len(seen) == 3
    assert len(set(seen)) == 1, f"consumer name changed between reads: {seen}"
```

Also add, in the same class:

```python
async def test_two_subscribers_get_distinct_consumer_names(self) -> None:
    """Two processes must not share one consumer name."""
    first = BodaiToolInvocationSubscriber()
    second = BodaiToolInvocationSubscriber()
    assert first._auto_consumer_name() != second._auto_consumer_name()
```

and, in the `start`/`stop` coverage:

```python
async def test_stop_deletes_the_consumer(self, subscriber) -> None:
    """stop() must remove the consumer so a restart cannot accumulate."""
    client = MagicMock()
    client.xgroup_delconsumer = AsyncMock()
    subscriber._redis_client = client
    subscriber._consumer_name = "akosha-fixed123"
    subscriber._task = None

    await subscriber.stop()

    client.xgroup_delconsumer.assert_awaited_once_with(
        STREAM_NAME, subscriber._consumer_group, "akosha-fixed123"
    )
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `.venv/bin/pytest tests/unit/test_bodai_event_subscriber.py::TestRunLoop::test_run_loop_uses_one_stable_consumer_name -v`
Expected: FAIL with `AssertionError: consumer name changed between reads`

- [ ] **Step 3: Implement the stable name**

In `start()`, after `self._redis_client = client` and before
`self._task = asyncio.create_task(self._run_loop(resume_id))`, add:

```python
# One consumer per process, not per read. The previous code called
# _auto_consumer_name() inside the read loop, so every XREADGROUP
# created a new consumer and the group grew without bound.
self._consumer_name = self._consumer_name or self._auto_consumer_name()
```

In `_run_loop`, change the `xreadgroup` call's `consumername` argument from
`self._consumer_name or self._auto_consumer_name()` to `self._consumer_name`.

In `stop()`, after the client is closed, if `self._consumer_name` is set and a
client was present, issue
`await client.xgroup_delconsumer(STREAM_NAME, self._consumer_group, self._consumer_name)`
inside the existing `contextlib.suppress(Exception)` block, then set
`self._consumer_name = None`.

Update the `_auto_consumer_name` docstring from "Generate a unique consumer
name per process." to "Generate a unique consumer name. Called once per
process from ``start()``; never from inside the read loop."

- [ ] **Step 4: Run the test to verify it passes**

Run: `.venv/bin/pytest tests/unit/test_bodai_event_subscriber.py -v`
Expected: PASS (all tests in the file)

- [ ] **Step 5: Commit**

```bash
git add akosha/ingestion/bodai_event_subscriber.py tests/unit/test_bodai_event_subscriber.py
git commit -m "fix(ingestion): use one Redis consumer per process, not one per read"
```

---

### Task 2: Decode the shape the producer actually writes

**Files:**
- Create: `tests/fixtures/bodai_events_stream_entry.json`
- Modify: `akosha/ingestion/bodai_event_subscriber.py` (`_decode_envelope`
  ~L526)
- Test: `tests/unit/test_bodai_event_subscriber.py`

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces: `_decode_envelope(message_payload: dict[str, Any]) -> dict[str, Any] | None`
  additionally accepts the live shape and returns
  `{"topic", "payload", "event_id", "headers"}`. The topic for the live shape
  is the stream entry's `channel` field.

- [ ] **Step 1: Capture the live wire shape as a fixture**

Create `tests/fixtures/bodai_events_stream_entry.json` containing one real
entry captured from `bodai:events`, in this shape (values are the live
field names, not real session data):

```json
{
  "stream_entry_fields": ["channel", "headers", "payload"],
  "channel": "bodai.hooks.post-tool-use",
  "headers": "{}",
  "payload": {
    "agent_id": "agent-1",
    "agent_type": "claude-code",
    "event": "PostToolUse",
    "harness": "claude-code",
    "permission_mode": "default",
    "session_id": "session-1",
    "timestamp": "2026-11-20T04:30:00+00:00",
    "tool_name": "Read",
    "tool_input": {"file_path": "/tmp/example"},
    "tool_use_id": "toolu_01"
  }
}
```

- [ ] **Step 2: Write the failing tests**

Add to `tests/unit/test_bodai_event_subscriber.py`:

```python
class TestDecodeLiveWireShape:
    """The shape oneiric's Redis Streams adapter actually writes."""

    def test_nested_payload_shape(self, live_entry) -> None:
        envelope = subscriber._decode_envelope(live_entry)
        assert envelope is not None
        assert envelope["topic"] == "bodai.hooks.post-tool-use"
        assert envelope["payload"]["tool_name"] == "Read"
        assert envelope["headers"] == {}

    def test_flat_enqueue_shape(self) -> None:
        envelope = subscriber._decode_envelope(
            {"channel": "task.handoff_orphan", "task_id": "t-1", "outcome": "ok"}
        )
        assert envelope is not None
        assert envelope["topic"] == "task.handoff_orphan"
        assert envelope["payload"]["task_id"] == "t-1"

    def test_invalid_payload_json_is_undecodable(self) -> None:
        assert subscriber._decode_envelope(
            {"channel": "x", "payload": "{not json", "headers": "{}"}
        ) is None

    def test_empty_headers_string_yields_empty_dict(self) -> None:
        envelope = subscriber._decode_envelope(
            {"channel": "x", "payload": "{}", "headers": ""}
        )
        assert envelope is not None
        assert envelope["headers"] == {}
```

Add a `live_entry` fixture in the same file that reads
`tests/fixtures/bodai_events_stream_entry.json` and returns the
`{channel, headers, payload}` dict with `payload` re-serialised to a JSON
string, matching the stream's byte layout.

- [ ] **Step 3: Run the tests to verify they fail**

Run: `.venv/bin/pytest tests/unit/test_bodai_event_subscriber.py::TestDecodeLiveWireShape -v`
Expected: FAIL — `_decode_envelope` returns `None` for the live shape.

- [ ] **Step 4: Implement the live-shape branch**

In `_decode_envelope`, after the `envelope=` branch and **before** the
`{topic, payload_json, headers_json}` triplet branch, insert:

```python
# Live shape: oneiric's Redis Streams adapter writes exactly three
# stream-entry fields -- ``channel`` (the bus topic), ``payload``
# (JSON-encoded envelope) and ``headers`` (JSON-encoded headers).
# A producer using the flat ``enqueue`` path puts the body fields
# directly on the entry with no nested ``payload``.
if "channel" in message_payload:
    topic = str(message_payload.get("channel") or "")
    raw_payload = message_payload.get("payload")
    if isinstance(raw_payload, (str, bytes)) and raw_payload:
        try:
            body = json.loads(raw_payload)
        except (ValueError, TypeError) as exc:
            logger.warning(
                "akosha.bodai_subscriber: failed to parse payload field: %s", exc
            )
            return None
    else:
        body = {
            key: value
            for key, value in message_payload.items()
            if key not in ("channel", "headers")
        }
    if not isinstance(body, dict):
        body = {}
    raw_headers = message_payload.get("headers")
    headers: dict[str, Any] = {}
    if isinstance(raw_headers, (str, bytes)) and raw_headers:
        try:
            decoded_headers = json.loads(raw_headers)
        except (ValueError, TypeError) as exc:
            logger.warning(
                "akosha.bodai_subscriber: failed to parse headers field: %s", exc
            )
            return None
        if isinstance(decoded_headers, dict):
            headers = decoded_headers
    return {
        "topic": topic,
        "payload": body,
        "event_id": str(headers.get("event_id") or ""),
        "headers": headers,
    }
```

- [ ] **Step 5: Remove the shape no producer emits**

Delete the `{topic, payload_json, headers_json}` triplet branch and its
helper `_envelope_triplet` in the test file. Nothing in the fleet writes
that shape: the only writer to `bodai:events` is
`oneiric/adapters/queue/redis_streams.py:publish`, which writes
`{channel, payload, headers}`, and the flat `enqueue` path used by
`session_buddy`'s publisher writes `channel` plus one field per body key.
Update the `_decode_envelope` docstring to list the two accepted shapes
(`envelope=<JSON>` and `channel`/`payload`/`headers`) and drop the
"Legacy fallback" line.

Also update the module docstring in `session_buddy/mcp/events/bodai_events_publisher.py`
if it is in this repo — it is not, so instead record the producer contract in
this plan's "Global Constraints" and leave session-buddy alone.

- [ ] **Step 6: Run the tests to verify they pass**

Run: `.venv/bin/pytest tests/unit/test_bodai_event_subscriber.py tests/unit/ingestion/test_bodai_event_subscriber.py -v`
Expected: PASS

- [ ] **Step 7: Commit**

```bash
git add akosha/ingestion/bodai_event_subscriber.py tests/unit/test_bodai_event_subscriber.py tests/fixtures/bodai_events_stream_entry.json
git commit -m "fix(ingestion): decode the bodai:events wire shape the producer writes"
```

---

### Task 3: Stop dropping the messages Task 2 can now decode

**Files:**
- Modify: `akosha/ingestion/bodai_event_subscriber.py`
  (`TOOL_INVOCATION_TOPIC` ~L56, the topic filter ~L462, the version gate
  ~L596, `_build_content` ~L616)
- Test: `tests/unit/test_bodai_event_subscriber.py`

**Interfaces:**
- Consumes: the `topic` value produced by Task 2's live-shape branch
  (`channel`).
- Produces: a module-level
  `INDEXED_CHANNELS: frozenset[str] = frozenset({"websocket_tool_invocation", "bodai.hooks.post-tool-use"})`
  and a `_build_content(payload: dict[str, Any]) -> str` that renders the
  fields the live payload actually carries.

- [ ] **Step 1: Write the failing tests**

```python
class TestIndexedChannels:
    def test_live_hook_channel_is_indexed(self) -> None:
        assert "bodai.hooks.post-tool-use" in INDEXED_CHANNELS

    def test_unversioned_payload_is_accepted(self) -> None:
        # The live producer emits no "version" field.
        assert subscriber._index_envelope(
            {"tool_name": "Read", "event": "PostToolUse"}, event_id="1-0"
        ) is True

    def test_mismatched_version_is_rejected(self) -> None:
        assert subscriber._index_envelope(
            {"version": "99.0.0", "tool_name": "Read"}, event_id="1-0"
        ) is False

    def test_build_content_uses_live_field_names(self) -> None:
        content = subscriber._build_content(
            {"tool_name": "Read", "event": "PostToolUse"}
        )
        assert "tool=Read" in content
        assert "surface=PostToolUse" in content

    def test_unrecognised_channel_is_not_indexed(self) -> None:
        assert "task.handoff_orphan" not in INDEXED_CHANNELS
```

The `subscriber` fixture in this file already stubs the embedding service and
the HotStore, so `_index_envelope` returns `True` for an accepted payload and
`False` for a rejected one without touching the network. The existing
`topic mismatch` test in `_process_entry` coverage must be updated to assert
set membership against `INDEXED_CHANNELS` rather than inequality against
`TOOL_INVOCATION_TOPIC`.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/pytest tests/unit/test_bodai_event_subscriber.py::TestIndexedChannels -v`
Expected: FAIL — `INDEXED_CHANNELS` is not defined and the version gate
rejects the unversioned payload.

- [ ] **Step 3: Implement the channel set and the filter change**

Replace the `TOOL_INVOCATION_TOPIC` constant with:

```python
#: Channels whose payloads become HotStore rows. ``websocket_tool_invocation``
#: is the canonical name; ``bodai.hooks.post-tool-use`` is what
#: ``mahavishnu``'s hook bridge actually publishes today.
INDEXED_CHANNELS: frozenset[str] = frozenset(
    {"websocket_tool_invocation", "bodai.hooks.post-tool-use"}
)
```

Keep `TOOL_INVOCATION_TOPIC` as a module constant for the tests that import
it, defined as the canonical member of the set. Change the filter at ~L462
from `if topic != TOOL_INVOCATION_TOPIC:` to
`if topic not in INDEXED_CHANNELS:`.

- [ ] **Step 4: Implement the version gate change**

Change the gate at ~L596 from

```python
if payload.get("version") != SUPPORTED_SCHEMA_VERSION:
    return False
```

to

```python
# A payload with no "version" is unversioned, not mismatched: the live
# producer emits none, and rejecting it silently dropped every event.
# Only an explicitly different version is a mismatch.
version = payload.get("version")
if version is not None and version != SUPPORTED_SCHEMA_VERSION:
    return False
```

- [ ] **Step 5: Implement `_build_content` for the live fields**

Replace the body of `_build_content` so it renders the live field names and
falls back to the old ones, keeping the "websocket tool invocation:" prefix
so the poll and push paths stay byte-comparable:

```python
return (
    f"websocket tool invocation: "
    f"tool={payload.get('tool_name') or payload.get('tool') or '?'} "
    f"surface={payload.get('surface') or payload.get('event') or '?'} "
    f"result={payload.get('result') or payload.get('outcome') or '?'} "
    f"duration_ms={payload.get('duration_ms') or '?'} "
    f"error={payload.get('error', '')!r}"
)
```

- [ ] **Step 6: Run the tests to verify they pass**

Run: `.venv/bin/pytest tests/unit/test_bodai_event_subscriber.py tests/unit/ingestion/test_bodai_event_subscriber.py -v`
Expected: PASS

- [ ] **Step 7: Commit**

```bash
git add akosha/ingestion/bodai_event_subscriber.py tests/unit/test_bodai_event_subscriber.py
git commit -m "fix(ingestion): index the channel the hook bridge publishes"
```

---

### Task 4: A permanent stall must not read as `warming_up`

**Files:**
- Modify: `akosha/mcp/server.py` (the `knowledge_graph_feed` builder ~L2098)
- Test: `tests/unit/test_wave5_lifespan_wiring.py`

**Interfaces:**
- Consumes: `cycles_total` and `last_updated_timestamp` already present on
  the feed state.
- Produces: `knowledge_graph_feed["stalled"] -> bool` and a
  `reason_codes` entry of `"feed_stalled"` when the feed has produced
  nothing across many cycles.

- [ ] **Step 1: Write the failing test**

Add to `tests/unit/test_wave5_lifespan_wiring.py`:

```python
def test_knowledge_graph_feed_reports_a_stall_not_a_warmup(self) -> None:
    result = _feed_states_for_test(cycles_total=3055, entities_count=0)
    kg = result["knowledge_graph_feed"]
    assert kg["stalled"] is True
    assert "feed_stalled" in kg["reason_codes"]
```

Use whatever helper that test module already uses to build feed states; if
none exists, construct the feed state object directly with
`cycles_total=3055` and `entities_count=0`.

- [ ] **Step 2: Run the test to verify it fails**

Run: `.venv/bin/pytest tests/unit/test_wave5_lifespan_wiring.py -v -k stalled`
Expected: FAIL — no `stalled` key.

- [ ] **Step 3: Implement the stall signal**

In `akosha/mcp/server.py`, next to the `knowledge_graph_feed` builder, add a
module-level threshold and set the flag:

```python
#: A feed that has cycled this many times without producing a single
#: entity is not warming up. It is stalled, and saying "warming_up" for a
#: six-week stall is how a severed ingestion pipeline stayed invisible.
FEED_STALL_CYCLES_THRESHOLD = 100
```

Then in the builder:

```python
kg_dict["stalled"] = bool(
    kg_dict.get("cycles_total", 0) >= FEED_STALL_CYCLES_THRESHOLD
    and kg_dict.get("entities_count", 0) == 0
)
if kg_dict["stalled"]:
    kg_dict.setdefault("reason_codes", []).append("feed_stalled")
```

**The gate, and why the rule above is not the whole rule.** The signal
is GATED on the producer having received input that should have landed.
Without the gate, a healthy and idle system crosses the threshold and
answers `/health` 503 for a feed that is empty because there was
nothing to ingest. The gate reads
`source_producer_entries_candidate`, which counts the entries on an
INDEXED channel plus the entries whose channel could not be read — the
two cases where a row was owed. It does NOT count "entries read": the
hook bridge publishes six channels and only one of them is indexed, so
gating on the raw read count would report a healthy pipeline that is
correctly dropping traffic as stalled.

The implementation is `apply_stall_status(feed, producer_active=...)`,
which moves the STATUS (not just a boolean) from `warming_up` to
`degraded`, because the status is the field the aggregator and the
operator read.

- [ ] **Step 4: Run the test to verify it passes**

Run: `.venv/bin/pytest tests/unit/test_wave5_lifespan_wiring.py tests/integration/test_health_aggregator.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add akosha/mcp/server.py tests/unit/test_wave5_lifespan_wiring.py
git commit -m "feat(health): report a stalled feed as stalled, not warming up"
```

---

### Task 5: A repeatable recovery script for the group and the backlog

**Files:**
- Create: `scripts/recover_event_stream.py`
- Test: `tests/unit/test_recover_event_stream.py`

**Interfaces:**
- Consumes: nothing from earlier tasks; it operates on the live Redis stream.
- Produces: a CLI with `--dry-run` (default) and `--apply`, exiting
  non-zero if the post-condition does not hold.

- [ ] **Step 1: Write the failing tests**

```python
class TestPlanRecoverEventStream:
    def test_dry_run_reports_without_deleting(self, fake_redis) -> None:
        result = plan_recover_event_stream(fake_redis, dry_run=True)
        assert result["leaked_consumers"] == 445943
        assert result["deleted"] == 0
        assert fake_redis.deleted == []

    def test_apply_deletes_only_pending_zero_leaked_consumers(self, fake_redis) -> None:
        result = plan_recover_event_stream(fake_redis, dry_run=False)
        assert result["deleted"] == 2
        assert fake_redis.deleted == ["akosha-aaa", "akosha-bbb"]

    def test_apply_preserves_foreign_consumers(self, fake_redis) -> None:
        plan_recover_event_stream(fake_redis, dry_run=False)
        assert "test-consumer" not in fake_redis.deleted
```

Use a `MagicMock` redis double exposing `xinfo_consumers`,
`xinfo_groups`, and `xgroup_delconsumer`.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/pytest tests/unit/test_recover_event_stream.py -v`
Expected: FAIL — the module does not exist.

- [ ] **Step 3: Implement the script**

`scripts/recover_event_stream.py` must:

1. Read the consumer list for `bodai:events` /
   `akosha-tool-invocation-indexers`.
2. Select consumers whose name starts with `akosha-` **and** whose `pending`
   is 0.
3. With `--apply`, delete exactly those via `XGROUP DELCONSUMER`, in
   pipelined batches of 5000.
4. Never delete the group, never delete a consumer with `pending > 0`, and
   never touch a consumer whose name does not start with `akosha-`.
5. Print a summary: `leaked_consumers`, `deleted`, `preserved_pending`,
   `pending_before`, `pending_after`, and the wall-clock seconds that
   `XINFO CONSUMERS` took before and after.

Expose a module-level
`plan_recover_event_stream(redis_client: Any, *, dry_run: bool) -> dict[str, Any]`
so the tests can drive it without touching the network.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/pytest tests/unit/test_recover_event_stream.py -v`
Expected: PASS

- [ ] **Step 5: Run it dry against the live stream**

Run: `.venv/bin/python scripts/recover_event_stream.py --dry-run`
Expected: prints the counts and deletes nothing.

- [ ] **Step 6: Commit**

```bash
git add scripts/recover_event_stream.py tests/unit/test_recover_event_stream.py
git commit -m "feat(scripts): add a repeatable bodai:events consumer recovery tool"
```

---

### Task 6: End-to-end verification and the operational runbook

> **Status: RAN on 2026-11-21.** The service was restarted onto the
> merged code, the group was cleaned, the backlog was replayed, and the
> results were observed. The run passed its primary criteria and
> exposed three defects the code-level fixes had not covered; those are
> fixed in `665bbe6` and recorded under "Verification runbook" below.
> The live service still runs the pre-fix code until it is restarted
> onto that commit.

**Files:**
- Modify: `docs/plans/2026-11-20-akosha-event-pipeline-recovery.md`
  (append a "Verification runbook" section with the observed results)

- [x] **Step 1: Restart the Akosha MCP server**

The running process started 2026-11-16 09:57:07 and never loaded the
2026-11-17 warm-tier commits (`7b04dc5`, `f2ff08b`, `c118972`, `9b6e5f9`) or
this fix. Restart it so it runs the merged code.

Run: `launchctl kickstart -k gui/$(id -u)/com.mcp.akosha`.

The launchd label is **`com.mcp.akosha`**, not `com.mahavishnu.akosha` —
the earlier draft of this step named a label that does not exist. The
plists are generated by `mcp-ctl` into `~/Library/LaunchAgents/`, and the
one for this server is `com.mcp.akosha.plist`.

#### Verified preconditions (measured 2026-11-21, before the run)

Everything below was read from the live system, not inferred.

| Fact | Value |
|---|---|
| Subscriber consumer name (fixed) | `akosha-<12 hex>` — resolved ONCE per process, not per read |
| Group consumers before the run | **36,287** and still growing at **0.5/second** |
| Group pending | 676 |
| Group `last-delivered-id` | `1795254219344-0` (the stream's last id) |
| Stream length | 14,328 entries |
| `XINFO CONSUMERS` cost | **10.8 s** |

**The consumer leak and the read timeouts are one feedback loop.** The
old code generated a fresh `uuid4` consumer name *inside* every
`XREADGROUP`, so each read registered a new consumer. That made the
group's consumer table enormous, which made `XREADGROUP` itself slower
than redis-py's 5-second default socket timeout, so every read aborted
with `TimeoutError` — and each aborted read had already registered its
consumer on the server. The leak fed the timeout, and the timeout fed
the leak. The fix breaks the loop by resolving one name per process.

That is also why the cleanup must follow the restart, not precede it:
while the old process is running it re-registers a consumer per read,
so deleting the leaked ones first would be undone immediately — and
would make the old reads *succeed*, which would make the leak faster.

**The recovery script could not run until this commit.** `scripts/recover_event_stream.py`
builds its client with `redis.Redis.from_url(url)` and no `socket_timeout`,
so it inherited redis-py 8's 5-second default — against a group whose
`XINFO CONSUMERS` takes 10.8 seconds. The tool reported "cannot read the
consumer list ... Timeout reading from socket", changed nothing, and
exited non-zero. Fixed in `0304857` (600-second socket timeout, 10-second
connect timeout). With it, the dry-run reports:

```
leaked_consumers   : 36001
would_delete       : 35890
preserved_pending  : 111
pending_before     : 676
pending_after      : 676
XINFO seconds      : 10.758 -> 1.258
```

The 111 consumers that still own pending messages are preserved:
`XGROUP DELCONSUMER` destroys those messages — they are neither acked
nor redelivered, and `XAUTOCLAIM` can no longer recover them.

- [x] **Step 2: Backfill the backlog — MANDATORY, not optional**

The group already delivered every entry (`lag=0`), so the 13,878 backlog
entries were consumed and acked without being indexed. Reset the group's
start position so the fixed subscriber re-reads them, and claim the messages
still pending on the old consumers:

```
XGROUP SETID bodai:events akosha-tool-invocation-indexers 0
XAUTOCLAIM bodai:events akosha-tool-invocation-indexers <fixed-consumer-name> 0 0
```

`XGROUP SETID` is what makes this work: the subscriber reads with `>`,
and `>` delivers everything after the group's `last-delivered-id`. Moving
that id back to `0` puts the whole back-catalogue back in front of the
reader. (An explicit read id could not have done this — see round two,
finding 9.)

**This step is not optional, and the health payload will not tell you to
do it.** After Step 1 the group is at the end of the stream, so the
subscriber reads NOTHING until the back-catalogue is put back in front of
it. A subscriber that reads nothing has landed nothing, so the stall
signal's gate stays closed and `/health` answers "warming_up" with
`entities_count: 0` — indistinguishable from a cold start. The signal
fires on "the producer received input that should have landed and nothing
landed", and after a restart on an already-drained group, the producer
has received nothing at all.

That is not a defect in the signal; it is what "idle" looks like. But
the operator has to run Step 2 by hand, so the plan says so here.

Use `scripts/recover_event_stream.py --apply` for the consumer cleanup in the
same pass.

- [x] **Step 3: Verify the pipeline end to end**

Run each of these and record the output in the plan's Verification runbook:

```bash
# 1. The consumer count must stop growing.
.venv/bin/python scripts/recover_event_stream.py --dry-run

# 2. The health feed must report a populated graph. This is THE
#    criterion: the fix's rows land in the HOT tier's `conversations`
#    table, and the knowledge-graph refresh reads them from there.
#    (The warm tier's `vectors_conversations` is written only by the
#    hot->warm migration, which this plan does not enable -- see the
#    header note. It is expected to stay at 0 and is NOT a criterion.)
curl -s http://127.0.0.1:8682/health | python3 -m json.tool | grep -A12 knowledge_graph_feed

# 3. No new read failures. The counter is in the payload, so it
#    cannot rot the way a log-file grep does.
curl -s http://127.0.0.1:8682/health | python3 -m json.tool \
  | grep -E 'source_producer_(read_errors_total|entries_candidate|entries_indexed)'
```

Expected: consumer count stable or falling; `knowledge_graph_feed` showing
`entities_count > 0` and `stalled: false`;
`source_producer_read_errors_total` at zero;
`source_producer_entries_indexed` above zero (rows actually landed) and
`source_producer_entries_candidate` at least as large (entries that owed a
row).

To read the warm tier's migration table as a sanity check (not a
criterion -- it stays empty until aging is enabled):

```bash
.venv/bin/python -c "
import asyncio, asyncpg
async def main():
    c = await asyncpg.connect('postgresql://postgres@127.0.0.1:6432/akosha_pgvector')
    print('vectors_conversations =', await c.fetchval('select count(*) from vectors_conversations'))
    await c.close()
asyncio.run(main())
"
```

The hot tier's `conversations` table cannot be counted from outside the
process: DuckDB takes a write lock on `hot.db` and a second process
opening the same file fails with "Conflicting lock is held". The health
endpoint is the operator-facing view of that table, which is why it is
the criterion.

- [x] **Step 4: Record the results and commit**

Append the observed output to the plan file, then:

```bash
git add docs/plans/2026-11-20-akosha-event-pipeline-recovery.md
git commit -m "docs(plan): record the event-pipeline recovery verification"
```

---

## Integration Contract

**Triggered from** — `python -m akosha mcp start`, via
`akosha/mcp/server.py:_lifespan_start_event_subscriber` ->
`akosha/ingestion/subscriber_wiring.py:build_event_subscribers` ->
`BodaiToolInvocationSubscriber.start()`. The upstream entry point that
produces the messages is `mahavishnu`'s hook bridge, which calls
`oneiric.adapters.queue.redis_streams.RedisStreamsAdapter.publish()`.

**Returns to / updates** — the `conversations` table in the Akosha hot tier
(DuckDB), which the `kg_refresh` loop reads to build the knowledge graph.
Rows that were previously dropped at the decoder now reach HotStore; the
watermark advances to the highest message id indexed, and it is where a
NEWLY CREATED consumer group starts so that a group created over an
already-indexed stream does not replay the back-catalogue. The restart
position itself is the consumer group's `last-delivered-id` — the
subscriber reads with `>`, because an explicit read id is a PEL read and
can never deliver a message nobody has seen. See "Round two", finding 9.
The warm tier's `vectors_conversations` table is NOT updated by this fix:
its only writer is the hot→warm migration (`AgingService._migrate_batch`),
which this plan does not enable. See the header note.

**Demonstrable by** — `curl -s http://127.0.0.1:8682/health` shows
`knowledge_graph_feed` with `entities_count > 0` and `stalled: false`;
`tests/unit/test_bodai_event_subscriber.py` passes with the live wire shape
pinned by `tests/fixtures/bodai_events_stream_entry.json`;
`scripts/recover_event_stream.py --dry-run` reports a stable consumer count.

**Rollback signal** — `knowledge_graph_feed.stalled` flipping back to `true`,
or `knowledge_graph_feed.entities_count` falling back to 0, or
`knowledge_graph_feed.source_producer_read_errors_total` rising above zero.
(The signal used to count `xreadgroup error` lines in
`~/.mahavishnu/logs/bodai-subscriber.log` — that is MAHAVISHNU's
subscriber's log, not this one's, so the counter read zero forever once
that file rotated or that component was fixed. A counter in the payload
cannot rot.)
Rollback is a plain revert of the merge commit plus a restart of the Akosha
MCP server; no data migration is involved, so the revert is safe.

**Observability added** — `knowledge_graph_feed.stalled` (bool) and the
`feed_stalled` reason code in the `/health` payload;
`knowledge_graph_feed.source_producer_entries_total` / `_entries_candidate`
/ `_entries_indexed` / `_read_errors_total`, which separate "the producer
read nothing" from "the producer read input and landed nothing";
the `BodaiToolInvocationSubscriber` log line naming the resolved consumer name
and the channel it indexed; `scripts/recover_event_stream.py --dry-run`
reporting leaked-consumer count and `XINFO CONSUMERS` latency as a
standing check on the leak's recurrence.

---

## Out of scope, and why

- **The embedding model, width and provider.** Owned by a separate live
  session. Re-ingestion in Task 6 uses whatever is committed today; if the
  width decision changes afterwards, that is a follow-up re-embed, not a
  change to this plan.
- **An upsert in the hot store.** Re-indexing an event that is already
  stored raises a primary-key violation, which the subscriber treats as an
  indexing failure: it retries three times and then drops the message with
  a warning. That is the safe outcome — the row IS in the store, so nothing
  is lost — but it is noisy. Making it an upsert means changing
  `oneiric`'s `DuckdbHotStore.insert`, which is a cross-repo change with
  its own publish gate, and it is not needed to make the pipeline deliver:
  the live back-catalogue was never indexed (the knowledge graph is empty
  precisely because nothing was), so Task 6's backfill inserts every row
  for the first time. The path that can hit it is a message indexed
  successfully and then orphaned before its XACK — rare, and it drops
  rather than corrupts.
- **`mahavishnu`'s own `bodai_subscriber`.** It logs
  `redis.exceptions.TimeoutError: Timeout reading from localhost:6379` into
  `~/.mahavishnu/logs/bodai-subscriber.log`, which reached 373 MB and is
  still growing at roughly 5 KB per 20 seconds. That file is not in this
  lane: the subscriber is `mahavishnu/core/events/bodai_subscriber.py` and
  its Redis client is created by
  `mahavishnu/core/events/bodai_subscriber.py:_create_redis_client`, which
  calls `aioredis.from_url(redis_url, decode_responses=False)` with no
  explicit `socket_timeout` while blocking for
  `DEFAULT_XREADGROUP_BLOCK_MS = 5000`. A socket timeout shorter than the
  block timeout would abort every read. Flagged to the mahavishnu lane
  owner; the group cleanup in Task 5 reduces the pressure but does not
  address that configuration.
- **The `bodai:events` stream retention.** The stream holds 13,878 entries
  with no trimming. Whether it should be capped is a separate decision.
- **The `bodai-default` and `oneiric` consumer groups**, both of which have
  zero consumers and a lag equal to the full stream length. Nothing
  subscribes to them in the current code.

---

## Ensemble review findings, and what was done about them

The trunk-based agent-review workflow's ensemble (a domain specialist, a
code-quality specialist and a random generalist) reviewed this branch and
returned `needs_adjustment` on all three. Every finding was real; all of them
are fixed on this branch. Recording them here because the plan is the
contract, and a reader should not have to reconstruct the review from the
commit history.

### From the code-quality reviewer (the recovery script)

1. **An unreadable group reported zero.** A Redis timeout was swallowed into
   an empty consumer list, so the tool printed `leaked_consumers: 0` and
   exited 0 — while the group held 14,950 leaked consumers. Not
   hypothetical: the live dry-run printed "cannot read the consumer list ...
   treating it as empty" during the work. A group that does not exist is now
   distinguished from a group that could not be read; the unreadable case
   raises `ConsumerListUnavailable` and `main()` exits 1.
2. **An unknown pending count was treated as zero.** `XGROUP DELCONSUMER`
   destroys every message still pending for the consumer, so a count the tool
   could not read must preserve the consumer. `_pending()` now returns
   `int | None` and "unknown" preserves.
3. **The post-condition was vacuous.** `deleted` counted the size of the
   batch queued, so `main()` compared a number against itself and could
   never fire. The count is now taken by re-reading the group. It could not
   have come from `XGROUP DELCONSUMER`'s return value: that is the number of
   PENDING ENTRIES destroyed, which is 0 for exactly the empty consumers
   this tool deletes best.

### From the domain specialist (the subscriber)

4. **A non-dict payload reached the indexer.** The canonical `envelope=`
   blob did not coerce its `payload` to `{}` while the two newer shapes
   did. A payload that was a string raised `AttributeError` inside
   `_index_envelope`, was never ACKed — and so became exactly the poison
   message below. All three shapes coerce now, and so do the headers.
5. **A poison message could starve the pipeline.** With the stable consumer
   name (the leak fix), a message that is never ACKed stays in that
   consumer's PEL and every `XREADGROUP` re-delivers it. Pending entries are
   ordered BEFORE new ones, so once ten of them fill the `count=10` batch
   every new message is starved behind them — the pipeline stops, with no
   error anywhere and no log line that says so. The retries are now bounded
   at `MAX_INDEX_RETRIES = 3`; after that the message is ACKed and dropped
   with a warning. A lost row is visible; a starved pipeline is not.

### From the test-coverage reviewer

6. **The stall signal was implemented but not wired.** Both test files
   hand-built a dict and called `apply_stall_status` directly, so deleting
   either call site in `_default_health_probe` would have left the suite
   green while `/health` went back to reporting a six-week starvation as
   `warming_up`. The probe cannot be started in a test (the OTLP receiver
   binds the canonical `:4318` unconditionally), so the wiring is pinned
   with the AST-guard idiom this repo already uses for the tool-profile
   guard: the guard names the feed dicts the signal must reach.
7. **The signal reached one feed of two that are starving.**
   `local_traces_feed` has cycled 46,571 times with zero entities while its
   OTLP receiver ingested 42,985 batches. The signal reaches it now.
   `mcp_tool_call_feed` is deliberately excluded: its emptiness can be
   legitimate (it counts spans carrying a `task_class` no instrumented tool
   call may set), and a false `degraded` on a healthy feed is worse than a
   missing signal. The guard says so, so the exclusion is a decision rather
   than an oversight.
8. **`_release_consumer`'s unreadable-pending branch was untested.** It is
   now: an unreadable pending count and a consumer that still owns messages
   both keep the consumer registered.

### One defect the review did not find, and the live service did

The recovery script's selector tested `str(entry["name"]).startswith("akosha-")`.
Redis returns the consumer name as BYTES, and `str(b"akosha-x")` is
`"b'akosha-x'"` — which starts with a quote. So the selector matched
NOTHING, and the tool reported `leaked_consumers: 0` for a group holding
14,950 leaked consumers. The report was not wrong about the group; it was
wrong about what it had been asked to look at, and it looked certain while
being blind.

It was found by running the tool against the live stream, not by reading
it: the first live dry-run printed zero leaked consumers for a group that
had visibly grown since the earlier cleanup. The fix decodes the name
first, and three tests pin it.

### What the live service showed after the earlier cleanup

The first cleanup removed 446,049 consumers. The service then kept leaking
one every ~35 seconds, because the running process still had the pre-fix
code — and every one of those reads timed out, minting another consumer
before it failed. The count at the time of writing: **15,171 leaked
consumers, 15,064 deletable, 107 preserved because they still own
messages, 672 pending messages untouched.** The count is only stopped by
restarting the service on the merged code, which is Task 6 Step 1.

---

## Round two: three more defects, found while fixing the first two

The retry and health fixes made a batch index CONCURRENTLY, and that
exposed two races — plus a defect that concurrency had been hiding for
months.

### 9. The resume path could never deliver a message

The subscriber resumed by reading with an EXPLICIT id — the watermark
plus one. Redis documents what an explicit id does:

> Any other ID, that is, 0 or any other valid ID or incomplete ID (just
> the millisecond time part), will have the effect of returning entries
> that are pending for the consumer sending the command with IDs greater
> than the one provided. So basically if the ID is not `>`, then the
> command will just let the client access its pending entries.

An explicit id is a PEL read. It replays the CALLING consumer's own
unacknowledged entries, and it can never deliver a message nobody has
seen. A restarted process has a new consumer name and therefore an
empty PEL, so it reads nothing, forever.

Verified four ways against Redis 7.4 (live) and fakeredis (the test
double, which implements the same semantics):

| probe | result |
|---|---|
| fresh group, 3 undelivered messages, read with `T-0` | empty |
| the same group, read with `>` | all three |
| consumer X reads `T-0` with `>`, a NEW message is published, X reads `T-0` again | empty |
| the group's `last-delivered-id` after an explicit read | unchanged |

The last row is the decisive one: the explicit read did not advance the
group either, so it consumed nothing at all.

This never showed up in production because the live subscriber had no
watermark row — nothing had ever been indexed, so it read with `>` and
worked. The moment the first index succeeded and wrote a watermark,
the NEXT restart would have read with an explicit id and gone
permanently deaf: a subscriber that reports itself running, consumes
nothing, and writes a watermark that proves it is alive. Task 6 Step 1
is exactly that restart.

The fix: the read always asks for NEW messages. The consumer group's
own `last-delivered-id` is the durable position — it outlives the
process, and `>` delivers everything after it. The watermark's job
changes: it is now where a NEWLY CREATED group starts (so a group
created over an already-indexed stream does not replay the
back-catalogue) and the record of how far the pipeline has got.

The watermark is also read by primary key now, not by vector search.
The row carries a zero-vector embedding, and a zero-vector query
against the HNSW index is degenerate — the row is not reliably in the
result set, so a search can miss a watermark that is right there.

### 10. The watermark write was neither atomic nor monotonic

`_update_watermark` was a DELETE followed by an INSERT on the same
primary key, with the INSERT awaited. Two concurrent updates
interleave — DELETE, DELETE, INSERT, INSERT — into a duplicate-key
error that leaves the row holding an arbitrary id. The batch became
concurrent, so this stopped being theoretical: the test log carried
`Duplicate key "conversation_id: __bodai_subscriber_watermark__" violates
primary key constraint`.

The watermark also has to be MONOTONIC — it is "how far the pipeline
has got", so it holds the highest message id indexed. Two things move
it backwards: a reclaimed pending entry carries an OLD id, and a slow
consumer can finish after a faster one.

Both are fixed at the write: an `asyncio.Lock` serialises the pair,
and the candidate id is compared against the persisted one first.

### 11. The websocket flag vetoed the only producer

`build_event_subscribers` returned `None` unless the WEBSOCKET
subscriber was enabled — and the websocket subscriber's own indexing
path was removed in 2026-09-26. It now only orchestrates the Redis
push subscriber. So the flag that gated the stack gated nothing: an
operator who enabled the producer and disabled the orchestrator got no
producer at all, with nothing in the logs to say why.

Either flag now builds the stack, and the push subscriber is present
when its own flag is on.

---

## Round three: what the third ensemble review found

The third ensemble (a domain specialist, a code-quality specialist and a
random generalist) reviewed the branch after round two and returned
`needs_adjustment` on all three. Every finding below was real. They are
recorded here because the plan is the contract, and because two of them
are about the plan lying to its reader.

### 12. The watermark's own read was not safe against a concurrent write

The monotonicity guard added in round two reads the watermark row and
compares it with the candidate id. The read was
`conn.execute(SELECT ...).fetchone()` on the shared DuckDB
connection — and the substrate's `_SerializedConnection` holds its
threading lock only for the `execute` itself. `fetchone` re-acquires
it afterwards, and a write that lands in that gap REPLACES the
connection's current result.

So a concurrent insert — which the concurrent batch processing makes
the normal case — could make the read return the insert's row instead
of the watermark's. `_message_id_from_metadata` then reads `None`, the
monotonicity guard is skipped, and the watermark moves backwards: the
exact defect round two said it had fixed, silently.

The read now holds the STORE's lock for the whole execute-then-fetch
pair. Every operation that goes through the HotStore's API takes that
lock, so holding it makes the connection quiescent. This is the pattern
`akosha/storage/metrics_query.py` already uses for its health-probe
queries.

### 13. "Atomic" did not mean all-or-nothing

The round-two write was a DELETE followed by an awaited INSERT. If the
INSERT failed, the row was already deleted — so the watermark was lost,
and a newly created consumer group would start at `"0"` and replay the
whole back-catalogue into a primary-key collision storm.

The write is now a single `UPDATE` of the row's `metadata` and
`timestamp` columns. It cannot fail half-way, and it cannot raise a
duplicate-key error because the primary key is not being written. The
INSERT only happens the first time, when the row does not exist yet.

The UPDATE names two columns rather than the whole row on purpose: the
`conversations` table belongs to the substrate, its DDL is scheduled to
change (Phase 0.7 of the warm-tier plan rebuilds it), and a subscriber
that hard-codes an eight-column INSERT would break on that rebuild.

### 14. `_is_newer` accepted an id it could not compare

The rule was "if either side does not parse, accept the candidate". That
is wrong in one direction: an unparsable CANDIDATE would overwrite a
good watermark with garbage, and the next read would reject the row as
malformed and report "no watermark" — destroying the progress record
and, with it, the start position a new group would use.

The rule is now asymmetric: an unparsable candidate is REJECTED, an
unparsable current is IGNORED (a valid id beats garbage), and equality
is not an advance.

### 15. The stall signal's counter measured the wrong thing

The signal was gated on `entries_consumed` — every entry READ. The
hook bridge publishes six channels and only one of them is indexed, so
a stream carrying only non-indexable traffic would report a HEALTHY
pipeline as STALLED. The gate now reads `entries_candidate`: the
entries on an indexed channel plus the entries whose channel could not
be read — the two cases where a row was owed.

The payload publishes all three numbers (`entries_total`,
`entries_candidate`, `entries_indexed`), so "the producer read
nothing", "the producer read input and landed nothing" and "the
producer landed rows" are three different readings.

### 16. The plan described a stall rule the code did not implement

Task 4 Step 3 specified the rule as unconditional. The gate was added
after the ensemble review and was recorded nowhere. A reader of the
plan would believe `/health` degrades on a starved feed; the code does
not, in the most likely state. Task 4 now states the gate, and says
which counter it reads and why the raw read count is the wrong one.

### 17. The rollback signal counted another component's log

It counted `xreadgroup error` lines in
`~/.mahavishnu/logs/bodai-subscriber.log` — MAHAVISHNU's subscriber's
log, not this one's. The plan's own out-of-scope list says that file
"is not in this lane". So the counter read zero forever once that file
rotated or that component was fixed, and akosha's own read failures
were covered by no check at all.

The payload now publishes `source_producer_read_errors_total`, and the
rollback signal reads that.

### 18. The docstrings still described the old watermark

The module docstring, `start()`'s docstring and the Integration
Contract all said the watermark was the resume point. It is not — the
consumer group's `last-delivered-id` is. That is not a wording nit: a
maintainer who trusted the docstring would conclude the `>` read was a
regression and "restore" the explicit-id resume, reintroducing the deaf
reader.

### 19. The config told the operator to enable the wrong flag

`akosha/config.py` said the websocket block "is the ONLY producer of the
`conversations` table". It is not — the Redis push subscriber is, and
the websocket subscriber's own indexing path was removed in 2026-09-26.
An operator who followed that docstring enabled the flag that does
nothing and left the graph empty. The docstring is corrected, the
wiring carries the reason in a comment, and the health payload's
`event_subscriber_enabled` now reflects EITHER flag, so it can no
longer say "enabled: false" next to "running: true".

### 20. The end-to-end suite ran on a wire shape nobody writes

The plan's Task 2 Step 5 says to delete the
`{topic, payload_json, headers_json}` triplet: nothing in the fleet
writes it — the only writer to `bodai:events` is oneiric's Redis
Streams adapter, which writes `channel` / `payload` / `headers`, and
`payload_json` appears nowhere in the fleet's production code.

The code kept it, and the end-to-end suite ran on it. So the branch's
headline claim — "the fixed subscriber indexes the live shape and
writes a watermark" — was demonstrated only at the decode level, never
through the read loop.

The triplet branch is deleted, the end-to-end helper emits the live
shape, and there is now a test that xadds the CAPTURED live entry into
fakeredis and asserts both a `conversations` row and a watermark row.

### 21. The watermark row was in the knowledge graph

The plan says the reserved id "must be filtered out at query time". The
query that feeds the graph (`query_traces`) did not filter it, so the
graph carried a phantom entity for the watermark — and the plan's
success criterion (`entities_count > 0`) was satisfiable by a
bookkeeping row that records no event. The reserved id now lives in
`akosha.models` (the one module both the writer and the query layer
can import) and `query_traces` excludes it.

### 22. No test pinned the branch's own headline promise

"Newly created group starts at the watermark" had no end-to-end test —
only a mock assertion that the id was forwarded to `xgroup_create`.
Reverting it to `"0"`, or passing the watermark to the READ instead of
to the group, would still have passed the suite.

There is now a fakeredis test: a stream with a message below the
watermark and one above it, a subscriber on a group that does not exist
yet, and the assertion that only the newer message is indexed.

### 23. Three things the review found and this plan does NOT fix

All three are recorded so the next reader knows they were considered and
why they were left:

- **The health payload cannot tell "the reader is deaf" from "there is
  nothing to read".** After a restart on an already-drained group, the
  subscriber reads nothing until the back-catalogue is put back in front
  of it, and a subscriber that reads nothing has landed nothing, so the
  stall signal's gate stays closed. The plan's Task 6 Step 2 says so in
  capitals instead. The general fix — publishing the group's LAG and
  degrading when `lag > 0` while nothing is read — does not work here:
  in the state that matters the group's lag is ZERO (the entries were
  delivered long ago) and the unread entries are in the OLD consumers'
  pending lists, not in the lag. A real "the reader is deaf" signal
  needs the pending count and the group's position, and it has to
  distinguish "the previous process left entries pending" from "this
  process is not reading them" — which needs the reclaim sweep's
  bookkeeping, not the group's lag.
- **The stall signal cannot fire once the graph has an entity.**
  `entities_count` is the size of the in-process graph builder, which is
  cumulative and never cleared, so `entities_count == 0` can never hold
  again in that process. A pipeline severed MID-LIFE keeps its entities,
  keeps the status healthy, and is invisible. Fixing that means basing
  the signal on "the producer has landed nothing RECENTLY" rather than
  "the graph has never had an entity" — a different signal, with its own
  threshold and its own false-positive surface. It belongs to the
  health-feed work, not to this repair.
- **A duplicate-key storm on a repeated backfill.** The plan argues
  the backfill "inserts every row for the first time" because the graph
  is empty. That holds the FIRST time. A second `XGROUP SETID` — or a
  SETID after a partial backfill — re-delivers already-indexed
  messages, each raising a primary-key violation, each retried three
  times before being dropped. The hot store's `insert()` has no
  upsert, and adding one is a change to the substrate (see the
  out-of-scope list). The cost is three wasted embeddings per
  re-delivered message, and a warning per message.

## Round four: the sweep that could not run

The redis-specialist's review of the third round found a defect that the
first three rounds had missed, and it is the same class as the ones the
second round found: a mechanism that exists, is correct, is tested
directly — and is never reached by the runtime.

### 24. The reclaim sweep was gated on traffic

`XREADGROUP` with `>` never returns a message that is already pending
for this consumer. That is the documented behaviour, and it is why
`XAUTOCLAIM` exists: the sweep is the ONLY path that can retry a
message whose indexing failed.

The sweep ran only after a **non-empty** `xreadgroup` response:

```python
if not response:
    await asyncio.sleep(0)
    continue

await self._process_response(response, client=client)
await self._retry_pending(client)
```

On a quiet stream the read blocks for `block_ms` and returns empty, so
the loop took the `continue` and the sweep never ran. The consequence
is the exact failure the sweep was added to prevent: a message whose
indexing failed sat in the PEL until the **next** message arrived. On
a low-traffic stream that is minutes or hours; on a stream that has
stopped, for ever — never retried, never dropped, and the retry cap
never reached. The counter stalls at 1 and the drop path is dead code
that the tests nevertheless exercised.

The tests did not catch it because they called `_retry_pending`
**directly**. A unit test of the sweep proves the sweep works. It
cannot prove the loop reaches it. Only a test that drives `_run_loop`
and asserts the sweep happened can do that, which is why the three new
tests are written against the loop and not against the sweep.

**The fix.** The sweep runs on both paths. It is rate-limited to once
per `RETRY_CLAIM_MIN_IDLE_MS`, because `XAUTOCLAIM` can only reclaim an
entry that has been pending at least that long — sweeping more often
cannot reclaim anything new, and one round trip per idle poll would be
forty a minute against a shared Redis. The first sweep runs immediately:
`_last_sweep_at` starts at zero and the monotonic clock does not.

**The generalisation.** A mechanism that is only reachable from one
branch of a loop is a mechanism that a quiet stream never uses. The
question to ask of any retry, sweep, or recovery path is not "does it
work when it runs" but "what happens when the thing it reacts to stops
arriving" — and for a retry path, the answer is usually the worst one.

### 25. The sweep threw away the cursor Redis handed it

`XAUTOCLAIM` replies with the id the **next** sweep should resume from.
The code discarded it and hard-coded `start_id="0-0"` on every call.

That matters because of how the scan is budgeted. Redis 7.4's
`xautoclaimCommand` sets `attempts = count * attempts_factor` (factor
10, so 100 for `count=10`) and the loop is
`while (attempts-- && count && raxNext(&ri))`. An entry skipped for
insufficient idle time is **examined**, so it consumes the budget even
though it is not claimed. The pending list is a rax ordered by message
id, so a scan from `"0-0"` always examines the lowest-id entries.

Restarting at `"0-0"` for ever therefore re-examines the same low-id
prefix. When that prefix is entirely inside the idle window — and ten
sweeps inside thirty seconds is ordinary under load — the sweep
examines 100 entries, claims none, and the entries past the prefix are
unreachable. Retry throughput collapses to zero exactly when the
pipeline is under the most pressure, and the pending list never drains.

**The fix.** Carry the cursor forward. A cursor of `"0-0"` means the
scan reached the end of the pending list, so the next sweep starts over
— otherwise the low-id entries would never be examined again.

### 26. The claim idle time equalled the processing timeout

`RETRY_CLAIM_MIN_IDLE_MS` was `30_000` and `per_event_timeout_seconds`
was `30.0`. Equal.

`XAUTOCLAIM` reclaims across the **whole consumer group** and transfers
ownership. An entry whose idle time has reached the processing timeout
is therefore eligible for a *peer's* claim while this process is still
working on it — concurrent double-processing, and a duplicate-key error
for whoever loses the race. The docstring claimed "Claims are done with
THIS consumer's name, so the claimed entries stay owned by the consumer
that is reading"; that is true of the *result* and false of the
*mechanism*, and the distinction is the whole hazard.

**The fix.** The claim idle time is the larger of the floor and **twice
the configured processing timeout**, so an operator who raises the
timeout cannot reopen the window. The sweep's cadence follows the same
value.

### 27. Three findings this plan records but does NOT fix

- **A re-indexed message is misclassified as a failure.** The insert
  happens before the XACK, so a message whose row was written and whose
  XACK then failed stays pending. The sweep reclaims it, the insert
  raises a duplicate-key error, and the state machine counts that as an
  *indexing* failure: `_entries_indexed` never increments, the retry
  budget burns on a message that was in fact indexed, and the log says
  "indexing failed" for a message that is in the store. **The data is
  not corrupted** — the row exists, the duplicate is rejected, and there
  is exactly one row — but the observable is wrong, and an operator
  chasing that warning finds nothing.

  The right fix is an idempotent insert in the hot store, which is a
  change to the substrate (the same out-of-scope item §23 records for
  the repeated-backfill case). A subscriber-local workaround — catching
  the duplicate-key exception and treating it as success — depends on
  recognising the substrate's exception type, which is fragile across
  the three backends. **Left for the substrate work.**
- **A watermark ahead of the stream's last id.** `XGROUP CREATE` does
  not validate the id against the stream, so a group can be created at
  an id the stream has not reached, and `>` reads then deliver nothing
  until the stream passes it. The watermark only ever advances on a
  *successful* index, so in normal operation it trails the group's
  `last-delivered-id`. The exposure is a hot store restored from a
  *newer* backup than the Redis snapshot it is paired with: the loss
  window is bounded by the gap between the two. Narrow, but it is a
  loss, and it is worth a line in the runbook rather than a fix here.
  That line is written, and it uses `XGROUP SETID` (the primitive this
  plan already uses at Task 6): the group already exists in this
  state, so `XGROUP CREATE` raises `BUSYGROUP` and ignores its id, and
  the only thing that moves an existing group's position is `SETID`.
- **A destroyed consumer group leaves the subscriber deaf until the
  process restarts.** `_ensure_consumer_group` has exactly one call
  site — `start()`. If the group is destroyed (`XGROUP DELGROUP`, a
  `FLUSHDB`, or a failover to a replica that never received the
  `XGROUP CREATE`), every `xreadgroup` raises `NOGROUP`, the read
  loop's error handler backs off and retries, and it never recreates
  the group. Nothing is indexed until someone restarts the process.
  The messages are not lost — they pile up unread in the stream — so
  the damage is a stall, not a loss.

  This is **pre-existing**: `main` creates the group in `start()` and
  nowhere else, so this branch neither introduces nor worsens it. It
  is also partly mitigated here: `source_producer_read_errors_total`
  climbs on every `NOGROUP`, so the condition is visible in the health
  payload instead of only in a log file. What is missing is the
  *status*: the stall gate reads `entries_candidate`, which never
  advances on a `NOGROUP` read, so the feed will not flip to
  `degraded` on its own. **The counter is the only signal.** The fix is
  a `NOGROUP` branch in the read loop that calls
  `_ensure_consumer_group` again — it is idempotent and tolerates
  `BUSYGROUP` — and it belongs with the health-feed work rather than
  in this repair.

### 28. The watermark row leaked into the search results

Finding 21 excluded the watermark row from `query_traces`. The other
read path was missed, and it leaks it badly.

The watermark's embedding is the **zero vector**, so its cosine
similarity is `NaN` — and DuckDB orders `NaN` **above** every number.
`NaN >= threshold` is therefore TRUE in DuckDB, so the threshold cannot
filter it, and `ORDER BY score DESC` puts it **first**. The zero-vector
fallback path leaks it worse still: it is time-ordered, and the
watermark's timestamp is rewritten on every successful index, so the
watermark is usually the newest row.

The cost is not cosmetic. `akosha/ingestion/worker.py` asks for
`limit=1` to test whether a row it is about to write already exists.
The watermark takes the one slot, so the real duplicate is never found
and duplicate detection silently stops working.

**The fix.** The exclusion is in both paths of the store, because the
store is where the reserved `conversation_id`'s meaning lives — the
substrate cannot know about it. The LOC guard moves from 500 to 506:
this is the third substrate-compatibility shim in the file, of the kind
the original threshold explicitly reserved room for, and 506 keeps the
≥28% reduction from ~704 intact (198/704 = 28.1%).

**The lesson.** A reserved-key row needs excluding from *every* read
path, and the paths fail differently: one leaks it by *sort order*, the
other by *recency*. A filter proven on one path says nothing about the
other.

## What was actually verified

Recorded so a later reader does not have to re-derive it, and so the
difference between a MEASUREMENT and a CLAIM stays visible.

| Check | Result | Where |
|---|---|---|
| Full suite | **3351 passed, 10 skipped**, exit 0 | `.venv/bin/pytest -q --no-cov` |
| Gate, fast hooks | **18/18 passed**, 0 issues | `crackerjack run -v -t` |
| Gate, comprehensive hooks | **12/12 passed**, 0 issues | `crackerjack run -v -t` |
| Gate, test phase | **58 tests, 0 failed, 0 errors** | `crackerjack run -v -t` |
| `ruff check akosha/` | clean | — |
| `ty check` | clean | — |
| Live end-to-end | **RAN 2026-11-21** — leak fixed, backlog replayed, `entities_count` 0 → 1 | "Verification runbook" below |
| Live re-verification of `665bbe6` | **OUTSTANDING** — the live service still runs the pre-fix code | "The remaining step" below |

Two of those numbers were earned the hard way, and the way is worth
recording because it is the kind of thing that looks like success:

- **The gate's own test phase is INCREMENTAL.** It runs testmon's
  selection, not the suite — 58 tests, not 3351 — and its coverage is
  off by design ("an incremental run's coverage covers only the affected
  tests, so it is not a measurement"). A green gate's test phase says
  the AFFECTED tests pass; it says nothing about the rest. The full
  suite is the separate measurement, and both are recorded above.
- **A pipeline reports its LAST command's exit status.** A run of
  `pytest ... | tail -12` exits 0 whatever pytest did, and `tail -12`
  is exactly long enough to cut pytest's summary line — the one line
  that lists `failed` and `errors`. A suite run that reports nothing
  but a trailing library's leak notices has told you nothing.

And one number is a LIE if quoted alone: the gate's test phase ran
against a tree that was not the one about to be committed, because a
command that was meant to run in the main checkout ran in a worktree
instead. It passed, and it was still the wrong tree. The lesson is
general — **a green result from an unverified target is not a
result** — and it is why the table above names the command, not just
the verdict.

## Verification runbook

Recorded 2026-11-21. Every number below was READ from the live system
after the run — from `/health`, from `XINFO GROUPS`, from the Akosha
log — and not inferred from the code.

### What was run

| Step | Command | Outcome |
|---|---|---|
| 1. Restart | `launchctl kickstart -k gui/$(id -u)/com.mcp.akosha` | Service back up, HTTP 200, `event_subscriber_running: true` |
| 2. Consumer cleanup | `scripts/recover_event_stream.py --apply` | **36,582 of 36,583** targeted consumers deleted |
| 2. Backlog reset | `XGROUP SETID bodai:events akosha-tool-invocation-indexers 0-0` | Group position reset to the start of the stream |
| 2. Pending reclaim | `XAUTOCLAIM bodai:events akosha-tool-invocation-indexers akosha-b0b632b473a4 0 0` | **397** pending entries claimed in one round |
| 3. Verify | `recover_event_stream.py --dry-run`, `GET /health` | See the tables below |

The recovery script exited **1**, not 0, and the exit code is worth
reading rather than glossing: its post-condition asserts that every
targeted consumer is gone, and one was not — the LIVE subscriber's own
consumer (`akosha-b0b632b473a4`), which the cleanup deleted and the
running process immediately re-registered. The bulk cleanup succeeded
(36,582 deleted); the script's post-condition is simply wrong for a
process that stays online.

### The leak is fixed

| Fact | Before | After |
|---|---|---|
| Group consumers | **36,667** and growing at 0.5/s | **85** and **Δ+0 over 8 s** |
| `XINFO CONSUMERS` cost | 10.8 s (23.6 s cold) | **0.27 s → 0.006 s** |
| `last-delivered-id` | `1795254219344-0` (stream tail) | rewound to `0-0`, then replayed to `1795257627636-0` |
| Stream length | 14,328 | 14,367 (new traffic during the run) |

### The pipeline ingests

| Signal | Before the run | After the run |
|---|---|---|
| `knowledge_graph_feed.status` | `warming_up` | **`healthy`** |
| `knowledge_graph_feed.entities_count` | 0 | **1** |
| `knowledge_graph_feed.reason_codes` | `["warming_up_empty_feed"]` | `[]` |
| `source_producer_entries_total` | 0 | **14,709** |
| `source_producer_read_errors_total` | — | **0** |
| Hot-store `conversations` rows (`kg_refresh: rows=`) | 88 | **100** (the refresh's `limit=100`) |

**The primary criterion — `entities_count > 0` with the feed healthy —
is met.** The warm tier's `vectors_conversations` stayed empty, as the
header note predicts: its only writer is the hot→warm migration, which
this plan does not enable.

### What the run found that the code did not cover

Three defects, all in the subscriber's own plumbing, all invisible to
the code-level tests and only visible against the live system.

**1. The health counters were not wired through.** The health payload
holds the `WebSocketInvocationsSubscriber` stack and reads the
producer's counters off it with `getattr`. The stack proxied
`entries_consumed` and NOTHING else, so `entries_candidate`,
`entries_indexed` and `read_errors` all read **0** on a producer that
was actively indexing and actively failing to read. `getattr` on a
missing attribute is silent, so a missing proxy reads as a counter of
zero — and the stall signal, which gates on the candidate count, could
never fire. The live proof: `/health` reported `read_errors_total: 0`
while the same process logged **31** `xreadgroup` timeouts.

**2. The subscriber's client still inherited redis-py's five-second
socket timeout.** That default is a bound on the SERVER's work, and the
server's work here is not bounded by the `BLOCK` duration: the reply to
a read is delayed by whatever the event loop is doing, and the event
loop is doing per-entry embedding and DuckDB work. The replay aborted
**31** reads client-side against a server that was answering normally,
plus **9** per-entry processing timeouts. This is the same trap as the
recovery script's failure, one layer down: the script was fixed in
`0304857`; the subscriber was not.

**3. A replayed row was treated as a failure.** The substrate's
`INSERT` has no `ON CONFLICT` clause, so re-indexing a
`conversation_id` that is already stored raises. The subscriber
treated that as an indexing failure and parked the message in the PEL
for three pointless retries: **170** duplicate-key failures during the
replay, and a pending list that could not drain. A duplicate key means
the message's work is DONE — the row is present — so it must be ACKed
and the watermark advanced. No new row landed, so the "rows that
actually landed" counter is not bumped.

All three are fixed in **`665bbe6`** (`fix/health-counters-and-timeout`),
each with a test that fails before the fix.

### The remaining step

The live service still runs the code from before `665bbe6`. To pick up
the three fixes and confirm them against the live system:

```bash
launchctl kickstart -k gui/$(id -u)/com.mcp.akosha
curl -s http://127.0.0.1:8682/health | python3 -m json.tool \
  | grep -E 'source_producer_(read_errors_total|entries_candidate|entries_indexed)'
```

The three counters must now be NON-ZERO and must MOVE: before the fix
they read zero because nothing was proxied, and after it they read the
producer's real numbers. A zero after the restart would mean the
proxies are still missing.

### One thing this plan does not fix

The `bodai-task-orphan-sweeper` group on the same stream carried **437
consumers and 18,204 pending** — the same consumer-leak shape, in a
different component (Mahavishnu's sweeper, not Akosha's subscriber).
It was outside this plan's scope. It is recorded here because the
shape is identical to the bug this plan fixed, and because the group
shares a stream with the one that was cleaned.

It has since been addressed on the Mahavishnu side — see
`mahavishnu/docs/plans/2026-11-21-task-orphan-sweeper-group-hygiene.md`.
The root cause there was worse than a leak: the sweeper read nothing
at all, because the same adapter dropped every entry it was handed.

### Replay detection verifies the row, not the error message

A security review of the duplicate-key handling found the wrong
verdict being computed. The subscriber decided that a failed insert was
a *replay* by matching free-form substrings against the exception
message — `"duplicate key"`, `"violates primary key constraint"`,
`"violates unique constraint"`.

That is a fail-open gate. Any error whose text merely *mentions* those
words — a connection error quoting a config key, a driver message, an
unrelated constraint — was read as a duplicate, and the message was
then acked as already-indexed even though it never was. The
watermark advanced and the message was dropped, so the loss was
silent.

The verdict now comes from the store. After a failed insert the
subscriber reads the row back by primary key:

```sql
SELECT conversation_id FROM conversations WHERE conversation_id = ?
```

`conversation_id` is the event's identity — the envelope's `event_id`,
or the stream message id — so a row found under that key IS this
event, already indexed. A read that fails reports "not stored", so an
unverifiable message stays a failure and is retried rather than
dropped. The watermark and the XACK still advance only after a
successful insert or a verified replay; every other error remains a
failure.

This is stronger than catching the backend's structured duplicate
exception and checking which constraint failed: that tells you what
kind of failure occurred, while the read tells you the fact that
matters — whether the work is done.

The replay gate then went one step further: it must prove the row is
THIS subscriber's row for THIS event.

* The read is scoped to `system_id = SYSTEM_ID_MAHAVISHNU`. The
  `conversations` primary key is `conversation_id` alone, so a row
  written by another system under the same id would otherwise satisfy
  the check.
* The stored `content` must equal the content this event would have
  written, so a producer that reused an event id for a different
  payload is caught rather than silently dropped.
* The reserved watermark id can never satisfy the check: that row is
  the subscriber's own bookkeeping, not an indexed event.

The read also runs on a worker thread now. `duckdb`'s `execute` blocks
the calling thread until the result is materialised, so calling it
from an `async def` pins the event loop for the whole query — and the
substrate's own docstring warns that callers which reach for `conn`
directly collide with an in-flight write. The replay verification
reads the store on the FAILURE path, where a backlog means hundreds of
reads in a row; pinning the loop there starves every other coroutine.
The read is also time-bounded, and a verification that could not
complete is reported as "not stored", so the message is retried rather
than dropped.

The upstream half — making the substrate's `INSERT` idempotent with
`ON CONFLICT (conversation_id) DO NOTHING` — is not changed here. The
substrate is oneiric's, and a fresh `uv sync` would install a
oneiric without that clause, so the change needs a oneiric release
before Akosha's pin is satisfied.
