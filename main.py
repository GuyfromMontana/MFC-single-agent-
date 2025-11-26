from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse
from datetime import datetime
import os
import json
from zep_cloud.client import Zep
from zep_cloud import Message
from supabase import create_client, Client
import logging

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI()

# Get Zep API key from environment
ZEP_API_KEY = os.getenv("ZEP_API_KEY", "").strip()

if not ZEP_API_KEY:
    raise ValueError("ZEP_API_KEY environment variable is required")

print(f"🔑 Zep API Key loaded: {ZEP_API_KEY[:5]}...{ZEP_API_KEY[-5:]}")
print(f"🔑 Key length: {len(ZEP_API_KEY)}")
print(f"🔑 Key starts with 'z_': {ZEP_API_KEY.startswith('z_')}")

# Initialize Zep client
zep = Zep(api_key=ZEP_API_KEY)

# Initialize Supabase client
SUPABASE_URL = os.getenv("SUPABASE_URL", "").strip()
SUPABASE_KEY = os.getenv("SUPABASE_KEY", "").strip()

if not SUPABASE_URL or not SUPABASE_KEY:
    raise ValueError("SUPABASE_URL and SUPABASE_KEY environment variables are required")

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)


@app.get("/")
async def root():
    return {
        "status": "MFC Agent Memory Service Running",
        "timestamp": datetime.now().isoformat(),
        "zep_configured": bool(ZEP_API_KEY),
        "supabase_configured": bool(SUPABASE_URL)
    }


@app.get("/health")
async def health_check():
    return {
        "status": "healthy",
        "service": "mfc-agent-memory",
        "timestamp": datetime.now().isoformat()
    }


@app.post("/")
async def handle_vapi_webhook(request: Request):
    """Handle all incoming webhooks from Vapi"""
    try:
        payload = await request.json()
        
        # Get the message type
        message_type = payload.get("message", {}).get("type", "unknown")
        print(f"📨 Received webhook: {message_type}")
        
        # Handle assistant.started - when call begins
        if message_type == "assistant.started":
            phone_number = payload.get("message", {}).get("call", {}).get("customer", {}).get("number")
            if phone_number:
                print(f"📞 Call started for: {phone_number}")
            return JSONResponse(content={"status": "acknowledged"})
        
        # Handle tool-calls - this is how Vapi requests memory and executes tools
        elif message_type in ["tool-calls", "function-call"]:
            print("🔍 Tool call received")
            
            message_data = payload.get("message", {})
            
            # The function data is in toolCallList or toolCalls
            tool_call_list = message_data.get("toolCallList", [])
            if not tool_call_list:
                tool_call_list = message_data.get("toolCalls", [])
            
            print(f"   📦 Tool call list: {json.dumps(tool_call_list, indent=2)}")
            
            # Get the first tool call (should only be one for get_caller_history)
            if tool_call_list and len(tool_call_list) > 0:
                tool_call = tool_call_list[0]
                
                # Extract function details from the tool call
                function_name = tool_call.get("function", {}).get("name")
                if not function_name:
                    function_name = tool_call.get("name")
                
                parameters = tool_call.get("function", {}).get("arguments", {})
                if not parameters:
                    parameters = tool_call.get("parameters", {})
                
                # Parse parameters if they're a string
                if isinstance(parameters, str):
                    try:
                        parameters = json.loads(parameters)
                    except json.JSONDecodeError:
                        parameters = {}
                
                # Get phone number from call data
                phone_number = message_data.get("call", {}).get("customer", {}).get("number")
                
                print(f"   Function: {function_name}")
                print(f"   Phone: {phone_number}")
                print(f"   Parameters: {parameters}")
                
                # Handle memory retrieval function
                if function_name == "get_caller_history":
                    print(f"   🧠 Retrieving memory for: {phone_number}")
                    context = await get_caller_context(phone_number)
                    # Add caller ID to the response
                    context["caller_phone"] = phone_number
                    print(f"   ✓ Memory retrieved: is_returning_caller={context.get('is_returning_caller')}")
                    return JSONResponse(content={
                        "result": context
                    })
                
                # Handle create_lead function
                elif function_name == "create_lead":
                    print(f"   💾 Creating lead for: {phone_number}")
                    result = await create_lead(phone_number, parameters)
                    print(f"   ✓ Lead result: {result}")
                    return JSONResponse(content={
                        "result": result
                    })
                
                # Handle other functions here as needed
                print(f"   ⚠️ Function not implemented: {function_name}")
                return JSONResponse(content={"result": f"Function {function_name} not implemented"})
            else:
                print(f"   ⚠️ No tool calls found in list")
                return JSONResponse(content={"result": "No tool calls found"})
        
        # Handle end-of-call-report for saving conversation
        elif message_type == "end-of-call-report":
            print("💾 End-of-call-report received")
            
            # Debug: Print top-level structure
            print(f"   Top-level payload keys: {list(payload.keys())}")
            
            # The actual data is in payload["message"]
            message_data = payload.get("message", {})
            print(f"   Message keys: {list(message_data.keys())}")
            
            # Extract phone number from call.customer.number
            call_data = message_data.get("call", {})
            customer_data = call_data.get("customer", {})
            phone_number = customer_data.get("number")
            
            # Extract call ID
            call_id = call_data.get("id")
            
            # Get transcript/messages - they're at the message level, not nested deeper
            transcript = message_data.get("transcript", "")
            messages = message_data.get("messages", [])
            
            print(f"   ✓ Messages found at message level!")
            print(f"   Transcript length: {len(transcript)}")
            
            if messages:
                print(f"   First message keys: {list(messages[0].keys())}")
            
            if phone_number and (transcript or messages):
                print(f"\n📞 Processing call:")
                print(f"   Phone: {phone_number}")
                print(f"   Call ID: {call_id}")
                print(f"   Transcript length: {len(transcript)}")
                print(f"   Messages: {len(messages)}")
                
                try:
                    await save_conversation(phone_number, call_id, transcript, messages)
                    return JSONResponse(content={"status": "success", "message": "Conversation saved"})
                except Exception as e:
                    print(f"❌ Error in save_conversation: {str(e)}")
                    import traceback
                    traceback.print_exc()
                    # Don't raise - just log and continue
                    return JSONResponse(content={"status": "error", "message": str(e)})
            else:
                print("⚠️ Missing required data:")
                print(f"   Phone: {phone_number}")
                print(f"   Transcript: {len(transcript) if transcript else 0}")
                print(f"   Messages: {len(messages) if messages else 0}")
                return JSONResponse(content={"status": "ignored", "reason": "missing_data"})
        
        # Handle other webhook types
        else:
            print(f"⚠️ Unhandled webhook type: {message_type}")
            return JSONResponse(content={"status": "ignored", "type": message_type})
            
    except Exception as e:
        print(f"❌ Error processing webhook: {str(e)}")
        import traceback
        traceback.print_exc()
        # Return 200 to prevent Vapi from retrying
        return JSONResponse(content={"status": "error", "message": str(e)})


async def get_caller_context(phone_number: str) -> dict:
    """
    Retrieve conversation history and context for a returning caller.
    Returns a summary that Vapi can use to personalize the greeting.
    """
    try:
        # Check if this caller exists in Zep
        try:
            user = zep.user.get(user_id=phone_number)
            print(f"   ✓ Found existing user: {phone_number}")
            
            # Try to get their conversation history
            try:
                # Get threads for this user
                threads = zep.thread.list(user_id=phone_number)
                thread_count = len(threads) if threads else 0
                print(f"   ✓ Found {thread_count} conversation threads")
                
                # User exists and has history
                return {
                    "is_returning_caller": True,
                    "conversation_count": thread_count,
                    "summary": f"Returning caller with {thread_count} previous conversations."
                }
            except Exception as thread_error:
                print(f"   ℹ Could not retrieve threads: {thread_error}")
                return {
                    "is_returning_caller": True,
                    "summary": "Returning caller with previous conversation history."
                }
            
        except Exception as e:
            # New caller - no history
            print(f"   ℹ New caller (no history): {phone_number}")
            return {
                "is_returning_caller": False,
                "summary": "First time caller - no previous conversation history."
            }
            
    except Exception as e:
        print(f"   ❌ Error retrieving caller context: {e}")
        import traceback
        traceback.print_exc()
        return {
            "is_returning_caller": False,
            "summary": "Unable to retrieve caller history."
        }


async def create_lead(phone_number: str, parameters: dict) -> dict:
    """
    Create a new lead in Supabase
    """
    try:
        first_name = parameters.get("first_name", "")
        last_name = parameters.get("last_name", "")
        email = parameters.get("email", "")
        county = parameters.get("county", "")
        primary_interest = parameters.get("primary_interest", "")
        herd_size = parameters.get("herd_size", "")
        livestock_type = parameters.get("livestock_type", "")
        
        # Use phone from parameters if provided, otherwise use caller ID
        lead_phone = parameters.get("phone", "") or phone_number
        
        # Build the name
        name = f"{first_name} {last_name}".strip()
        if not name:
            name = "Unknown Caller"
        
        lead_data = {
            "name": name,
            "phone": lead_phone,
            "email": email,
            "county": county,
            "notes": primary_interest,
            "herd_size": herd_size,
            "livestock_type": livestock_type,
            "status": "new",
            "created_at": datetime.utcnow().isoformat()
        }
        
        # Remove empty fields
        lead_data = {k: v for k, v in lead_data.items() if v}
        lead_data["status"] = "new"  # Always include status
        
        print(f"   📝 Lead data: {lead_data}")
        
        result = supabase.table("leads").insert(lead_data).execute()
        
        print(f"   ✓ Created lead: {name}")
        return {
            "success": True,
            "lead_id": result.data[0]["id"] if result.data else None,
            "message": f"Lead created successfully for {name}"
        }
    except Exception as e:
        print(f"   ❌ Error creating lead: {str(e)}")
        import traceback
        traceback.print_exc()
        return {
            "success": False,
            "error": str(e),
            "message": "Failed to create lead - please try again or contact support"
        }


async def save_conversation(phone_number: str, call_id: str, transcript: str, messages: list):
    """
    Save conversation to Zep memory using thread.add_messages
    """
    try:
        print(f"\n💾 Saving conversation for: {phone_number}")
        
        # Use phone number as user_id
        user_id = phone_number
        
        # Create thread_id combining phone and call_id for uniqueness
        thread_id = f"mfc_{phone_number}_{call_id}"
        print(f"   Thread: {thread_id}")
        
        # Ensure user exists in Zep
        try:
            user = zep.user.get(user_id=user_id)
            print(f"   ✓ User exists in Zep")
        except Exception as e:
            print(f"   Creating new user in Zep: {user_id}")
            zep.user.add(
                user_id=user_id,
                first_name=phone_number,
                metadata={
                    "phone": phone_number,
                    "source": "mfc_voice_agent"
                }
            )
            print(f"   ✓ Created new user in Zep: {user_id}")
        
        # Format messages for Zep with character limit
        MAX_MESSAGE_LENGTH = 2500  # Zep's limit
        zep_messages = []
        truncated_count = 0
        
        for msg in messages:
            role = msg.get("role", "user")
            content = msg.get("message", "")
            
            # Skip tool calls and system messages
            if role in ["tool_calls", "tool_call_result", "system"]:
                continue
            
            # Map Vapi roles to Zep roles
            if role == "assistant" or role == "bot":
                zep_role = "assistant"
            else:
                zep_role = "user"
            
            if content:
                # Truncate message if it exceeds Zep's limit
                if len(content) > MAX_MESSAGE_LENGTH:
                    content = content[:MAX_MESSAGE_LENGTH - 50] + "... [truncated]"
                    truncated_count += 1
                
                # Use 'role' not 'role_type' for newer Zep SDK
                zep_messages.append(
                    Message(
                        role=zep_role,
                        content=content
                    )
                )
        
        print(f"   Formatted messages: {len(zep_messages)}")
        if truncated_count > 0:
            print(f"   ⚠️ Truncated {truncated_count} messages that exceeded 2500 chars")
        
        if not zep_messages:
            print("   ⚠️ No messages to save")
            return
        
        print(f"   Thread: {thread_id}")
        print(f"   Messages: {len(zep_messages)}")
        
        # First create the thread, then add messages
        try:
            # Create thread first
            try:
                zep.thread.add(
                    thread_id=thread_id,
                    user_id=user_id,
                    metadata={
                        "call_id": call_id,
                        "phone": phone_number,
                        "source": "mfc_voice_agent",
                        "created_at": datetime.utcnow().isoformat()
                    }
                )
                print(f"   ✓ Created thread: {thread_id}")
            except Exception as thread_error:
                # Thread might already exist, that's okay
                if "already exists" in str(thread_error).lower():
                    print(f"   ℹ Thread already exists: {thread_id}")
                else:
                    print(f"   ⚠️ Thread creation note: {thread_error}")
            
            # Now add messages to the thread
            zep.thread.add_messages(
                thread_id=thread_id,
                messages=zep_messages
            )
            
            print(f"   ✓ Conversation saved successfully to thread: {thread_id}")
            print(f"   Messages saved: {len(zep_messages)}")
            
        except Exception as e:
            print(f"   ❌ Error saving conversation: {str(e)}")
            import traceback
            traceback.print_exc()
            # Don't raise - just log the error
            
    except Exception as e:
        print(f"❌ Error in save_conversation: {str(e)}")
        import traceback
        traceback.print_exc()
        # Don't raise - just log the error


if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 3001))
    uvicorn.run(app, host="0.0.0.0", port=port)
