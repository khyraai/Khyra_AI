"""
Sarvam TTS Streaming Load Test
--------------------------------
Tests /text-to-speech/stream under varying concurrency levels.
Measures: TTFB, total generation time, bytes received, errors.

Usage:
    python scripts/sarvam_tts_loadtest.py
    python scripts/sarvam_tts_loadtest.py --concurrency 1 5 10 20 --runs 5
"""
import asyncio
import argparse
import os
import sys
import time
import statistics
from dataclasses import dataclass, field
from typing import Optional

import aiohttp

# ── Config ────────────────────────────────────────────────────────────────────
API_KEY  = os.getenv("SARVAM_API_KEY", "sk_20bh4602_uytsFQjPukx6BCkOFjiqqxnH")
URL      = "https://api.sarvam.ai/text-to-speech/stream"
HEADERS  = {"api-subscription-key": API_KEY, "Content-Type": "application/json"}

# Representative clinic responses (short / medium / long)
TEST_TEXTS = {
    "short_en":  ("en-IN", "Hello! How may I assist you today?"),
    "medium_en": ("en-IN", "I can help you book an appointment. Could I please get your name, age, and the reason for your visit?"),
    "long_en":   ("en-IN", "I'm sorry, our centre hours are 10:00 AM to 1:00 PM and 4:00 PM to 7:00 PM on weekdays. We are closed on Sundays. The next available slot for Doctor Naga Deepti is Monday at 10:00 AM. Would you like me to book that for you?"),
    "short_kn":  ("kn-IN", "ನಮಸ್ಕಾರ! ನಿಮಗೆ ಏನು ಸಹಾಯ ಮಾಡಲಿ?"),
    "medium_kn": ("kn-IN", "ಖಂಡಿತ. ದಯವಿಟ್ಟು ನಿಮ್ಮ ಹೆಸರು, ವಯಸ್ಸು ಮತ್ತು ಭೇಟಿಯ ಕಾರಣ ತಿಳಿಸುತ್ತೀರಾ?"),
}

SPEAKER       = "kavya"
MODEL         = "bulbul:v3"
SAMPLE_RATE   = 16000
CHUNK_SIZE    = 8192

# ── Result dataclass ──────────────────────────────────────────────────────────
@dataclass
class Result:
    label:      str
    concurrency: int
    run:        int
    ttfb_ms:    Optional[float] = None   # ms to first byte
    total_ms:   Optional[float] = None   # ms to last byte
    bytes_rcvd: int = 0
    audio_dur_s: float = 0.0             # bytes → audio seconds at 16kHz s16le
    error:      Optional[str] = None

    @property
    def ok(self) -> bool:
        return self.error is None

# ── Single request ────────────────────────────────────────────────────────────
async def single_request(
    session: aiohttp.ClientSession,
    label: str,
    lang: str,
    text: str,
    concurrency: int,
    run: int,
) -> Result:
    r = Result(label=label, concurrency=concurrency, run=run)
    payload = {
        "text":                 text,
        "target_language_code": lang,
        "speaker":              SPEAKER,
        "model":                MODEL,
        "output_audio_codec":   "linear16",
        "speech_sample_rate":   SAMPLE_RATE,
        "pace":                 0.95,
        "enable_preprocessing": True,
    }
    t_start = time.perf_counter()
    try:
        async with session.post(URL, headers=HEADERS, json=payload, timeout=aiohttp.ClientTimeout(total=30)) as resp:
            if resp.status != 200:
                body = await resp.text()
                r.error = f"HTTP {resp.status}: {body[:120]}"
                return r

            first_byte = True
            async for chunk in resp.content.iter_chunked(CHUNK_SIZE):
                if not chunk:
                    continue
                if first_byte:
                    r.ttfb_ms = (time.perf_counter() - t_start) * 1000
                    first_byte = False
                r.bytes_rcvd += len(chunk)

    except asyncio.TimeoutError:
        r.error = "Timeout (>30s)"
        return r
    except Exception as exc:
        r.error = str(exc)[:120]
        return r

    r.total_ms   = (time.perf_counter() - t_start) * 1000
    r.audio_dur_s = r.bytes_rcvd / (SAMPLE_RATE * 2)  # s16le = 2 bytes/sample
    return r

# ── Concurrency batch ─────────────────────────────────────────────────────────
async def run_batch(
    concurrency: int,
    runs_per_label: int,
    labels: list[tuple[str, str, str]],
) -> list[Result]:
    connector = aiohttp.TCPConnector(limit=concurrency + 4)
    async with aiohttp.ClientSession(connector=connector) as session:
        tasks = []
        for run_idx in range(runs_per_label):
            for label, lang, text in labels:
                tasks.append(single_request(session, label, lang, text, concurrency, run_idx + 1))
        results = await asyncio.gather(*tasks)
    return list(results)

# ── Stats helper ──────────────────────────────────────────────────────────────
def percentile(data: list[float], p: int) -> float:
    if not data:
        return float("nan")
    s = sorted(data)
    k = (len(s) - 1) * p / 100
    lo, hi = int(k), min(int(k) + 1, len(s) - 1)
    return s[lo] + (s[hi] - s[lo]) * (k - lo)

def print_stats(results: list[Result], concurrency: int):
    from collections import defaultdict
    by_label: dict[str, list[Result]] = defaultdict(list)
    for r in results:
        if r.concurrency == concurrency:
            by_label[r.label].append(r)

    print(f"\n{'─'*72}")
    print(f"  Concurrency = {concurrency}")
    print(f"{'─'*72}")
    print(f"  {'Label':<12} {'OK':>3} {'Err':>3} {'TTFB avg':>9} {'TTFB p95':>9} {'Total avg':>10} {'Audio s':>8} {'RTF':>6}")
    print(f"  {'─'*12} {'─'*3} {'─'*3} {'─'*9} {'─'*9} {'─'*10} {'─'*8} {'─'*6}")

    for label, res in sorted(by_label.items()):
        ok  = [r for r in res if r.ok]
        err = [r for r in res if not r.ok]

        ttfbs  = [r.ttfb_ms  for r in ok if r.ttfb_ms  is not None]
        totals = [r.total_ms for r in ok if r.total_ms is not None]
        audios = [r.audio_dur_s for r in ok]

        ttfb_avg  = statistics.mean(ttfbs)  if ttfbs  else float("nan")
        ttfb_p95  = percentile(ttfbs, 95)   if ttfbs  else float("nan")
        total_avg = statistics.mean(totals) if totals else float("nan")
        audio_avg = statistics.mean(audios) if audios else float("nan")
        rtf       = total_avg / (audio_avg * 1000) if audio_avg else float("nan")  # total_ms / audio_ms

        print(f"  {label:<12} {len(ok):>3} {len(err):>3} "
              f"{ttfb_avg:>8.0f}ms {ttfb_p95:>8.0f}ms "
              f"{total_avg:>9.0f}ms {audio_avg:>7.2f}s {rtf:>5.2f}x")

        for r in err[:2]:
            print(f"    ⚠  error: {r.error}")

    print()

# ── Main ──────────────────────────────────────────────────────────────────────
async def main():
    parser = argparse.ArgumentParser(description="Sarvam TTS streaming load test")
    parser.add_argument("--concurrency", nargs="+", type=int, default=[1, 3, 5, 10],
                        help="Concurrency levels to test (default: 1 3 5 10)")
    parser.add_argument("--runs",        type=int, default=3,
                        help="Runs per label per concurrency level (default: 3)")
    parser.add_argument("--labels",      nargs="+", default=list(TEST_TEXTS.keys()),
                        help="Which labels to test (default: all)")
    args = parser.parse_args()

    labels = [(lbl, *TEST_TEXTS[lbl]) for lbl in args.labels if lbl in TEST_TEXTS]
    if not labels:
        print("No valid labels selected. Available:", list(TEST_TEXTS.keys()))
        sys.exit(1)

    print(f"\nSarvam TTS Load Test")
    print(f"  Speaker : {SPEAKER}   Model : {MODEL}")
    print(f"  Labels  : {[l[0] for l in labels]}")
    print(f"  Runs    : {args.runs} per label per concurrency level")
    print(f"  Levels  : {args.concurrency}")

    all_results: list[Result] = []

    for c in args.concurrency:
        print(f"\n  ⏳ Running concurrency={c} ({len(labels) * args.runs} requests)...", end="", flush=True)
        t0 = time.perf_counter()
        results = await run_batch(c, args.runs, labels)
        elapsed = time.perf_counter() - t0
        all_results.extend(results)
        ok_count  = sum(1 for r in results if r.ok)
        err_count = sum(1 for r in results if not r.ok)
        print(f" done in {elapsed:.1f}s  ✅{ok_count} ❌{err_count}")

    # Print summary
    print("\n" + "="*72)
    print("  RESULTS SUMMARY")
    print("="*72)
    print("  RTF = total_delivery_ms / audio_duration_ms  (lower = faster than real-time)")

    for c in args.concurrency:
        print_stats(all_results, c)

    # Overall error summary
    errors = [(r.label, r.concurrency, r.error) for r in all_results if not r.ok]
    if errors:
        print(f"  Errors ({len(errors)} total):")
        for lbl, c, err in errors[:10]:
            print(f"    [{lbl} c={c}] {err}")

if __name__ == "__main__":
    asyncio.run(main())
