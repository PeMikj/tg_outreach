# Schema Files

SQL files in this directory define the database bootstrap for the PoC runtime.

- `001_init.sql`
  initial schema for the Postgres-backed runtime

The current application still applies lightweight additive compatibility checks at startup
for non-destructive column additions, but the primary schema definition lives here rather
than inline in the application module.
