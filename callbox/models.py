from typing import Literal
from pydantic import BaseModel, Field, ConfigDict, field_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)


class Login(StrictModel):
    token: str = Field(min_length=16, max_length=200)


class NewCall(StrictModel):
    label: str = Field(default='Test caller', min_length=1, max_length=80)
    language: Literal['en', 'hinglish'] = 'en'
    source: Literal['browser', 'simulator'] = 'browser'
    provider: Literal['local', 'openai'] = 'local'
    consent: bool = False


class Turn(StrictModel):
    text: str = Field(min_length=1, max_length=2000)
    request_id: str = Field(min_length=8, max_length=100, pattern=r'^[A-Za-z0-9_-]+$')


class DeviceCreate(StrictModel):
    name: str = Field(min_length=2, max_length=64)
    kind: Literal['simulator', 'esp32-lab'] = 'simulator'


class Settings(StrictModel):
    name: str = Field(min_length=2, max_length=80)
    business: Literal['clinic', 'salon', 'service'] = 'clinic'
    timezone: Literal['Asia/Kolkata'] = 'Asia/Kolkata'
    open_hour: int = Field(ge=0, le=22)
    close_hour: int = Field(ge=1, le=23)
    slot_minutes: Literal[15, 20, 30, 60] = 30
    fee: int = Field(ge=0, le=100000)
    address: str = Field(min_length=2, max_length=180)
    staff_label: str = Field(min_length=2, max_length=80)
    # Per-tenant voice identity. Each tenant's agent sounds like that tenant,
    # rather than every business sharing one generic receptionist.
    language: Literal['en', 'hinglish', 'hi'] = 'en'
    voice: str = Field(default='alloy', min_length=2, max_length=40)
    greeting: str = Field(default='', max_length=400)
    persona: str = Field(default='', max_length=600)
    @field_validator('close_hour')
    @classmethod
    def after_open(cls, value, info):
        if value <= info.data.get('open_hour', 0):
            raise ValueError('Closing time must be later than opening time.')
        return value


class Confirmation(StrictModel):
    confirm: Literal[True]


class ProviderIntent(StrictModel):
    intent: Literal['book', 'hours', 'fee', 'location', 'human', 'medical', 'emergency', 'unknown']
    date: str | None


class TenantCreate(StrictModel):
    id: str = Field(min_length=2, max_length=40, pattern=r'^[a-z0-9][a-z0-9-]*$')
    name: str = Field(min_length=2, max_length=80)
    language: Literal['en', 'hinglish', 'hi'] = 'en'
    voice: str = Field(default='alloy', min_length=2, max_length=40)
    persona: str = Field(default='', max_length=600)


class NumberAssign(StrictModel):
    number: str = Field(min_length=6, max_length=24)
    provider: Literal['plivo', 'exotel', 'telnyx', 'twilio', 'sip', 'sarvam', 'lab'] = 'lab'
    inbound: bool = True
    outbound: bool = False
    status: Literal['pending_kyc', 'active', 'suspended'] = 'pending_kyc'


class NumberActivate(StrictModel):
    caller_id_verified: bool = False
    outbound: bool | None = None
