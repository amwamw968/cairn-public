"""Evaluate recall suggestions on synthetic examples against a local model.

This is an exploratory smoke benchmark, not a measure of teaching outcomes.
The cases contain no learner data. Cloud use is restricted to an explicit
DeepSeek opt-in and uses a key from the named environment variable.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import time
from pathlib import Path
from urllib.parse import urlsplit

from cairn.models import Question
from cairn.recap.assessment import assess
from cairn.tutor import llm


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", required=True, help="Loopback OpenAI-compatible server base URL")
    parser.add_argument("--model", required=True, help="Model name as exposed by the server")
    parser.add_argument("--repeat", type=int, default=1, choices=range(1, 6))
    parser.add_argument("--allow-deepseek", action="store_true", help="Allow the official DeepSeek HTTPS API")
    parser.add_argument("--key-env", help="Name of an environment variable holding the API key")
    args = parser.parse_args()
    url = urlsplit(args.url)
    local = url.scheme in {"http", "https"} and url.hostname in {"127.0.0.1", "::1", "localhost"}
    cloud = args.allow_deepseek and url.scheme == "https" and url.hostname == "api.deepseek.com" and not url.port
    if (not (local or cloud) or url.username or url.password or url.query or url.fragment or url.path not in {"", "/"}):
        parser.error("--url must be a loopback base URL or the explicitly allowed DeepSeek HTTPS API")
    if cloud and not args.key_env:
        parser.error("DeepSeek requires --key-env")
    if args.key_env and (not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", args.key_env) or not os.environ.get(args.key_env)):
        parser.error("the named key environment variable is invalid or empty")
    key_value = os.environ.get(args.key_env) if args.key_env else None
    os.environ["CAIRN_TUTOR_LLM_URL"] = args.url
    os.environ["CAIRN_TUTOR_LLM_MODEL"] = args.model
    os.environ.pop("CAIRN_TUTOR_LLM_API_KEY", None)
    if key_value:
        os.environ["CAIRN_TUTOR_LLM_API_KEY"] = key_value
    os.environ.pop("CAIRN_TUTOR_LLM_THINKING", None)
    if cloud:
        os.environ["CAIRN_TUTOR_LLM_THINKING"] = "disabled"
    # Never load a personal key file into this synthetic evaluation.
    os.environ["CAIRN_TUTOR_LLM_KEY_FILE"] = "/dev/null"
    source = Path(__file__).resolve().parents[1] / "tests" / "recap_synthetic_cases.json"
    cases = json.loads(source.read_text(encoding="utf-8"))
    original_post = llm._post
    requests = {"attempted": 0, "succeeded": 0}
    def counted_post(*args, **kwargs):
        requests["attempted"] += 1
        result = original_post(*args, **kwargs)
        if result[0] == 200:
            requests["succeeded"] += 1
        return result
    llm._post = counted_post
    mismatches = []
    results: dict[str, int] = {"adequate": 0, "needs_more": 0, "unavailable": 0}
    started = time.monotonic()
    for iteration in range(args.repeat):
        for case in cases:
            question = Question(learner_id=1, subject="synthetic", question_type="synthetic",
                                content=case["question"], answer=case["answer"], analysis=case["reference"])
            result, _focus = assess(question, case["initial"], case.get("followup"))
            results[result] += 1
            if result not in case["expected"]:
                mismatches.append({"id": case["id"], "iteration": iteration + 1,
                                   "expected": case["expected"], "actual": result})
            print(f"{iteration + 1}/{args.repeat} {case['id']}: {result}", flush=True)
    total = len(cases) * args.repeat
    print(json.dumps({"cases": len(cases), "repeat": args.repeat, "total": total,
                      "matches": total - len(mismatches), "results": results, "model_requests": requests,
                      "mismatches": mismatches, "seconds": round(time.monotonic() - started, 2)},
                     ensure_ascii=False), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
