# Limitations

## Out of scope (not built)

- Authentication and authorization. Every endpoint is open.
- Users, sign-up, member onboarding or account-creation endpoints (accounts come from the seed script).
- Any UI or admin dashboard.
- Notifications (email, SMS, webhooks).
- Analytics and reporting.
- Multi-currency cash, fees or pricing in money.
- Kubernetes manifests and CI pipelines.
- Any feature not listed in the scope.

## Known limitations of what was built

- **5xx is treated as failure.** Per the brief, a partner 5xx after all retries is compensated. If a partner applied the credit and still answered 5xx on every retry, the member would be refunded and credited. Partners de-duplicate by transfer id, so a successful retry prevents this in practice, but the risk is not zero.
- **Reconciler trusts a "not found".** After the age gate, a partner's 404 is taken as "never applied". A request that reaches the partner after that point would double-pay. Tune `RECONCILE_MIN_AGE_SECONDS` above the partner's worst processing time.
- **Hot clearing row.** Every transfer out of a program locks that program's single clearing account row, so throughput per source program is bounded by row-lock time.
- **Bad partner config strands a transfer.** If a program's `adapter` name is not registered, the debit commits and the outbox job keeps failing until the config is fixed. It is logged on every relay cycle.
- **No rate deletion.** Rates can only be superseded by a new version.
- **No pagination cursor.** `GET /accounts/{id}/transfers` uses limit/offset.
- **Idempotency keys are kept forever.** There is no expiry or cleanup job.
- **Mock partners keep credits in memory.** Restarting a mock loses its records, which would make the reconciler see "not found".
