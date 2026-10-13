"""Cross-tenant ("system") queries. ADR-0010 confines them to this package.

Everything outside this package takes a :class:`~rl_core.tenancy.TenantContext`. The
functions here cannot, because no tenant is known yet or because the caller works for all
tenants by design:

* :mod:`.identity`: creating organizations, and looking up an API key by its public prefix,
  which is how a request *finds out* its tenant.
* :mod:`.jobs`: the job runner claims due jobs across tenants, then works on each claimed
  job in that job's own TenantContext.
* :mod:`.outbox`: the relay publishes every tenant's queued messages.
* :mod:`.partitions`: a ``spans`` partition holds one day of every tenant's spans.
* :mod:`.telemetry`: tenant existence checks for span batches, and rollup retention.

Do not add a function here when a tenant-scoped one in :mod:`rl_db.repositories` would do.
"""
