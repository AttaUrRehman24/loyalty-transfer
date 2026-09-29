"""API layer: HTTP routes, request/response schemas, error mapping.

Imports only `app.services`, `app.domain` and `app.observability`. Routes stay thin:
validate input, call one service method, shape the output.
"""
