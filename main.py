from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse
from datetime import datetime
import os
import json
from zep_cloud.client import Zep
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
            
            # Get the first tool call
            if tool_call_list and len(tool_call_list) > 0:
                tool_call = tool_call_list[0]
                
                # IMPORTANT: Get the tool call ID for the response
                tool_call_id = tool_call.get("id")
                
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
                print(f"   Tool Call ID: {tool_call_id}")
                print(f"   Phone: {phone_number}")
                print(f"   Parameters: {parameters}")
                
                # Handle memory retrieval function
                if function_name == "get_caller_history":
                    print(f"   🧠 Retrieving memory for: {phone_number}")
                    context = await get_caller_context(phone_number)
                    context["caller_phone"] = phone_number
                    print(f"   ✓ Memory retrieved: is_returning_caller={context.get('is_returning_caller')}")
                    
                    # Return in Vapi's expected format with toolCallId
                    return JSONResponse(content={
                        "results": [
                            {
                                "toolCallId": tool_call_id,
                                "result": json.dumps(context)
                            }
                        ]
                    })
                
                # Handle create_lead function
                elif function_name == "create_lead":
                    print(f"   💾 Creating lead for: {phone_number}")
                    result = await create_lead(phone_number, parameters)
                    print(f"   ✓ Lead result: {result}")
                    
                    # Return in Vapi's expected format with toolCallId
                    return JSONResponse(content={
                        "results": [
                            {
                                "toolCallId": tool_call_id,
                                "result": json.dumps(result)
                            }
                        ]
                    })
                
                # Handle lookup_town function
                elif function_name == "lookup_town":
                    print(f"   🗺️ Looking up town for routing")
                    result = await lookup_town(parameters)
                    print(f"   ✓ Town lookup result: {result}")
                    
                    # Return in Vapi's expected format with toolCallId
                    return JSONResponse(content={
                        "results": [
                            {
                                "toolCallId": tool_call_id,
                                "result": json.dumps(result)
                            }
                        ]
                    })
                
                # Handle other functions here as needed
                print(f"   ⚠️ Function not implemented: {function_name}")
                return JSONResponse(content={
                    "results": [
                        {
                            "toolCallId": tool_call_id,
                            "result": json.dumps({"error": f"Function {function_name} not implemented"})
                        }
                    ]
                })
            else:
                print(f"   ⚠️ No tool calls found in list")
                return JSONResponse(content={"results": []})
        
        # Handle end-of-call-report for saving conversation
        elif message_type == "end-of-call-report":
            print("💾 End-of-call-report received")
            
            message_data = payload.get("message", {})
            call_data = message_data.get("call", {})
            customer_data = call_data.get("customer", {})
            phone_number = customer_data.get("number")
            call_id = call_data.get("id")
            transcript = message_data.get("transcript", "")
            messages = message_data.get("messages", [])
            
            if phone_number and (transcript or messages):
                print(f"\n📞 Processing call:")
                print(f"   Phone: {phone_number}")
                print(f"   Call ID: {call_id}")
                print(f"   Messages: {len(messages)}")
                
                try:
                    await save_conversation(phone_number, call_id, transcript, messages)
                    return JSONResponse(content={"status": "success", "message": "Conversation saved"})
                except Exception as e:
                    print(f"❌ Error in save_conversation: {str(e)}")
                    return JSONResponse(content={"status": "partial", "message": "Call logged, memory save failed"})
            else:
                return JSONResponse(content={"status": "ignored", "reason": "missing_data"})
        
        # Handle other webhook types
        else:
            print(f"⚠️ Unhandled webhook type: {message_type}")
            return JSONResponse(content={"status": "ignored", "type": message_type})
            
    except Exception as e:
        print(f"❌ Error processing webhook: {str(e)}")
        import traceback
        traceback.print_exc()
        return JSONResponse(content={"status": "error", "message": str(e)})


async def get_caller_context(phone_number: str) -> dict:
    """
    Retrieve conversation history and context for a returning caller.
    """
    try:
        # Check if this caller exists in Zep
        try:
            user = zep.user.get(user_id=phone_number)
            print(f"   ✓ Found existing user: {phone_number}")
            return {
                "is_returning_caller": True,
                "summary": "Returning caller with previous conversation history."
            }
        except Exception as e:
            print(f"   ℹ New caller (no history): {phone_number}")
            return {
                "is_returning_caller": False,
                "summary": "First time caller - no previous conversation history."
            }
            
    except Exception as e:
        print(f"   ❌ Error retrieving caller context: {e}")
        return {
            "is_returning_caller": False,
            "summary": "Unable to retrieve caller history."
        }


async def lookup_town(parameters: dict) -> dict:
    """
    Look up a town to find the assigned territory and LPS (Livestock Specialist).
    
    Queries:
    1. town_distances table - find territory for the town
    2. territories table - get territory_id (NOTE: appends " Territory" to match naming)
    3. specialists table - get LPS contact info
    """
    try:
        town_name = parameters.get("town_name", "")
        state = parameters.get("state", "")
        
        if not town_name:
            return {
                "success": False,
                "error": "No town name provided",
                "message": "I need to know what town you're near to connect you with the right specialist."
            }
        
        print(f"   🗺️ Looking up town: {town_name}")
        
        # Step 1: Look up town in town_distances table
        town_query = supabase.table("town_distances")\
            .select("town_name, state, county, assigned_territory, nearest_distance")\
            .ilike("town_name", town_name)
        
        if state:
            town_query = town_query.eq("state", state.upper())
        
        town_result = town_query.execute()
        
        if not town_result.data:
            print(f"   ⚠️ Town not found: {town_name}")
            return {
                "success": False,
                "error": "Town not found",
                "town_searched": town_name,
                "message": f"I don't have {town_name} in my database. What's a nearby larger town, or what county are you in?"
            }
        
        town_data = town_result.data[0]
        territory_name = town_data["assigned_territory"]
        print(f"   ✓ Found town: {town_data['town_name']} → Territory: {territory_name}")
        
        # Step 2: Look up territory to get territory_id
        # FIX: town_distances has "Missoula" but territories table has "Missoula Territory"
        # So we append " Territory" to match the naming convention
        territory_name_for_lookup = f"{territory_name} Territory"
        print(f"   🔍 Looking up territory: {territory_name_for_lookup}")
        
        territory_result = supabase.table("territories")\
            .select("id, territory_name, territory_code")\
            .eq("territory_name", territory_name_for_lookup)\
            .eq("is_active", True)\
            .execute()
        
        territory_id = None
        if territory_result.data:
            territory_id = territory_result.data[0]["id"]
            print(f"   ✓ Found territory_id: {territory_id}")
        else:
            print(f"   ⚠️ Territory not found in territories table: {territory_name_for_lookup}")
        
        # Step 3: Look up specialist for this territory
        specialist_info = None
        if territory_id:
            specialist_result = supabase.table("specialists")\
                .select("id, first_name, last_name, email, phone")\
                .eq("territory_id", territory_id)\
                .eq("is_active", True)\
                .execute()
            
            if specialist_result.data:
                spec = specialist_result.data[0]
                specialist_info = {
                    "id": spec["id"],
                    "first_name": spec["first_name"],
                    "last_name": spec["last_name"],
                    "full_name": f"{spec['first_name']} {spec['last_name']}",
                    "email": spec["email"],
                    "phone": spec.get("phone")
                }
                print(f"   ✓ Found specialist: {specialist_info['full_name']} ({specialist_info['email']})")
            else:
                print(f"   ⚠️ No active specialist found for territory_id: {territory_id}")
        
        specialist_message = f"Your local specialist is {specialist_info['full_name']}." if specialist_info else ""
        
        return {
            "success": True,
            "town": town_data["town_name"],
            "state": town_data["state"],
            "county": town_data["county"],
            "territory": territory_name,
            "territory_id": territory_id,
            "distance_miles": float(town_data["nearest_distance"]) if town_data["nearest_distance"] else None,
            "specialist": specialist_info,
            "message": f"{town_name} is in our {territory_name} territory. {specialist_message}"
        }
        
    except Exception as e:
        print(f"   ❌ Error in lookup_town: {str(e)}")
        import traceback
        traceback.print_exc()
        return {
            "success": False,
            "error": str(e),
            "message": "I had trouble looking up that location. What county are you in?"
        }


async def create_lead(phone_number: str, parameters: dict) -> dict:
    """
    Create a new lead in Supabase.
    Uses correct column names: first_name, last_name, lead_status, territory_id, city
    """
    try:
        first_name = parameters.get("first_name", "")
        last_name = parameters.get("last_name", "")
        email = parameters.get("email", "")
        county = parameters.get("county", "")
        town = parameters.get("town", "")
        territory = parameters.get("territory", "")
        territory_id = parameters.get("territory_id", None)
        specialist_id = parameters.get("specialist_id", None)
        specialist_email = parameters.get("specialist_email", "")
        primary_interest = parameters.get("primary_interest", "")
        herd_size = parameters.get("herd_size", "")
        livestock_type = parameters.get("livestock_type", "")
        
        # Use phone from parameters if provided, otherwise use caller ID
        lead_phone = parameters.get("phone", "") or phone_number
        
        # Build notes combining relevant info
        notes_parts = []
        if county:
            notes_parts.append(f"County: {county}")
        if herd_size:
            notes_parts.append(f"Herd size: {herd_size}")
        if livestock_type:
            notes_parts.append(f"Livestock: {livestock_type}")
        notes = " | ".join(notes_parts) if notes_parts else None
        
        # Build lead data with CORRECT column names
        lead_data = {
            "first_name": first_name or "Unknown",
            "last_name": last_name or "Caller",
            "phone": lead_phone,
            "lead_status": "new",
            "lead_source": "voice_agent"
        }
        
        # Add optional fields only if they have values
        if email:
            lead_data["email"] = email
        if town:
            lead_data["city"] = town  # Store town in city column
        if primary_interest:
            lead_data["primary_interest"] = primary_interest
        if notes:
            lead_data["notes"] = notes
        if territory_id:
            lead_data["territory_id"] = territory_id
        if specialist_id:
            lead_data["assigned_specialist_id"] = specialist_id
        
        # Parse herd_size as integer if provided
        if herd_size:
            try:
                lead_data["herd_size"] = int(str(herd_size).replace(",", ""))
            except:
                pass
        
        print(f"   📝 Lead data: {lead_data}")
        
        result = supabase.table("leads").insert(lead_data).execute()
        
        print(f"   ✓ Created lead: {first_name} {last_name}")
        
        if specialist_email:
            print(f"   📧 Lead assigned to specialist: {specialist_email}")
        
        return {
            "success": True,
            "lead_id": result.data[0]["id"] if result.data else None,
            "message": f"Lead created successfully for {first_name} {last_name}",
            "territory": territory,
            "specialist_email": specialist_email
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
    Save conversation to Zep using thread API.
    Zep SDK v2 uses: zep.thread for conversation management
    """
    try:
        print(f"\n💾 Saving conversation for: {phone_number}")
        
        user_id = phone_number
        thread_id = f"mfc_{phone_number}_{call_id}"
        
        # Ensure user exists in Zep
        try:
            user = zep.user.get(user_id=user_id)
            print(f"   ✓ User exists in Zep")
        except Exception:
            try:
                zep.user.add(
                    user_id=user_id,
                    first_name=phone_number,
                    metadata={
                        "phone": phone_number,
                        "source": "mfc_voice_agent"
                    }
                )
                print(f"   ✓ Created new user in Zep: {user_id}")
            except Exception as e:
                print(f"   ⚠️ Could not create user: {e}")
        
        # Format messages for Zep
        zep_messages = []
        for msg in messages:
            role = msg.get("role", "user")
            content = msg.get("message", "")
            
            if role in ["tool_calls", "tool_call_result", "system"]:
                continue
            
            if role in ["assistant", "bot"]:
                zep_role = "assistant"
            else:
                zep_role = "user"
            
            if content:
                if len(content) > 2500:
                    content = content[:2450] + "... [truncated]"
                zep_messages.append({
                    "role_type": zep_role,
                    "content": content
                })
        
        print(f"   Formatted messages: {len(zep_messages)}")
        
        if not zep_messages:
            print("   ⚠️ No messages to save")
            return
        
        # Use zep.thread API (SDK v2)
        try:
            # First, try to create the thread (no metadata parameter)
            try:
                zep.thread.create(
                    thread_id=thread_id,
                    user_id=user_id
                )
                print(f"   ✓ Created thread: {thread_id}")
            except Exception as thread_err:
                if "already exists" in str(thread_err).lower():
                    print(f"   ℹ Thread already exists: {thread_id}")
                else:
                    print(f"   ⚠️ Thread creation note: {thread_err}")
            
            # Now add messages to the thread - use 'role' not 'role_type'
            from zep_cloud import Message
            msgs = [Message(role=m["role_type"], content=m["content"]) for m in zep_messages]
            
            zep.thread.add_messages(
                thread_id=thread_id,
                messages=msgs
            )
            print(f"   ✓ Saved {len(msgs)} messages to thread: {thread_id}")
            
        except Exception as e:
            print(f"   ❌ Error with zep.thread: {str(e)}")
            # Log available thread methods for debugging
            if hasattr(zep, 'thread'):
                thread_methods = [a for a in dir(zep.thread) if not a.startswith('_')]
                print(f"   ℹ Available thread methods: {thread_methods}")
            
    except Exception as e:
        print(f"❌ Error in save_conversation: {str(e)}")
        import traceback
        traceback.print_exc()


if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 3001))
    uvicorn.run(app, host="0.0.0.0", port=port)






