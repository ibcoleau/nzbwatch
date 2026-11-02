# Changelog

## 0.2.9

- Configurable 14-day terminal history/event retention and 7-day successful completed-watch retention (each may be disabled with 0).
- Persist successful watch completion time even after detailed results are pruned. Preserve the grabs ledger.
- Three Pushover notification privacy modes, with consistent redaction across event types.
- Split Activity into Active, Errors and Completed (plus skipped duplicates); paginate event log via authenticated API.
- Settings categories displayed as tabs; add light/dark theme and custom accent color.
- Nonbreaking SQLite/config migration from v0.2.8.
