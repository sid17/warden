# Scripts

Standalone utilities for testing, debugging, and maintenance. Not part of the orchestrator's runtime — these are tools you run manually.

| Script | Purpose |
|--------|---------|
| `otel-waterfall-test.py` | Validate the OTel trace pipeline end-to-end — sends a test message through ChatAPI and verifies spans appear in the collector |
| `openharness-agent-trace-test.py` | Test OpenHarness provider with trace capture — validates Langfuse observation hierarchy |
| `openharness-event-dump.py` | Dump raw OpenHarness event stream to stdout — useful for debugging message handler transforms |
| `clear-sessions.sh` | Clear the SQLite session database (`data/sessions.db`) |
