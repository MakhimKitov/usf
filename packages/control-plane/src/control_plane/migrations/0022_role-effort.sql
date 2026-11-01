-- 0022 — a role carries `effort` beside its model (#119).
--
-- The SDK takes an effort level — how hard the model thinks within one model,
-- trading thoroughness for tokens — and the row is where a burst's composition
-- lives (D5), so the level is an open field like `model`: `role set` moves it,
-- audited, and the next burst reads it. platform_app already holds UPDATE on
-- roles (0014), which covers a new column; no grant changes.
--
-- Nullable with no default: NULL is "leave it to the CLI's default for the
-- model", which is exactly what every burst ran at before this column, so the
-- seeded rows need no backfill and the ADD is catalog-only.
--
-- No CHECK on the levels, deliberately, as `mount_mode` has none: this is an
-- open field, not a wall. The levels are the SDK's enumeration, checked in code
-- at `role set` and again when a row is loaded for a spawn — a new SDK level is
-- a code change, never a migration.

ALTER TABLE roles ADD COLUMN effort text;
