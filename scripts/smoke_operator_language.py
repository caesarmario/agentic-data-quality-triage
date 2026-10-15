"""One explicit Gemini-backed Copilot API acceptance, orchestrated by DAG 92."""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import requests
from apps.common.i18n import normalize_language


def run(language: str, alert_ref: str, request=requests.request) -> dict:
    language = normalize_language(language)
    if not re.fullmatch(r"DQ-[0-9]{8}-[A-F0-9]{6}", alert_ref):
        raise ValueError("A canonical synthetic incident Alert Ref is required.")
    base = os.getenv("CONTROL_PLANE_API_URL", "http://api:8000").rstrip("/")
    question = (
        "Jelaskan masalah ini dan satu langkah aman berikutnya dalam dua paragraf singkat. "
        "Bedakan alert awal, hasil pemeriksaan terakhir, dan apakah buktinya masih berlaku sekarang."
    )
    response = request(method="POST", url=base + "/api/v1/copilot/answer",
                       json={"question": question, "alert_key": alert_ref, "language": language}, timeout=60)
    response.raise_for_status()
    payload = response.json()
    runtime = payload.get("llm_runtime") or {}
    if (payload.get("language") != language or payload.get("alert_display_id") != alert_ref
            or runtime.get("provider") != "gemini" or runtime.get("used_heuristic") is not False
            or runtime.get("fallback_reason") or not str(runtime.get("model") or "")
            or not str(payload.get("answer") or "").strip()):
        raise ValueError("Strict operator acceptance requires the selected incident, language and external Gemini without fallback.")
    tokens = int(runtime.get("input_tokens", 0)) + int(runtime.get("output_tokens", 0))
    cost = float(runtime.get("estimated_cost_usd", 0))
    if not 0 < tokens <= 16384 or not 0 <= cost <= 0.01:
        raise ValueError("Operator acceptance exceeded its bounded token/cost contract.")
    return {"status": "success", "language": language, "alert_ref": alert_ref,
            "agent_run_id": payload.get("agent_run_id"), "provider": runtime["provider"], "model": runtime["model"],
            "used_heuristic": False, "total_tokens": tokens, "estimated_cost_usd": cost,
            "answer_preview": str(payload["answer"])[:1500], "model_call_limit": 1,
            "billing_record": "provider dashboard; cost here is an application estimate"}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--language", required=True, choices=("en", "id"))
    parser.add_argument("--alert-ref", required=True)
    args = parser.parse_args()
    try:
        print(json.dumps(run(args.language, args.alert_ref), ensure_ascii=False))
        return 0
    except Exception as exc:
        print(json.dumps({"status": "failed", "error_type": type(exc).__name__, "retry": "none"}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
