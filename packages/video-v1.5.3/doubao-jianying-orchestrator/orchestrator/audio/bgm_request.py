"""Pure BGM input contract shared by queue, CLI and production audio."""
import pathlib
import re


def normalize(value):
    if isinstance(value, dict):
        return dict(value) if value else {'auto': True}
    text = str(value or '').strip()
    if not text:
        return {'auto': True}
    path_like = (pathlib.Path(text).is_absolute() or bool(re.match(r'^[A-Za-z]:[\\/]', text))
                 or text.startswith('\\\\') or '/' in text or '\\' in text
                 or pathlib.Path(text).suffix.lower() in {'.mp3', '.m4a', '.wav', '.aac', '.flac'})
    return {'path': text} if path_like else {'music_id': text}
