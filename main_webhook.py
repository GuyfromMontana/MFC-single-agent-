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
