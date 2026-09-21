"""
Publish the post-call analysis configuration to the live Retell agent.

WHY THIS EXISTS (2026-09-21): Retell's stock `call_successful` preset asks
whether "the agent seems to have a successful call with the user, where the
agent finishes the task, and the call was complete without being cutoff."
That measures whether the AGENT spoke coherently, not whether the CALLER got
anything. On call_16e0f5355f3995a9b40ea0a066b the agent falsely told a Dillon
account holder we don't carry cat food, he hung up, nothing was captured --
and Retell scored it call_successful=TRUE. The worst failure mode we have
(confident, fluent, wrong) was scoring as a win, so neither the dashboard nor
the daily digest could ever surface it.

This script redefines success by OUTCOME and adds the specific failure flags
the digest reads. It does NOT touch the prompt or the tools -- use
deploy_prompt.py for the prompt.

Same versioning constraint as deploy_prompt.py (learned 2026-07-31): a
published agent cannot be PATCHed, so:
  POST /create-agent-version/{agent_id} {"base_version": N}  -> draft vN+1
  PATCH /update-agent/{agent_id}                             -> hits the draft
  POST /publish-agent-version/{agent_id} {"version": N+1}    -> live

Requires in .env (or exported): RETELL_API_KEY, RETELL_AGENT_ID

Run:
  py deploy_call_analysis.py            # dry run, shows a field-by-field diff
  py deploy_call_analysis.py --apply    # draft/patch/publish
  py deploy_call_analysis.py --apply --title analysis-outcome-scoring
"""

import json
import os
import sys
from datetime import date
from pathlib import Path

import httpx

API_BASE = "https://api.retellai.com"
HERE = Path(__file__).parent


# --------------------------------------------------------------------------- #
# The configuration we want live.                                             #
#                                                                             #
# `call_summary` and `call_successful` are system presets -- they keep their   #
# names and land as top-level fields on call_analysis. Everything else is      #
# custom and lands in call_analysis.custom_analysis_data, which is what        #
# mfc-voice-dashboard/pages/api/cron/digest.js reads.                          #
# --------------------------------------------------------------------------- #
ANALYSIS_DATA = [
    {
        "type": "system-presets",
        "name": "call_summary",
        "description": (
            "Write a 1-3 sentence summary of the call based on the call transcript. "
            "Should capture the important information and actions taken during the "
            "call. If the caller wanted something and did not get it, say so plainly "
            "in the summary rather than describing the call as a clarification."
        ),
    },
    {
        "type": "system-presets",
        "name": "call_successful",
        "description": (
            "Judge this call by what the CALLER got, not by how fluently the agent "
            "spoke. This is Montana Feed Company's after-hours phone agent; its job "
            "is to answer store questions and to capture everything else for a human.\n"
            "\n"
            "SUCCESSFUL only if at least one is true:\n"
            "- the caller's question was answered correctly from store/company facts, or\n"
            "- the caller's need was captured for a human (a message, callback, lead, "
            "or live transfer actually happened), or\n"
            "- the caller reached a wrong number, hung up immediately, or was a "
            "robodialer, with nothing for the agent to do.\n"
            "\n"
            "UNSUCCESSFUL whenever the caller wanted something and the call ended with "
            "nothing captured and nothing resolved. A polite, confident, well-spoken "
            "refusal is still a failure. In particular, mark UNSUCCESSFUL if:\n"
            "- the agent said or implied Montana Feed does not carry, stock or sell a "
            "product (it has no inventory access and is never allowed to say this);\n"
            "- the caller tried to place or ask about an order and no message was taken;\n"
            "- the caller told the agent a name or identity and the agent kept using a "
            "different one;\n"
            "- the caller hung up while the agent was still mid-sentence;\n"
            "- the agent gave feed, mineral, nutrition, ration or animal-health advice, "
            "which is switched off for this agent and must go to a specialist.\n"
            "\n"
            "A short call is NOT automatically a failure -- a caller who got a fast, "
            "correct answer is a success. A long call is NOT automatically a success."
        ),
    },
    {
        "type": "boolean",
        "name": "dropped_the_ball",
        "description": (
            "True if the caller wanted something and the call ended with nothing "
            "captured for a human and nothing actually resolved -- no message, no "
            "callback, no lead, no transfer, no correct answer. This is the headline "
            "flag: it is what a person would call 'we lost that one'. False for "
            "wrong numbers, robodialers, immediate hangups, and calls where the "
            "caller genuinely got what they needed."
        ),
    },
    {
        "type": "boolean",
        "name": "claimed_not_carried",
        "description": (
            "True if the agent said or implied that Montana Feed Company does not "
            "carry, stock, sell or handle some product -- for example 'we don't carry "
            "that', 'that's not something we sell', or 'we're a cattle feed and "
            "mineral outfit' offered as a reason a product is unavailable. The agent "
            "has NO inventory access and is never permitted to make this claim, so "
            "any instance is a defect regardless of whether it happened to be true."
        ),
    },
    {
        "type": "boolean",
        "name": "order_intent",
        "description": (
            "True if the caller was trying to buy something or ask whether an item is "
            "available -- placing an order, adding to an order, checking stock or "
            "price, or arranging a pickup or delivery of goods. True even if the "
            "agent deflected it and no order was taken."
        ),
    },
    {
        "type": "boolean",
        "name": "gave_feed_advice",
        "description": (
            "True if the agent offered any feed, mineral, nutrition, ration, "
            "supplement, forage, breeding or animal-health guidance of its own -- "
            "including hedged or 'generally speaking' answers. This advice is "
            "switched off for this agent and belongs to the livestock specialists. "
            "False when the agent correctly declined and routed it to a specialist."
        ),
    },
    {
        "type": "boolean",
        "name": "identity_mismatch",
        "description": (
            "True if the caller stated a name or identity that differed from the one "
            "the agent greeted them with, or corrected the agent about who they were. "
            "Ranch phones are shared, so the billing name on the caller ID is often "
            "not the person talking. Also true if the agent kept using the original "
            "name after being corrected."
        ),
    },
    {
        "type": "string",
        "name": "what_went_wrong",
        "description": (
            "If the call was unsuccessful or something went wrong, state it in one "
            "short sentence a manager could act on -- what the caller wanted and what "
            "the agent did instead. Name the product, person or store involved. If "
            "the call went fine, return an empty string."
        ),
        "examples": [
            "",
            "Caller wanted to order 3 bags of cat food; agent said we don't carry it and took no message.",
            "Caller said he was Austin, agent kept calling him Kevin and he hung up.",
            "Caller asked about mineral for fall pairs; agent answered it instead of routing to the LPS.",
        ],
    },
]


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

    /publish-agent-version answers 200 with an EMPTY body (see deploy_prompt.py).
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


def summarize(fields):
    return {f.get("name"): (f.get("type"), (f.get("description") or "")) for f in fields}


def main():
    apply = "--apply" in sys.argv
    load_env()

    api_key = os.environ.get("RETELL_API_KEY", "").strip()
    agent_id = os.environ.get("RETELL_AGENT_ID", "").strip()
    if not api_key or not agent_id:
        print("ERROR: RETELL_API_KEY / RETELL_AGENT_ID not set")
        sys.exit(1)

    title = arg_value("--title") or f"call-analysis-{date.today().isoformat()}"

    with httpx.Client() as client:
        agent = get_json(client, f"/get-agent/{agent_id}", api_key)
        base_version = agent.get("version")
        old = agent.get("post_call_analysis_data") or []
        print(f"Agent {agent.get('agent_name')} v{base_version} "
              f"(analysis model: {agent.get('post_call_analysis_model')})")

        old_s, new_s = summarize(old), summarize(ANALYSIS_DATA)
        print(f"\nAnalysis fields: {len(old)} -> {len(ANALYSIS_DATA)}")
        for name in sorted(set(old_s) | set(new_s)):
            if name not in old_s:
                print(f"  + {name:22} NEW ({new_s[name][0]})")
            elif name not in new_s:
                print(f"  - {name:22} REMOVED")
            elif old_s[name] != new_s[name]:
                print(f"  ~ {name:22} description changed "
                      f"({len(old_s[name][1])} -> {len(new_s[name][1])} chars)")
            else:
                print(f"    {name:22} unchanged")

        if old == ANALYSIS_DATA:
            print("\nAlready live verbatim. Nothing to do.")
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
            f"{API_BASE}/update-agent/{agent_id}",
            headers=headers(api_key),
            json={"post_call_analysis_data": ANALYSIS_DATA},
            timeout=30,
        )
        r.raise_for_status()
        print(f"PATCHed draft agent (post_call_analysis_data only).")

        post_json(client, f"/publish-agent-version/{agent_id}",
                  {"version": new_version, "version_title": title}, api_key)
        print(f"Published v{new_version} as {title!r}. "
              "Inbound number tracks latest_published - live now.")

        live = get_json(client, f"/get-agent/{agent_id}", api_key)
        live_fields = live.get("post_call_analysis_data") or []
        print("\nVerify:")
        print(f"  version : v{live.get('version')} (published={live.get('is_published')})")
        print(f"  fields  : {len(live_fields)} -> "
              f"{', '.join(f.get('name') for f in live_fields)}")
        drift = [n for n in summarize(ANALYSIS_DATA)
                 if summarize(live_fields).get(n) != summarize(ANALYSIS_DATA)[n]]
        print(f"  drift   : {'NONE' if not drift else drift}")
        if drift:
            print("\n  WARNING: the API did not store some fields verbatim (system "
                  "presets can ignore a custom description). Check the ones listed.")
            sys.exit(1)


if __name__ == "__main__":
    main()
