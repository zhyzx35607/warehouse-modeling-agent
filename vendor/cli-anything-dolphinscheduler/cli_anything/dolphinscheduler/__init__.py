"""cli-anything-dolphinscheduler.

A stateful command-line interface to a running Apache DolphinScheduler
instance. This package is a structured REST client to the real
DolphinScheduler API server (the "backend engine"); it does NOT reimplement
workflow scheduling in Python.

The DolphinScheduler API server is a hard dependency. Point the CLI at a
running instance via ``--api-url`` / ``DS_API_URL`` and authenticate with an
access token (``--token`` / ``DS_TOKEN``) or username/password.
"""

__version__ = "1.0.0"
