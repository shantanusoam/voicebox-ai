from array import array

from callbox.pipeline_runtime import LocalTurnDetector


def pcm(level, ms=20, rate=24000):
    samples = rate * ms // 1000
    return array('h', [level] * samples).tobytes()


def test_local_vad_preserves_preroll_and_ends_after_silence():
    detector = LocalTurnDetector(24000, threshold=200, silence_ms=100,
                                 max_turn_seconds=5, pre_roll_ms=40)
    assert detector.feed(pcm(40))[1] is None
    assert detector.feed(pcm(50))[1] is None

    started, turn = detector.feed(pcm(2000))
    assert started is True and turn is None
    result = None
    for _ in range(5):
        _, result = detector.feed(pcm(0))
    assert result is not None
    assert len(result) >= 24000 * 2 * 0.06


def test_local_vad_interrupt_reset_discards_partial_turn():
    detector = LocalTurnDetector(24000, threshold=200, silence_ms=100,
                                 max_turn_seconds=5)
    assert detector.feed(pcm(2000))[0] is True
    detector.reset()
    assert detector.finish() is None


def test_pipeline_availability_requires_all_selected_components(config):
    config.voice_runtime_kind = 'pipeline'
    config.pipeline_stt_provider = 'groq'
    config.pipeline_llm_provider = 'openrouter'
    config.pipeline_tts_provider = 'sarvam'
    config.groq_key = 'g'
    config.openrouter_key = 'o'
    config.sarvam_key = ''
    assert not config.pipeline_available
    assert not config.realtime_available
    config.sarvam_key = 's'
    assert config.pipeline_available
    assert config.realtime_available
    assert config.provider_configured
