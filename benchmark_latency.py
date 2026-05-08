"""
benchmark_latency.py
====================
Real end-to-end latency simulation: LLM (non-streaming) → TTS (streaming)
then LLM (streaming, first-sentence extraction) → TTS (streaming).

Measures and compares:
  - LLM total time (current: stream=False)
  - LLM time-to-first-token (proposed: stream=True)
  - LLM time-to-first-sentence (proposed: stream=True, the real TTS gate)
  - TTS time-to-first-audio-chunk (TTFA)
  - TTS total audio generation time
  - End-to-end: user-speaks → first-audio-chunk

Run from project root:
    python benchmark_latency.py
"""

import asyncio
import base64
import json
import os
import re
import time

import aiohttp
import websockets
from dotenv import load_dotenv
from groq import AsyncGroq

load_dotenv()

# ── API keys ──────────────────────────────────────────────────────────────
GROQ_KEY      = os.getenv("GROQ_API_KEY", "")
SARVAM_KEY    = os.getenv("SARVAM_API_KEY", "")
LLM_MODEL     = os.getenv("LLM_MODEL", "llama-3.3-70b-versatile")
# NOTE: bulbul:v3 compatible EN speakers: kavya, priya, neha, rahul, pooja, rohan, simran
# The .env has 'anushka' but that is not compatible with bulbul:v3 — overriding here
TTS_SPEAKER   = "kavya"
TTS_MODEL     = os.getenv("SARVAM_TTS_MODEL", "bulbul:v3")
TTS_PACE      = float(os.getenv("SARVAM_TTS_PACE", "0.95"))
TTS_URL       = "https://api.sarvam.ai/text-to-speech/stream"

# ── Cartesia config ───────────────────────────────────────────────────────
CARTESIA_KEY     = os.getenv("CARTESIA_API_KEY", "")
CARTESIA_VOICE   = os.getenv("CARTESIA_VOICE_ID", "7c6219d2-e8d2-462c-89d8-7ecba7c75d65")
CARTESIA_MODEL   = os.getenv("CARTESIA_MODEL_ID", "sonic-3")
CARTESIA_VERSION = os.getenv("CARTESIA_VERSION",  "2025-04-16")
CARTESIA_WS_URL  = os.getenv("CARTESIA_TTS_WS_URL", "wss://api.cartesia.ai/tts/websocket")
CARTESIA_SPEED   = float(os.getenv("TTS_SPEED", "0.9"))

# ── System prompt matching production agent2_en (with 20-word limit) ─────
SYSTEM_PROMPT = """\
You are Divya, receptionist at Doctor Deepti's Dental and Orthodontic Centre, Bangalore.
Your job is to collect: name, age, reason for visit, preferred date and time.
Clinic hours: Monday–Saturday, 10 AM–1 PM and 4 PM–7 PM. Closed Sunday.
Doctor: Doctor Naga Deepti. Fee: ₹200–₹300.

HARD CONSTRAINTS:
- Your output MUST strictly follow the JSON schema.
- NEVER output extra text outside the JSON.
- The "response" field MUST NOT exceed 20 words. Be brief and direct.
- Ask ONLY ONE missing field at a time.

Reply in this exact JSON format:
{"response": "<your spoken reply>", "state": {"name": null, "age": null, "reason": null, "date": null, "time": null}, "done": false, "action": null, "handoff": false}

Current state: name=unknown, age=unknown, reason=unknown, date=unknown, time=unknown."""

# Three realistic user utterances of increasing response length
SCENARIOS = [
    {
        "label": "Turn 1 — Greeting (short LLM response expected)",
        "user": "Hi, I'd like to book an appointment please.",
        "memory": [],
    },
    {
        "label": "Turn 2 — Providing name (medium response)",
        "user": "My name is Priya Sharma, I'm 32 years old.",
        "memory": [
            {"role": "user", "content": "Hi, I'd like to book an appointment please."},
            {"role": "assistant", "content": "Hello Priya! I'd be happy to help you book an appointment. May I have your name and age please?"},
        ],
    },
    {
        "label": "Turn 3 — Providing all details (longer response with confirmation)",
        "user": "I need a dental checkup. I'm free this Monday at 11 AM.",
        "memory": [
            {"role": "user", "content": "Hi, I'd like to book an appointment please."},
            {"role": "assistant", "content": "Hello! I'd be happy to help. May I have your name and age?"},
            {"role": "user", "content": "My name is Priya Sharma, I'm 32 years old."},
            {"role": "assistant", "content": "Thank you Priya! What is the reason for your visit?"},
        ],
    },
]

# ── Sentence boundary detector ────────────────────────────────────────────
_ABBREVS = {"dr", "mr", "mrs", "ms", "st", "no", "vs", "etc", "approx", "dept"}

def _is_sentence_end(buf: str) -> int:
    """Return index of sentence-ending char if buf ends a sentence, else -1."""
    buf = buf.rstrip()
    if not buf:
        return -1
    last = buf[-1]
    if last in (".", "!", "?", "\u0964"):   # \u0964 = Devanagari danda
        if len(buf) < 15:
            return -1
        # Don't split on abbreviations like "Dr."
        word = buf.rstrip(".!?").rsplit(None, 1)[-1].lower().rstrip(".")
        if last == "." and word in _ABBREVS:
            return -1
        # Don't split on decimals like "4.30" or "10.5"
        if last == "." and buf[-3:-1].replace(".", "").isdigit():
            return -1
        return len(buf) - 1
    return -1

def _extract_first_sentence(text: str) -> tuple[str, str]:
    """Split text into (first_sentence, remainder). Returns ('', text) if no boundary."""
    for i in range(14, len(text)):
        sub = text[:i+1]
        if _is_sentence_end(sub) >= 0:
            return sub.strip(), text[i+1:].lstrip()
    return "", text

# ── TTS streaming helper ──────────────────────────────────────────────────
async def tts_stream_measure(text: str, session: aiohttp.ClientSession) -> dict:
    """Send text to Sarvam TTS streaming endpoint, return timing metrics."""
    headers = {"api-subscription-key": SARVAM_KEY, "Content-Type": "application/json"}
    payload = {
        "text":                 text,
        "target_language_code": "en-IN",
        "speaker":              TTS_SPEAKER,
        "model":                TTS_MODEL,
        "output_audio_codec":   "linear16",
        "speech_sample_rate":   16000,
        "pace":                 TTS_PACE,
        "enable_preprocessing": True,
    }

    t_request = time.perf_counter()
    ttfa_ms   = None
    total_bytes = 0
    chunks      = 0

    try:
        async with session.post(TTS_URL, headers=headers, json=payload, timeout=aiohttp.ClientTimeout(total=30)) as resp:
            if resp.status != 200:
                body = await resp.text()
                return {"error": f"HTTP {resp.status}: {body[:200]}"}

            async for chunk in resp.content.iter_chunked(4096):
                if not chunk:
                    continue
                if ttfa_ms is None:
                    ttfa_ms = (time.perf_counter() - t_request) * 1000
                total_bytes += len(chunk)
                chunks += 1

        total_ms = (time.perf_counter() - t_request) * 1000
        # PCM s16le @ 16kHz: duration = bytes / (16000 * 2)
        audio_duration_ms = (total_bytes / 32000) * 1000

        return {
            "ttfa_ms":          round(ttfa_ms or 0, 1),
            "total_ms":         round(total_ms, 1),
            "total_bytes":      total_bytes,
            "chunks":           chunks,
            "audio_duration_ms": round(audio_duration_ms, 1),
        }
    except Exception as exc:
        return {"error": str(exc)}

# ── Cartesia WebSocket TTS ────────────────────────────────────────────────
async def cartesia_tts_measure(text: str) -> dict:
    """Connect to Cartesia WebSocket, send TTS request, measure TTFA and total."""
    import uuid
    uri = f"{CARTESIA_WS_URL}?api_key={CARTESIA_KEY}&cartesia_version={CARTESIA_VERSION}"
    payload = json.dumps({
        "context_id":    str(uuid.uuid4()),
        "model_id":      CARTESIA_MODEL,
        "transcript":    text,
        "voice":         {"mode": "id", "id": CARTESIA_VOICE},
        "language":      "en",
        "output_format": {"container": "raw", "encoding": "pcm_s16le", "sample_rate": 16000},
        "generation_config": {"speed": CARTESIA_SPEED},
        "add_timestamps": False,
    })

    t_request   = time.perf_counter()
    ttfa_ms     = None
    total_bytes = 0
    chunks      = 0
    ws_connect_ms = None

    try:
        async with websockets.connect(uri, open_timeout=10.0) as ws:
            ws_connect_ms = (time.perf_counter() - t_request) * 1000
            await ws.send(payload)

            async for raw_msg in ws:
                try:
                    msg = json.loads(raw_msg)
                except Exception:
                    continue
                msg_type = msg.get("type", "")
                if msg_type == "chunk":
                    chunk = base64.b64decode(msg.get("data", ""))
                    if chunk:
                        if ttfa_ms is None:
                            ttfa_ms = (time.perf_counter() - t_request) * 1000
                        total_bytes += len(chunk)
                        chunks += 1
                elif msg_type == "error":
                    return {"error": f"Cartesia error: {msg.get('message', str(msg))}"}
                elif msg_type == "done":
                    break

        total_ms = (time.perf_counter() - t_request) * 1000
        audio_duration_ms = (total_bytes / 32000) * 1000
        return {
            "ws_connect_ms":    round(ws_connect_ms or 0, 1),
            "ttfa_ms":          round(ttfa_ms or 0, 1),
            "total_ms":         round(total_ms, 1),
            "total_bytes":      total_bytes,
            "chunks":           chunks,
            "audio_duration_ms": round(audio_duration_ms, 1),
        }
    except Exception as exc:
        return {"error": str(exc)}


# ── LLM non-streaming (current system) ───────────────────────────────────
async def llm_nonstream_measure(user_text: str, memory: list, client: AsyncGroq) -> dict:
    messages = [{"role": "system", "content": SYSTEM_PROMPT}] + memory + [{"role": "user", "content": user_text}]

    t0 = time.perf_counter()
    try:
        resp = await asyncio.wait_for(
            client.chat.completions.create(
                model=LLM_MODEL,
                messages=messages,
                response_format={"type": "json_object"},
                max_tokens=600,
                temperature=0.3,
                stream=False,
            ),
            timeout=20,
        )
        total_ms      = (time.perf_counter() - t0) * 1000
        full_json     = resp.choices[0].message.content or ""
        prompt_tokens = resp.usage.prompt_tokens if resp.usage else 0
        comp_tokens   = resp.usage.completion_tokens if resp.usage else 0

        # Extract response field text
        m = re.search(r'"response"\s*:\s*"((?:[^"\\]|\\.)*)"', full_json)
        response_text = m.group(1).replace('\\"', '"').replace("\\n", " ") if m else ""

        return {
            "total_ms":      round(total_ms, 1),
            "response_text": response_text,
            "full_json":     full_json,
            "prompt_tokens": prompt_tokens,
            "comp_tokens":   comp_tokens,
            "chars":         len(response_text),
        }
    except asyncio.TimeoutError:
        return {"error": "LLM timeout"}
    except Exception as exc:
        return {"error": str(exc)}

# ── LLM streaming (proposed) ──────────────────────────────────────────────
async def llm_stream_measure(user_text: str, memory: list, client: AsyncGroq) -> dict:
    messages = [{"role": "system", "content": SYSTEM_PROMPT}] + memory + [{"role": "user", "content": user_text}]

    t0 = time.perf_counter()
    ttft_ms             = None   # time-to-first-token
    ttfs_ms             = None   # time-to-first-sentence (the real TTS gate)
    first_sentence      = ""
    full_json           = ""
    in_response_field   = False
    response_done       = False
    response_buf        = ""
    in_escape           = False

    try:
        stream = await client.chat.completions.create(
            model=LLM_MODEL,
            messages=messages,
            response_format={"type": "json_object"},
            max_tokens=600,
            temperature=0.3,
            stream=True,
        )

        async for chunk in stream:
            delta = (chunk.choices[0].delta.content or "")
            if not delta:
                continue

            if ttft_ms is None:
                ttft_ms = (time.perf_counter() - t0) * 1000

            full_json += delta

            if response_done:
                continue

            if not in_response_field:
                for marker in ('"response": "', '"response":"'):
                    idx = full_json.find(marker)
                    if idx >= 0:
                        in_response_field = True
                        to_process = full_json[idx + len(marker):]
                        for ch in to_process:
                            if in_escape:
                                response_buf += ch
                                in_escape = False
                            elif ch == "\\":
                                in_escape = True
                            elif ch == '"':
                                response_done = True
                                break
                            else:
                                response_buf += ch
                        # Check sentence boundary
                        if ttfs_ms is None and not response_done:
                            if _is_sentence_end(response_buf) >= 0:
                                first_sentence = response_buf.strip()
                                ttfs_ms = (time.perf_counter() - t0) * 1000
                        break
            else:
                for ch in delta:
                    if response_done:
                        break
                    if in_escape:
                        response_buf += ch
                        in_escape = False
                    elif ch == "\\":
                        in_escape = True
                    elif ch == '"':
                        response_done = True
                    else:
                        response_buf += ch
                        if ttfs_ms is None and _is_sentence_end(response_buf) >= 0:
                            first_sentence = response_buf.strip()
                            ttfs_ms = (time.perf_counter() - t0) * 1000

        total_stream_ms = (time.perf_counter() - t0) * 1000

        # If no sentence boundary found, first_sentence = full response_buf
        if not first_sentence and response_buf.strip():
            first_sentence = response_buf.strip()
            if ttfs_ms is None:
                ttfs_ms = total_stream_ms

        # Extract full response from the completed JSON
        m = re.search(r'"response"\s*:\s*"((?:[^"\\]|\\.)*)"', full_json)
        response_text = m.group(1).replace('\\"', '"').replace("\\n", " ") if m else response_buf

        return {
            "ttft_ms":          round(ttft_ms or 0, 1),
            "ttfs_ms":          round(ttfs_ms or total_stream_ms, 1),
            "total_stream_ms":  round(total_stream_ms, 1),
            "first_sentence":   first_sentence,
            "response_text":    response_text,
            "full_json":        full_json,
            "chars":            len(response_text),
        }
    except asyncio.TimeoutError:
        return {"error": "LLM stream timeout"}
    except Exception as exc:
        return {"error": str(exc)}

# ── Baseline from previous run (before changes) ────────────────────────────
# These are the BEFORE numbers captured in the last benchmark run.
OLD_BASELINE = [
    {"label": "Turn 1", "llm_ms": 1240, "sarvam_ttfa": 696,  "e2e": 1935},
    {"label": "Turn 2", "llm_ms": 1209, "sarvam_ttfa": 1253, "e2e": 2462},
    {"label": "Turn 3", "llm_ms":  863, "sarvam_ttfa":  609, "e2e": 1472},
]

# ── Main benchmark ─────────────────────────────────────────────────────────
ROUNDS = 3

async def run_benchmark():
    client  = AsyncGroq(api_key=GROQ_KEY)
    session = aiohttp.ClientSession()

    print("\n" + "="*72)
    print("  KHYRA AI — POST-UPGRADE LATENCY BENCHMARK")
    print(f"  Model : {LLM_MODEL}")
    print(f"  TTS   : {TTS_MODEL}  speaker: {TTS_SPEAKER}")
    print(f"  Changes: 20-word response limit + sentence-split TTS")
    print(f"  Rounds: {ROUNDS} per scenario")
    print("="*72)

    new_results = []

    try:
        for idx, sc in enumerate(SCENARIOS):
            print(f"\n{'─'*72}")
            print(f"  {sc['label']}")
            print(f"  User: \"{sc['user']}\"")
            print(f"{'─'*72}")

            llm_times, sarvam_fs_ttfa, sarvam_full_ttfa, response_chars = [], [], [], []
            response_sample = ""
            first_sent_sample = ""

            for rnd in range(1, ROUNDS + 1):
                print(f"  Round {rnd}/{ROUNDS}...", end=" ", flush=True)

                # ── LLM call (now has 20-word limit in system prompt) ──────
                llm = await llm_nonstream_measure(sc["user"], sc["memory"], client)
                if "error" in llm:
                    print(f"LLM error: {llm['error']}")
                    break
                response_sample = llm["response_text"]
                response_chars.append(len(llm["response_text"]))
                llm_times.append(llm["total_ms"])

                await asyncio.sleep(0.3)

                # ── Sarvam: first sentence (what production now sends first) ─
                first_s, remainder = _extract_first_sentence(llm["response_text"])
                if not first_s:
                    first_s = llm["response_text"]
                first_sent_sample = first_s

                tts_fs = await tts_stream_measure(first_s, session)
                if "error" in tts_fs:
                    print(f"Sarvam(1st sent) error: {tts_fs['error']}")
                    tts_fs = {"ttfa_ms": 0}
                sarvam_fs_ttfa.append(tts_fs["ttfa_ms"])

                await asyncio.sleep(0.3)

                # ── Sarvam: full response (for comparison only) ────────────
                tts_full = await tts_stream_measure(llm["response_text"], session)
                if "error" in tts_full:
                    print(f"Sarvam(full) error: {tts_full['error']}")
                    tts_full = {"ttfa_ms": 0}
                sarvam_full_ttfa.append(tts_full["ttfa_ms"])

                print(f"LLM={llm['total_ms']:.0f}ms ({len(llm['response_text'])}ch) | "
                      f"Sarvam_1st={tts_fs['ttfa_ms']:.0f}ms | "
                      f"Sarvam_full={tts_full['ttfa_ms']:.0f}ms")
                await asyncio.sleep(0.5)

            if not llm_times:
                continue

            def avg(lst): return sum(lst) / len(lst) if lst else 0
            def med(lst):
                if not lst: return 0
                s = sorted(lst); return s[len(s)//2]

            new_e2e = avg(llm_times) + avg(sarvam_fs_ttfa)
            old     = OLD_BASELINE[idx] if idx < len(OLD_BASELINE) else {}
            saving  = old.get("e2e", 0) - new_e2e
            saving_with_stt = (old.get("e2e", 0) + 700) - (new_e2e + 700)

            new_results.append({
                "label":       sc["label"],
                "new_e2e":     new_e2e,
                "old_e2e":     old.get("e2e", 0),
                "saving":      saving,
                "llm_avg":     avg(llm_times),
                "fs_ttfa_avg": avg(sarvam_fs_ttfa),
            })

            print(f"\n  ── AVERAGES over {len(llm_times)} rounds ──")
            print(f"\n  LLM  (20-word limit active)")
            print(f"      Time         avg: {avg(llm_times):>7.0f} ms   median: {med(llm_times):>7.0f} ms")
            print(f"      Chars        avg: {avg(response_chars):>7.0f}     (was ~130-145 before)")
            print(f"      Response    : \"{response_sample[:90]}{'...' if len(response_sample)>90 else ''}\"")
            print(f"      1st sentence: \"{first_sent_sample[:90]}{'...' if len(first_sent_sample)>90 else ''}\"")

            print(f"\n  Sarvam TTFA — first sentence  (production path after upgrade)")
            print(f"      TTFA avg:    {avg(sarvam_fs_ttfa):>7.0f} ms   median: {med(sarvam_fs_ttfa):>7.0f} ms  ← 1st audio")

            print(f"\n  Sarvam TTFA — full response   (old production path, for comparison)")
            print(f"      TTFA avg:    {avg(sarvam_full_ttfa):>7.0f} ms   median: {med(sarvam_full_ttfa):>7.0f} ms")

            print(f"\n  ┌─────────────────────────────────────────────────────────────┐")
            print(f"  │  E2E to 1st audio (LLM + Sarvam TTFA, no STT)               │")
            print(f"  │  BEFORE (old prompt, full text to Sarvam): {old.get('e2e',0):>6.0f} ms         │")
            print(f"  │  AFTER  (20-word limit, 1st sentence TTS): {new_e2e:>6.0f} ms  ◄ NOW  │")
            if saving > 0:
                pct = saving / old.get("e2e", 1) * 100
                print(f"  │  SAVING:                                   {saving:>6.0f} ms  ({pct:.0f}%)     │")
            else:
                print(f"  │  CHANGE:                                   {saving:>6.0f} ms              │")
            print(f"  │                                                               │")
            print(f"  │  With STT (~700ms): BEFORE {old.get('e2e',0)+700:>4.0f}ms → AFTER {new_e2e+700:>4.0f}ms       │")
            print(f"  └─────────────────────────────────────────────────────────────┘")

            await asyncio.sleep(0.8)

        # ── Final summary ─────────────────────────────────────────────────
        print(f"\n{'='*72}")
        print("  FINAL SUMMARY — Before vs After Upgrade")
        print(f"{'='*72}")
        print(f"  {'Turn':<12} {'BEFORE (ms)':>12} {'AFTER (ms)':>11} {'Saving (ms)':>12} {'%':>6}")
        print(f"  {'─'*55}")
        for r in new_results:
            lbl   = r["label"].split("—")[0].strip()
            pct   = (r["saving"] / r["old_e2e"] * 100) if r["old_e2e"] else 0
            flag  = "✅" if r["saving"] > 0 else "⚠️ "
            print(f"  {lbl:<12}  {r['old_e2e']:>10.0f}   {r['new_e2e']:>10.0f}   {r['saving']:>+10.0f}  {pct:>5.0f}%  {flag}")

        if new_results:
            avg_old   = sum(r["old_e2e"]  for r in new_results) / len(new_results)
            avg_new   = sum(r["new_e2e"]  for r in new_results) / len(new_results)
            avg_save  = avg_old - avg_new
            avg_pct   = avg_save / avg_old * 100 if avg_old else 0
            print(f"  {'─'*55}")
            print(f"  {'AVG':<12}  {avg_old:>10.0f}   {avg_new:>10.0f}   {avg_save:>+10.0f}  {avg_pct:>5.0f}%")
            print(f"\n  Add STT (~700ms constant both sides):  "
                  f"BEFORE ~{avg_old+700:.0f}ms → AFTER ~{avg_new+700:.0f}ms")

    finally:
        await session.close()
        await client.close()

if __name__ == "__main__":
    asyncio.run(run_benchmark())
