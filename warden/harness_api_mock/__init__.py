"""Mock harness service — a standalone FastAPI app byte-faithful at the HTTP wire
boundary to the real Runs API, with the agent run faked by deterministic canned
event scripts (supplied by the active profile; see ``profile_loader``).
"""
