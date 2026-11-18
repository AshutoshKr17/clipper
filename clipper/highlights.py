import bisect
import json
import os
import re

SELECT_PROMPT = """You are a top short-form video editor. Your clips get millions of views on YouTube
Shorts and Instagram Reels. You receive a timestamped transcript of a long stream or video.

Find moments that work as standalone vertical shorts. The best clips are micro-stories:
- HOOK: the first 2 seconds grab attention (a bold claim, a shocking reaction, a question, conflict).
- BUILD: tension, setup or context that keeps people watching.
- PAYOFF: a punchline, reveal, clutch play, emotional peak or satisfying conclusion.
Strong categories: intense reactions, funny banter, hot takes, surprising reveals, clutch or fail
moments, rage, wholesome moments, quotable one-liners, useful insights.

Some lines carry audience signals in braces, e.g. {{chat x4.2}} = chat was 4.2x busier than usual,
{{replayed 0.91}} = viewers rewatched this part a lot (0-1). These are strong evidence of a great moment,
but the clip must still make sense without them.

Rules:
- Each clip is between {min_len} and {max_len} seconds and makes sense with zero prior context.
- Start and end on sentence boundaries using the transcript timestamps. Never end mid-thought.
- No overlapping clips. Prefer quality over quantity; skip boring stretches entirely.
- score 1-100 = realistic viral potential. Be strict: most moments are below 60.

Respond with JSON only:
{{"clips": [{{"start": 12.3, "end": 55.0, "score": 87, "why": "short reason"}}]}}"""

REFINE_PROMPT = """You are finalising short-form clips before publishing. For each candidate you get its
transcript with surrounding context (timestamps in seconds).

For each candidate:
1. Tighten "start" so the clip opens on the strongest hook (cut slow intros). Keep needed context.
2. Set "end" right after the payoff lands. If the thought is cut off, extend to complete it.
   Keep the duration between {min_len} and {max_len} seconds.
3. Re-score 1-100 for realistic viral potential after your edits.
4. "title": under 60 characters, curiosity-driven, no hashtags, not misleading.
5. "hook_text": 2-6 punchy words shown on screen in the first seconds (e.g. "HE DIDN'T SEE IT COMING").
6. "description": 1-2 sentences.
7. "tags": 3-8 lowercase keywords without '#'.

Respond with JSON only:
{{"clips": [{{"id": 0, "start": 12.3, "end": 55.0, "score": 80, "title": "...", "hook_text": "...", "description": "...", "tags": ["..."]}}]}}"""


def annotate_signals(lines: list[dict], chat_times: list[float], heatmap: list[dict]) -> list[dict]:
    """Attach {chat xN} / {replayed V} markers to lines where the audience reacted."""
    if not chat_times and not heatmap:
        return lines
    out = []
    total_span = (lines[-1]["end"] - lines[0]["start"]) or 1.0
    base_rate = len(chat_times) / total_span if chat_times else 0
    for line in lines:
        marks = []
        if base_rate:
            # Chat reacts a few seconds after the moment happens.
            lo = bisect.bisect_left(chat_times, line["start"] + 2)
            hi = bisect.bisect_right(chat_times, line["end"] + 12)
            rate = (hi - lo) / (line["end"] - line["start"] + 10)
            ratio = rate / base_rate
            if ratio >= 2:
                marks.append(f"chat x{ratio:.1f}")
        if heatmap:
            vals = [h["value"] for h in heatmap if h["start_time"] < line["end"] and h["end_time"] > line["start"]]
            if vals and max(vals) >= 0.6:
                marks.append(f"replayed {max(vals):.2f}")
        out.append({**line, "signals": ", ".join(marks)} if marks else line)
    return out


def _format_lines(lines: list[dict]) -> str:
    rows = []
    for l in lines:
        sig = f" {{{l['signals']}}}" if l.get("signals") else ""
        rows.append(f"[{l['start']:.1f}-{l['end']:.1f}]{sig} {l['text']}")
    return "\n".join(rows)


def _windows(lines: list[dict], max_chars: int, overlap: int = 15) -> list[list[dict]]:
    windows, cur, size = [], [], 0
    for line in lines:
        n = len(line["text"]) + 24
        if cur and size + n > max_chars:
            windows.append(cur)
            cur = cur[-overlap:]
            size = sum(len(l["text"]) + 24 for l in cur)
        cur.append(line)
        size += n
    if cur:
        windows.append(cur)
    return windows


def _parse_json(text: str) -> dict:
    """Parse the first JSON object in a model reply, ignoring any prose or code fences around it."""
    text = text.strip()
    decoder = json.JSONDecoder()
    for m in re.finditer(r"\{", text):
        try:
            obj, _ = decoder.raw_decode(text[m.start():])
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            return obj
    raise ValueError(f"no JSON object in model reply: {text[:200]!r}")


def _openai_chat(client, model: str, system: str, user: str) -> str:
    messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
    try:
        resp = client.chat.completions.create(
            model=model, messages=messages, response_format={"type": "json_object"}
        )
    except Exception as e:
        if "response_format" not in str(e) and "json" not in str(e).lower():
            raise
        resp = client.chat.completions.create(model=model, messages=messages)
    return resp.choices[0].message.content


def gradient_key() -> str:
    """DigitalOcean Gradient model access key. Accepts GRADIENT_API_KEY or AI_KEY."""
    key = os.getenv("GRADIENT_API_KEY") or os.getenv("AI_KEY")
    if not key:
        raise SystemExit("Missing key: set AI_KEY (or GRADIENT_API_KEY) in .env")
    return key


def ask_llm(system: str, user: str) -> dict:
    provider = os.getenv("LLM_PROVIDER", "gradient").lower()
    if provider == "anthropic":
        import anthropic

        client = anthropic.Anthropic()
        resp = client.messages.create(
            model=os.getenv("LLM_MODEL", "claude-sonnet-4-5"),
            max_tokens=4096,
            system=system,
            messages=[{"role": "user", "content": user}],
        )
        return _parse_json("".join(b.text for b in resp.content if b.type == "text"))

    from openai import OpenAI

    if provider == "gradient":
        client = OpenAI(
            base_url=os.getenv("GRADIENT_BASE_URL", "https://inference.do-ai.run/v1/"),
            api_key=gradient_key(),
        )
        model = os.getenv("LLM_MODEL", "anthropic-claude-sonnet-5.5")
    else:
        client = OpenAI(base_url=os.getenv("OPENAI_BASE_URL") or None)
        model = os.getenv("LLM_MODEL", "gpt-4o-mini")
    return _parse_json(_openai_chat(client, model, system, user))


def _snap(start: float, end: float, lines: list[dict], min_len: float, max_len: float) -> tuple[float, float] | None:
    starts = [l["start"] for l in lines if l["start"] <= start + 0.5]
    ends = [l["end"] for l in lines if l["end"] >= end - 0.5]
    start = starts[-1] if starts else start
    end = ends[0] if ends else end
    if end - start > max_len:
        fitting = [l["end"] for l in lines if start < l["end"] <= start + max_len]
        end = fitting[-1] if fitting else start + max_len
    if end - start < min_len * 0.75:
        return None
    return round(max(0.0, start - 0.15), 2), round(end + 0.35, 2)


def _dedupe(clips: list[dict], count: int) -> list[dict]:
    clips = sorted(clips, key=lambda c: c.get("score", 0), reverse=True)
    picked = []
    for c in clips:
        if all(c["end"] <= p["start"] or c["start"] >= p["end"] for p in picked):
            picked.append(c)
        if len(picked) >= count:
            break
    return picked


def _context(lines: list[dict], start: float, end: float, pad: float = 20) -> list[dict]:
    return [l for l in lines if l["end"] >= start - pad and l["start"] <= end + pad]


def find_highlights(
    lines: list[dict],
    count: int = 5,
    min_len: float = 20,
    max_len: float = 59,
    max_chars: int = 80000,
    refine: bool = True,
) -> list[dict]:
    select = SELECT_PROMPT.format(min_len=int(min_len), max_len=int(max_len))
    windows = _windows(lines, max_chars)
    candidates = []
    for i, window in enumerate(windows, 1):
        print(f"[highlights] pass 1: window {i}/{len(windows)} ({len(window)} lines)")
        user = f"Return up to {max(4, count * 2)} clips from this transcript.\n\n" + _format_lines(window)
        try:
            result = ask_llm(select, user)
        except Exception as e:
            print(f"[highlights] window {i} failed: {e}")
            continue
        for c in result.get("clips", []):
            snapped = _snap(float(c["start"]), float(c["end"]), window, min_len, max_len)
            if snapped:
                candidates.append({**c, "start": snapped[0], "end": snapped[1]})

    shortlist = _dedupe(candidates, count * 2)
    if not shortlist:
        return []
    if not refine:
        return sorted(
            [{**c, "title": c.get("why", "Clip")[:60], "hook_text": "", "description": "", "tags": []}
             for c in shortlist[:count]],
            key=lambda c: c["start"],
        )

    print(f"[highlights] pass 2: refining {len(shortlist)} candidates")
    blocks = []
    for idx, c in enumerate(shortlist):
        blocks.append(
            f"### Candidate {idx} (current {c['start']:.1f}-{c['end']:.1f}, why: {c.get('why', '')})\n"
            + _format_lines(_context(lines, c["start"], c["end"]))
        )
    refine_prompt = REFINE_PROMPT.format(min_len=int(min_len), max_len=int(max_len))
    try:
        refined = ask_llm(refine_prompt, "\n\n".join(blocks)).get("clips", [])
    except Exception as e:
        print(f"[highlights] refine failed ({e}); using pass-1 results")
        refined = []

    final = []
    for r in refined:
        idx = r.get("id")
        if not isinstance(idx, int) or not 0 <= idx < len(shortlist):
            continue
        snapped = _snap(float(r["start"]), float(r["end"]), lines, min_len, max_len)
        if not snapped:
            continue
        final.append({
            "start": snapped[0],
            "end": snapped[1],
            "score": r.get("score", shortlist[idx].get("score", 0)),
            "title": r.get("title") or "Clip",
            "hook_text": r.get("hook_text", ""),
            "description": r.get("description", ""),
            "tags": r.get("tags", []),
        })
    if not final:
        final = [{**c, "title": c.get("why", "Clip")[:60], "hook_text": "", "description": "", "tags": []}
                 for c in shortlist]
    return sorted(_dedupe(final, count), key=lambda c: c["start"])
