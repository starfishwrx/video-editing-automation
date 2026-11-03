"""Final audio duration; video last-frame safety margins never apply to speech."""
from ..media.probe import probe_media

AUDIO_EXTENSIONS = {'.wav', '.mp3', '.m4a', '.aac', '.ogg', '.opus', '.flac', '.aif', '.aiff', '.wma'}
PROBE_KIND = 'final_audio_us_v1'


def probe_audio(path, *, ffprobe='ffprobe'):
    facts = probe_media(path, ffprobe=ffprobe)
    if not facts['audio'] or any(s.get('codec_type') == 'video' for s in facts['streams']):
        raise ValueError('NARRATION_AUDIO_ONLY_REQUIRED')
    return {'duration': facts['duration_us'] / 1_000_000, 'duration_us': facts['duration_us'],
            'duration_source': 'final_audio_stream', 'duration_confidence': 'HIGH'}
