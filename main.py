from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse
from datetime import datetime, timedelta
import os
import json
import re
from zep_cloud.client import Zep
from supabase import create_client, Client
import logging
import httpx
from retell_handlers import router as retell_router

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI()
app.include_router(retell_router)

# Get Zep API key from environment
ZEP_API_KEY = os.getenv("ZEP_API_KEY", "").strip()

if not ZEP_API_KEY:
    raise ValueError("ZEP_API_KEY environment variable is required")

print(f"🔑 Zep API Key loaded: {ZEP_API_KEY[:5]}...{ZEP_API_KEY[-5:]}")

# Initialize Zep client
zep = Zep(api_key=ZEP_API_KEY)

# Initialize Supabase client
SUPABASE_URL = os.getenv("SUPABASE_URL", "").strip()
SUPABASE_KEY = os.getenv("SUPABASE_KEY", "").strip()

if not SUPABASE_URL or not SUPABASE_KEY:
    raise ValueError("SUPABASE_URL and SUPABASE_KEY environment variables are required")

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

# Initialize Resend for email notifications
RESEND_API_KEY = os.getenv("RESEND_API_KEY", "").strip()
if RESEND_API_KEY:
    print(f"📧 Resend API Key loaded: {RESEND_API_KEY[:5]}...{RESEND_API_KEY[-5:]}")
else:
    print("⚠️ RESEND_API_KEY not set - email notifications disabled")

# Email configuration
FROM_EMAIL = os.getenv("FROM_EMAIL", "Montana Feed Company <leads@montanafeed.com>")

# Call summary toggle - set SEND_CALL_SUMMARIES=false to disable
SEND_CALL_SUMMARIES = os.getenv("SEND_CALL_SUMMARIES", "true").lower() == "true"
print(f"📞 Call summaries: {'enabled' if SEND_CALL_SUMMARIES else 'disabled'}")

# Words that should NEVER be extracted as names
EXCLUDED_NAME_WORDS = {
    'montana', 'missoula', 'darby', 'hamilton', 'dillon', 'billings', 'bozeman',
    'butte', 'helena', 'kalispell', 'lewistown', 'miles', 'city', 'columbus',
    'glasgow', 'havre', 'great', 'falls', 'wyoming', 'riverton', 'buffalo',
    'in', 'at', 'from', 'near', 'looking', 'calling', 'interested', 'a', 'the',
    'here', 'there', 'just', 'really', 'very', 'good', 'fine', 'great', 'well',
    'yeah', 'yes', 'no', 'not', 'ok', 'okay', 'sure', 'right', 'hi', 'hello',
    'thanks', 'thank', 'please', 'sorry', 'um', 'uh', 'so', 'and', 'but', 'or',
    'feed', 'company', 'purina', 'specialist', 'agent', 'customer', 'caller',
    'unknown', 'rancher', 'farmer', 'producer',
    'cattle', 'cow', 'cows', 'bull', 'bulls', 'calf', 'calves', 'herd', 'head',
    'chicken', 'chickens', 'sheep', 'horse', 'horses',
}


def is_valid_name(name: str) -> bool:
    """Check if a string is likely a real person's name."""
    if not name:
        return False
    
    name_lower = name.lower().strip()
    
    if len(name_lower) < 2 or len(name_lower) > 20:
        return False
    
    if name_lower in EXCLUDED_NAME_WORDS:
        return False
    
    if any(c.isdigit() for c in name_lower):
        return False
    
    vowels = set('aeiou')
    has_vowel = any(c in vowels for c in name_lower)
    has_consonant = any(c.isalpha() and c not in vowels for c in name_lower)
    if not (has_vowel and has_consonant):
        return False
    
    return True


def format_phone_display(phone: str) -> str:
    """Format phone number for display: +14065551234 -> (406) 555-1234"""
    if phone and len(phone) == 12 and phone.startswith("+1"):
        return f"({phone[2:5]}) {phone[5:8]}-{phone[8:]}"
    return phone or ""


def format_duration(seconds: float) -> str:
    """Format seconds into human-readable duration."""
    if not seconds:
        return "Unknown"
    minutes = int(seconds // 60)
    secs = int(seconds % 60)
    if minutes > 0:
        return f"{minutes}m {secs}s"
    return f"{secs}s"


async def send_email(to_email: str, subject: str, html_body: str, text_body: str) -> bool:
    """Send email using Resend API."""
    if not RESEND_API_KEY:
        print("   ⚠️ Email skipped - RESEND_API_KEY not configured")
        return False
    
    if not to_email:
        print("   ⚠️ Email skipped - no recipient email provided")
        return False
    
    try:
        async with httpx.AsyncClient() as client:
            response = await client.post(
                "https://api.resend.com/emails",
                headers={
                    "Authorization": f"Bearer {RESEND_API_KEY}",
                    "Content-Type": "application/json"
                },
                json={
                    "from": FROM_EMAIL,
                    "to": [to_email],
                    "subject": subject,
                    "html": html_body,
                    "text": text_body
                }
            )
            
            if response.status_code in [200, 201]:
                print(f"   ✅ Email sent to {to_email}")
                return True
            else:
                print(f"   ❌ Email failed: {response.status_code} - {response.text}")
                return False
                
    except Exception as e:
        print(f"   ❌ Email error: {str(e)}")
        return False


async def send_lead_notification_email(
    specialist_email: str,
    specialist_name: str,
    lead_name: str,
    lead_phone: str,
    lead_town: str,
    lead_county: str,
    lead_interest: str,
    lead_herd_size: str,
    lead_livestock_type: str
):
    """Send email notification to specialist about new lead."""
    phone_display = format_phone_display(lead_phone)
    
    subject = f"🐄 New Lead: {lead_name} from {lead_town or 'Unknown Location'}"
    
    details = []
    if lead_phone:
        details.append(f"<strong>Phone:</strong> {phone_display}")
    if lead_town:
        details.append(f"<strong>Town:</strong> {lead_town}")
    if lead_county:
        details.append(f"<strong>County:</strong> {lead_county}")
    if lead_interest:
        details.append(f"<strong>Interested In:</strong> {lead_interest}")
    if lead_herd_size:
        details.append(f"<strong>Herd Size:</strong> {lead_herd_size} head")
    if lead_livestock_type:
        details.append(f"<strong>Operation Type:</strong> {lead_livestock_type}")
    
    details_html = "<br>".join(details) if details else "No additional details provided."
    
    html_body = f"""
    <div style="font-family: Arial, sans-serif; max-width: 600px; margin: 0 auto;">
        <div style="background-color: #1a5f2a; color: white; padding: 20px; text-align: center;">
            <h1 style="margin: 0;">Montana Feed Company</h1>
            <p style="margin: 5px 0 0 0;">New Lead Notification</p>
        </div>
        
        <div style="padding: 20px; background-color: #f9f9f9;">
            <h2 style="color: #1a5f2a; margin-top: 0;">Hey {specialist_name.split()[0] if specialist_name else 'there'}!</h2>
            
            <p>You have a new lead from the voice agent:</p>
            
            <div style="background-color: white; border: 1px solid #ddd; border-radius: 8px; padding: 15px; margin: 15px 0;">
                <h3 style="margin-top: 0; color: #333;">{lead_name}</h3>
                {details_html}
            </div>
            
            <p><strong>Please follow up within 24 hours.</strong></p>
            
            <div style="margin-top: 20px;">
                <a href="tel:{lead_phone}" style="display: inline-block; background-color: #1a5f2a; color: white; padding: 12px 24px; text-decoration: none; border-radius: 5px; font-weight: bold;">
                    📞 Call {lead_name.split()[0] if lead_name else 'Now'}
                </a>
            </div>
        </div>
        
        <div style="padding: 15px; text-align: center; color: #666; font-size: 12px;">
            <p>This lead was captured by the Montana Feed Company Voice Agent<br>
            "Better feed. Better beef."</p>
        </div>
    </div>
    """
    
    text_body = f"""
New Lead for {specialist_name}

Name: {lead_name}
Phone: {phone_display}
Town: {lead_town or 'Not provided'}
County: {lead_county or 'Not provided'}
Interest: {lead_interest or 'Not specified'}
Herd Size: {lead_herd_size or 'Not provided'}
Operation: {lead_livestock_type or 'Not specified'}

Please follow up within 24 hours.

--
Montana Feed Company Voice Agent
"Better feed. Better beef."
    """
    
    return await send_email(specialist_email, subject, html_body, text_body)


async def send_callback_notification_email(
    specialist_email: str,
    specialist_name: str,
    caller_name: str,
    caller_phone: str,
    callback_date: str,
    callback_time: str,
    callback_timeframe: str,
    reason: str
):
    """Send email notification to specialist about scheduled callback."""
    phone_display = format_phone_display(caller_phone)
    
    timing_parts = []
    if callback_date:
        timing_parts.append(callback_date)
    if callback_time:
        timing_parts.append(f"at {callback_time}")
    elif callback_timeframe:
        timing_parts.append(f"in the {callback_timeframe}")
    
    timing_str = " ".join(timing_parts) if timing_parts else "at their convenience"
    
    subject = f"📅 Callback Requested: {caller_name} - {timing_str}"
    
    html_body = f"""
    <div style="font-family: Arial, sans-serif; max-width: 600px; margin: 0 auto;">
        <div style="background-color: #2563eb; color: white; padding: 20px; text-align: center;">
            <h1 style="margin: 0;">Montana Feed Company</h1>
            <p style="margin: 5px 0 0 0;">📅 Callback Request</p>
        </div>
        
        <div style="padding: 20px; background-color: #f9f9f9;">
            <h2 style="color: #2563eb; margin-top: 0;">Hey {specialist_name.split()[0] if specialist_name else 'there'}!</h2>
            
            <p>A caller has requested a callback:</p>
            
            <div style="background-color: white; border: 2px solid #2563eb; border-radius: 8px; padding: 15px; margin: 15px 0;">
                <h3 style="margin-top: 0; color: #333;">{caller_name}</h3>
                <p style="margin: 5px 0;"><strong>📞 Phone:</strong> {phone_display}</p>
                <p style="margin: 5px 0;"><strong>🕐 When:</strong> {timing_str}</p>
                {f'<p style="margin: 5px 0;"><strong>📝 Reason:</strong> {reason}</p>' if reason else ''}
            </div>
            
            <div style="background-color: #fef3c7; border: 1px solid #f59e0b; border-radius: 8px; padding: 15px; margin: 15px 0;">
                <p style="margin: 0; color: #92400e;"><strong>⚠️ Please add this to your calendar!</strong></p>
            </div>
            
            <div style="margin-top: 20px;">
                <a href="tel:{caller_phone}" style="display: inline-block; background-color: #2563eb; color: white; padding: 12px 24px; text-decoration: none; border-radius: 5px; font-weight: bold;">
                    📞 Call {caller_name.split()[0] if caller_name else 'Now'}
                </a>
            </div>
        </div>
        
        <div style="padding: 15px; text-align: center; color: #666; font-size: 12px;">
            <p>This callback was scheduled by the Montana Feed Company Voice Agent<br>
            "Better feed. Better beef."</p>
        </div>
    </div>
    """
    
    text_body = f"""
Callback Request for {specialist_name}

Caller: {caller_name}
Phone: {phone_display}
When: {timing_str}
{f'Reason: {reason}' if reason else ''}

Please add this to your calendar!

--
Montana Feed Company Voice Agent
"Better feed. Better beef."
    """
    
    return await send_email(specialist_email, subject, html_body, text_body)


async def get_specialist_for_caller(phone_number: str) -> dict:
    """Look up which specialist should receive notifications for this caller."""
    result = {
        "specialist_email": None,
        "specialist_name": None,
        "caller_name": None,
        "territory": None
    }
    
    try:
        # Check leads table first
        lead_result = supabase.table("leads")\
            .select("first_name, last_name, city, assigned_specialist_id, territory_id")\
            .eq("phone", phone_number)\
            .order("created_at", desc=True)\
            .limit(1)\
            .execute()
        
        if lead_result.data and len(lead_result.data) > 0:
            lead = lead_result.data[0]
            
            first_name = lead.get("first_name", "")
            last_name = lead.get("last_name", "")
            if first_name and first_name not in ["Unknown"]:
                result["caller_name"] = f"{first_name} {last_name}".strip()
            
            specialist_id = lead.get("assigned_specialist_id")
            if specialist_id:
                spec_result = supabase.table("specialists")\
                    .select("first_name, last_name, email")\
                    .eq("id", specialist_id)\
                    .execute()
                
                if spec_result.data and len(spec_result.data) > 0:
                    spec = spec_result.data[0]
                    result["specialist_email"] = spec.get("email")
                    result["specialist_name"] = f"{spec.get('first_name', '')} {spec.get('last_name', '')}".strip()
            
            territory_id = lead.get("territory_id")
            if territory_id:
                terr_result = supabase.table("territories")\
                    .select("territory_name")\
                    .eq("id", territory_id)\
                    .execute()
                
                if terr_result.data and len(terr_result.data) > 0:
                    result["territory"] = terr_result.data[0].get("territory_name")
            
            print(f"   ✓ Found specialist from leads: {result['specialist_name']} ({result['specialist_email']})")
            return result
        
        # Check callbacks table
        callback_result = supabase.table("callbacks")\
            .select("caller_name, specialist_id, specialist_email")\
            .eq("caller_phone", phone_number)\
            .order("created_at", desc=True)\
            .limit(1)\
            .execute()
        
        if callback_result.data and len(callback_result.data) > 0:
            callback = callback_result.data[0]
            result["caller_name"] = callback.get("caller_name")
            result["specialist_email"] = callback.get("specialist_email")
            
            specialist_id = callback.get("specialist_id")
            if specialist_id:
                spec_result = supabase.table("specialists")\
                    .select("first_name, last_name")\
                    .eq("id", specialist_id)\
                    .execute()
                
                if spec_result.data and len(spec_result.data) > 0:
                    spec = spec_result.data[0]
                    result["specialist_name"] = f"{spec.get('first_name', '')} {spec.get('last_name', '')}".strip()
            
            print(f"   ✓ Found specialist from callbacks: {result['specialist_name']} ({result['specialist_email']})")
            return result
        
        # Try Zep for caller name
        try:
            user = zep.user.get(user_id=phone_number)
            if hasattr(user, 'first_name') and user.first_name and is_valid_name(user.first_name):
                result["caller_name"] = user.first_name
        except:
            pass
        
        print(f"   ⚠️ No specialist found for caller: {phone_number}")
        return result
        
    except Exception as e:
        print(f"   ❌ Error looking up specialist: {str(e)}")
        return result


async def send_call_summary_email(
    specialist_email: str,
    specialist_name: str,
    caller_name: str,
    caller_phone: str,
    call_duration: float,
    messages: list,
    actions_taken: list
):
    """Send call summary email to specialist after call ends."""
    phone_display = format_phone_display(caller_phone)
    duration_display = format_duration(call_duration)
    caller_display = caller_name or "Unknown Caller"
    
    # Format conversation
    conversation_html = ""
    conversation_text = ""
    
    for msg in messages:
        role = msg.get("role", "user")
        content = msg.get("message", "")
        
        if role in ["tool_calls", "tool_call_result", "system"] or not content:
            continue
        
        if role in ["assistant", "bot"]:
            role_label = "Agent"
            bg_color = "#e8f5e9"
        else:
            role_label = caller_display.split()[0] if caller_display else "Caller"
            bg_color = "#e3f2fd"
        
        conversation_html += f"""
        <div style="background-color: {bg_color}; padding: 10px; margin: 5px 0; border-radius: 5px;">
            <strong>{role_label}:</strong> {content}
        </div>
        """
        conversation_text += f"{role_label}: {content}\n\n"
    
    # Format actions
    actions_html = ""
    actions_text = ""
    if actions_taken:
        actions_html = "<ul>" + "".join(f"<li>{action}</li>" for action in actions_taken) + "</ul>"
        actions_text = "\n".join(f"• {action}" for action in actions_taken)
    else:
        actions_html = "<p style='color: #666;'>No actions recorded</p>"
        actions_text = "No actions recorded"
    
    call_time = datetime.now().strftime("%I:%M %p on %B %d, %Y")
    
    subject = f"📞 Call Summary: {caller_display} - {duration_display}"
    
    html_body = f"""
    <div style="font-family: Arial, sans-serif; max-width: 700px; margin: 0 auto;">
        <div style="background-color: #374151; color: white; padding: 20px; text-align: center;">
            <h1 style="margin: 0;">Montana Feed Company</h1>
            <p style="margin: 5px 0 0 0;">📞 Call Summary</p>
        </div>
        
        <div style="padding: 20px; background-color: #f9f9f9;">
            <h2 style="color: #374151; margin-top: 0;">Hey {specialist_name.split()[0] if specialist_name else 'there'}!</h2>
            
            <p>Here's a summary of a recent call:</p>
            
            <div style="background-color: white; border: 1px solid #ddd; border-radius: 8px; padding: 15px; margin: 15px 0;">
                <table style="width: 100%; border-collapse: collapse;">
                    <tr>
                        <td style="padding: 5px 10px 5px 0; color: #666;">Caller:</td>
                        <td style="padding: 5px 0;"><strong>{caller_display}</strong></td>
                    </tr>
                    <tr>
                        <td style="padding: 5px 10px 5px 0; color: #666;">Phone:</td>
                        <td style="padding: 5px 0;"><strong>{phone_display}</strong></td>
                    </tr>
                    <tr>
                        <td style="padding: 5px 10px 5px 0; color: #666;">Duration:</td>
                        <td style="padding: 5px 0;">{duration_display}</td>
                    </tr>
                    <tr>
                        <td style="padding: 5px 10px 5px 0; color: #666;">Time:</td>
                        <td style="padding: 5px 0;">{call_time}</td>
                    </tr>
                </table>
            </div>
            
            <h3 style="color: #374151; margin-top: 25px;">📋 Actions Taken</h3>
            <div style="background-color: white; border: 1px solid #ddd; border-radius: 8px; padding: 15px; margin: 10px 0;">
                {actions_html}
            </div>
            
            <h3 style="color: #374151; margin-top: 25px;">💬 Conversation</h3>
            <div style="background-color: white; border: 1px solid #ddd; border-radius: 8px; padding: 15px; margin: 10px 0; max-height: 400px; overflow-y: auto;">
                {conversation_html if conversation_html else '<p style="color: #666;">No conversation recorded</p>'}
            </div>
            
            <div style="margin-top: 20px;">
                <a href="tel:{caller_phone}" style="display: inline-block; background-color: #374151; color: white; padding: 12px 24px; text-decoration: none; border-radius: 5px; font-weight: bold;">
                    📞 Call Back
                </a>
            </div>
        </div>
        
        <div style="padding: 15px; text-align: center; color: #666; font-size: 12px;">
            <p>Montana Feed Company Voice Agent<br>
            "Better feed. Better beef."</p>
        </div>
    </div>
    """
    
    text_body = f"""
Call Summary for {specialist_name}

Caller: {caller_display}
Phone: {phone_display}
Duration: {duration_display}
Time: {call_time}

ACTIONS TAKEN:
{actions_text}

CONVERSATION:
{conversation_text}

--
Montana Feed Company Voice Agent
"Better feed. Better beef."
    """
    
    return await send_email(specialist_email, subject, html_body, text_body)


def extract_actions_from_messages(messages: list) -> list:
    """Extract actions taken during the call from the messages."""
    actions = []
    
    for msg in messages:
        role = msg.get("role", "")
        
        if role == "tool_call_result":
            tool_name = msg.get("name", "")
            result_str = msg.get("result", "")
            
            try:
                result = json.loads(result_str) if isinstance(result_str, str) else result_str
                
                if tool_name == "create_lead" and result.get("success"):
                    actions.append("✅ Lead created for follow-up")
                
                elif tool_name == "schedule_callback" and result.get("success"):
                    timing = result.get("timing_summary", "")
                    actions.append(f"📅 Callback scheduled: {timing}")
                
                elif tool_name == "lookup_town" and result.get("success"):
                    town = result.get("town", "")
                    territory = result.get("territory", "")
                    if town and territory:
                        actions.append(f"🗺️ Location identified: {town} ({territory})")
                
            except:
                pass
    
    return actions


@app.get("/")
async def root():
    return {
        "status": "MFC Agent Memory Service Running",
        "timestamp": datetime.now().isoformat(),
        "zep_configured": bool(ZEP_API_KEY),
        "supabase_configured": bool(SUPABASE_URL),
        "email_configured": bool(RESEND_API_KEY),
        "call_summaries_enabled": SEND_CALL_SUMMARIES
    }


@app.get("/health")
async def health_check():
    return {
        "status": "healthy",
        "service": "mfc-agent-memory",
        "timestamp": datetime.now().isoformat()
    }


@app.post("/api/query-knowledge")
async def query_knowledge(request: Request):
    """
    Knowledge base search endpoint for Retell agent.
    Searches the knowledge_base table for cattle nutrition information,
    USDA program details, product recommendations, and feeding strategies.
    """
    try:
        data = await request.json()
        query = data.get('query', '')
        
        if not query:
            return JSONResponse(
                content={
                    'error': 'Query parameter is required',
                    'results': [],
                    'query': ''
                },
                status_code=400
            )
        
        logger.info(f"[Knowledge Query] Searching for: {query}")
        
        # Extract search terms
        search_terms = [
            word for word in re.sub(r'[^\w\s]', ' ', query.lower()).split()
            if len(word) > 2 and word not in [
                'what', 'how', 'when', 'where', 'why', 'the', 'for', 'and',
                'you', 'should', 'can', 'will', 'are', 'our', 'your', 'they',
                'have', 'has', 'been', 'this', 'that', 'with'
            ]
        ]
        
        logger.info(f"[Knowledge Query] Search terms: {', '.join(search_terms)}")
        
        results = []
        
        # Strategy 1: Keyword match (best accuracy)
        if search_terms:
            try:
                response = supabase.table('knowledge_base') \
                    .select('*') \
                    .contains('keywords', search_terms[:3]) \
                    .eq('is_active', True) \
                    .order('priority', desc=True) \
                    .limit(5) \
                    .execute()
                
                if response.data:
                    results = response.data
                    logger.info(f"[Knowledge Query] Found {len(results)} results via keyword match")
            except Exception as e:
                logger.warning(f"[Knowledge Query] Keyword search error: {e}")
        
        # Strategy 2: Full text search fallback
        if not results and search_terms:
            try:
                search_pattern = '%' + '%'.join(search_terms) + '%'
                response = supabase.table('knowledge_base') \
                    .select('*') \
                    .or_(f"question.ilike.{search_pattern},answer.ilike.{search_pattern}") \
                    .eq('is_active', True) \
                    .order('priority', desc=True) \
                    .limit(5) \
                    .execute()
                
                if response.data:
                    results = response.data
                    logger.info(f"[Knowledge Query] Found {len(results)} results via text search")
            except Exception as e:
                logger.warning(f"[Knowledge Query] Text search error: {e}")
        
        # Strategy 3: Category match (last resort)
        if not results:
            category_map = {
                'mineral': 'Minerals',
                'protein': 'Protein',
                'tub': 'Tubs',
                'breeding': 'Reproduction',
                'calving': 'Reproduction',
                'pregnant': 'Reproduction',
                'usda': 'USDA Programs',
                'nrcs': 'USDA Programs',
                'eqip': 'USDA Programs',
                'csp': 'USDA Programs',
                'conservation': 'USDA Programs',
                'regenerative': 'USDA Programs',
                'grazing': 'USDA Programs'
            }
            
            detected_category = None
            for term, category in category_map.items():
                if term in query.lower():
                    detected_category = category
                    break
            
            if detected_category:
                try:
                    response = supabase.table('knowledge_base') \
                        .select('*') \
                        .eq('category', detected_category) \
                        .eq('is_active', True) \
                        .order('priority', desc=True) \
                        .limit(3) \
                        .execute()
                    
                    if response.data:
                        results = response.data
                        logger.info(f"[Knowledge Query] Found {len(results)} results via category: {detected_category}")
                except Exception as e:
                    logger.warning(f"[Knowledge Query] Category search error: {e}")
        
        # Format response
        formatted_results = [
            {
                'question': r.get('question'),
                'answer': r.get('answer'),
                'category': r.get('category'),
                'subcategory': r.get('subcategory'),
                'related_products': r.get('related_products', []),
                'source': r.get('source')
            }
            for r in results
        ]
        
        response_data = {
            'results': formatted_results,
            'query': query,
            'found': len(results) > 0
        }
        
        logger.info(f"[Knowledge Query] Returning {len(results)} results")
        
        return JSONResponse(content=response_data)
        
    except Exception as e:
        logger.error(f"[Knowledge Query] Error: {str(e)}")
        import traceback
        traceback.print_exc()
        return JSONResponse(
            content={
                'error': 'Internal server error',
                'results': [],
                'query': data.get('query', '') if 'data' in locals() else ''
            },
            status_code=500
        )


@app.post("/")
async def handle_vapi_webhook(request: Request):
    """Handle all incoming webhooks from Vapi"""
    try:
        payload = await request.json()
        
        message_type = payload.get("message", {}).get("type", "unknown")
        print(f"📨 Received webhook: {message_type}")
        
        if message_type == "assistant.started":
            phone_number = payload.get("message", {}).get("call", {}).get("customer", {}).get("number")
            if phone_number:
                print(f"📞 Call started for: {phone_number}")
            return JSONResponse(content={"status": "acknowledged"})
        
        elif message_type in ["tool-calls", "function-call"]:
            print("🔍 Tool call received")
            
            message_data = payload.get("message", {})
            
            tool_call_list = message_data.get("toolCallList", [])
            if not tool_call_list:
                tool_call_list = message_data.get("toolCalls", [])
            
            print(f"   📦 Tool call list: {json.dumps(tool_call_list, indent=2)}")
            
            if tool_call_list and len(tool_call_list) > 0:
                tool_call = tool_call_list[0]
                
                tool_call_id = tool_call.get("id")
                
                function_name = tool_call.get("function", {}).get("name")
                if not function_name:
                    function_name = tool_call.get("name")
                
                parameters = tool_call.get("function", {}).get("arguments", {})
                if not parameters:
                    parameters = tool_call.get("parameters", {})
                
                if isinstance(parameters, str):
                    try:
                        parameters = json.loads(parameters)
                    except json.JSONDecodeError:
                        parameters = {}
                
                phone_number = message_data.get("call", {}).get("customer", {}).get("number")
                
                print(f"   Function: {function_name}")
                print(f"   Tool Call ID: {tool_call_id}")
                print(f"   Phone: {phone_number}")
                print(f"   Parameters: {parameters}")
                
                if function_name == "get_caller_history":
                    print(f"   🧠 Retrieving memory for: {phone_number}")
                    context = await get_caller_context(phone_number)
                    context["caller_phone"] = phone_number
                    print(f"   ✓ Memory retrieved: is_returning_caller={context.get('is_returning_caller')}, is_known_contact={context.get('is_known_contact')}, name={context.get('caller_name')}")
                    
                    return JSONResponse(content={
                        "results": [
                            {
                                "toolCallId": tool_call_id,
                                "result": json.dumps(context)
                            }
                        ]
                    })
                
                elif function_name == "create_lead":
                    print(f"   💾 Creating lead for: {phone_number}")
                    result = await create_lead(phone_number, parameters)
                    print(f"   ✓ Lead result: {result}")
                    
                    return JSONResponse(content={
                        "results": [
                            {
                                "toolCallId": tool_call_id,
                                "result": json.dumps(result)
                            }
                        ]
                    })
                
                elif function_name == "lookup_town":
                    print(f"   🗺️ Looking up town for routing")
                    result = await lookup_town(parameters)
                    print(f"   ✓ Town lookup result: {result}")
                    
                    return JSONResponse(content={
                        "results": [
                            {
                                "toolCallId": tool_call_id,
                                "result": json.dumps(result)
                            }
                        ]
                    })
                
                elif function_name == "schedule_callback":
                    print(f"   📅 Scheduling callback for: {phone_number}")
                    result = await schedule_callback(phone_number, parameters)
                    print(f"   ✓ Callback result: {result}")
                    
                    return JSONResponse(content={
                        "results": [
                            {
                                "toolCallId": tool_call_id,
                                "result": json.dumps(result)
                            }
                        ]
                    })
                
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
        
        elif message_type == "end-of-call-report":
            print("💾 End-of-call-report received")
            
            message_data = payload.get("message", {})
            call_data = message_data.get("call", {})
            customer_data = call_data.get("customer", {})
            phone_number = customer_data.get("number")
            call_id = call_data.get("id")
            transcript = message_data.get("transcript", "")
            messages = message_data.get("messages", [])
            
            # Get call duration
            call_duration = None
            if call_data.get("startedAt") and call_data.get("endedAt"):
                try:
                    start = datetime.fromisoformat(call_data["startedAt"].replace("Z", "+00:00"))
                    end = datetime.fromisoformat(call_data["endedAt"].replace("Z", "+00:00"))
                    call_duration = (end - start).total_seconds()
                except:
                    pass
            
            if phone_number and (transcript or messages):
                print(f"\n📞 Processing call:")
                print(f"   Phone: {phone_number}")
                print(f"   Call ID: {call_id}")
                print(f"   Messages: {len(messages)}")
                print(f"   Duration: {format_duration(call_duration) if call_duration else 'Unknown'}")
                
                try:
                    # Save to Zep
                    await save_conversation(phone_number, call_id, transcript, messages)
                    
                    # Send call summary if enabled
                    if SEND_CALL_SUMMARIES and messages:
                        print("   📧 Preparing call summary email...")
                        
                        specialist_info = await get_specialist_for_caller(phone_number)
                        
                        if specialist_info.get("specialist_email"):
                            actions = extract_actions_from_messages(messages)
                            
                            await send_call_summary_email(
                                specialist_email=specialist_info["specialist_email"],
                                specialist_name=specialist_info.get("specialist_name", "Specialist"),
                                caller_name=specialist_info.get("caller_name"),
                                caller_phone=phone_number,
                                call_duration=call_duration,
                                messages=messages,
                                actions_taken=actions
                            )
                        else:
                            print("   ⚠️ No specialist email found - skipping call summary")
                    
                    return JSONResponse(content={"status": "success", "message": "Conversation saved"})
                except Exception as e:
                    print(f"❌ Error in end-of-call processing: {str(e)}")
                    import traceback
                    traceback.print_exc()
                    return JSONResponse(content={"status": "partial", "message": "Call logged, some features failed"})
            else:
                return JSONResponse(content={"status": "ignored", "reason": "missing_data"})
        
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
    Enhanced caller recognition that checks:
    1. Supabase caller_contacts table (Epicor customers + Wyoming prospects)
    2. Zep conversation memory (for returning callers with history)
    3. Leads table (for recent lead info)
    
    Returns combined context for the voice agent.
    """
    try:
        # Initialize context
        context = {
            "is_returning_caller": False,
            "is_known_contact": False,
            "is_existing_customer": False,
            "is_prospect": False,
            "caller_name": None,
            "customer_name": None,
            "city": None,
            "state": None,
            "county": None,
            "primary_warehouse": None,
            "territory": None,
            "total_sales": 0,
            "last_purchase": None,
            "last_town": None,
            "last_topic": None,
            "summary": "First time caller - no previous information.",
            "greeting_hint": "New caller. Collect their name and information."
        }
        
        # Normalize phone number to +1XXXXXXXXXX format
        normalized_phone = phone_number
        if phone_number:
            digits = re.sub(r'\D', '', phone_number)
            if len(digits) == 10:
                normalized_phone = '+1' + digits
            elif len(digits) == 11 and digits[0] == '1':
                normalized_phone = '+' + digits
        
        print(f"   🔍 Looking up caller: {normalized_phone}")
        
        # ============================================
        # STEP 1: Check Supabase caller_contacts table
        # ============================================
        try:
            contact_result = supabase.table("caller_contacts")\
                .select("*")\
                .eq("phone_normalized", normalized_phone)\
                .execute()
            
            if contact_result.data and len(contact_result.data) > 0:
                contact = contact_result.data[0]
                print(f"   ✓ Found in caller_contacts: {contact.get('customer_name')}")
                
                context["is_known_contact"] = True
                context["caller_name"] = contact.get("first_name") or contact.get("customer_name")
                context["customer_name"] = contact.get("customer_name")
                context["city"] = contact.get("city")
                context["state"] = contact.get("state")
                context["primary_warehouse"] = contact.get("primary_warehouse")
                context["territory"] = contact.get("territory")
                context["is_existing_customer"] = contact.get("is_existing_customer", False)
                context["is_prospect"] = contact.get("is_prospect", False)
                context["total_sales"] = contact.get("total_sales", 0) or 0
                context["last_purchase"] = contact.get("last_purchase")
                context["last_town"] = contact.get("city")
                
                # Build summary and greeting hint
                if context["is_existing_customer"]:
                    sales_str = f"${context['total_sales']:,.0f} lifetime" if context['total_sales'] else "existing"
                    context["summary"] = f"Known customer: {contact.get('customer_name')} from {contact.get('city')}, {contact.get('state')}. {sales_str} customer. Primary warehouse: {contact.get('primary_warehouse')}."
                    context["greeting_hint"] = f"This is {context['caller_name']}, an existing customer from {contact.get('city')}. Greet them warmly by name."
                elif context["is_prospect"]:
                    context["summary"] = f"Wyoming prospect: {contact.get('customer_name')} from {contact.get('city')}, {contact.get('state')}. Seedstock operation - not yet a customer."
                    context["greeting_hint"] = f"This is {context['caller_name']}, a prospect from {contact.get('city')}. They're a seedstock operation we've identified but haven't done business with yet. Greet them by name."
            else:
                print(f"   ℹ Not found in caller_contacts")
                
        except Exception as contact_err:
            print(f"   ⚠️ Error checking caller_contacts: {contact_err}")
        
        # ============================================
        # STEP 2: Check Zep for conversation history
        # ============================================
        try:
            user = zep.user.get(user_id=phone_number)
            print(f"   ✓ Found in Zep: returning caller")
            context["is_returning_caller"] = True
            
            # If we didn't get a name from caller_contacts, try Zep
            if not context["caller_name"]:
                if hasattr(user, 'first_name') and user.first_name:
                    potential_name = user.first_name
                    if potential_name != phone_number and is_valid_name(potential_name):
                        context["caller_name"] = potential_name
                        print(f"   ✓ Found name from Zep: {context['caller_name']}")
            
            # Get metadata from Zep
            if hasattr(user, 'metadata') and user.metadata:
                meta = user.metadata
                if not context["last_town"] and meta.get('town'):
                    context["last_town"] = meta['town']
                if meta.get('last_interest'):
                    context["last_topic"] = meta['last_interest']
                    
        except Exception as zep_err:
            print(f"   ℹ Not found in Zep (new caller to voice system)")
        
        # ============================================
        # STEP 3: Check leads table if still no name
        # ============================================
        if not context["caller_name"]:
            try:
                lead_result = supabase.table("leads")\
                    .select("first_name, last_name, city, primary_interest")\
                    .eq("phone", phone_number)\
                    .order("created_at", desc=True)\
                    .limit(1)\
                    .execute()
                
                if lead_result.data and len(lead_result.data) > 0:
                    lead = lead_result.data[0]
                    potential_name = lead.get("first_name")
                    if potential_name and is_valid_name(potential_name):
                        context["caller_name"] = potential_name
                        context["is_returning_caller"] = True
                        print(f"   ✓ Found name from leads: {context['caller_name']}")
                    if not context["last_town"] and lead.get("city"):
                        context["last_town"] = lead["city"]
                    if lead.get("primary_interest"):
                        context["last_topic"] = lead["primary_interest"]
                        
            except Exception as lead_err:
                print(f"   ⚠️ Could not check leads: {lead_err}")
        
        # ============================================
        # STEP 4: Build final summary
        # ============================================
        if context["is_known_contact"] and context["is_returning_caller"]:
            # Known contact who has called before
            context["summary"] += f" Has called before."
            context["greeting_hint"] = f"This is {context['caller_name']}, a returning caller and {'existing customer' if context['is_existing_customer'] else 'prospect'}. Greet them warmly and reference that you remember them."
        
        elif context["is_known_contact"] and not context["is_returning_caller"]:
            # Known contact, first time calling
            context["greeting_hint"] = f"This is {context['caller_name']} calling for the first time, but they're {'already a customer' if context['is_existing_customer'] else 'a prospect we know about'}. Greet them by name."
        
        elif not context["is_known_contact"] and context["is_returning_caller"]:
            # Has called before but not in our contact list
            summary_parts = ["Returning caller"]
            if context["caller_name"]:
                summary_parts = [f"Returning caller named {context['caller_name']}"]
            if context["last_town"]:
                summary_parts.append(f"from {context['last_town']}")
            if context["last_topic"]:
                summary_parts.append(f"previously interested in {context['last_topic']}")
            context["summary"] = " ".join(summary_parts) + "."
            context["greeting_hint"] = f"Returning caller ({context['caller_name'] or 'name known'}). They've called before. Greet them warmly."
        
        # If still no summary set, use default
        if not context["is_known_contact"] and not context["is_returning_caller"]:
            context["summary"] = "First time caller - no previous information."
            context["greeting_hint"] = "New caller. Collect their name and information."
        
        print(f"   ✓ Context ready: known_contact={context['is_known_contact']}, returning={context['is_returning_caller']}, name={context['caller_name']}")
        return context
            
    except Exception as e:
        print(f"   ❌ Error retrieving caller context: {e}")
        import traceback
        traceback.print_exc()
        return {
            "is_returning_caller": False,
            "is_known_contact": False,
            "caller_name": None,
            "summary": "Unable to retrieve caller history.",
            "greeting_hint": "New caller. Collect their name and information."
        }


async def lookup_town(parameters: dict) -> dict:
    """Look up a town to find the assigned territory and LPS."""
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
        
        territory_name_for_lookup = territory_name
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
            "message": f"{town_name} is in our {territory_name}. {specialist_message}"
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
    """Create a new lead in Supabase and send email notification."""
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
        specialist_name = parameters.get("specialist_name", "")
        primary_interest = parameters.get("primary_interest", "")
        herd_size = parameters.get("herd_size", "")
        livestock_type = parameters.get("livestock_type", "")
        
        lead_phone = parameters.get("phone", "") or phone_number
        
        notes_parts = []
        if county:
            notes_parts.append(f"County: {county}")
        if herd_size:
            notes_parts.append(f"Herd size: {herd_size}")
        if livestock_type:
            notes_parts.append(f"Livestock: {livestock_type}")
        notes = " | ".join(notes_parts) if notes_parts else None
        
        lead_data = {
            "first_name": first_name or "Unknown",
            "last_name": last_name or "Caller",
            "phone": lead_phone,
            "lead_status": "new",
            "lead_source": "voice_agent"
        }
        
        if email:
            lead_data["email"] = email
        if town:
            lead_data["city"] = town
        if primary_interest:
            lead_data["primary_interest"] = primary_interest
        if notes:
            lead_data["notes"] = notes
        if territory_id:
            lead_data["territory_id"] = territory_id
        if specialist_id:
            lead_data["assigned_specialist_id"] = specialist_id
        
        if herd_size:
            try:
                lead_data["herd_size"] = int(str(herd_size).replace(",", ""))
            except:
                pass
        
        print(f"   📝 Lead data: {lead_data}")
        
        result = supabase.table("leads").insert(lead_data).execute()
        
        print(f"   ✓ Created lead: {first_name} {last_name}")
        
        # Send email notification
        if specialist_email:
            print(f"   📧 Sending email notification to: {specialist_email}")
            await send_lead_notification_email(
                specialist_email=specialist_email,
                specialist_name=specialist_name or "Specialist",
                lead_name=f"{first_name} {last_name}".strip() or "Unknown Caller",
                lead_phone=lead_phone,
                lead_town=town,
                lead_county=county,
                lead_interest=primary_interest,
                lead_herd_size=herd_size,
                lead_livestock_type=livestock_type
            )
        
        # Update Zep user
        if first_name and is_valid_name(first_name):
            try:
                zep.user.update(
                    user_id=phone_number,
                    first_name=first_name,
                    last_name=last_name if last_name and last_name not in ["Caller", "Unknown"] else None,
                    metadata={
                        "name": first_name,
                        "town": town,
                        "last_interest": primary_interest
                    }
                )
                print(f"   ✓ Updated Zep user with name: {first_name}")
            except Exception as zep_err:
                print(f"   ⚠️ Could not update Zep user: {zep_err}")
        
        return {
            "success": True,
            "lead_id": result.data[0]["id"] if result.data else None,
            "message": f"Lead created successfully for {first_name} {last_name}",
            "territory": territory,
            "specialist_email": specialist_email,
            "email_sent": bool(specialist_email and RESEND_API_KEY)
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


async def schedule_callback(phone_number: str, parameters: dict) -> dict:
    """Schedule a callback request and notify the specialist."""
    try:
        caller_name = parameters.get("caller_name", "Unknown Caller")
        callback_date = parameters.get("callback_date", "")
        callback_time = parameters.get("callback_time", "")
        callback_timeframe = parameters.get("callback_timeframe", "")
        reason = parameters.get("reason", "")
        specialist_id = parameters.get("specialist_id")
        specialist_email = parameters.get("specialist_email", "")
        specialist_name = parameters.get("specialist_name", "")
        territory_id = parameters.get("territory_id")
        
        print(f"   📅 Scheduling callback:")
        print(f"      Caller: {caller_name}")
        print(f"      Phone: {phone_number}")
        print(f"      Date: {callback_date}")
        print(f"      Time: {callback_time}")
        print(f"      Timeframe: {callback_timeframe}")
        print(f"      Reason: {reason}")
        
        # Parse date
        parsed_date = None
        today = datetime.now().date()
        
        if callback_date:
            date_lower = callback_date.lower().strip()
            if date_lower == "today":
                parsed_date = today
            elif date_lower == "tomorrow":
                parsed_date = today + timedelta(days=1)
            elif date_lower in ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]:
                days = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
                target_day = days.index(date_lower)
                current_day = today.weekday()
                days_ahead = target_day - current_day
                if days_ahead <= 0:
                    days_ahead += 7
                parsed_date = today + timedelta(days=days_ahead)
            else:
                try:
                    parsed_date = datetime.strptime(callback_date, "%Y-%m-%d").date()
                except:
                    try:
                        parsed_date = datetime.strptime(callback_date, "%m/%d/%Y").date()
                    except:
                        parsed_date = None
        
        callback_data = {
            "caller_phone": phone_number,
            "caller_name": caller_name,
            "requested_date": parsed_date.isoformat() if parsed_date else None,
            "requested_time": callback_time,
            "requested_timeframe": callback_timeframe,
            "reason": reason,
            "status": "pending"
        }
        
        if specialist_id:
            callback_data["specialist_id"] = specialist_id
        if specialist_email:
            callback_data["specialist_email"] = specialist_email
        if territory_id:
            callback_data["territory_id"] = territory_id
        
        print(f"   📝 Callback data: {callback_data}")
        
        result = supabase.table("callbacks").insert(callback_data).execute()
        
        callback_id = result.data[0]["id"] if result.data else None
        print(f"   ✓ Created callback: {callback_id}")
        
        # Build timing string with explicit date
        timing_parts = []
        if parsed_date:
            day_name = parsed_date.strftime("%A")
            month_day = parsed_date.strftime("%B %d")
            
            if parsed_date == today:
                timing_parts.append(f"today, {day_name}, {month_day}")
            elif parsed_date == today + timedelta(days=1):
                timing_parts.append(f"tomorrow, {day_name}, {month_day}")
            else:
                timing_parts.append(f"{day_name}, {month_day}")
        
        if callback_time:
            timing_parts.append(f"at {callback_time}")
        elif callback_timeframe:
            timing_parts.append(f"in the {callback_timeframe}")
        
        timing_str = " ".join(timing_parts) if timing_parts else "as soon as possible"
        
        print(f"   📅 Today is: {today.strftime('%A, %B %d, %Y')}")
        print(f"   📅 Callback scheduled for: {timing_str}")
        
        # Send email
        email_sent = False
        if specialist_email:
            print(f"   📧 Sending callback notification to: {specialist_email}")
            email_sent = await send_callback_notification_email(
                specialist_email=specialist_email,
                specialist_name=specialist_name or "Specialist",
                caller_name=caller_name,
                caller_phone=phone_number,
                callback_date=parsed_date.strftime("%A, %B %d, %Y") if parsed_date else callback_date,
                callback_time=callback_time,
                callback_timeframe=callback_timeframe,
                reason=reason
            )
        
        return {
            "success": True,
            "callback_id": callback_id,
            "scheduled_date": parsed_date.isoformat() if parsed_date else None,
            "scheduled_time": callback_time,
            "scheduled_timeframe": callback_timeframe,
            "timing_summary": timing_str,
            "email_sent": email_sent,
            "message": f"I've scheduled a callback for {timing_str}. {specialist_name or 'Your specialist'} will give you a call."
        }
        
    except Exception as e:
        print(f"   ❌ Error scheduling callback: {str(e)}")
        import traceback
        traceback.print_exc()
        return {
            "success": False,
            "error": str(e),
            "message": "I had trouble scheduling that callback. Let me take your information and have someone call you back."
        }


async def save_conversation(phone_number: str, call_id: str, transcript: str, messages: list):
    """Save conversation to Zep."""
    try:
        print(f"\n💾 Saving conversation for: {phone_number}")
        
        user_id = phone_number
        thread_id = f"mfc_{phone_number}_{call_id}"
        
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
        
        try:
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
            
            from zep_cloud import Message
            msgs = [Message(role=m["role_type"], content=m["content"]) for m in zep_messages]
            
            zep.thread.add_messages(
                thread_id=thread_id,
                messages=msgs
            )
            print(f"   ✓ Saved {len(msgs)} messages to thread: {thread_id}")
            
        except Exception as e:
            print(f"   ❌ Error with zep.thread: {str(e)}")
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














