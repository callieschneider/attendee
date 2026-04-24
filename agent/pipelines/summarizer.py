"""
Transcript summarizer pipeline.
Ported from abstraKt's transcript-summarizer.ts.
Uses Gemini Flash with strict JSON mode to extract {summary, tasks[]} from raw transcript.
"""
import json
import logging

from django.conf import settings

log = logging.getLogger("agent.pipelines.summarizer")

SYSTEM_PROMPT = """You are a meeting notes assistant. Given the raw transcript of a meeting, produce a JSON object with exactly two keys:
- "summary": A concise markdown summary (3-8 bullet points covering key discussion points and decisions)
- "tasks": An array of action items extracted from the meeting, each with: {"title": "...", "assignee": "...", "due_date": "YYYY-MM-DD or null"}

Output ONLY valid JSON. No markdown code fences. No extra text. No commentary."""


def summarize_transcript(transcript_text: str) -> dict:
    """
    Summarize a meeting transcript using Gemini Flash.
    Returns {"summary": str, "tasks": list[dict]}.
    """
    if not transcript_text or not transcript_text.strip():
        return {"summary": "", "tasks": []}

    # Truncate very long transcripts to avoid token limits
    text = transcript_text.strip()[:24000]

    try:
        import google.generativeai as genai
        genai.configure(api_key=settings.GOOGLE_API_KEY)
        model_name = getattr(settings, "AGENT_SUMMARIZER_MODEL", "gemini-2.5-flash")
        model = genai.GenerativeModel(model_name)

        response = model.generate_content(
            [
                {"role": "user", "parts": [
                    SYSTEM_PROMPT,
                    f"\n\nTranscript:\n{text}",
                ]}
            ],
            generation_config=genai.GenerationConfig(
                response_mime_type="application/json",
                temperature=0.2,
            ),
        )
        raw = response.text.strip()
    except Exception:
        log.exception("summarize_transcript: Gemini API call failed")
        return {"summary": "Summarization failed — transcript available in full.", "tasks": []}

    # Parse JSON response
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        log.warning("summarize_transcript: Gemini returned non-JSON: %s", raw[:200])
        # Try to extract JSON if wrapped in markdown fences
        import re
        match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", raw, re.DOTALL)
        if match:
            try:
                data = json.loads(match.group(1))
            except json.JSONDecodeError:
                data = {"summary": raw[:2000], "tasks": []}
        else:
            data = {"summary": raw[:2000], "tasks": []}

    summary = data.get("summary", "")
    tasks = data.get("tasks", [])

    # Normalise tasks
    clean_tasks = []
    for t in tasks[:50]:  # cap at 50 action items
        if isinstance(t, dict) and t.get("title"):
            clean_tasks.append({
                "title": str(t.get("title", ""))[:512],
                "assignee": str(t.get("assignee", ""))[:255],
                "due_date": t.get("due_date"),
            })
        elif isinstance(t, str) and t.strip():
            clean_tasks.append({"title": t.strip()[:512], "assignee": "", "due_date": None})

    return {"summary": summary, "tasks": clean_tasks}
