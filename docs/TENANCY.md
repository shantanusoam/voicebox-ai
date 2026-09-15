# Multi-tenancy

A tenant **is** a workspace. Every table in `db.py` was already workspace-scoped
and every query already filtered by it, so tenancy here is about two things:
routing a call to the right tenant, and proving one tenant can never reach
another's data.

## Numbers are tenant resources

```text
+918000001001  ->  aarogya-clinic   (Aarogya Clinic, Hinglish, voice shimmer)
+918000001002  ->  sunrise-dental   (Sunrise Dental, English,  voice alloy)
```

`tenant_numbers` carries the provider, the provider's own id for the number,
inbound/outbound flags, `caller_id_verified` and a KYC status. The application
never learns which telephony provider is underneath, so a tenant can move from
a rented DID to their own SIP trunk without touching business logic.

```bash
curl -X POST .../api/platform/tenants -H "Authorization: Bearer $ADMIN" \
  -d '{"id":"aarogya-clinic","name":"Aarogya Clinic","language":"hinglish","voice":"shimmer"}'
curl -X POST .../api/platform/tenants/aarogya-clinic/numbers -H "Authorization: Bearer $ADMIN" \
  -d '{"number":"08000001001","provider":"exotel"}'
curl -X POST .../api/platform/numbers/+918000001001/activate -H "Authorization: Bearer $ADMIN" \
  -d '{"caller_id_verified":true,"outbound":true}'
```

Platform endpoints require the **admin token specifically**. A signed-in tenant
session is deliberately not enough: a tenant must never be able to create or
inspect another.

## Inbound routing

A carrier is inconsistent about number format. `+918012345678`,
`918012345678`, `08012345678`, `8012345678` and `080-1234-5678` all turn up for
one Indian number, so `normalise()` converges them before lookup; routing on
the raw string loses calls.

**An unrecognised number is refused, never defaulted.** Answering a stranger's
call with someone else's business identity, and writing to their database, is
worse than not answering at all. A number also does not route until its KYC
status is `active`.

### SIP carries no dialled number

AudioSocket has no field for it, and that number is what selects the tenant, so
it arrives out of band: the dialplan registers `uuid -> called/caller` over HTTP
before connecting, and the bridge correlates on the UUID.

Two details that cost a test run each:

- `+` means *space* in a URL query, so `CURL()` delivered `' 918000001001'`.
  `normalise()` absorbed it, but the dialplan now percent-encodes rather than
  depending on that.
- An explicit extension beats a pattern match in Asterisk, so an echo test on
  `1002` silently shadowed the tenant whose DID ended `1002`. The echo test now
  lives on `9999`, outside the DID range.

## Outbound caller ID

Caller ID cannot be chosen freely. A provider will only present a number you
are authorised to use, and India is stricter than most: domestic outbound
generally has to originate from a number rented through the provider, and the
verified-external-CLI arrangements available elsewhere do not apply.

`NumberService.caller_id()` therefore refuses unless the number is both
`outbound` and `caller_id_verified`, failing locally rather than having a
carrier reject or silently rewrite the call. Design around authorised
per-tenant numbers, not caller-ID substitution.

Caller ID *name* (CNAM) is a separate mechanism and is not reliably available in
India. Assume the receiving handset shows the number.

## Per-tenant identity

Each tenant carries its own `name`, `language` (`en` / `hi` / `hinglish`),
`voice`, `greeting` and `persona`. `realtime.py` builds the system prompt and
selects the voice from the tenant record, so each business sounds like itself
rather than sharing one generic receptionist. The Hinglish rule asks for Hindi
sentence structure with English clinical and scheduling terms, and never to
translate a name.

## Isolation

`tests/test_tenancy.py` tests this from several directions rather than trusting
the WHERE clauses:

- records, summaries and calls are scoped per tenant
- a `call_id` belonging to another tenant raises `call_missing`
- a tool executed in one tenant's `ToolContext` cannot reach another's data
- verification established in one tenant does not leak into another
- two tenants **can** book the same wall-clock slot: the `one_active_slot`
  unique index is per workspace, because two clinics share a clock, not a
  calendar

## Known limit

Per-call locks and admission control still live in one process's memory. Run a
second worker and the ordering guarantee behind `hold_slot -> confirm_booking`
is gone, leaving only the unique index. Moving that state to Postgres advisory
locks or Redis is the prerequisite for running tenants across more than one
worker - see `docs/ARCHITECTURE.md`.
