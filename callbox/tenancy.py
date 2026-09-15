"""Numbers as tenant resources, and the routing that makes a call tenant-aware.

A called number resolves to exactly one tenant; a tenant's outbound caller ID
is one of its own authorised numbers. The application never learns which
telephony provider is underneath, so a tenant can move from a rented DID to
their own SIP trunk without touching business logic.

Caller ID cannot be chosen freely. A provider will only present a number you
are authorised to use, and India is stricter than most: domestic outbound
generally has to originate from a number rented through the provider, and
verified-external-CLI arrangements available elsewhere do not apply. This
module therefore refuses to present a number that is not both `outbound` and
`caller_id_verified` - failing locally, rather than having a carrier reject or
silently rewrite the call.
"""
import re

from .errors import AppError

E164 = re.compile(r'^\+[1-9]\d{7,14}$')

PROVIDERS = ('plivo', 'exotel', 'telnyx', 'twilio', 'sip', 'sarvam', 'lab')
STATUSES = ('pending_kyc', 'active', 'suspended')


def normalise(number):
    """Accept the shapes a carrier actually sends and return E.164.

    Inbound webhooks and SIP headers are inconsistent: '+918012345678',
    '918012345678', '08012345678' and '8012345678' all turn up for one Indian
    number, so routing on the raw string loses calls.
    """
    if not isinstance(number, str):
        return None
    digits = re.sub(r'[^\d+]', '', number.strip())
    if not digits:
        return None
    if digits.startswith('+'):
        return digits if E164.match(digits) else None
    digits = digits.lstrip('0')
    if not digits:
        return None
    # A bare Indian subscriber number is 10 digits; anything longer already
    # carries a country code.
    candidate = '+91' + digits if len(digits) == 10 else '+' + digits
    return candidate if E164.match(candidate) else None


class NumberService:
    """Resolve inbound calls to tenants and pick authorised outbound caller IDs."""

    def __init__(self, db):
        self.db = db

    # -- provisioning --------------------------------------------------
    def assign(self, workspace, number, provider, inbound=True, outbound=False,
               status='pending_kyc', provider_number_id=None):
        e164 = normalise(number)
        if not e164:
            raise AppError(422, 'bad_number', 'Provide a valid phone number in E.164 form.')
        if provider not in PROVIDERS:
            raise AppError(422, 'bad_provider',
                           'Unknown telephony provider: ' + ', '.join(PROVIDERS) + '.')
        if status not in STATUSES:
            raise AppError(422, 'bad_status', 'Unknown number status.')
        return self.db.add_number(workspace, e164, provider, inbound=inbound, outbound=outbound,
                                  status=status, provider_number_id=provider_number_id)

    def activate(self, number, caller_id_verified=False, outbound=None):
        """Mark a number live once the provider's KYC has actually completed."""
        e164 = normalise(number)
        return self.db.set_number_status(e164, status='active',
                                         caller_id_verified=caller_id_verified,
                                         outbound=outbound)

    def numbers(self, workspace=None):
        return self.db.numbers(workspace)

    # -- inbound -------------------------------------------------------
    def resolve(self, called_number):
        """Which tenant owns the number that was dialled.

        Returns None rather than guessing. Routing an unrecognised number to
        a default tenant would answer a stranger's call with someone else's
        business identity and write to their database.
        """
        e164 = normalise(called_number)
        if not e164:
            return None
        row = self.db.number(e164)
        if not row or not row['inbound'] or row['status'] != 'active':
            return None
        return row['workspace']

    def resolve_or_raise(self, called_number):
        workspace = self.resolve(called_number)
        if workspace is None:
            raise AppError(404, 'unrouted_number',
                           'That number is not assigned to an active tenant.')
        return workspace

    # -- outbound ------------------------------------------------------
    def caller_id(self, workspace):
        """The number this tenant is permitted to present, or an error."""
        candidates = [n for n in self.db.numbers(workspace)
                      if n['outbound'] and n['status'] == 'active']
        if not candidates:
            raise AppError(409, 'no_outbound_number',
                           'This tenant has no active number approved for outbound calls.')
        verified = [n for n in candidates if n['caller_id_verified']]
        if not verified:
            raise AppError(409, 'caller_id_unverified',
                           'This tenant has no verified caller ID. A provider will not present '
                           'an unauthorised number, and Indian domestic calls generally must '
                           'originate from a number rented through the provider.')
        return verified[0]['e164']

    # -- tenant view ---------------------------------------------------
    def tenant(self, workspace):
        settings = self.db.settings_or_none(workspace)
        if settings is None:
            raise AppError(404, 'workspace_missing', 'Workspace not found.')
        return {'id': workspace, 'settings': settings,
                'numbers': self.db.numbers(workspace)}

    def tenants(self):
        return [{'id': w['id'], 'name': w.get('name'),
                 'numbers': [n['e164'] for n in self.db.numbers(w['id'])]}
                for w in self.db.workspaces()]
