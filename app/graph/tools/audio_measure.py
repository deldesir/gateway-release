"""Measure a rehearsal recording with ffmpeg alone: duration, pauses, speech ratio.

No model turn and no transcript. These are the three numbers the practice plan allows a
speaker to see, and only after his own hearing of the recording. The voice-note tool uses
this module; the same numbers come from recordings kept on disk:

    uv run python -m app.graph.tools.audio_measure recording.ogg [more ...]
"""
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass

SILENCE_DB = -30        # quieter than this counts as silence
PAUSE_SECONDS = 0.6     # a silence at least this long counts as a pause


@dataclass
class Measure:
    duration: float          # seconds
    pauses: int              # silences of PAUSE_SECONDS or more
    silence_total: float     # seconds of silence in those pauses
    longest_pause: float     # seconds
    speech_ratio: float      # share of the recording that is not a pause


def measure(path: str) -> Measure:
    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", path],
        capture_output=True, text=True, timeout=60,
    )
    duration = float((probe.stdout or "0").strip() or 0)
    silence = subprocess.run(
        ["ffmpeg", "-i", path, "-af", f"silencedetect=noise={SILENCE_DB}dB:d={PAUSE_SECONDS}", "-f", "null", "-"],
        capture_output=True, text=True, timeout=300,
    )
    durations = [float(x) for x in re.findall(r"silence_duration:\s*([\d.]+)", silence.stderr)]
    total = sum(durations)
    return Measure(
        duration=duration,
        pauses=len(durations),
        silence_total=total,
        longest_pause=max(durations) if durations else 0.0,
        speech_ratio=max(0.0, (duration - total) / duration) if duration else 0.0,
    )


def download(url: str, timeout: int = 60) -> str:
    """Fetch a voice note to a temporary file; the caller deletes it."""
    import httpx

    with tempfile.NamedTemporaryFile(suffix=".audio", delete=False) as tmp:
        with httpx.Client(timeout=timeout, follow_redirects=True) as client:
            r = client.get(url)
            r.raise_for_status()
            tmp.write(r.content)
        return tmp.name


def main(argv=None) -> int:
    paths = sys.argv[1:] if argv is None else argv
    if not paths:
        print(__doc__)
        return 2
    for p in paths:
        m = measure(p)
        mins, secs = divmod(int(m.duration), 60)
        print(f"{p}: {mins}:{secs:02d}, {m.pauses} pause(s) of {PAUSE_SECONDS}s or more, "
              f"longest {m.longest_pause:.1f}s, speech {m.speech_ratio:.0%}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
