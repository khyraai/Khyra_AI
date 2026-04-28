import audioop
import time
from collections import deque
from dataclasses import dataclass
from typing import Deque, List


@dataclass
class VadBufferConfig:
    sample_rate_hz: int = 8000
    sample_width_bytes: int = 2
    frame_ms: int = 20
    start_trigger_ms: int = 60
    min_speech_ms: int = 350
    silence_end_ms: int = 1500
    target_emit_silence_ms: int = 500
    target_chunk_ms: int = 1200
    max_chunk_ms: int = 1800
    short_utt_min_ms: int = 300
    short_utt_max_ms: int = 600
    short_utt_silence_ms: int = 120
    continuation_min_speech_ms: int = 700
    continuation_max_silence_ms: int = 300
    cooldown_ms: int = 150
    preroll_ms: int = 120
    rms_speech_threshold: int = 320


class SpeechChunkBuffer:
    def __init__(self, config: VadBufferConfig | None = None):
        self.config = config or VadBufferConfig()
        self.frame_bytes = int(
            (self.config.sample_rate_hz * self.config.sample_width_bytes * self.config.frame_ms) / 1000
        )
        self._carry = bytearray()
        self._speech_frames: List[bytes] = []
        self._preroll: Deque[bytes] = deque(
            maxlen=max(1, int(self.config.preroll_ms / self.config.frame_ms))
        )
        self._in_speech = False
        self._speech_ms = 0
        self._silence_ms = 0
        self._trigger_ms = 0
        self._last_emit_ts = 0.0

    def set_rms_speech_threshold(self, value: int):
        self.config.rms_speech_threshold = max(1, int(value))

    def _reset_phrase(self):
        self._speech_frames = []
        self._in_speech = False
        self._speech_ms = 0
        self._silence_ms = 0
        self._trigger_ms = 0

    def _emit_current(self) -> bytes:
        out = b"".join(self._speech_frames)
        self._last_emit_ts = time.time()
        self._reset_phrase()
        return out

    def _is_speech_frame(self, frame: bytes) -> bool:
        try:
            return audioop.rms(frame, self.config.sample_width_bytes) >= self.config.rms_speech_threshold
        except Exception:
            return False

    def ingest(self, pcm16_8k_bytes: bytes) -> List[bytes]:
        if not pcm16_8k_bytes:
            return []

        self._carry.extend(pcm16_8k_bytes)
        out: List[bytes] = []

        while len(self._carry) >= self.frame_bytes:
            frame = bytes(self._carry[: self.frame_bytes])
            del self._carry[: self.frame_bytes]

            now = time.time()
            in_cooldown = (now - self._last_emit_ts) < (self.config.cooldown_ms / 1000.0)
            is_speech = self._is_speech_frame(frame)

            if not self._in_speech:
                self._preroll.append(frame)
                if in_cooldown:
                    continue

                if is_speech:
                    self._trigger_ms += self.config.frame_ms
                else:
                    self._trigger_ms = 0

                if self._trigger_ms >= self.config.start_trigger_ms:
                    self._in_speech = True
                    self._speech_frames = list(self._preroll)
                    self._speech_ms = self.config.frame_ms * len(self._speech_frames)
                    self._silence_ms = 0
                continue

            self._speech_frames.append(frame)
            self._speech_ms += self.config.frame_ms
            if is_speech:
                self._silence_ms = 0
            else:
                self._silence_ms += self.config.frame_ms

            if self._speech_ms >= self.config.max_chunk_ms:
                out.append(self._emit_current())
                continue

            if (
                self._speech_ms > self.config.continuation_min_speech_ms
                and self._silence_ms < self.config.continuation_max_silence_ms
            ):
                continue

            if (
                self._speech_ms >= self.config.short_utt_min_ms
                and self._speech_ms <= self.config.short_utt_max_ms
                and self._silence_ms >= self.config.short_utt_silence_ms
            ):
                out.append(self._emit_current())
                continue

            if self._silence_ms >= self.config.silence_end_ms:
                if self._speech_ms >= self.config.min_speech_ms:
                    out.append(self._emit_current())
                else:
                    self._reset_phrase()

        return out

    def flush(self) -> bytes:
        if self._in_speech and self._speech_ms >= self.config.min_speech_ms:
            return self._emit_current()
        self._reset_phrase()
        self._carry.clear()
        self._preroll.clear()
        return b""
