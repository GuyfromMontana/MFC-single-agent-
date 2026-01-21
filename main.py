# MFC Voice Agent - Complete Fix Guide
## Solving the Darby Hallucination, Long Pauses, and Missing Nutrition

---

## What Was Wrong (Based on Your Jan 21 Call Log)

### 1. ❌ Nutrition Function Not Working
**Problem:** Agent couldn't provide cattle nutrition advice (the main purpose!)
**Root Cause:** Backend endpoint at wrong path
- Your code: `/retell/search_knowledge_base`
- Retell calls: `/retell/functions/search_knowledge_base`
- Missing `/functions/` in the path = 404 error

### 2. ❌ Darby Location Hallucination
**Problem:** Agent assumed you were in Darby without asking, never corrected when lookup failed
**Root Causes:**
- Darby not in database (returned "TOWN_NOT_FOUND")
- Prompt had no instructions on what to do when town lookup fails
- Agent hallucinated/assumed instead of asking for clarification

### 3. ❌ Long Awkward Pauses (18+ seconds)
**Problem:** Dead air during searches
**Root Cause:** Prompt doesn't tell agent to say "Let me check that..." before calling search_knowledge_base
- Search can take 3-5 seconds (embedding + database query)
- No conversational buffer = awkward silence

### 4. ❌ Callback Function Failing
**Problem:** Database schema error
**Root Cause:** `callbacks` table missing `phone_number` column

---

## The Three Files You Need

I've created three fixed files for you:

### 1. **main_FIXED.py** (Your Railway Backend)
**What changed:**
- ✅ Fixed endpoint path: `/retell/search_knowledge_base` → `/retell/functions/search_knowledge_base`
- ✅ Improved town lookup to return "TOWN_NOT_FOUND:" when no match
- ✅ Better error handling and logging
- ✅ Handles both parameter structures from Retell

**To deploy:**
1. Download `main_FIXED.py`
2. Rename your current `main.py` to `main_OLD.py` (backup)
3. Rename `main_FIXED.py` to `main.py`
4. Commit to GitHub (via GitHub Desktop)
5. Railway will auto-deploy

### 2. **retell_prompt_v3.2_FIXED.txt** (Your Retell Agent Prompt)
**What changed:**
- ✅ Added instructions for handling TOWN_NOT_FOUND
- ✅ Added conversational buffers ("Let me check that...") before searches
- ✅ Clear examples of correct vs incorrect town lookup handling
- ✅ Never assume locations - always ask for clarification

**To deploy:**
1. Log into Retell dashboard
2. Go to your MFC agent
3. Copy the entire contents of `retell_prompt_v3.2_FIXED.txt`
4. Paste into agent's system prompt (replace current prompt)
5. Save

### 3. **database_fixes.sql** (Supabase Database Updates)
**What it does:**
- ✅ Adds `phone_number` column to `callbacks` table
- ✅ Adds Ravalli County (Darby's county) to routing
- ✅ Adds other missing Western Montana counties
- ✅ Optional: Creates `montana_towns` table for better lookups

**To deploy:**
1. Log into Supabase
2. Go to SQL Editor
3. Paste the SQL script
4. Run it
5. Verify with the verification queries at the bottom

---

## Step-by-Step Deployment

### Phase 1: Database (Do This First)
**Time: 5 minutes**

1. Log into Supabase at https://supabase.com
2. Select your MFC project
3. Click "SQL Editor" in left sidebar
4. Click "New Query"
5. Copy/paste contents of `database_fixes.sql`
6. Click "Run"
7. Check for errors
8. Run the verification queries to confirm:
   - `callbacks` table has `phone_number` column
   - Ravalli County exists in `county_coverage`

### Phase 2: Backend Code (Railway)
**Time: 10 minutes**

1. Open your MFC project folder on your computer
2. Find `main.py`
3. Rename it to `main_OLD_backup.py`
4. Save `main_FIXED.py` as `main.py` in the same folder
5. Open GitHub Desktop
6. You should see changes to `main.py`
7. Write commit message: "Fix search_knowledge_base endpoint and town lookup"
8. Click "Commit to main"
9. Click "Push origin"
10. Go to Railway dashboard
11. Watch the deployment logs
12. Wait for "Deployment successful"
13. Check health endpoint: https://your-railway-url/health

### Phase 3: Retell Prompt Update
**Time: 5 minutes**

1. Log into Retell dashboard
2. Find your Montana Feed Company agent
3. Click to edit
4. Find the "System Prompt" or "General Prompt" section
5. Select all current text (Ctrl+A or Cmd+A)
6. Delete it
7. Copy the entire contents of `retell_prompt_v3.2_FIXED.txt`
8. Paste into the prompt field
9. Scroll through to make sure it all copied correctly
10. Click "Save" or "Update Agent"

---

## Testing After Deployment

### Test 1: Nutrition Search (The Big One)
**Call the agent and say:**
"What mineral should I feed my pregnant cows?"

**Expected behavior:**
- Agent says "Let me check that for you..." (fills the pause)
- Brief pause (3-5 seconds)
- Agent provides specific product recommendation with reasoning
- No error, no "I can't help with that"

**If it fails:**
- Check Railway logs for errors on `/retell/functions/search_knowledge_base`
- Verify endpoint is responding at the new path

### Test 2: Darby Town Lookup
**Call the agent and say:**
"I'm in Darby."

**Expected behavior:**
- Agent says "Hmm, I'm not finding Darby in my system. What county are you in?"
- You say "Ravalli"
- Agent says "Perfect! You're in Ravalli County. Taylor Staudenmeyer is your specialist there."

**If it fails:**
- Check Supabase - does Ravalli County exist in county_coverage?
- Check Railway logs - is lookup_town being called?

### Test 3: Callback Scheduling
**Call the agent and say:**
"Have Brady call me back tomorrow morning about minerals."

**Expected behavior:**
- Agent confirms: "All set! I've scheduled a callback for tomorrow morning..."
- No database error in Railway logs

**If it fails:**
- Check Supabase callbacks table - does phone_number column exist?
- Check Railway logs for PostgreSQL errors

### Test 4: End-to-End Nutrition Consultation
**Call and say:**
"My cows are thin going into winter. What should I feed them?"

**Expected behavior:**
1. Agent says "Let me check that for you..."
2. Short pause
3. Agent provides specific advice about high-energy tubs (38-20 E)
4. Explains WHY (energy needs, body condition recovery)
5. Mentions product by name
6. Offers specialist follow-up

---

## Common Issues & Solutions

### Issue: Search still not working
**Check:**
- Railway deployment completed successfully?
- Endpoint path in code is `/retell/functions/search_knowledge_base`?
- Retell tool definition calls the same path?
- Railway logs show the endpoint being hit?

**Fix:**
- Redeploy to Railway
- Verify Retell tool configuration matches new path

### Issue: Still hallucinating locations
**Check:**
- Did you update the Retell prompt?
- Check Retell dashboard - is the new prompt actually saved?

**Fix:**
- Copy prompt again, make sure you got ALL of it
- Save/update in Retell

### Issue: Still getting callback database errors
**Check:**
- Did you run the SQL in Supabase?
- Does the callbacks table have phone_number column?

**Fix:**
- Run: `ALTER TABLE callbacks ADD COLUMN IF NOT EXISTS phone_number TEXT;`
- Verify: `SELECT * FROM information_schema.columns WHERE table_name = 'callbacks';`

---

## What Each Fix Does

### Backend Fix (main_FIXED.py)
```python
# OLD (broken):
@app.post("/retell/search_knowledge_base")

# NEW (working):
@app.post("/retell/functions/search_knowledge_base")
```

Also improved the response format for TOWN_NOT_FOUND so the prompt can detect it.

### Prompt Fix (v3.2)
Added two critical sections:

**1. Conversational buffer:**
```
Agent: "Let me check that for you..."
[calls search_knowledge_base]
```

**2. Town not found handling:**
```
[lookup returns "TOWN_NOT_FOUND: Darby"]
Agent: "Hmm, I'm not finding Darby in my system. What county are you in?"
```

### Database Fix (SQL)
```sql
-- Fix callbacks schema
ALTER TABLE callbacks ADD COLUMN IF NOT EXISTS phone_number TEXT;

-- Add missing counties
INSERT INTO county_coverage (county_name, primary_lps, state)
VALUES ('Ravalli County', 'Taylor Staudenmeyer', 'MT');
```

---

## Expected Results After All Fixes

✅ Nutrition questions work perfectly
✅ No more Darby hallucinations (asks for county)
✅ No awkward long pauses (conversational buffers)
✅ Callback scheduling works without errors
✅ Agent can actually do its job (cattle consultation)

---

## Next Steps After Testing

1. **Monitor real calls** - Check Railway logs for any errors
2. **Gather missing towns** - When agent says "town not found", add that town to database
3. **Optimize search speed** - If searches are still slow, we can add more caching
4. **Update ranch consultation skill** - Make sure all Montana counties are documented

---

## Questions to Ask Yourself After Testing

- Did nutrition search work on first try?
- Did agent handle unknown towns gracefully?
- Were there any awkward pauses?
- Did callback scheduling succeed without errors?
- Can you actually use this agent now for real ranch consultations?

If you answered YES to all five, **you're good to go!**

If any are NO, let me know which test failed and I'll help you debug it.

---

## File Summary

You now have:
1. ✅ `main_FIXED.py` - Deploy to Railway
2. ✅ `retell_prompt_v3.2_FIXED.txt` - Update in Retell dashboard
3. ✅ `database_fixes.sql` - Run in Supabase SQL editor
4. ✅ This implementation guide

**Total deployment time: ~20 minutes**
**Total testing time: ~10 minutes**

**You should have a fully working MFC voice agent after this.**
