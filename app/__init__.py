"""Loyalty points transfer engine.

This package holds the whole service. It is split into layers:
`api` (HTTP) -> `services` (business flows) -> `domain` (rules and models) <- `infra`
(Postgres, Redis, partner HTTP clients). `main.py` and `worker.py` wire the layers together.
"""
