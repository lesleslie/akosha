______________________________________________________________________

## status: active role: canonical date: 2026-07-16 last_reviewed: 2026-11-20 superseded_by: null blocks_on: [] topic: observability

# INGESTION_BACKLOG - Large Backlog Response

## Severity

**HIGH**

## Detection

- Alert: akosha_ingestion_backlog_count > 10000
- Dashboard: Grafana dashboard: "Akosha System Health"
- Symptom: Uploads taking >30 minutes to appear in search results

## The subscriber reads nothing because its group starts ahead of the stream

The event subscriber creates its consumer group at the **watermark** —
the highest message id it has successfully indexed — so a group created
over an already-indexed stream does not replay the back-catalogue.
Redis does **not** validate that id against the stream, so a group can
be created at an id the stream has not reached, and `>` reads then
deliver nothing until the stream's ids pass it.

The exposure is a hot store restored from a **newer** backup than the
Redis snapshot it is paired with. The loss window is bounded by the gap
between the two.

**Detection.** The subscriber is running and reading, but nothing
arrives: `knowledge_graph_feed.entities_count == 0` with
`source_producer_entries_candidate == 0`. Compare the watermark row
(`conversations`, `conversation_id =
"__bodai_subscriber_watermark__"`, `metadata.last_message_id`) against
`XINFO STREAM bodai:events`'s last id. **A watermark above the
stream's last id is the signature.**

**Response.** Restore the hot store and Redis from the SAME point in
time.

The alternative is to move the EXISTING group's position back to the
start of the stream:

```bash
XGROUP SETID bodai:events akosha-tool-invocation-indexers 0
```

**Deleting the watermark row does NOT work**, and the reason is the
whole trap: in this state the group ALREADY EXISTS, so `XGROUP CREATE`
raises `BUSYGROUP` and ignores its `id` argument entirely — the group's
`last-delivered-id` stays where it was and `>` still delivers nothing.
Deleting the watermark also destroys the diagnostic evidence.

`SETID` moves an existing group's `last-delivered-id`, and `>` resolves
against the group's position at read time, so the running subscriber
starts delivering on its next read — no restart. The cost is a full
replay of the back-catalogue: every already-indexed message takes the
duplicate-key path, is counted as an indexing failure, and is ACKed and
dropped after `MAX_INDEX_RETRIES` attempts. The rows are not
duplicated; the replay is noisy and wasteful.
