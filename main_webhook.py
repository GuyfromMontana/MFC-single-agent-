from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse
from zep_cloud.client import Zep
import os
import logging

app = FastAPI()

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Zep client
zep = Zep(api_key=os.getenv("ZEP_API_KEY"))

def format_phone_for_zep(phone: str) -> str:
    """Convert phone to Zep user_id format: +1XXXXXXXXXX"""
    clean = ''.join(filter(str.isdigit, phone))
    if len(clean) == 10:
        return f"+1{clean}"
    elif len(clean) == 11 and clean.startswith('1'):
        return f"+{clean}"
    return phone

async def lookup_caller_in_zep(from_number: str) -> dict:
    """Look up caller in Zep and return their info"""
    try:
        user_id = format_phone_for_zep(from_number)
        logger.info(f"Looking up caller: {user_id}")
        
        # Get user from Zep
        user = zep.user.get(user_id=user_id)
        
        if user and user.first_name:
            logger.info(f"Found returning caller: {user.first_name}")
            return {
                "caller_name": user.first_name,
                "is_returning": True,
                "user_id": user_id
            }
        else:
            logger.info(f"New caller: {user_id}")
            return {
                "caller_name": "New caller",
                "is_returning": False,
                "user_id": user_id
            }
            
    except Exception as e:
        logger.error(f"Zep lookup error: {str(e)}")
        # Default to new caller on error
        return {
            "caller_name": "New caller",
            "is_returning": False,
            "user_id": from_number
        }

@app.post("/retell/webhook/inbound")
async def retell_inbound_webhook(request: Request):
    """
    Retell inbound call webhook - fires at call start
    Returns dynamic variables for caller personalization
    """
    try:
        body = await request.json()
        logger.info(f"Inbound webhook received: {body}")
        
        # Extract call details (Retell sends different structures)
        call = body.get("call", {})
        from_number = call.get("from_number", "")
        call_id = call.get("call_id", "")
        
        if not from_number:
            logger.warning("No from_number in webhook")
            from_number = "unknown"
        
        # Look up caller in Zep
        caller_info = await lookup_caller_in_zep(from_number)
        
        # Build response in Retell's expected format
        response = {
            "response_id": 1,  # Retell wants a response_id
            "dynamic_variables": {
                "caller_name": caller_info["caller_name"],
                "is_returning": str(caller_info["is_returning"]).lower(),
            },
            "metadata": {
                "caller_name": caller_info["caller_name"],
                "is_returning": caller_info["is_returning"],
                "from_number": from_number,
                "user_id": caller_info["user_id"],
                "call_id": call_id,
            }
        }
        
        logger.info(f"Returning webhook response: {response}")
        return JSONResponse(content=response)
        
    except Exception as e:
        logger.error(f"Webhook error: {str(e)}")
        # Return safe defaults on error
        return JSONResponse(
            content={
                "response_id": 1,
                "dynamic_variables": {
                    "caller_name": "New caller",
                    "is_returning": "false",
                },
                "metadata": {
                    "error": str(e)
                }
            }
        )

@app.get("/health")
async def health_check():
    """Health check endpoint"""
    return {"status": "healthy", "service": "MFC-Retell-Webhook"}

# Your existing custom function endpoints can stay below
# ...existing code for lookup_town, create_lead, etc...
```

### Environment Variables (Railway)

Make sure these are set in your Railway service:
```
ZEP_API_KEY=your_zep_api_key_here
```

---

## 2. Retell Dashboard Configuration

### Step A: Register the Webhook

1. **Go to Retell Dashboard** → **Webhooks** section
2. **Add New Webhook**:
   - **Webhook URL**: `https://mfc-single-agent-production.up.railway.app/retell/webhook/inbound`
   - **Events to Subscribe**: 
     - ✅ `call_started` (this is the critical one)
     - ✅ `call_ended` (optional, for cleanup)
   - **Description**: "Montana Feed - Caller Recognition via Zep"

3. **Test Connection** (if available in UI)

### Step B: Configure Your Agent

1. **Go to your Montana Feed agent** in Retell
2. **Update Begin Message**:
```
{{#if caller_name}}
  {{#if (eq caller_name "New caller")}}
    Hi, thanks for calling Montana Feed Company! How can I help you today?
  {{else}}
    Hi {{caller_name}}, thanks for calling Montana Feed Company! How can I help you today?
  {{/if}}
{{else}}
  Hi, thanks for calling Montana Feed Company! How can I help you today?
{{/if}}
```

**Simpler alternative** (if Retell doesn't support conditionals in begin message):
```
Hi {{caller_name}}, thanks for calling Montana Feed Company! How can I help you today?
```
(Just let it say "Hi New caller" for first-timers, then the LLM will handle it properly via system prompt)

### Step C: Update System Prompt

Add this section **at the very top** of your agent's system prompt:
```
## CALLER RECOGNITION

You have access to dynamic variables that identify the caller:
- {{caller_name}}: The caller's first name, or "New caller" if unknown
- {{is_returning}}: "true" if returning customer, "false" if new

GREETING PROTOCOL:
- If {{caller_name}} is NOT "New caller": Greet warmly using their name naturally (e.g., "Hi [Name], great to hear from you!"). Do NOT ask for their name again.
- If {{caller_name}} IS "New caller": Use generic greeting and ask for their name once during initial conversation for future reference.

The system automatically looks up callers before the call starts - you don't need to search for them.

[Rest of your existing system prompt follows...]
```

---

## 3. Testing Protocol

### Test 1: New Caller Flow

**Action**: Call from a number NOT in Zep
```
Expected webhook response:
{
  "dynamic_variables": {
    "caller_name": "New caller",
    "is_returning": "false"
  }
}

Expected agent behavior:
- Generic greeting
- Asks for name during conversation
- Otherwise normal cattle nutrition consultation
