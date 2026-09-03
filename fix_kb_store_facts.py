"""
Correct the store/staff facts in the voice agent's knowledge base.

Found 2026-09-01 while checking whether "MT Feed Co Location Info.docx" covered
the agent's post-advisory-shutoff scope. These rows are all in the NON-advisory
allowlist, so they survived the shutoff and the agent serves them today:

  1. The LPS roster named 8 specialists including Isabell Gilleard (resigned
     2026-08-11) and Danielle Peterson (not on MFC's roster), and omitted
     Mike Vanek (hired 2026). Seven actual.
  2. The store-manager row listed four managers and omitted Riverton entirely.
  3. Three rows published 307-851-1393 as the Wyoming number - that is KayLee
     Klaahsen's LPS cell, not the store line (Eagle ST: 307-840-5469). Both
     numbers are real; they differ in purpose.
  4. "Does MFC serve Wyoming?" claimed Riverton is serviced out of the Columbus
     warehouse. Riverton is its own warehouse with its own manager.
  5. Miles City manager spelled "Tamara"; order_users says Tamra.
  6. Dillon had no address/hours row in `locations` - the only store missing one.

Sources of truth used (in precedence order):
  - Eagle `ST` table       -> store addresses + store phone lines (POS system of record)
  - Supabase `order_users` -> staff names, roles, store assignment
  - Supabase `specialists` -> LPS phone numbers
  - "MT Feed Co Location Info.docx" -> manager full names, Riverton manager

DELIBERATELY NOT CHANGED:
  - The Riverton STREET ADDRESS. Three sources disagree (doc: 10749 WY-789;
    Eagle: 91 Marlatt Road, Shoshoni; Supabase warehouses: 148 Marlatt Spur).
    Eagle is the system of record, so the KB keeps Eagle's until Guy resolves it.
  - Every county/territory claim. The doc and the routing table disagree on 19
    counties and nothing available can arbitrate, so the rewritten answers
    describe LPS by HOME STORE, which both sources agree on.

Embeddings are computed over "Question: ...\n\nAnswer: ..." (see
backfill_embeddings.py), so every edited row is re-embedded here. A changed
answer left with its old embedding would drift out of retrieval range.

Run:
  py fix_kb_store_facts.py           # dry run - show every before/after
  py fix_kb_store_facts.py --apply   # write + re-embed
"""

import os
import sys
from pathlib import Path

HERE = Path(__file__).parent

HOURS = "Open Monday through Friday, 9:00 AM to 5:00 PM."

# --- rewritten answers, keyed by the exact `question` text -------------------
UPDATES = {
    "Who are the Livestock Performance Specialists?": (
        "Montana Feed Company works with seven Purina-certified Livestock "
        "Performance Specialists across Montana and Wyoming. Out of Dillon: "
        "Taylor Staudenmeyer, 406-596-5995. Out of Columbus: Hannah Imer, "
        "406-697-0366. Out of Lewistown: Brady Johnson, 406-648-7033, and Mike "
        "Vanek, 406-366-4668. Out of Miles City: Austin Buzanowski, "
        "406-839-1255, and Caitlin Lapicki, 406-851-8749. Out of Riverton, "
        "Wyoming: KayLee Klaahsen, 307-851-1393."
    ),
    "Who are the store managers?": (
        "Our store managers are: Kase Stoddard in Dillon (406-499-9642), Dan "
        "Otis in Columbus (406-931-0030), Brenda Atchison-Curry in Lewistown "
        "(406-380-2099), Tamra Hodgins in Miles City (406-851-1833), and "
        "Kristena Dickinson in Riverton, Wyoming (307-840-5469)."
    ),
    "Does Montana Feed Company serve Wyoming?": (
        "Yes. Riverton, Wyoming is one of our five warehouses with its own "
        "manager and store line - it is not serviced out of Columbus. The "
        "Riverton store number is 307-840-5469 and the manager is Kristena "
        "Dickinson. The Livestock Performance Specialist covering Wyoming is "
        "KayLee Klaahsen, 307-851-1393."
    ),
    "What is the Riverton office address and contact?": (
        "The Montana Feed Company Riverton warehouse mailing address is PO Box "
        "1418, Riverton, WY 82501. The physical location is 91 Marlatt Road, "
        "Shoshoni, WY. The store number is 307-840-5469 and the manager is "
        "Kristena Dickinson. " + HOURS + " Your Livestock Performance "
        "Specialist is KayLee Klaahsen at 307-851-1393."
    ),
    "Where are Montana Feed Company stores located?": (
        "Montana Feed Company has five warehouses, all open Monday through "
        "Friday, 9:00 AM to 5:00 PM. Dillon: 10010 MT-41, Dillon, MT 59725, "
        "manager Kase Stoddard, 406-499-9642. Columbus: 903 Clough Avenue "
        "South, Columbus, MT 59019, manager Dan Otis, 406-931-0030. Lewistown: "
        "66 Fairway Lane, Lewistown, MT 59457, manager Brenda Atchison-Curry, "
        "406-380-2099. Miles City: 207 City View Drive, Miles City, MT 59301, "
        "manager Tamra Hodgins, 406-851-1833. Riverton: 91 Marlatt Road, "
        "Shoshoni, WY, manager Kristena Dickinson, 307-840-5469."
    ),
    "What is the Miles City office address and contact?": (
        "The Miles City office is located at 207 City View Drive, PO Box 156, "
        "Miles City, MT 59301. Manager: Tamra Hodgins. Phone: 406-851-1833. "
        + HOURS
    ),
    "What is the phone number for the Miles City store?": (
        "The Miles City store phone number is 406-851-1833. The store manager "
        "is Tamra Hodgins. Miles City services eastern Montana including "
        "Custer, Powder River, and surrounding counties."
    ),
    "What is the phone number for the Columbus store?": (
        "The Columbus store phone number is 406-931-0030. The store manager is "
        "Dan Otis. Columbus services south-central Montana. Riverton, Wyoming "
        "is a separate warehouse with its own manager and phone line."
    ),
    "What is the phone number for the Dillon store?": (
        "The Dillon store phone number is 406-499-9642. The store manager is "
        "Kase Stoddard. Dillon services southwest Montana including Beaverhead, "
        "Madison, and Silver Bow counties, and also provides delivery service "
        "to the Missoula area."
    ),
    "What is the phone number for the Lewistown store?": (
        "The Lewistown store phone number is 406-380-2099. The store manager is "
        "Brenda Atchison-Curry. Lewistown services central Montana including "
        "Fergus County and surrounding areas."
    ),
}

# --- rows that don't exist yet ----------------------------------------------
INSERTS = [
    {
        "question": "What is the Dillon office address and hours?",
        "answer": (
            "The Dillon office is located at 10010 MT-41, Dillon, MT 59725. "
            "Manager: Kase Stoddard. Phone: 406-499-9642. " + HOURS + " Dillon "
            "is Montana Feed Company's southwest Montana warehouse."
        ),
        "category": "locations",
        "is_active": True,
        "customer_facing": True,
        "priority": 0,
    },
]


def load_env():
    for line in (HERE / ".env").read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def embed_text(question, answer):
    """Match backfill_embeddings.py exactly - the RPC compares against these."""
    return "Question: " + question + "\n\nAnswer: " + answer


def main():
    apply = "--apply" in sys.argv
    load_env()
    from supabase import create_client

    sb = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_SERVICE_KEY"])
    openai_client = None
    if apply:
        from openai import OpenAI
        openai_client = OpenAI(api_key=os.environ["OPENAI_API_KEY"])

    touched = 0
    for question, new_answer in UPDATES.items():
        rows = (sb.table("knowledge_base")
                  .select("id,question,answer,category")
                  .eq("question", question).execute().data)
        if not rows:
            print("\n!! NOT FOUND: " + question)
            continue
        print("\n=== %s   [%d row(s), %s]" % (question, len(rows), rows[0]["category"]))
        print("  OLD: " + rows[0]["answer"][:190])
        print("  NEW: " + new_answer[:190])
        if not apply:
            continue
        emb = openai_client.embeddings.create(
            model="text-embedding-3-small",
            input=embed_text(question, new_answer),
        ).data[0].embedding
        for row in rows:  # duplicates exist in `locations`; fix every copy
            (sb.table("knowledge_base")
               .update({"answer": new_answer, "embedding": emb})
               .eq("id", row["id"]).execute())
            touched += 1

    for rec in INSERTS:
        existing = (sb.table("knowledge_base").select("id")
                      .eq("question", rec["question"]).execute().data)
        if existing:
            print("\n=== INSERT SKIPPED (already present): " + rec["question"])
            continue
        print("\n=== INSERT [%s]: %s" % (rec["category"], rec["question"]))
        print("  " + rec["answer"][:190])
        if not apply:
            continue
        rec = dict(rec)
        rec["embedding"] = openai_client.embeddings.create(
            model="text-embedding-3-small",
            input=embed_text(rec["question"], rec["answer"]),
        ).data[0].embedding
        sb.table("knowledge_base").insert(rec).execute()
        touched += 1

    if apply:
        print("\nAPPLIED - %d row(s) written." % touched)
    else:
        print("\nDRY RUN - nothing written. Re-run with --apply.")


if __name__ == "__main__":
    main()
