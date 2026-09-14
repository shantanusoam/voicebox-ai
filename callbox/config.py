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

    @classmethod
    def from_env(cls):
        load_env(ROOT / '.env')
        data = Path(os.getenv('CALLBOX_DATA_DIR', str(ROOT / '.runtime'))).resolve()
        data.mkdir(parents=True, exist_ok=True)
        # Transcripts and names live in this directory. Without an explicit
        # mode it inherits the umask and is commonly world-readable.
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
            # Re-assert on every start, not only when the file is created.
            with contextlib.suppress(OSError):
                token_file.chmod(0o600)
        return cls(data, token, demo, os.getenv('CALLBOX_SECURE_COOKIE', '0') == '1',
                   os.getenv('OPENAI_API_KEY', ''), os.getenv('OPENAI_STT_MODEL', 'gpt-4o-mini-transcribe'),
                   os.getenv('OPENAI_TTS_MODEL', 'gpt-4o-mini-tts'),
                   os.getenv('OPENAI_INTENT_MODEL', 'gpt-4.1-mini'), os.getenv('OPENAI_VOICE', 'coral'))
