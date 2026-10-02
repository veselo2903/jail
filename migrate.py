"""Run migrations and historical backfills before starting workers."""
import os
os.environ.pop("JAIL_SKIP_MIGRATIONS", None)
import app  # imports perform all versioned migrations under the database lock
