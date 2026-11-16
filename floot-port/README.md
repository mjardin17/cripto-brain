# Floot source snapshot

This directory contains a recovered source snapshot from the separate Floot project. It is preserved under `floot-port/` so it does not overwrite the existing repository application or its runtime.

- Runtime origin: Floot-hosted React 19 application with Floot serverless endpoints and managed Postgres.
- Snapshot scope: main page(s), styles, authentication endpoints and schemas, selected helpers, and the UI components included in this recovery batch.
- This is not a complete standalone export: Floot's generated/shared component library, runtime configuration, hosted assets, environment variables, database contents, and deployment wiring are not included.
- Secrets and live database state were intentionally excluded. Configure environment variables and platform services separately before running outside Floot.
- Treat this as a source recovery checkpoint, not a verified production-ready migration.
