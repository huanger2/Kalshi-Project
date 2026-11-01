import os

import Accounts
import Kalshi

# Every sign-in hashes a password; at the real cost that's ~0.3s each, which
# adds up over hundreds of tests. Hashes stay correct, just cheap to make.
Accounts.PBKDF2_ITERATIONS = 1_000

# The first sign-in in a test creates the first admin (Accounts.bootstrap()),
# which moves the single-login era's Kalshi keys onto it. Never let a test
# read the real ones from AIAPIKEYS.env; tests of that step patch this back.
Kalshi.legacy_env_credentials = lambda env: None

# Encrypting a Kalshi key would otherwise generate secrets.key in the
# project folder (the data directory when DATA_DIR isn't set).
os.environ.setdefault("SECRETS_KEY", "test-only-secrets-key")

# The web service's background result checker (webapp/auto_settle.py) would
# start whenever a test runs the app's startup; tests call settle_once() directly.
os.environ["AUTO_SETTLE_MINUTES"] = "0"
