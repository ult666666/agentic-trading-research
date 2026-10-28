# Databento Shadow Onboarding

> **Public snapshot:** this document comes from the private repository and describes the
> private system as of the dates it gives. Many files it names, such as `config/`, `data/` and
> `prompts/` files, `AGENTS.md` and many scripts, are private and not included here. Links point
> only to files in this repository.

Status: account setup is the only manual gate. No market-data download, subscription, purchase,
provider authority, model scoring, broker action, or order action is performed by this workflow.

## Decision

Databento historical usage-based access is the current evaluation path. Its historical service
supports cost quotes before download and does not require a subscription. New accounts advertise
$125 of historical-data credit. The evaluation campaign has a fixed total spending cap for
Databento, and the local probe adds a smaller cap per quoted request.

Massive remains disabled. Its published individual market-data terms default market data to
display use and require separate licensing for non-display use or an investment strategy. A $199
individual plan therefore does not satisfy this system's internal backtesting/model-research
license gate. No Massive credential should be purchased until written rights are obtained.

Authoritative references checked 2026-07-11:

- https://databento.com/pricing/
- https://databento.com/docs/api-reference-historical/metadata/metadata-list-schemas
- https://databento.com/docs/api-reference-historical
- https://databento.com/corporate-actions
- https://massive.com/legal/individuals-terms-of-service
- https://massive.com/terms/market_data_terms.pdf

## Manual Gate

The account owner must create the Databento account and personally accept the applicable service
and market-data terms. Do not send an API key in chat. After Databento shows the API key, run:

```bash
./scripts/setup_databento_key.sh
```

The prompt hides input and stores the key only in git-ignored `secrets/trading.env` with mode 600.

## Read-only Probe

After local key setup:

```bash
python3 scripts/databento_onboarding.py
```

The probe calls only `metadata.list_datasets` and `metadata.get_cost`. It verifies authentication,
checks `EQUS.MINI`, and quotes two small historical requests. It never calls a time-series download
or batch endpoint. The sanitized receipt is written beneath
`data/runtime/provider_bakeoff/onboarding/` and contains neither the key nor account data.

`ready_for_manual_license_review` means only that authentication and cost quoting worked. The
Databento license-manager terms must still be reviewed against every right in
`config/provider_license_matrix.template.json`. Provider adapters remain disabled and shadow-only
until that separate gate and the 30-session bake-off pass.
