"""Operational protocol constants, not clinical policy or an agreed service level."""
JOB_LEASE_SECONDS = 600
OUTBOX_STATES = ("queued", "retry", "failed", "available_in_app", "superseded", "sent")
JOB_STATES = ("queued", "running", "succeeded", "failed", "cancelled", "applied")
