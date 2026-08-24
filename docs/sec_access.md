# SEC Automated-Access Boundary

Status: `sec_access_policy/1.0` is implemented as a research-only prerequisite for the SEC
metadata intake lane. It does not grant source authority, catalyst confirmation, a Buy rating,
broker access, or execution permission.

Authority: [`AGENTS.md`](../AGENTS.md) overrides this document.

## Official Basis

The SEC states that `data.sec.gov` APIs require no authentication or API key, but automated users
must identify their application and contact, comply with fair-access policy, and remain at or
below 10 requests per second:

- https://www.sec.gov/search-filings/edgar-application-programming-interfaces
- https://www.sec.gov/about/developer-resources
- https://www.sec.gov/about/webmaster-frequently-asked-questions

This project uses a stricter ceiling of five requests per second, represented by a minimum
0.2-second inter-request gap.

## One-Time Local Setup

Run the helper in an attended local terminal. Do not paste the identity into chat, a tracked file,
or a command argument.

```bash
./scripts/setup_sec_user_agent.sh
```

The hidden value must contain an application identifier and exactly one reachable, non-placeholder
email address. The helper separately requires the exact response `YES` to acknowledge that this
identity will be transmitted to `data.sec.gov` in automated GET requests. It writes only these two
entries to the gitignored `secrets/trading.env`, while preserving every other line:

```text
SEC_USER_AGENT=<private application and contact identity>
SEC_USER_AGENT_APPROVED=1
```

The directory is forced to mode `0700`, the file to `0600`, symlinks are refused, and the update is
atomic. The identity is validated over stdin and is never printed, hashed, included in an artifact,
or passed as a command-line argument. Setup performs no network request.

## Readiness And Collection

Generate a no-network, redacted receipt:

```bash
python3 scripts/sec_access.py readiness --target YYYY-MM-DD \
  --out data/runtime/health/YYYY-MM-DD_sec_access_readiness.json
```

The 05:00 intake runner performs this check before collection. A structural policy/readiness
failure stops the intake runner. A missing or invalid identity blocks only SEC collection; BLS and
local adapters continue. `data_lake_intake.py` validates identity and consent again immediately
before calling the adapter, and `sec_edgar.py` validates once more immediately before opening the
connection.

The transport boundary fixes all of the following:

- HTTPS GET to exactly `data.sec.gov/submissions/`;
- no redirects, so the private identity cannot be forwarded to another host;
- 15-second maximum timeout and 10,000,000-byte maximum response;
- JSON media types only;
- five requests per second maximum;
- normalized filing metadata plus a source-payload hash only; raw response bytes are discarded.

Readiness and intake artifacts contain only booleans, modes, bounded reason codes, policy hashes,
and collection metadata. They never contain the identity, contact address, identity length, or an
identity hash.

## Attended Probe

The probe is deliberately absent from every scheduler and runner. It performs one fixed Apple CIK
submissions request only when both stored consent and an explicit per-invocation confirmation are
present:

```bash
python3 scripts/sec_access.py probe --target YYYY-MM-DD \
  --confirm-external-pii-transmission \
  --out data/runtime/health/YYYY-MM-DD_sec_access_probe.json
```

Its receipt stores only whether the payload was an object, matched the fixed CIK, and exposed the
expected recent-filings array. It does not persist the response body or identity. Omitting the flag
produces a blocked receipt without opening a network connection.

## Fail-Closed States

Expected non-ready reason codes include missing identity, missing transmission consent, placeholder
or unreachable contact, malformed identity, unsafe file permissions, or symlinked storage. SEC
collection records `missing_required_env` or `invalid_required_env`; the downstream SEC shadow lane
remains an honest waiting state with zero source records and no fabricated review queue.

Run the focused offline regressions with:

```bash
python3 scripts/test_sec_access.py
python3 scripts/test_setup_sec_user_agent.py
python3 scripts/test_provider_adapters.py
python3 scripts/test_data_lake_foundation.py
python3 scripts/test_data_intake_schedule.py
python3 scripts/test_sec_review_shadow.py
```

Rollback removes the SEC access policy/schemas/tools and the readiness call from
`run_data_intake.sh`, then restores SEC collection to disabled. Do not roll back by weakening
identity validation, allowing redirects, increasing the rate, persisting raw payloads, or scheduling
the attended probe.
