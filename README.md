# Agentic CDP (v0.1)

A customer data platform in which **AI agents propose and deterministic gates decide**. Every merge, schema mapping, consent decision and erasure is recorded in a hash-chained ledger, and the whole identity graph can be recomputed from stored records and recorded decisions **without calling any model** (hermetic replay).

The design goal is autonomy you can audit. The system maps new sources, resolves identities, builds golden records and enforces consent by itself. It sends to a human only the questions whose answer would change the result. It halts, rather than publishes, when a run looks catastrophic.

```
 Sources ──► Connectors ──► Schema-mapping agent ──► gates M1–M6 ──► Canonical records
 (SFDC, HubSpot,            (LLM or deterministic)   (vocabulary, value      (normalized + match keys,
  Shopify, Stripe,                                     validation, identity    consent events)
  Segment, CSV, SQL)                                   coverage, consensus)          │
                                                                                     ▼
 Golden profiles ◄── Survivorship ◄── Constrained clustering ◄── Gates A1–A5 ◄── Blocking + scoring
 (lineage per field,  (documented,     (no 2 DOBs / suffixes /     (truthful        (Fellegi–Sunter bands;
  stable ids, aliases) deterministic)   cannot-links per cluster)   evidence,         gray zone → adjudication
       │                                        │                   sufficiency)      agent)
       ▼                                        ▼
 Consent-gated export              Circuit breakers ──► halt, don't publish
       └─────────────── everything ──► hash-chained ledger (HMAC digests only, no PII) ──► hermetic replay
```

## What it does

| Capability | How |
|---|---|
| **Connectors** | Salesforce (SOQL and `nextRecordsUrl`), HubSpot (list API and search API for incremental syncs), Shopify (Link-header pagination), Stripe (`starting_after`), Segment (signed webhook, `identify` calls), CSV, JSONL, any SQLAlchemy database. Incremental cursors, retry with `Retry-After`, per-vendor egress allowlist, HTTPS only, secrets referenced by env-var name only. |
| **Autonomous schema mapping** | The agent maps unknown columns into a closed canonical vocabulary. Gates validate every mapped column against its actual values (≥90% must parse), reject invented targets, require an identity key, and cross-check an LLM against the deterministic baseline. If the agent is rejected, the deterministic fallback is used. If the result is uncertain, the mapping goes to review. |
| **Schema drift** | A changed field set, or a previously empty column that starts carrying values, triggers a re-map. A batch whose identity columns stop validating is quarantined (rolled back) and not ingested. |
| **Identity resolution** | Blocking, then weighted field comparison (nicknames, gmail dot/plus folding, E.164 phones, typo-tolerant names), then three bands. Auto-merge requires a strong identifier (email, phone or DOB). The gray zone goes to the adjudication agent, gated. |
| **Hard-negative defenses** | *Ambiguous identifiers*: a phone, email or name-at-address seen on provably different people (different DOBs, different generational suffixes, clearly different first names) stops counting as evidence. That covers household landlines, family inboxes, `noemail@store.com` placeholders and father/son pairs. *Constrained clustering*: a union that would put two DOBs, two suffixes or a cannot-link in one cluster is refused, which blocks transitive over-merge. |
| **Golden records** | Per-field survivorship (trust rank, recency, frequency). The address is taken as a whole block from one record and never mixed across records. Every field carries lineage. Profile ids stay stable across runs, and retired ids redirect via aliases. |
| **Consent** | Latest event wins and ties resolve to deny. An event is recorded only when a value *changes*. Opt-outs (never grants) propagate along a shared email or phone and to identity-uncertain review neighbors. Export is fail-closed: only an explicit, current grant is exported. |
| **Erasure (GDPR/CCPA)** | Deletes PII from the raw, canonical and golden layers and from decision caches. SQLite runs with `secure_delete`. The ledger keeps HMAC digests only. Suppression hashes block re-ingestion from lagging sources, but identifiers still held by other people (such as a family inbox) are never suppressed. Erasing by an identifier shared across profiles is refused, so you must erase by profile id. |
| **Human-in-the-loop** | The review queue shows only questions whose answer would change the graph. Pairs that end up merged anyway become *moot*, and duplicate questions between the same two clusters are *superseded*. Steward decisions become must-link or cannot-link constraints. Steward-authored mappings pass the same gates as the AI's unless an admin forces them, and the force is ledgered. |
| **Circuit breakers** | A run whose largest cluster exceeds its limit, or whose profile count collapses by more than 20%, is **halted, not published**. Only an admin can force it. |
| **Audit** | Every state change commits in the same transaction as its ledger event. `/audit/verify` checks the chain. `/audit/replay` recomputes the graph from records and recorded decisions and compares digests. |

## Quick start

```bash
pip install -e ".[test]"
python -m pytest -q                     # 40 tests
python -m bench.run                     # full benchmark + gates → bench/out/REPORT.md
                                        # (committed results: bench/results/, write-up: GATES.md)

export ACDP_PII_HMAC_KEY=$(openssl rand -hex 32)
export ACDP_API_KEYS="$(openssl rand -hex 16):admin"
acdp source-add hubspot hubspot --config '{"token_env":"HUBSPOT_TOKEN"}' --trust 60
acdp source-add crm salesforce --config '{"instance_url":"https://acme.my.salesforce.com","token_env":"SF_TOKEN"}' --trust 80
acdp run                                # ingest all sources, then resolve (the autopilot)
acdp serve                              # console at http://127.0.0.1:8080
```

Docker: `cp .env.example .env`, fill it in, then `POSTGRES_PASSWORD=... docker compose -f deploy/docker-compose.yml up`.

**Using an LLM.** Set `ACDP_LLM_PROVIDER=anthropic` (or `openai`, which works with any OpenAI-compatible server including vLLM and Ollama) plus `ACDP_LLM_MODEL` and `ACDP_LLM_API_KEY`. By default (`ACDP_LLM_PII_MODE=derived`) the model sees masked value shapes and computed comparisons, never raw PII. Up to 2,000 gray-zone calls per run are sent to the model. Any overflow goes to the deterministic adjudicator, which is gated the same way.

## API (X-API-Key; roles viewer < steward < admin)

`GET /stats` · `GET|POST /sources` · `PUT /sources/{id}/mapping` · `POST /sources/{id}/ingest` · `POST /runs` · `GET /review` · `POST /review/{id}` · `GET /profiles?q=` · `GET /profiles/{id}` (lineage, members, consent, alias-following, 410 when erased) · `POST /profiles/{id}/split` · `POST /constraints` · `POST /privacy/erasure` (admin) · `GET /export/{purpose}` · `GET /audit/verify` · `GET /audit/replay` · `GET /audit/events` · `POST /ingest/segment/{source}` (HMAC-SHA1 `X-Signature`)

If no API keys are configured, every request is refused.

## Layout

```
acdp/engine.py        the only writer; ingest, resolve, publish, replay, review, erase, export
acdp/gates.py         deterministic gates (mapping M1–M6, adjudication A1–A5)
acdp/resolution.py    compare/score/band, ambiguity, name rarity, constrained clustering, survivorship, consent
acdp/agents/          Agent protocol; deterministic, Anthropic, OpenAI-compatible
acdp/connectors/      file/SQL + Salesforce, HubSpot, Shopify, Stripe, Segment
acdp/ledger.py        hash chain
acdp/api.py, console/ FastAPI + single-file operator console
bench/                synthetic universe with ground truth, vendor mocks, adversarial agents, gate runner
```

## Known limitations (read before production)

- **SaaS connectors are verified only against mocks** that reproduce each vendor's documented REST shapes, pagination and auth. They have not been run against live tenants. Stripe's incremental sync keys on `created`, so updates to existing customers need a full resync (a `customer.updated` webhook is on the roadmap). API versions are pinned in config.
- **Resolution is batch.** Each run recomputes the graph, with blocking and cached pair decisions. That is fine into the low hundreds of thousands of records on one node. Streaming or incremental resolution and warehouse-native execution are roadmap items. The 5,000-person benchmark timing is in GATES.md.
- **Phone normalization** without libphonenumber is NANP-aware only. Other countries require a leading `+`.
- **Single tenant per deployment.** Isolate tenants by running separate databases and processes.
- **Father/son pairs with identical names, a shared landline, and no DOB or suffix anywhere in their records** can still merge. The benchmark measures this (see GATES.md). No rule can separate records that carry no distinguishing evidence.
- **Consent correctness is bounded by recall.** An opt-out on a record that the engine cannot link to the person (no shared identifier and no gray-zone pair) cannot reach that person's profile. The pre-registered consent gate fails for this reason. See GATES.md.
