from dataclasses import dataclass
from pathlib import Path
import contextlib
import os
import secrets

ROOT = Path(__file__).resolve().parents[1]


def load_env(path: Path) -> None:
    """Small .env reader: no evaluation, interpolation, or shell execution."""
    if path.exists():
        for line in path.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith('#') or '=' not in line:
                continue
            key, value = line.split('=', 1)
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


@dataclass
class Config:
    data_dir: Path
    admin_token: str
    demo: bool = True
    secure_cookie: bool = False
    api_key: str = ''
    stt_model: str = 'gpt-4o-mini-transcribe'
    tts_model: str = 'gpt-4o-mini-tts'
    intent_model: str = 'gpt-4.1-mini'
    voice: str = 'coral'
    workspace: str = 'clinic-demo'
    session_seconds: int = 8 * 3600
    max_active_calls: int = 12
    max_call_seconds: int = 1800
    provider_kind: str = 'openai'
    openrouter_key: str = ''
    openrouter_stt_model: str = 'google/gemini-3.8-flash'
    openrouter_intent_model: str = 'google/gemini-3.8-flash'
    openrouter_tts_model: str = 'openai/gpt-audio-mini'
    openrouter_voice: str = 'alloy'
    openrouter_tts_rate: int = 24000
    max_turn_cost_usd: float = 0.05

    # Voice runtime selection. `openai` is the existing native full-duplex
    # Realtime path. `pipeline` is the provider-neutral, lower-cost
    # STT -> tool-calling LLM -> TTS path.
    voice_runtime_kind: str = 'openai'
    realtime_model: str = 'gpt-realtime-2.1-mini'
    realtime_voice: str = 'alloy'
    realtime_rate: int = 24000
    realtime_max_seconds: int = 300

    # Component pipeline. These are deliberately independent so cost/quality
    # routing can change without changing the business agent or telephony.
    pipeline_stt_provider: str = 'groq'
    pipeline_tts_provider: str = 'sarvam'
    pipeline_llm_provider: str = 'openrouter'
    pipeline_llm_model: str = ''
    pipeline_vad_rms: int = 220
    pipeline_silence_ms: int = 550
    pipeline_max_turn_seconds: int = 20

    groq_key: str = ''
    groq_stt_model: str = 'whisper-large-v3-turbo'

    sarvam_key: str = ''
    sarvam_stt_model: str = 'saaras:v3'
    sarvam_stt_mode: str = 'codemix'
    sarvam_tts_model: str = 'bulbul:v3'
    sarvam_voice: str = 'shubh'
    sarvam_language: str = 'hi-IN'
    sarvam_tts_rate: int = 24000

    @property
    def pipeline_available(self) -> bool:
        if self.pipeline_stt_provider == 'groq':
            stt = bool(self.groq_key)
        elif self.pipeline_stt_provider == 'sarvam':
            stt = bool(self.sarvam_key)
        elif self.pipeline_stt_provider == 'openai':
            stt = bool(self.api_key)
        else:
            stt = False

        if self.pipeline_tts_provider == 'sarvam':
            tts = bool(self.sarvam_key)
        elif self.pipeline_tts_provider == 'openai':
            tts = bool(self.api_key)
        else:
            tts = False

        if self.pipeline_llm_provider == 'openrouter':
            llm = bool(self.openrouter_key)
        elif self.pipeline_llm_provider == 'openai':
            llm = bool(self.api_key)
        else:
            llm = False
        return stt and tts and llm

    @property
    def realtime_available(self) -> bool:
        """Whether the selected browser/SIP voice runtime can start."""
        if self.voice_runtime_kind == 'pipeline':
            return self.pipeline_available
        return bool(self.api_key)

    @property
    def provider_configured(self) -> bool:
        """Whether paid voice can be started by the existing call guard.

        ``Agent.start`` predates the runtime split and still uses this guard
        for the websocket call record. A configured component pipeline is
        therefore a valid paid provider even when the legacy HTTP playground
        remains set to ``none``.
        """
        if self.voice_runtime_kind == 'pipeline' and self.pipeline_available:
            return True
        if self.provider_kind == 'openrouter':
            return bool(self.openrouter_key)
        if self.provider_kind == 'openai':
            return bool(self.api_key)
        return False

    @classmethod
    def from_env(cls):
        load_env(ROOT / '.env')
        data = Path(os.getenv('CALLBOX_DATA_DIR', str(ROOT / '.runtime'))).resolve()
        data.mkdir(parents=True, exist_ok=True)
        with contextlib.suppress(OSError):
            data.chmod(0o700)
        token = os.getenv('CALLBOX_ADMIN_TOKEN', '')
        demo = os.getenv('CALLBOX_DEMO', '1') == '1'
        if not demo and len(token) < 32:
            raise RuntimeError('CALLBOX_ADMIN_TOKEN must be at least 32 characters outside demo mode.')
        token_file = data / 'admin-token'
        if not token:
            token = token_file.read_text().strip() if token_file.exists() else secrets.token_urlsafe(32)
            token_file.write_text(token + '\n')
        if token_file.exists():
            with contextlib.suppress(OSError):
                token_file.chmod(0o600)

        openai_key = os.getenv('OPENAI_API_KEY', '')
        openrouter_key = os.getenv('OPENROUTER_API_KEY', '')
        kind = os.getenv('CALLBOX_PROVIDER', '').strip().lower()
        if kind not in {'openai', 'openrouter', 'none'}:
            if openrouter_key and openai_key:
                raise RuntimeError('Set CALLBOX_PROVIDER to openai or openrouter: both keys are present.')
            kind = 'openrouter' if openrouter_key else 'openai' if openai_key else 'none'

        voice_runtime = os.getenv('CALLBOX_VOICE_RUNTIME', 'openai').strip().lower()
        if voice_runtime not in {'openai', 'pipeline'}:
            raise RuntimeError('CALLBOX_VOICE_RUNTIME must be openai or pipeline.')
        pipeline_stt = os.getenv('CALLBOX_PIPELINE_STT', 'groq').strip().lower()
        if pipeline_stt not in {'groq', 'sarvam', 'openai'}:
            raise RuntimeError('CALLBOX_PIPELINE_STT must be groq, sarvam or openai.')
        pipeline_tts = os.getenv('CALLBOX_PIPELINE_TTS', 'sarvam').strip().lower()
        if pipeline_tts not in {'sarvam', 'openai'}:
            raise RuntimeError('CALLBOX_PIPELINE_TTS must be sarvam or openai.')
        pipeline_llm = os.getenv('CALLBOX_PIPELINE_LLM', 'openrouter').strip().lower()
        if pipeline_llm not in {'openrouter', 'openai'}:
            raise RuntimeError('CALLBOX_PIPELINE_LLM must be openrouter or openai.')

        try:
            max_cost = float(os.getenv('CALLBOX_MAX_TURN_COST_USD', '0.05'))
            vad_rms = int(os.getenv('CALLBOX_PIPELINE_VAD_RMS', '220'))
            silence_ms = int(os.getenv('CALLBOX_PIPELINE_SILENCE_MS', '550'))
            max_turn = int(os.getenv('CALLBOX_PIPELINE_MAX_TURN_SECONDS', '20'))
            sarvam_rate = int(os.getenv('SARVAM_TTS_RATE', '24000'))
        except ValueError:
            raise RuntimeError('Numeric CallBox/Sarvam voice settings contain an invalid value.')
        if max_cost <= 0:
            raise RuntimeError('CALLBOX_MAX_TURN_COST_USD must be greater than zero.')
        if not 50 <= vad_rms <= 5000:
            raise RuntimeError('CALLBOX_PIPELINE_VAD_RMS must be between 50 and 5000.')
        if not 250 <= silence_ms <= 2000:
            raise RuntimeError('CALLBOX_PIPELINE_SILENCE_MS must be between 250 and 2000.')
        if not 3 <= max_turn <= 30:
            raise RuntimeError('CALLBOX_PIPELINE_MAX_TURN_SECONDS must be between 3 and 30.')
        if sarvam_rate not in {8000, 16000, 22050, 24000, 32000, 44100, 48000}:
            raise RuntimeError('SARVAM_TTS_RATE is not a supported Sarvam sample rate.')

        llm_model = os.getenv('CALLBOX_PIPELINE_LLM_MODEL', '').strip()
        if not llm_model:
            llm_model = (os.getenv('OPENROUTER_INTENT_MODEL', 'google/gemini-3.8-flash')
                         if pipeline_llm == 'openrouter'
                         else os.getenv('OPENAI_INTENT_MODEL', 'gpt-4.1-mini'))

        return cls(data, token, demo,
                   os.getenv('CALLBOX_SECURE_COOKIE', '0') == '1',
                   openai_key,
                   os.getenv('OPENAI_STT_MODEL', 'gpt-4o-mini-transcribe'),
                   os.getenv('OPENAI_TTS_MODEL', 'gpt-4o-mini-tts'),
                   os.getenv('OPENAI_INTENT_MODEL', 'gpt-4.1-mini'),
                   os.getenv('OPENAI_VOICE', 'coral'),
                   provider_kind=kind,
                   openrouter_key=openrouter_key,
                   openrouter_stt_model=os.getenv('OPENROUTER_STT_MODEL', 'google/gemini-3.8-flash'),
                   openrouter_intent_model=os.getenv('OPENROUTER_INTENT_MODEL', 'google/gemini-3.8-flash'),
                   openrouter_tts_model=os.getenv('OPENROUTER_TTS_MODEL', 'openai/gpt-audio-mini'),
                   openrouter_voice=os.getenv('OPENROUTER_VOICE', 'alloy'),
                   max_turn_cost_usd=max_cost,
                   voice_runtime_kind=voice_runtime,
                   realtime_model=os.getenv('CALLBOX_REALTIME_MODEL', 'gpt-realtime-2.1-mini'),
                   realtime_voice=os.getenv('CALLBOX_REALTIME_VOICE', 'alloy'),
                   realtime_rate=int(os.getenv('CALLBOX_REALTIME_RATE', '24000')),
                   realtime_max_seconds=int(os.getenv('CALLBOX_REALTIME_MAX_SECONDS', '300')),
                   pipeline_stt_provider=pipeline_stt,
                   pipeline_tts_provider=pipeline_tts,
                   pipeline_llm_provider=pipeline_llm,
                   pipeline_llm_model=llm_model,
                   pipeline_vad_rms=vad_rms,
                   pipeline_silence_ms=silence_ms,
                   pipeline_max_turn_seconds=max_turn,
                   groq_key=os.getenv('GROQ_API_KEY', ''),
                   groq_stt_model=os.getenv('GROQ_STT_MODEL', 'whisper-large-v3-turbo'),
                   sarvam_key=os.getenv('SARVAM_API_KEY', ''),
                   sarvam_stt_model=os.getenv('SARVAM_STT_MODEL', 'saaras:v3'),
                   sarvam_stt_mode=os.getenv('SARVAM_STT_MODE', 'codemix'),
                   sarvam_tts_model=os.getenv('SARVAM_TTS_MODEL', 'bulbul:v3'),
                   sarvam_voice=os.getenv('SARVAM_VOICE', 'shubh'),
                   sarvam_language=os.getenv('SARVAM_LANGUAGE', 'hi-IN'),
                   sarvam_tts_rate=sarvam_rate)
