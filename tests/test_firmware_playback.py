"""Exercise the actual firmware playback queue under a host compiler."""
from pathlib import Path
import subprocess


def test_playback_queue_burst_and_epoch(tmp_path):
    root = Path(__file__).resolve().parents[1]
    app = root / 'firmware/esp32-hfp-bridge/callbox_hfp'
    main = app / 'main'
    ring = app / 'components/callbox_ring/include'
    binary = tmp_path / 'playback_queue_test'
    subprocess.run(['cc', '-std=c11', '-Wall', '-Wextra', '-Werror',
                    '-I', str(main), '-I', str(ring),
                    str(main / 'playback_queue.c'),
                    str(app / 'tests/playback_queue_test.c'), '-o', str(binary)], check=True)
    subprocess.run([str(binary)], check=True)
