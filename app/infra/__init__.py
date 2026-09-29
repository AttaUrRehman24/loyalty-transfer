"""Infrastructure layer: Postgres, Redis and partner HTTP adapters.

Implements the Protocols in `app.domain.ports`. Imports `app.domain` and
`app.observability` only; it never imports `services` or `api`.
"""
