"""Tenancy: who owns which data, and how one deployment keeps organisations apart.

Hierarchy
- The organisation is the tenant. Every piece of customer data belongs to exactly one organisation.
- A department is a scope inside an organisation: people, rules, rotas and roles are granted per department.
- A workspace is a rota scenario inside a department (a draft, a fork or the published baseline). It is not a
  tenant and never widens one.

One tenant context per entry point
Every entry point resolves one validated context (organisation, department, principal) before it touches customer
data, and the whole unit of work runs under it:
- Requests: the signed-in session names its department; the department names its organisation.
- Streams: the context is fixed when the stream opens, and each event is checked against it before it is sent.
- Calendar feed tokens: the token resolves to one feed row, which names its department.
- Sign-in callbacks: the flow row written when sign-in starts names the organisation; the callback never takes it
  from the provider's response.
- Background jobs and outbox messages: the claimed row names its department; the worker sets the context per
  claimed row, never per process.
- Operator tools and scripts: they name the organisation explicitly and refuse to run without one.
The context is set per transaction, as the PostgreSQL setting that row-level security reads and the value the ORM
filter reads. A missing or mismatched context fails closed.

Principals and roles
- A principal is a sign-in identity (issuer and subject, or a local account). Identities are platform data.
- A principal reaches an organisation only through a membership in it, and a department only through role
  bindings there; role names come from ROLE_NAMES. A session binds one principal to one department at a time.
- Platform operators act outside every organisation; their access to one is explicit, time-bound and audited.

Data split
- Platform data: identities and accounts, sign-in configuration and flows, rate limits, branding, and worker and
  system state. It never holds customer content.
- Organisation data: everything else. A row is owned directly (it carries organisation_id) or through a named
  parent (a shift through its workspace, an assignment through its shift). Stored JSON, job and outbox payloads,
  files and cache entries belong to the row or scope they were made for, and their keys carry that scope.
- Every new table, stored format, cache namespace and job payload declares its owner in this module.

Placement
- Default: one pooled PostgreSQL database. Every organisation-owned table carries organisation_id NOT NULL. Foreign
  keys between organisation-owned tables are composite (organisation_id, parent id), so no row can point into
  another organisation. Keys that customers choose (codes, slugs, names) are unique per organisation.
- Backstop: row-level security is enabled and forced on every organisation-owned table, with a policy comparing
  organisation_id to the transaction's setting. The application connects as a role that owns no table and has no
  BYPASSRLS; migrations run as a separate owner role.
- A customer who needs stronger isolation gets a dedicated deployment of the same build with its own database and
  one organisation; the code path is the same.

Locks, counters and caches
- The planned write lock is per department and per workspace rather than one global lock. Its key carries the
  organisation, and locks are always taken in the order organisation, department, workspace.
- Revision counters, caches and read projections are keyed by organisation and department, so one organisation's
  writes never invalidate or serve another organisation's reads.
"""
