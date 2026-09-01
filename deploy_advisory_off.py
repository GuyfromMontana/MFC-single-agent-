"""
Publish the ADVISORY-OFF configuration to the live Retell agent.

MFC staff were not comfortable with the agent giving nutrition / feed /
product advice, so that capability ships disabled while every other feature
(routing, messages, callbacks, leads, store info, transfers) goes live.

This script does three things to the Retell LLM:
  1. Swaps general_prompt to retell_system_prompt_v14.md (advisory-off prompt)
  2. Removes the `search_products` and `get_recommendations` tools
  3. Rewrites the `search_knowledge_base` description to company-facts-only

The server-side half of the shutoff lives in config.ADVISORY_MODE (default
"off") on Railway — this script only handles the Retell half. Both halves are
independent on purpose: the backend refuses to serve advice even if a stale
agent version somehow still lists the tools.

Retell versioning (same flow as deploy_retell_config.py, learned 2026-07-31):
a published LLM cannot be PATCHed, so:
  POST /create-agent-version/{agent_id} {"base_version": N}  -> draft vN+1
  PATCH /update-retell-llm/{llm_id}                          -> hits the draft
  POST /publish-agent-version/{agent_id} {"version": N+1}    -> live

TO REVERSE: set ADVISORY_MODE=on on the Railway service, then republish an
agent version carrying the v13 prompt with both tools restored. Both halves
are required — either one alone leaves the advisor off.

Run:
  py deploy_advisory_off.py           # dry run: show the diff, change nothing
  py deploy_advisory_off.py --apply   # draft -> patch -> publish
"""

import json
import os
import sys
from datetime import date
from pathlib import Path

import httpx

API_BASE = "https://api.retellai.com"
HERE = Path(__file__).parent
PROMPT_PATH = HERE / "retell_system_prompt_v14.md"

ADVISORY_TOOLS = {"search_products", "get_recommendations"}

KB_DESCRIPTION = (
    "Searches the knowledge base for COMPANY facts only - who owns Montana Feed "
    "Company, history, the Axmen connection, the Purina partnership, who staff "
    "members are, what a Livestock Performance Specialist does, how to order, "
    "delivery, financing, custom mixes, commodity loads. It does NOT cover "
    "nutrition, feed, minerals, cattle health, or product recommendations - that "
    "advice is turned off on this agent and those queries return NO_MATCH by "
    "design. On NO_MATCH, do not guess: defer to the caller's livestock "
    "specialist and take a message."
)


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
    r = client.post(f"{API_BASE}{path}", headers=headers(api_key), json=body, timeout=30)
    r.raise_for_status()
    return r.json()


def build_tools(live_tools):
    """Drop the advisory tools; retune the KB tool's description."""
    out = []
    for tool in live_tools:
        name = tool.get("name")
        if name in ADVISORY_TOOLS:
            continue
        tool = json.loads(json.dumps(tool))  # don't mutate the fetched object
        if name == "search_knowledge_base":
            tool["description"] = KB_DESCRIPTION
            tool["execution_message_description"] = "Let me pull that up"
        out.append(tool)
    return out


def main():
    apply = "--apply" in sys.argv
    load_env()

    api_key = os.environ.get("RETELL_API_KEY", "").strip()
    agent_id = os.environ.get("RETELL_AGENT_ID", "").strip()
    if not api_key or not agent_id:
        print("ERROR: RETELL_API_KEY / RETELL_AGENT_ID not set")
        sys.exit(1)

    if not PROMPT_PATH.exists():
        print(f"ERROR: {PROMPT_PATH.name} not found")
        sys.exit(1)
    new_prompt = PROMPT_PATH.read_text(encoding="utf-8")

    with httpx.Client() as client:
        agent = get_json(client, f"/get-agent/{agent_id}", api_key)
        base_version = agent.get("version")
        llm_id = agent["response_engine"].get("llm_id")
        print(f"Agent {agent.get('agent_name')} v{base_version} -> LLM {llm_id}")

        llm = get_json(client, f"/get-retell-llm/{llm_id}", api_key)
        old_prompt = llm.get("general_prompt") or ""
        old_tools = llm.get("general_tools") or []
        new_tools = build_tools(old_tools)

        removed = [t.get("name") for t in old_tools if t.get("name") in ADVISORY_TOOLS]
        print(f"\nPrompt : {old_prompt.splitlines()[0][:60]!r} ({len(old_prompt)} chars)")
        print(f"      -> {new_prompt.splitlines()[0][:60]!r} ({len(new_prompt)} chars)")
        print(f"\nTools  : {[t.get('name') for t in old_tools]}")
        print(f"      -> {[t.get('name') for t in new_tools]}")
        print(f"Removed: {removed or 'none (already gone)'}")

        if not apply:
            print("\nDRY RUN - nothing changed. Re-run with --apply to publish.")
            return

        draft = post_json(client, f"/create-agent-version/{agent_id}",
                          {"base_version": base_version}, api_key)
        new_version = draft.get("version")
        print(f"\nCreated draft agent version v{new_version}")

        r = httpx.patch(
            f"{API_BASE}/update-retell-llm/{llm_id}",
            headers=headers(api_key),
            json={"general_prompt": new_prompt, "general_tools": new_tools},
            timeout=30,
        )
        r.raise_for_status()
        print(f"PATCHed draft LLM {llm_id}")

        title = f"advisory-off-{date.today().isoformat()}"
        post_json(client, f"/publish-agent-version/{agent_id}",
                  {"version": new_version, "version_title": title}, api_key)
        print(f"Published v{new_version} as {title!r}")

        live = get_json(client, f"/get-retell-llm/{llm_id}", api_key)
        live_tools = [t.get("name") for t in (live.get("general_tools") or [])]
        live_prompt = (live.get("general_prompt") or "").splitlines()[0][:60]
        print("\nVERIFY (live LLM):")
        print(f"  prompt: {live_prompt!r}")
        print(f"  tools : {live_tools}")
        leaked = [n for n in live_tools if n in ADVISORY_TOOLS]
        print(f"  advisory tools present: {leaked or 'NONE - correct'}")


if __name__ == "__main__":
    main()
