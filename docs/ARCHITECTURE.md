# Architecture

Companion to the [README](../README.md). This covers layering, the data model,
and the invariants the code relies on. The README covers what the system does and
what broke while building it.

---

## Layering

```
app/domain/       enums, the event contract          ← depends on nothing
app/providers/    vendor adapters, registry          ← depends on domain
app/instrumentation/  wrapper, redaction             ← depends on domain, providers, events
app/events/       EventBus interface + Redis impl    ← depends on domain
app/db/           models, repositories, migrations   ← depends on domain
app/services/     chat orchestration                 ← depends on all of the above
app/api/          routers, schemas, SSE, deps        ← depends on services
app/worker/       ingestion consumer                 ← depends on domain, events, db
```

Two rules make this hold:

**The event contract lives in `domain/`, below everything.** The wrapper produces
it, the bus transports it, the worker validates against it, the repository
persists it. Producer and consumer physically cannot drift, because there is one
schema and it sits beneath all of them. `extra="forbid"` turns any drift into a
loud failure rather than a silently dropped field.

**The worker imports the app package; it does not re-implement it.** Same image,
different entrypoint. A parallel worker tree would need its own copy of the event
schema and the ORM models — two copies that agree today and diverge later.

---

## The instrumentation seam

```python
class InstrumentedProvider(BaseProvider):
    async def stream_chat(self, request):
        event_id = uuid4()                  # before the call, so it always exists
        started_at, start = now(), monotonic()
        try:
            async for chunk in self._inner.stream_chat(request):
                if ttft is None and chunk.delta_text:
                    ttft = elapsed()        # first *token*, not first event
                ...
                yield chunk
        except BaseException as exc:        # CancelledError is not an Exception
            status, error_type, error_message = classify(exc)
            raise                           # bare re-raise: nothing swallowed
        finally:
            await publish_resiliently(bus, build_event(...))
```

Four things are load-bearing here.

**`BaseException`, not `Exception`.** `CancelledError` and `GeneratorExit` both
inherit from `BaseException`. An `except Exception` looks correct, passes review,
and never fires on the one case the cancellation dashboard exists to show.

**The bare `raise`.** Catching `BaseException` is only safe if it always
re-raises. Swallowing a `CancelledError` would fix the logging gap and break
cancellation itself — the task would appear to finish normally instead of
actually stopping.

**One `finally`.** Success, error and cancellation all converge on it, so "one
call, one row" is a property of the control flow rather than of remembering to
log on each path.

**`monotonic()` for durations, wall clock for timestamps.** A clock adjustment
mid-call must not be able to produce a negative latency.

Because `BaseProvider.complete()` is built on `stream_chat`, non-streaming calls
are instrumented by the same code. There is no second path to keep in sync.

### Why the publish is shielded

```python
task = asyncio.create_task(bus.publish(event))
try:
    await asyncio.shield(task)
except asyncio.CancelledError:
    raise      # the task keeps running; the event survives
```

`Task.cancel()` delivers `CancelledError` once. A watcher that keeps polling
after it has already cancelled calls `cancel()` again, and that second delivery
lands inside this `finally` — destroying the event. Measured: zero events instead
of one. `app/api/sse.py` also guards its watcher to cancel exactly once; these
are independent layers, because a stable seam must not depend on every caller
getting cancellation discipline right.

`shield` protects against *callers*. It does nothing about the process exiting,
so `drain_pending_publishes()` runs in the FastAPI shutdown handler with a
bounded grace period — otherwise a rolling deploy would drop in-flight events the
same way, just triggered by SIGTERM.

---

## A chat turn, in two transactions

```
T1 ── create conversation (if new) + user message ── COMMIT
     │
     ├── provider.complete() / stream_chat()      ← outside any transaction
     │     └── wrapper publishes exactly one event
     │
T2 ── assistant message ── COMMIT
```

Forced by the logging requirement, not chosen for elegance. Under one
transaction, a provider failure rolls the turn back, and the event then
references a conversation that was never committed — so the worker's insert hits
a foreign-key violation on precisely the row the errors dashboard needs most.

Committing the user turn first makes `conversation_id` durable before the call,
so every outcome can reference it. The provider call sits outside any transaction
because a model call can run for minutes; holding a connection and its locks open
that long exhausts the pool under trivial concurrency.

**Cost:** a failed first message leaves a conversation with a user message and no
reply. Visible, retryable, and covered by tests.

---

## Data model

```sql
conversations   id · title · status · created_at · updated_at
messages        id · conversation_id → · role · content · seq · created_at
inference_logs  id · conversation_id ⇢ · message_id ⇢ · provider · model · status
                streamed · started_at · completed_at · ingested_at
                latency_ms · ttft_ms · input_tokens · output_tokens
                input_preview · output_preview · error_type · error_message
                finish_reason · raw_metadata(jsonb) · schema_version
events_raw      id · event_type · payload(jsonb) · received_at
                processed_at · processing_status · processing_error
```

`→` is `ON DELETE CASCADE`; `⇢` is `ON DELETE SET NULL`.

| Decision | Why |
|---|---|
| **Three timestamps** | `started_at`/`completed_at` are measured in the request path; `ingested_at` is when the worker wrote the row. They can be far apart. Dashboards bucket on `completed_at`, or a backlog renders as a traffic spike that never happened |
| **`id` is client-generated** | Minted by the wrapper before the call, reused as the PK. This is what makes at-least-once ingestion idempotent rather than duplicate-producing |
| **`seq` on messages** | Explicit ordering. Timestamps are the wrong tool: two messages in the same millisecond, or replicas with skewed clocks, order non-deterministically |
| **`message_id` nullable** | Errors and cancellations produce no assistant message — precisely the rows the dashboards exist to surface |
| **Logs survive their conversation** | `ON DELETE SET NULL`: deleting a transcript must not erase the record of what it cost to produce |
| **`provider` unconstrained** | Adding a provider is a config change, not a migration. `status`/`role` *are* constrained — their domains are closed |
| **Enums store values + CHECK** | SQLAlchemy 2.0 defaults to neither. `native_enum=False` alone yields an unconstrained VARCHAR storing `'ERROR'` where the domain says `'error'` |
| **CHECK on durations/tokens** | A seconds-vs-milliseconds bug fails loudly instead of silently poisoning every percentile |
| **`raw_metadata` JSONB** | Vendor-specific detail worth keeping but not worth a column — request ids, reasoning-token splits |
| **Partial indexes** | The errors view and the unprocessed-events triage read a small slice of a large table |

---

## Ingestion

```
publish (API)  →  XADD inference_logs  (bounded MAXLEN, ~200ms timeout, never raises)
                        ↓
worker         →  XREADGROUP ingestors  (consumer group; each entry to exactly one replica)
                        ↓
               1. INSERT events_raw            ← before parsing
               2. validate against InferenceEvent
               3. INSERT inference_logs ON CONFLICT DO NOTHING
               4. mark events_raw processed
               ── COMMIT ──
               5. XACK                          ← only now
```

**Acking last** is what makes this at-least-once rather than at-most-once. Dying
between the write and the ack causes redelivery, which the idempotent upsert
absorbs. The reverse order would silently lose events on every crash.

**Landing the raw payload before parsing** means a payload the schema rejects is
still on disk with the reason attached, so a schema fix becomes a replay rather
than a permanent hole.

**Failure handling:**

| Failure | Behaviour |
|---|---|
| Redis unreachable at publish | Event dropped after the timeout; chat succeeds. Stated tradeoff |
| Postgres unreachable | Batch not acked, redelivered; worker backs off. No loss inside the retention window |
| Malformed JSON | Dead-lettered and acked — otherwise redelivered forever |
| Schema violation | Recorded `failed` in `events_raw` with the error, dead-lettered, acked. Replayable |
| No usable event id | Dead-lettered without touching the database — no id means no idempotency |
| Consumer group missing | `poll()` recreates it. Previously an infinite silent loop |

---

## Observability of the pipeline itself

The chat path and the ingestion path fail independently. The chat API can be
perfectly healthy while nothing at all is being logged — which is exactly what
happened during the `NOGROUP` bug: process alive, liveness probe green, zero rows
landing.

So `/metrics/summary` returns an `ingestion` block alongside the chat metrics.
`lag_seconds` is time since the last successful write, deliberately **not**
filtered by the query window — a windowed version would report "no data" during
exactly the outage it exists to reveal, indistinguishable from a quiet period.

This is also why the worker has no HTTP liveness probe. A probe would report
healthy during precisely this failure. The signal has to come from whether rows
are landing, not from whether the process is up.

---

## Testing strategy

| Layer | What it covers | Needs |
|---|---|---|
| Unit (57) | Event contract, redaction, providers, registry, wrapper invariants, health probes | Nothing |
| Integration (60) | Repositories, chat API, streaming + cancellation, ingestion, metrics | Postgres |

`make test` runs unit tests only, so the fast gate needs no services and no keys.

The tests worth singling out, because each pins a property that fails *silently*:

- `test_second_turn_receives_the_first_turn_as_context` — asserts on the
  provider's report of what it received, not on our intent to send it
- `test_generator_close_is_recorded_as_cancelled` — locks the Starlette
  `GeneratorExit` coupling
- `test_watcher_stops_polling_once_it_has_cancelled` — pins the cancel-once guard
- `test_redelivery_does_not_duplicate_the_row` — the property that keeps every
  dashboard count honest across restarts
- `test_buckets_follow_completed_at_not_ingested_at` — a one-word change review
  would not catch
- `test_freeform_pii_is_not_caught` — asserts the documented redaction gap, so the
  README's claim stays honest and a future NER upgrade has a failing test to flip

---

## Scaling

| Component | How it scales |
|---|---|
| `backend` | HPA on CPU, 2–10. Slow scale-down (300s) because streaming responses are long-lived |
| `worker` | HPA 2–8. Redis consumer groups hand each entry to exactly one replica, so replicas need no coordination code. CPU is a poor proxy — stream depth via KEDA is the right signal |
| `postgres` | Vertical, plus a read replica for dashboard queries. Managed database in production |
| `redis` | Single instance with AOF. `maxmemory-policy noeviction` so a stalled worker degrades into rejected writes — which the publisher already treats as "drop and carry on" — rather than an OOM kill that also takes out chat |

The API and the worker are separate Deployments specifically because their
scaling pressures are opposite: the API scales with concurrent users, the worker
with event volume and write throughput. Coupling them would mean scaling the chat
path to clear an ingestion backlog.
