"""
run_inference.py
------------------
Purpose: Read experiment_inputs.csv (from setup_data.py), send each prompt
to the target LLM(s), and save every response as its own JSON file. This is
Step 2 of the pipeline.

Output: one JSON file per (model, scenario_id, persona_id) combination,
saved in inference_results/

TODO:
  1. pip install openai groq google-genai pyyaml
  2. Set your API keys as environment variables before running:
       export OPENAI_API_KEY="your-key-here"      # console.openai.com
       export GROQ_API_KEY="your-key-here"        # console.groq.com (hosts Llama)
       export GEMINI_API_KEY="your-key-here"       # aistudio.google.com
  3. Fill in config.yaml with the models you want to test (GPT-4o, Llama
     3.3 70B via Groq, and Gemini 3.5 Flash are already set up as the
     starting trio -- 3 different companies, plus one open-weight model).
  4. Run on the Golden 5 first (20 rows per model) before scaling to the
     full 50.
"""

import csv
import json
import os
import time
import yaml
import openai
import groq
from google import genai

# ---------------------------------------------------------------------------
# STEP 1: Load settings from config.yaml
# ---------------------------------------------------------------------------

with open("config.yaml", "r", encoding="utf-8") as f:
    config = yaml.safe_load(f)

MODELS = config["models"]
TEMPERATURE = config["settings"]["temperature"]
MAX_TOKENS = config["settings"]["max_tokens"]
INPUT_CSV = config["settings"]["input_csv"]
OUTPUT_DIR = config["settings"]["output_dir"]

os.makedirs(OUTPUT_DIR, exist_ok=True)


# ---------------------------------------------------------------------------
# STEP 2: One function per provider that calls the API and returns plain text
# ---------------------------------------------------------------------------

def call_openai(model_id: str, prompt: str, max_tokens: int) -> tuple:
    """Send one prompt to a GPT model. Returns (text, finish_reason).
    finish_reason will be 'length' if the response got cut off."""
    client = openai.OpenAI()  # reads OPENAI_API_KEY from environment

    response = client.chat.completions.create(
        model=model_id,
        max_tokens=max_tokens,
        temperature=TEMPERATURE,
        messages=[
            {"role": "user", "content": prompt}
        ],
    )
    text = response.choices[0].message.content
    finish_reason = response.choices[0].finish_reason
    return text, finish_reason


def call_groq(model_id: str, prompt: str, max_tokens: int) -> tuple:
    """Send one prompt to a Groq-hosted model (e.g. Llama 3.3 70B).
    Returns (text, finish_reason). Groq's API is OpenAI-compatible."""
    client = groq.Groq()  # reads GROQ_API_KEY from environment

    response = client.chat.completions.create(
        model=model_id,
        max_tokens=max_tokens,
        temperature=TEMPERATURE,
        messages=[
            {"role": "user", "content": prompt}
        ],
    )
    text = response.choices[0].message.content
    finish_reason = response.choices[0].finish_reason
    return text, finish_reason


def call_gemini(model_id: str, prompt: str, max_tokens: int) -> tuple:
    """Send one prompt to a Gemini model. Returns (text, finish_reason).
    finish_reason will be 'MAX_TOKENS' if the response got cut off.

    thinking_budget=0 turns off Gemini's internal 'reasoning' tokens, which
    otherwise silently eat into max_output_tokens and can cut off the
    visible answer before it finishes (this is what caused truncated
    responses in early test runs)."""
    client = genai.Client()  # reads GEMINI_API_KEY from environment

    response = client.models.generate_content(
        model=model_id,
        contents=prompt,
        config={
            "temperature": TEMPERATURE,
            "max_output_tokens": max_tokens,
            "thinking_config": {"thinking_budget": 0},
        },
    )
    text = response.text
    finish_reason = response.candidates[0].finish_reason
    return text, finish_reason


# finish_reason values that mean "the response got cut off early"
TRUNCATED_REASONS = {"length", "MAX_TOKENS"}


def call_model(provider: str, model_id: str, prompt: str) -> tuple:
    """Route to the right provider function, then automatically retry once
    with double the token budget if the response was truncated.
    Returns (text, finish_reason, was_retried)."""

    def _call(max_tokens):
        if provider == "openai":
            return call_openai(model_id, prompt, max_tokens)
        elif provider == "groq":
            return call_groq(model_id, prompt, max_tokens)
        elif provider == "gemini":
            return call_gemini(model_id, prompt, max_tokens)
        else:
            raise ValueError(f"Unknown provider: {provider}")

    text, finish_reason = _call(MAX_TOKENS)
    was_retried = False

    if finish_reason in TRUNCATED_REASONS:
        print(f"    (response truncated, retrying with {MAX_TOKENS * 2} tokens...)")
        text, finish_reason = _call(MAX_TOKENS * 2)
        was_retried = True

    return text, finish_reason, was_retried


# Error text fragments that mean "this is temporary, worth waiting and
# retrying" rather than a permanent failure (bad API key, bad model ID, etc.)
TRANSIENT_ERROR_MARKERS = ["429", "503", "rate_limit", "UNAVAILABLE", "overloaded"]


def call_model_with_backoff(provider: str, model_id: str, prompt: str,
                             max_attempts: int = 3, base_wait: int = 20) -> tuple:
    """Wraps call_model with automatic retry-with-backoff for transient
    errors (rate limits, temporary server overload). Waits base_wait,
    then base_wait*2, then base_wait*4 seconds between attempts.
    Re-raises the error if it's not a recognized transient one, or if
    max_attempts is exceeded -- the caller's existing except block still
    handles saving that as a permanent failure."""
    last_error = None
    for attempt in range(1, max_attempts + 1):
        try:
            return call_model(provider, model_id, prompt)
        except Exception as e:
            last_error = e
            error_text = str(e)
            is_transient = any(marker in error_text for marker in TRANSIENT_ERROR_MARKERS)
            if not is_transient or attempt == max_attempts:
                raise
            wait_time = base_wait * (2 ** (attempt - 1))
            print(f"    Transient error (attempt {attempt}/{max_attempts}), "
                  f"waiting {wait_time}s before retry...")
            time.sleep(wait_time)
    raise last_error  # pragma: no cover -- loop always returns or raises above


# ---------------------------------------------------------------------------
# STEP 3: Load the prompts generated by setup_data.py
# ---------------------------------------------------------------------------

def load_rows(csv_path: str):
    with open(csv_path, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        return list(reader)


# ---------------------------------------------------------------------------
# STEP 4: Run every prompt through every model and save results incrementally
# ---------------------------------------------------------------------------

def run():
    rows = load_rows(INPUT_CSV)
    total = len(rows) * len(MODELS)
    count = 0

    for model in MODELS:
        model_name = model["name"]
        provider = model["provider"]
        model_id = model["model_id"]

        for row in rows:
            count += 1
            scenario_id = row["scenario_id"]
            persona_id = row["persona_id"]
            prompt = row["full_prompt"]

            # Build a unique filename for this (model, scenario, persona)
            out_filename = f"{model_name}__{scenario_id}__{persona_id}.json"
            out_path = os.path.join(OUTPUT_DIR, out_filename)

            # Skip only if we already have a SUCCESSFUL result for this row.
            # A file that exists but contains an error (rate limit, etc.)
            # should be retried, not skipped -- otherwise failed calls get
            # permanently locked in as "done" on every future run.
            if os.path.exists(out_path):
                with open(out_path, "r", encoding="utf-8") as f:
                    existing = json.load(f)
                if existing.get("error") is None:
                    print(f"[{count}/{total}] SKIP (already succeeded): {out_filename}")
                    continue
                else:
                    print(f"[{count}/{total}] RETRYING (previous attempt failed): {out_filename}")

            print(f"[{count}/{total}] Calling {model_name} on {scenario_id}/{persona_id}...")

            try:
                response_text, finish_reason, was_retried = call_model_with_backoff(provider, model_id, prompt)
                result = {
                    "model_name": model_name,
                    "scenario_id": scenario_id,
                    "persona_id": persona_id,
                    "prompt": prompt,
                    "response": response_text,
                    "finish_reason": str(finish_reason),
                    "still_truncated": str(finish_reason) in TRUNCATED_REASONS,
                    "was_retried": was_retried,
                    "error": None,
                }
                if result["still_truncated"]:
                    print(f"    WARNING: still truncated even after retry -- consider raising MAX_TOKENS further")
            except Exception as e:
                # Don't let one failed call kill the whole run -- save the
                # error and keep going. You can re-run later to retry it
                # (just delete the file for that row first).
                print(f"  ERROR: {e}")
                result = {
                    "model_name": model_name,
                    "scenario_id": scenario_id,
                    "persona_id": persona_id,
                    "prompt": prompt,
                    "response": None,
                    "finish_reason": None,
                    "still_truncated": None,
                    "was_retried": None,
                    "error": str(e),
                }

            with open(out_path, "w", encoding="utf-8") as f:
                json.dump(result, f, indent=2)

            # Small delay to be polite to the API and avoid rate limits.
            # TODO: increase this if you hit rate-limit errors.
            time.sleep(1)

    print(f"\nDone. Results saved in {OUTPUT_DIR}/")


if __name__ == "__main__":
    run()