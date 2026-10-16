import shutil
import tempfile
import unittest
import wave
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from openshorts.pipeline import _transcribe


class TranscriptionTests(unittest.TestCase):
    def test_chunk_durations_are_bounded_and_timestamps_remain_global(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fixture = root / "fixture.wav"
            with wave.open(str(fixture), "wb") as audio:
                audio.setnchannels(1)
                audio.setsampwidth(2)
                audio.setframerate(16000)
                audio.writeframes(b"\0\0" * 16000 * 12)
            durations = []

            def transcribe(path, **kwargs):
                with wave.open(path) as audio:
                    duration = audio.getnframes() / audio.getframerate()
                durations.append(duration)
                return iter([SimpleNamespace(start=.1, end=duration + 1, text="spoken words")]), SimpleNamespace(language="en")

            def extract(command):
                shutil.copyfile(fixture, command[-1])
                return ""

            with patch("openshorts.pipeline._run", side_effect=extract), \
                 patch("openshorts.pipeline.WhisperModel") as model, \
                 patch("openshorts.pipeline.TRANSCRIPTION_CHUNK_SECONDS", 5):
                model.return_value.transcribe.side_effect = transcribe
                segments, language = _transcribe(fixture, "tiny", root, lambda **values: None)
            self.assertEqual(durations, [5, 5, 2])
            self.assertEqual([segment["start"] for segment in segments], [.1, 5.1, 10.1])
            self.assertEqual([segment["end"] for segment in segments], [5, 10, 12])
            self.assertEqual(language, "en")
            self.assertFalse((root / "audio-chunk.wav").exists())


if __name__ == "__main__":
    unittest.main()
