# BLS Release Calendar

## Purpose

The BLS calendar adapter adds official scheduled macro-event risk to the free source ensemble. It
reads the Bureau of Labor Statistics ICS calendar, converts Eastern Time correctly through
daylight-saving transitions, and stores bounded point-in-time schedule observations.

The official calendar is documented at
<https://www.bls.gov/schedule/news_release/bls.ics>. BLS states that calendar times are Eastern Time
and that the calendar is updated when schedules change.

## Hard Boundary

A calendar entry proves only what was scheduled when this system observed that calendar version.
It does not prove that:

- the release actually occurred at that time;
- an API observation was publicly available at that time;
- the release contained a particular reference period or revision;
- a release-time event study is point-in-time safe.

Every payload therefore pins `scheduled_only=true`, `actual_release_confirmed=false`,
`observation_publication_timestamp_granted=false`, and
`release_time_event_study_eligible=false`. Original BLS time-series records remain
retrieval-only.

## Data Contract

The adapter:

- accepts only the exact HTTPS BLS calendar endpoint and `text/calendar`;
- caps response size and timeout;
- unfolds RFC 5545 continuation lines;
- accepts only explicit UTC or US-Eastern/America/New_York timed events;
- skips all-day holidays;
- preserves cancellations and calendar sequence values;
- maps exact release families to configured series IDs without claiming an observation-period join;
- hashes and discards raw ICS bytes.

The default map links Consumer Price Index to `CUUR0000SA0` and Employment Situation to
`LNS14000000`. This helps the macro agent recognize upcoming risk; it does not create actual-value
authority.

## Commands

```bash
python3 scripts/test_bls_release_calendar.py
python3 scripts/free_source_ensemble.py --check
python3 scripts/data_lake_intake.py --target YYYY-MM-DD --dry-run --no-network
```

The daily intake stores these events under the optional `release_calendar` dataset. That dataset
does not satisfy `macro_observations`, daily-history, corporate-action, or security-master
readiness.
