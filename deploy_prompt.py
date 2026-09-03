"""
Publish a system prompt file to the live Retell agent, leaving tools alone.

`deploy_advisory_off.py` also publishes a prompt, but it exists to strip the
two advisory tools and retune the KB tool description — running it to ship an
unrelated prompt change conflates two decisions. This script only swaps
`general_prompt`.

Retell versioning (learned 2026-07-31): a published LLM cannot be PATCHed, so:
  POST /create-agent-version/{agent_id} {"base_version": N}  -> draft vN+1
  PATCH /update-retell-llm/{llm_id}                          -> hits the draft
  POST /publish-agent-version/{agent_id} {"version": N+1}    -> live

The inbound number is pinned to agent_version "latest_published", so the
publish goes live immediately.

Requires in .env (or exported): RETELL_API_KEY, RETELL_AGENT_ID

Run:
  py deploy_prompt.py retell_system_prompt_v15.md            # dry run
  py deploy_prompt.py retell_system_prompt_v15.md --apply    # draft/patch/publish
  py deploy_prompt.py retell_system_prompt_v15.md --apply --title territory-rebuild
"""

import os
import sys
from datetime import date
from pathlib import Path

import httpx

API_BASE = "https://api.retellai.com"
HERE = Path(__file__).parent


def load_env():
    env_file = HERE / ".env"
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def headers(api_key):
    return {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}


def get_json(client, path, api_key):
    r = client.get(f"{API_BASE}{path}", headers=headers(api_key), timeout=30)
    r.raise_for_status()
    return r.json()


def post_json(client, path, body, api_key):
    """POST and return the parsed body, or {} when there isn't one.

    /publish-agent-version answers 200 with an EMPTY body — blindly calling
    .json() on it raises JSONDecodeError *after* the publish has already
    succeeded, which reads like a failed deploy when it wasn't.
    """
    r = client.post(f"{API_BASE}{path}", headers=headers(api_key), json=body, timeout=30)
    r.raise_for_status()
    if not r.content:
        return {}
    try:
        return r.json()
    except ValueError:
        return {}


def arg_value(flag, default=None):
    if flag in sys.argv:
        i = sys.argv.index(flag)
        if i + 1 < len(sys.argv):
            return sys.argv[i + 1]
    return default


def main():
    apply = "--apply" in sys.argv
    load_env()

    positional = [a for a in sys.argv[1:] if not a.startswith("--")]
    # Drop a value that belongs to --title.
    title_val = arg_value("--title")
    if title_val and title_val in positional:
        positional.remove(title_val)
    if not positional:
        print("ERROR: give the prompt file, e.g. "
              "py deploy_prompt.py retell_system_prompt_v15.md --apply")
        sys.exit(1)

    prompt_path = HERE / positional[0]
    if not prompt_path.exists():
        print(f"ERROR: {prompt_path.name} not found")
        sys.exit(1)
    new_prompt = prompt_path.read_text(encoding="utf-8")

    api_key = os.environ.get("RETELL_API_KEY", "").strip()
    agent_id = os.environ.get("RETELL_AGENT_ID", "").strip()
    if not api_key or not agent_id:
        print("ERROR: RETELL_API_KEY / RETELL_AGENT_ID not set")
        sys.exit(1)

    title = title_val or f"{prompt_path.stem}-{date.today().isoformat()}"

    with httpx.Client() as client:
        agent = get_json(client, f"/get-agent/{agent_id}", api_key)
        engine = agent.get("response_engine") or {}
        if engine.get("type") != "retell-llm":
            print(f"ERROR: response engine is {engine.get('type')!r}; "
                  "this script only supports retell-llm")
            sys.exit(1)
        llm_id = engine.get("llm_id")
        base_version = agent.get("version")
        print(f"Agent {agent.get('agent_name')} v{base_version} -> LLM {llm_id}")

        llm = get_json(client, f"/get-retell-llm/{llm_id}", api_key)
        old_prompt = llm.get("general_prompt") or ""
        tool_names = [t.get("name") for t in (llm.get("general_tools") or [])]

        first_old = old_prompt.splitlines()[0][:60] if old_prompt else "(empty)"
        first_new = new_prompt.splitlines()[0][:60] if new_prompt else "(empty)"
        print(f"\nPrompt : {first_old!r} ({len(old_prompt)} chars)")
        print(f"      -> {first_new!r} ({len(new_prompt)} chars)")
        print(f"Tools  : {len(tool_names)} left untouched -> {', '.join(n for n in tool_names if n)}")

        if old_prompt == new_prompt:
            print("\nPrompt is already live verbatim. Nothing to do.")
            return

        if not apply:
            print(f"\nDRY RUN - nothing changed. Re-run with --apply to publish "
                  f"as {title!r}.")
            return

        draft = post_json(client, f"/create-agent-version/{agent_id}",
                          {"base_version": base_version}, api_key)
        new_version = draft.get("version")
        print(f"\nCreated draft agent version v{new_version}")

        r = client.patch(
            f"{API_BASE}/update-retell-llm/{llm_id}",
            headers=headers(api_key),
            json={"general_prompt": new_prompt},
            timeout=30,
        )
        r.raise_for_status()
        print(f"PATCHed draft LLM {llm_id} (prompt only).")

        post_json(client, f"/publish-agent-version/{agent_id}",
                  {"version": new_version, "version_title": title}, api_key)
        print(f"Published v{new_version} as {title!r}. "
              "Inbound number tracks latest_published - live now.")

        live = get_json(client, f"/get-retell-llm/{llm_id}", api_key)
        live_first = (live.get("general_prompt") or "").splitlines()[0][:60]
        live_tools = [t.get("name") for t in (live.get("general_tools") or [])]
        print("\nVerify:")
        print(f"  prompt: {live_first!r}")
        print(f"  tools : {len(live_tools)} -> {', '.join(n for n in live_tools if n)}")


if __name__ == "__main__":
    main()
