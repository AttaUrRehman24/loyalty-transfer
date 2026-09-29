"""Services layer: business flows built on the domain rules.

Services depend only on `app.domain` (models, rules, ports) and `app.observability`.
They never import `api` or `infra`; real infrastructure is passed in by `app.main`.
"""
