# LLM Inference Logger

Instrumentation, ingestion and observability for multi-provider LLM inference.

A chat application that logs every model call — success, failure, or abandoned
mid-stream — through an event pipeline into Postgres, and a dashboard that reads
it back. Multi-provider (Anthropic, OpenAI, Groq, Gemini, Ollama) with a first-class
mock provider, so
**the whole system runs and demonstrates itself with no API keys and no network.**

```bash
git clone <repo> && cd llm-inference-logger
make up          # five services, one command
open http://localhost:5173
```

Then send a message. Try the `mock-error` model to populate the errors panel, or
`mock-cancel` and press **Stop** to see a cancelled call recorded. Both are
reachable from the UI on purpose — see [Why the mock provider is
load-bearing](#why-the-mock-provider-is-load-bearing).

---

## Contents

- [What it does](#what-it-does)
- [Architecture](#architecture)
- [Running it](#running-it)
- [Why the mock provider is load-bearing](#why-the-mock-provider-is-load-bearing)
- [What broke and how I found it](#what-broke-and-how-i-found-it) ← the interesting part
- [Design decisions and tradeoffs](#design-decisions-and-tradeoffs)
- [Known limitations](#known-limitations)
- [What I'd do with more time](#what-id-do-with-more-time)

---

## What it does

| | |
|---|---|
| **Chat** | Multi-turn conversations, streamed token by token over SSE, resumable after a page refresh |
| **Cancellation** | Stop a generation mid-stream; partial output is kept and the call is logged as `cancelled`, not as an error |
| **Instrumentation** | One `inference_logs` row per model call, always — latency, TTFT, tokens, status, redacted previews |
| **Ingestion** | Events published to Redis Streams, consumed by a separate worker, written idempotently |
| **Dashboard** | Latency percentiles, throughput by outcome, error rate, per-provider breakdown, and ingestion-pipeline health |
| **Providers** | Anthropic, OpenAI, Groq, Gemini, Ollama, and a deterministic mock. Adding a provider requires no changes to the instrumentation |

---

## Architecture

```
                    ┌──────────────┐
  browser ──HTTP/SSE─▶   frontend   │  React + Vite, nginx (also proxies /api → one origin)
                    └──────┬───────┘
                           │
                    ┌──────▼─────────────────────────────┐
                    │            backend (FastAPI)        │
                    │                                     │
                    │  /chat  /chat/stream  /conversations │
                    │  /metrics/summary  /metrics/errors   │
                    │                                     │
                    │  ┌───────────────────────────────┐  │
                    │  │  InstrumentedProvider          │  │  ← the one seam
                    │  │  wraps stream_chat; emits      │  │
                    │  │  exactly one event per call    │  │
                    │  └──────────────┬────────────────┘  │
                    │  ┌──────────────▼────────────────┐  │
                    │  │  ProviderRegistry              │  │
                    │  │  anthropic · openai · groq ·   │  │
                    │  │  gemini · ollama · mock        │  │
                    │  └───────────────────────────────┘  │
                    └──────┬──────────────────────┬───────┘
                           │ EventBus (interface) │ SQL
                    ┌──────▼───────┐              │
                    │ Redis Streams │              │
                    │ inference_logs│              │
                    │  (+ :dlq)     │              │
                    └──────┬────────┘              │
                           │ XREADGROUP            │
                    ┌──────▼──────────┐            │
                    │ worker           │            │
                    │ validate→persist │            │
                    └──────┬───────────┘            │
                    ┌──────▼────────────────────────▼───┐
                    │             Postgres               │
                    │ conversations · messages           │
                    │ inference_logs · events_raw        │
                    └────────────────────────────────────┘
```

The request path never blocks on logging. The wrapper publishes an event with a
short timeout and moves on; a logging outage costs telemetry, not availability.

**The wrapper is the whole design.** It wraps `stream_chat`, and non-streaming
`complete()` is built on `stream_chat`, so both paths are instrumented by the
same code. A `try/except/finally` means every terminal outcome converges on one
`finally` — so "one call, one row" holds by construction rather than by
remembering to log on each path. Adding a provider adds zero lines to it.

More detail, including the layering rules and the data model: **[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)**.

---

## Running it

### Docker Compose (recommended)

```bash
make up        # build + start postgres, redis, migrate, backend, worker, frontend
make logs      # tail everything
make down      # stop
```

- app → <http://localhost:5173>
- API docs → <http://localhost:8000/docs>

### Local development

```bash
make setup       # uv sync + npm install
make infra-up    # postgres + redis only
make migrate
make api         # terminal 1
make worker      # terminal 2
make web         # terminal 3
```

### Kubernetes (local, via kind)

```bash
make k8s-deploy   # create cluster, build+load images, apply manifests
make k8s-status
open http://localhost:8080
make kind-down
```

> Every Kubernetes target passes `--context kind-llm-logger` explicitly. A
> developer's active context is frequently a real cluster, and an unqualified
> `kubectl apply` would land there. The safe thing happens by default rather
> than by remembering.

### Quality gates

```bash
make check      # ruff + mypy --strict + unit tests
make test-all   # adds integration tests (needs make infra-up)
```

121 tests — 117 backend, 4 frontend. Backend unit tests need no Postgres, no
Redis, and no API keys.

The frontend tests are deliberately *temporal*: they assert that something is on
screen **before** a stream completes, because three consecutive bugs lived in
that seam and none was visible from reading the code or from the backend suite.

### Providers

Six are supported. **Mock needs nothing** and is the default; the others are
enabled by putting a credential in `.env` and restarting.

| Provider | Cost | Where to get a key |
|---|---|---|
| `mock` | Free, no network | — always available |
| `groq` | **Free tier, no card** | <https://console.groq.com> — verified live |
| `gemini` | **Free tier, no card** | <https://aistudio.google.com/apikey> — verified live |
| `ollama` | **Free, fully local** | `brew install ollama && ollama serve && ollama pull llama3.2:1b`, then `OLLAMA_ENABLED=true` |
| `anthropic` | Prepaid, ~$5 min | <https://platform.claude.com> |
| `openai` | Prepaid, ~$5 min | <https://platform.openai.com> |

> A Claude.ai Pro or ChatGPT Plus subscription does **not** include API access.
> They are separately billed products.

Groq, Gemini and Ollama all speak the OpenAI wire format, so they reuse the
existing OpenAI adapter pointed at a different `base_url` —
see [`openai_compatible.py`](backend/app/providers/openai_compatible.py). Each
keeps its **own** provider name rather than masquerading as `openai`, because
`inference_logs.provider` is what every dashboard panel groups by; sharing a
name would make two vendors' traffic indistinguishable, which is the one thing a
multi-provider observability tool must not lose.

The model picker is built from `/providers`, so it only ever offers what the
server actually has credentials for — it cannot present an option that 400s.

Drop `ANTHROPIC_API_KEY` / `OPENAI_API_KEY` into `.env` and restart. The registry
picks them up at startup; providers without a key are simply not registered, so a
missing key is a clear `400 provider_not_configured` at the API boundary rather
than an authentication error thrown from inside a vendor SDK mid-stream.

---

## Why the mock provider is load-bearing

It is not a test double. It is a real `BaseProvider` that the wrapper, the bus,
the worker and the database cannot distinguish from Anthropic — and it does three
jobs nothing else can:

1. **A reviewer with no API key sees the system work.** Streaming, TTFT, token
   accounting, errors, cancellation, and every dashboard panel populate on a
   fresh clone. "Trust me it works" becomes "watch it work."
2. **Failure paths become deterministic.** You cannot ask a real provider to fail
   on demand. Without `mock-error`, the errors dashboard would ship untested and
   demo against an empty table.
3. **It makes multi-turn plumbing observable.** Replies report which turn they
   are and how much history they received. A chat UI looks perfectly healthy
   while silently dropping conversation history — every reply is fluent, it just
   has amnesia. Reporting what the provider *actually received* turns that
   invisible failure into an assertion.

| Model | Behaviour |
|---|---|
| `mock` | Ordinary streamed reply |
| `mock-instant` | No delays; used by the test suite |
| `mock-slow` | Long pauses between tokens; for demonstrating latency |
| `mock-error` | Fails *after* emitting output — a partial preview plus an error |
| `mock-cancel` | **Never terminates on its own.** So a `cancelled` row from it can only have come from a real interruption |

---

## What broke and how I found it

Every bug below was found by running the system and reading what actually
happened — not by re-reading the design. They are here in specific detail
because the specifics are the point.

### 1. A single transaction made the most important log row unwritable

**Symptom.** A chat turn originally ran inside one transaction with the provider
call inside it. On a provider failure the transaction rolled back — correctly, so
no orphaned user message. But that rollback also destroyed the conversation the
event referenced, so the worker's insert would hit a foreign-key violation
against a conversation that never committed.

**Why it mattered.** The single most important row on an errors dashboard is the
failure. The design made it the one row that *could not be written*.

**The fix.** Split the turn into two transactions with the provider call between
them. Committing the user turn first makes `conversation_id` durable *before* the
call, so success, error and cancellation can all reference it safely.

The FK problem and the lost-error-log problem turned out to be the same problem.
It is also better behaviour: the user's message stays on screen when the model
fails. The cost — a failed first message leaves a conversation with a user
message and no reply — is visible, retryable, and covered by a test.

### 2. Cancellation erased its own log entry

**Symptom.** `asyncio.shield` and a bare `await` in the wrapper's `finally` both
publish correctly under a single `cancel()`. Under a watcher that keeps polling
after it has already cancelled, **zero events were published** — measured, not
theorised.

**Why.** `Task.cancel()` delivers `CancelledError` once and does not re-arm. But
a second `cancel()` lands while the `finally` is mid-publish and kills it. The
cancellation destroys the very record it was supposed to create — and silently,
because a missing row looks exactly like no traffic.

**The fix, in two independent layers.** The disconnect watcher returns
immediately after cancelling, so the case does not arise. The publish runs in its
own task behind `asyncio.shield`, so it does not matter if it ever does. A
wrapper meant to be a stable seam must not depend on every future caller having
correct cancellation discipline.

A test asserts the watcher's poll count *stops growing* after it cancels, so the
guard is pinned rather than trusted.

### 3. `GeneratorExit`, not `CancelledError`

**Symptom.** The wrapper caught `BaseException` (necessary — `CancelledError`
does not inherit from `Exception`) and classified anything non-provider as an
error. Live runs showed cancelled calls landing correctly as `cancelled`, but
`client_disconnected` **never appeared in the logs**.

**Why that absence mattered.** Starlette signals a disconnect by calling
`aclose()` on the response generator, which raises `GeneratorExit` — not
`CancelledError`. That is the path that actually fires in production, *ahead of*
the `is_disconnected()` watcher. Classified as an error, every closed browser tab
would have been filed as a provider failure.

I found it by noticing something that wasn't in the logs, not by reading the
disconnect-handling code and concluding it looked right.

> **Known coupling.** This depends on Starlette implementation behaviour, not a
> documented ASGI guarantee. A dedicated test locks `GeneratorExit → cancelled`
> so a future Starlette change surfaces as a test failure rather than as a slow
> drift in what the dashboard claims.

### 4. A worker that was alive, healthy, and doing nothing

**Symptom.** Found from a mistake in my own demo script: `redis-cli DEL
inference_logs` also deletes the stream's consumer group. The worker then looped
on `NOGROUP` forever — process running, liveness probe passing, ingesting
absolutely nothing.

**Why this one is a different species.** The other three were logic bugs. This
was a *silent* outage: nothing crashed, nothing alerted, and every chat-path
panel looked perfectly healthy. `ensure_group()` only ran at startup, so there
was no recovery path. An eviction or a flushed database would do the same in
production.

**The fix, at two levels.** `poll()` recreates the group on `NOGROUP` and
continues. And — the more important half — **ingestion lag is now a dashboard
panel.** Lag is time since the last successful write, so it grows regardless of
*why* ingestion stopped: a dead worker, an unreachable database, a consumer group
that no longer exists. The failure mode was invisibility, so the fix is a
measurement, not just a retry.

### 5. Bugs the type checker and the tests could not see

Smaller, but the same theme — each was invisible until something ran:

- **SQLAlchemy enums stored member *names*, not values,** and
  `native_enum=False` alone produced an unconstrained `VARCHAR` because
  `create_constraint` defaults to `False` in 2.0. Caught by reading the emitted
  DDL in `psql` rather than trusting the model definition. Reading generated DDL
  after every migration is now a standing habit.
- **OpenAI's `max_tokens` is rejected by every reasoning model.** The adapter
  worked on `gpt-4o` and would have 400'd the moment anyone selected an o-series
  or gpt-5 model. `max_completion_tokens` is also the true analogue of
  Anthropic's budget — both cover reasoning *plus* visible output.
- **`completion_tokens` silently bundles invisible reasoning tokens** with the
  text the user sees, so `output_tokens` alone cannot answer "how much did we
  spend on reasoning nobody read?" The split is preserved in `raw_metadata`.
- **Token usage arrives across multiple stream events.** Anthropic sends input
  tokens on `message_start` and output tokens at the end; replacing usage
  per-chunk would drop whichever half arrived first — leaving half the cost data
  null for one provider only, and passing every test written against the mock.
- **`vars()` fails on `slots=True` dataclasses.** `/metrics/summary` returned a
  500 in the container while every repository test passed. Testing the query
  layer is not testing the endpoint; the serialisation boundary between them is
  exactly where that class of mistake lives. Endpoint tests were added.
- **`capabilities: drop: ["ALL"]` broke nginx**, whose entrypoint needs
  `CAP_CHOWN`. Fixed by switching to the unprivileged image rather than
  loosening the security context to suit the base image.
- **A vendor put `usage` on every chunk, and the adapter threw the text away.**
  Gemini streamed a complete answer; the app displayed nothing, recorded
  `success`, and logged 40 output tokens. The adapter read usage and content as
  mutually exclusive — `if event.usage: yield usage; continue` — which is right
  for OpenAI, where usage arrives once on a trailing event with no `choices`, and
  catastrophic for Gemini, which attaches usage to *every* delta. Every text
  chunk was skipped.

  This is the same failure shape as the rest of this list: not a crash, a
  confident and completely empty answer. Both are now read from the same event,
  with a regression test that replays a Gemini-shaped stream without needing a
  key.
- **My defaults for both free providers were stale on first contact.**
  `llama-3.3-70b-versatile` and `gemini-2.0-flash` had both been retired. Each
  returned a clean 404 naming the problem — the adapters' error translation
  working — and the fix was to *ask the vendor* (`GET {base_url}/models`) rather
  than trust a default that had aged. Gemini now pins `-latest` precisely because
  pinning is what went stale.
- **Groq's free tier counts `max_completion_tokens` against its TPM budget.** So
  the global 16,000 default was not merely generous there, it was fatal: `413
  rate_limit_exceeded — Limit 8000, Requested 16076`, rejected before generating
  anything. Output ceilings are now a per-provider property that clamps the
  configured budget, because "what the caller wants" and "what the provider will
  accept" are different facts.
- **Fixing the vanishing message broke streaming.** Moving the navigate to the
  `start` frame made a `useEffect` keyed on `conversationId` fire *mid-stream*,
  and that effect called `reset()` — wiping the accumulating tokens, so the reply
  again only appeared once complete. One bug traded for another in the same
  seam.

  The fix is a self-navigation guard, so the reset can tell "the user clicked a
  different conversation" apart from "we just created this one". More useful
  than the fix: after regressing this seam twice by reasoning about browser
  behaviour, it now has real tests. **And I verified they fail on the broken
  code** — the first version passed against the bug, because rendering
  `ChatWindow` without the route table left `useParams()` permanently empty, so
  the effect never re-fired. A test that cannot fail is worse than no test.
- **Your own message vanished while waiting for the reply.** Three innocuous
  decisions combined into it: the composer clears the input on send, the
  transcript renders only server state, and the transcript query was invalidated
  in a `finally` — so between pressing Send and the reply finishing, the message
  existed nowhere on screen. On a slow model that is seconds of staring at an
  empty panel wondering whether anything happened.

  Fixed on both sides of the seam. The message is echoed locally the instant
  it is sent, and the conversation id is now reported at the `start` frame
  rather than after the stream ends — so the transcript query can point at the
  right conversation while tokens are still arriving. That second half works
  *because* of the two-transaction design: the user turn is committed before the
  provider is called, so a mid-stream refetch genuinely returns it. Verified by
  querying the transcript after four tokens had streamed and finding `seq=0`
  already present. The echo is derived rather than stored — if the refetch has
  landed, it suppresses itself, so there is no window where both copies render.
- **A stale browser tab produced an unexplained `network_error`.** A page left
  open on `/c/<id>` whose conversation had since been deleted got a dead socket
  and no message. The cause was structural: validation ran *inside* the response
  generator, so `NotFoundError` was raised after `StreamingResponse` had already
  sent its 200 headers. Starlette then hit `RuntimeError: Caught handled
  exception, but response already started`, the connection was torn down
  mid-flight, and nginx logged `upstream prematurely closed connection` — while
  the browser had nothing to report but a failed fetch.

  Everything knowable up front now runs *before* the response starts, so an
  unknown conversation is a 404 and an unsupported model a 400. A catch inside
  the generator is the second layer, because an exception escaping after the
  first byte can no longer become a status code at all. And the UI redirects
  off a dead conversation URL rather than leaving the user unable to send
  anything.
- **The model picker silently routed to the wrong provider.** Selecting
  `llama3.2:1b` returned a *mock* reply. Two bugs stacked: the dropdown sent
  only the model name, so the backend fell back to the default provider — and
  `MockProvider` then happily served a model it does not own, because
  `_DELAYS.get(model, default)` treats any unknown name as "use the default
  delay". The second bug is what made the first invisible: instead of a 400, you
  got a fluent, confident, completely wrong answer.

  Fixed on both sides. Providers now declare whether they serve a model —
  defaulting to *yes*, since real vendors have open catalogues and reject
  unknown models themselves, with only the mock's closed set enforced. And the
  dropdown carries provider **and** model, JSON-encoded rather than delimited,
  because model names legitimately contain both `:` (`llama3.2:1b`) and `/`
  (`meta-llama/llama-4`).
- **Compose never read the root `.env`.** It looks for `.env` in the *compose
  file's* directory (`infra/`), not the repo root — so every
  `${VAR:-default}` had been silently falling back to its default since M5.
  The README's own instruction, "drop your API key in `.env` and restart",
  would have done nothing: no key, no error, no clue. It stayed invisible until
  a variable appeared whose default (`OLLAMA_ENABLED=false`) differed from what
  was needed. Fixed with an explicit `--env-file .env`, chosen over
  `--project-directory` because that would also have moved relative
  build-context resolution.
- **nginx cached the backend's IP forever.** A static `proxy_pass http://backend:8000`
  is resolved once at startup, so the moment the backend container got a new
  address every request 502'd against the dead one until nginx itself was
  restarted — and in Kubernetes *every* rolling deploy changes pod IPs. Fixed
  with a `resolver` plus a **variable** in `proxy_pass`, which is what defers
  the lookup; verified by forcing the backend from `172.19.0.4` to
  `172.19.0.7` and watching nginx follow it. Two traps inside the fix: a
  variable `proxy_pass` does not append the original URI (so `$request_uri` is
  explicit), and the image's resolver-population script returns early unless
  `NGINX_ENTRYPOINT_LOCAL_RESOLVERS` is set — which left the placeholder
  unsubstituted and nginx refusing to boot.

---

## Design decisions and tradeoffs

### Three timestamps, not one

`inference_logs` records `started_at` and `completed_at` (measured in the request
path) plus `ingested_at` (server-side). A single `created_at DEFAULT now()` would
record *ingest* time, because the worker writes rows asynchronously — so a
worker backlog would render as twenty silent minutes followed by a traffic spike
that never happened, with every latency percentile computed over the wrong
population.

Every dashboard query buckets on `completed_at`. With three timestamps adjacent
in one table that is a one-word mistake review will not catch, so a test asserts
the bucketing column directly.

### Redis Streams over Kafka

Simpler to operate and genuinely lightweight, with weaker durability and
retention guarantees. Fine at this scale. Because the wrapper depends on an
`EventBus` interface rather than on Redis, swapping the transport is an
infrastructure change, not an application rewrite — and the same interface is
what lets the entire instrumentation path be tested with no broker at all.

### At-least-once, made idempotent

The wrapper mints the event id *before* the call and it becomes the
`inference_logs` primary key, so redelivery collides on the PK
(`ON CONFLICT DO NOTHING`) instead of duplicating. The worker acks **after** the
write commits: acking first would silently lose events on every crash; acking
last makes redelivery the failure mode, which the upsert absorbs. Verified by
replaying the stream — `duplicates=1, inserted=0`, row count unchanged.

### `events_raw` + `inference_logs`

Roughly double the storage, in exchange for replay: a parsing bug can be fixed
and re-applied to the original payloads rather than leaving permanently wrong
rows. Worth it at this volume; at massive volume it needs a retention policy
attached.

### Regex redaction, not NER

Catches structured identifiers — emails, phone numbers, card-shaped digit runs,
SSN-shaped strings, API keys. **It does not catch names or addresses written in
prose.** A test asserts that gap so this claim stays honest, and Presidio is the
upgrade path.

Redaction runs in the API process *before* the event is published, so unredacted
content never reaches Redis, `events_raw`, or `inference_logs`. Redacting in the
worker instead would leave raw PII sitting exactly where an audit would look.

### A custom dashboard, not Prometheus + Grafana

Grafana is the industry-standard answer and would mean exposing `/metrics` in
Prometheus text format instead. This trades standardisation for one fewer moving
part in compose and Kubernetes, and for panels aimed at this system specifically
— the ingestion-lag panel exists because of a bug a generic dashboard would not
have surfaced.

### SSE, not WebSockets

One-directional token streaming over ordinary HTTP: it passes through proxies and
ingresses unchanged, browsers reconnect on their own, and Starlette streams it
natively. A WebSocket would buy bidirectionality this feature does not use.

Metadata and content are separate event types: `ttft` is its own frame rather
than a field on the first chunk, so a client rendering text never unpacks timing
data. Failures arrive as an `error` frame **on a 200**, because once headers are
sent the status line is committed.

---

## Known limitations

Stated rather than hidden.

- **`inference_logs.message_id` is always NULL.** The assistant message is
  written after the call returns, so publishing its id would race the worker into
  a foreign-key violation. Correlation is on `conversation_id` + `started_at`.
  Closing it properly means pre-generating the message UUID *and* making the FK
  soft (indexed, unenforced) — the worker can still outrun the API's commit even
  with a known id. That is real scope, not a patch.
- **Redis outage loses events.** The publisher gives up after ~200ms and the
  chat request succeeds anyway. A deliberate tradeoff, not an oversight.
- **Single Postgres instance.** Dashboard queries compete with the write path. A
  read replica for analytics is the obvious next step; a managed database is the
  real production answer, and the `StatefulSet` here exists so the stack is
  self-contained on a laptop.
- **Worker HPA scales on CPU**, which is a poor proxy. The right signal is Redis
  stream depth, which needs KEDA or a Prometheus adapter. Noted in the manifest
  rather than faked.
- **No authentication.** Single-user demo.
- **`ConversationStatus.CANCELLED` is unused.** Cancellation is recorded on the
  inference log, where it belongs; the enum value is reserved.
- **Secrets are committed as placeholders** so `kubectl apply -k` works on a
  fresh cluster. Real deployments use External Secrets or a CSI driver.

## What I'd do with more time

- **OpenTelemetry spans instead of a bespoke event schema.** The wrapper is
  already a span boundary in everything but name; exporting OTLP would make this
  interoperable with existing tracing rather than a private format.
- **Partition `inference_logs` by month**, with a retention policy for
  `events_raw`. The indexes are right for the query shapes; the table is not
  designed to grow forever.
- **Retry with jittered backoff in the worker.** It currently sleeps a flat
  second on failure, which thunders when several replicas recover together.
- **Alerting on the ingestion-lag panel.** The measurement exists; nothing pages
  on it. That is the gap between "visible" and "noticed."
- **Generate the frontend's API types from the OpenAPI schema** FastAPI already
  serves, instead of hand-writing them.
- **Read replica for dashboard queries**, isolating analytics from the write path.
