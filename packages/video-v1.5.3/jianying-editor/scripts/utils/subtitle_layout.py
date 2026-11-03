"""Shared subtitle geometry for libass and editable native captions.

Coordinates use the editor's half-canvas convention. Only display line breaks
and, when necessary, font size/position change; cue text and timing are inputs.
"""
from functools import lru_cache
from io import BytesIO
import math
import os
from pathlib import Path
import unicodedata

from PIL import ImageFont

SAFE_MARGIN = .10
NATIVE_FONT_SIZE = 8.0
FONT_SIZE_PX = 56.0


def font_file(font_name="Microsoft YaHei", bold=True, font_path=None):
    if font_path:
        path = Path(font_path)
        if not path.is_file():
            raise ValueError("SUBTITLE_FONT_UNAVAILABLE:" + str(path))
        return str(path)
    windows = Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts"
    names = {"microsoft yahei": "msyhbd.ttc" if bold else "msyh.ttc",
             "simhei": "simhei.ttf", "arial": "arialbd.ttf" if bold else "arial.ttf"}
    candidate = windows / names.get(font_name.lower(), font_name)
    if candidate.is_file():
        return str(candidate)
    # The fallback is conservative for CJK (one em per character), rather than
    # measuring the narrow missing-glyph box as if it were a real Chinese glyph.
    return "DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf"


@lru_cache(maxsize=64)
def _font(path, size):
    # FreeType retains open file handles on Windows. Load bytes so cached fonts
    # neither lock collected media nor prevent a temporary draft being removed.
    source = BytesIO(Path(path).read_bytes()) if Path(path).is_file() else path
    return ImageFont.truetype(source, size)


def _characters(text):
    result = []
    for char in text:
        if result and (unicodedata.combining(char) or char in "\ufe0f\u200d"
                       or result[-1].endswith("\u200d")):
            result[-1] += char
        else:
            result.append(char)
    return result


def _wrap(text, measure, limit):
    """Fewest lines, then balanced widths; preserve all original characters."""
    output = []
    for paragraph in text.split("\n"):
        chars = _characters(paragraph)
        n = len(chars)
        best = {n: (0, 0, [])}
        for i in range(n - 1, -1, -1):
            choices = []
            for j in range(i + 1, n + 1):
                line = "".join(chars[i:j])
                width = measure(line)
                if width > limit:
                    break
                # Prefer word boundaries, but allow an oversized word to split.
                inside_word = (j < n and chars[j - 1].isascii() and chars[j].isascii()
                               and chars[j - 1].isalnum() and chars[j].isalnum())
                bad_punctuation = (j < n and chars[j] in "，。！？、；：,.!?;:）】》」』"
                                   or chars[j - 1] in "（【《「『")
                count, cost, rest = best[j]
                penalty = limit * limit * (2 if inside_word or bad_punctuation else 0)
                choices.append((count + 1, cost + (limit - width) ** 2 + penalty, [line] + rest))
            if not choices:
                raise ValueError("SUBTITLE_SINGLE_GLYPH_EXCEEDS_SAFE_WIDTH")
            best[i] = min(choices, key=lambda row: row[:2])
        output.extend(best[0][2] if n else [""])
    return output


def layout_subtitle(text, width, height, *, font_size_px=FONT_SIZE_PX,
                    font_name="Microsoft YaHei", bold=True, font_path=None,
                    x=0.0, y=-.55, margin=SAFE_MARGIN, outline_px=2.0):
    if (not isinstance(text, str) or not text.strip() or '\t' in text
            or not all(math.isfinite(v) for v in (width, height, font_size_px, x, y, margin, outline_px))
            or min(width, height, font_size_px) <= 0 or not 0 < margin < .5 or outline_px < 0):
        raise ValueError("SUBTITLE_LAYOUT_INPUT_INVALID")
    text = text.replace('\r\n', '\n').replace('\r', '\n')
    path = font_file(font_name, bold, font_path)
    safe = (width * margin, height * margin, width * (1 - margin), height * (1 - margin))
    # A little spare width covers rasterizer/hinting differences and bold edges.
    padding = outline_px + max(1, font_size_px * .04)
    maximum = safe[2] - safe[0] - 2 * padding
    size = max(1, round(font_size_px))
    while True:
        font = _font(path, size)
        fallback_cjk = Path(path).name.startswith("DejaVuSans")
        def measure(value):
            measured = font.getlength(value)
            if fallback_cjk:
                measured += sum(max(0, size - font.getlength(c)) for c in value
                                if unicodedata.east_asian_width(c) in {"W", "F"})
            return measured
        lines = _wrap(text, measure, maximum)
        ascent, descent = font.getmetrics()
        advance = max(ascent + descent, math.ceil(size * 1.3))
        box_width = max(measure(line) for line in lines) + 2 * padding
        box_height = advance * len(lines) + 2 * padding
        if box_height <= safe[3] - safe[1]:
            break
        size -= 1
        if size < min(12, round(font_size_px)):
            raise ValueError("SUBTITLE_TOO_LONG_FOR_READABLE_SAFE_LAYOUT")
    center_x = min(max(width * (1 + x) / 2, safe[0] + box_width / 2), safe[2] - box_width / 2)
    center_y = min(max(height * (1 - y) / 2, safe[1] + box_height / 2), safe[3] - box_height / 2)
    return {"text": "\n".join(lines), "lines": lines, "font_path": path,
            "font_size_px": size, "line_advance_px": advance, "alignment": 1,
            "max_line_width": 1 - 2 * margin,
            "x": 2 * center_x / width - 1, "y": 1 - 2 * center_y / height,
            "safe_rect": list(safe),
            "bounds": [center_x - box_width / 2, center_y - box_height / 2,
                       center_x + box_width / 2, center_y + box_height / 2]}
