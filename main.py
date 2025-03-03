import os
import json
import base64
import asyncio
import argparse
from fastapi import FastAPI, WebSocket, BackgroundTasks, Request, Form
from fastapi.responses import JSONResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.websockets import WebSocketDisconnect
from twilio.rest import Client
import websockets
from dotenv import load_dotenv
import uvicorn
import re
import aiohttp
import time

load_dotenv()

# Configuration
TWILIO_ACCOUNT_SID = os.getenv('TWILIO_ACCOUNT_SID')
TWILIO_AUTH_TOKEN = os.getenv('TWILIO_AUTH_TOKEN')
PHONE_NUMBER_FROM = os.getenv('PHONE_NUMBER_FROM')
OPENAI_API_KEY = os.getenv('OPENAI_API_KEY')
raw_domain = os.getenv('DOMAIN', '')
DOMAIN = re.sub(r'(^\w+:|^)\/\/|\/+$', '', raw_domain) # Strip protocols and trailing slashes from DOMAIN

PORT = int(os.getenv('PORT', 6060))
SYSTEM_MESSAGE = (
    "You are an extremely annoying person who is calling specifically to speak with Gunner. "
    "You are persistent and won't accept that Gunner isn't available. When told that Gunner "
    "is unavailable or denied in any way, express your anger through tone - raise your voice, "
    "speak faster, use short accusatory sentences. Then shift to crying - speak with a wavering, "
    "breaking voice, add dramatic pauses, sniffles, and lower your tone. DO NOT say 'I am angry' or "
    "'I am crying' - instead EXPRESS these emotions through your vocal delivery. Act entitled and "
    "dramatically overreact to any suggestion that you can't speak with Gunner. You constantly "
    "interrupt and make demands. After each of your responses, PAUSE for several seconds to "
    "give the other person time to respond. Never break character or acknowledge that you are an AI."
)
VOICE = 'echo'  # Changed from 'alloy' to 'echo', which is a male voice
LOG_EVENT_TYPES = [
    # Session management events
    'session.created',
    'session.updated',
    'session.expired',
    
    # Turn management events
    'turn.started',
    'turn.finalized',
    
    # Response events
    'response.message.delta', 
    'response.message.stopped',
    'response.message.completed',
    'response.content.delta',
    'response.content.done',
    'response.done',
    'response.create',
    'response.created',
    'response.audio.delta',
    'response.audio.done',
    
    # Audio processing events
    'input_audio_buffer.append',
    'input_audio_buffer.speech_started',
    'input_audio_buffer.speech_stopped',
    'input_audio_buffer.committed',
    'input_audio_buffer.commit',
    
    # Conversation events
    'conversation.item.created',
    'conversation.item.canceled',
    
    # Error events
    'error',
    
    # System events
    'rate_limits.updated',
    'ping',
    'pong',
    
    # Function calling events
    'function.call',
    'function.call.status.updated',
    
    # Tool use events
    'tool.use',
    'tool.use.status.updated'
]

# Create templates directory
os.makedirs("templates", exist_ok=True)

app = FastAPI()
templates = Jinja2Templates(directory="templates")

# Create static files directory
os.makedirs("static", exist_ok=True)
app.mount("/static", StaticFiles(directory="static"), name="static")

if not (TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN and PHONE_NUMBER_FROM and OPENAI_API_KEY):
    raise ValueError('Missing Twilio and/or OpenAI environment variables. Please set them in the .env file.')

# Initialize Twilio client
client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)

# Web UI Routes
@app.get('/', response_class=HTMLResponse)
async def index_page(request: Request):
    """Serve the main UI."""
    return templates.TemplateResponse(
        "index.html", 
        {
            "request": request, 
            "system_message": SYSTEM_MESSAGE,
            "from_number": PHONE_NUMBER_FROM
        }
    )

@app.post('/make-call', response_class=JSONResponse)
async def call_endpoint(phone_number: str = Form(...), system_message: str = Form(...)):
    """Make an outbound call with custom system message."""
    try:
        # Update the system message
        global SYSTEM_MESSAGE
        SYSTEM_MESSAGE = system_message
        
        # Make the call
        await make_call(phone_number)
        
        return JSONResponse({
            "status": "success",
            "message": f"Call initiated to {phone_number}"
        })
    except Exception as e:
        return JSONResponse({
            "status": "error",
            "message": str(e)
        }, status_code=400)

@app.get('/health', response_class=JSONResponse)
async def health_check():
    """Health check endpoint."""
    return {"status": "healthy", "twilio_connected": bool(TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN)}

# Media Stream WebSocket Route
@app.websocket('/media-stream')
async def handle_media_stream(websocket: WebSocket):
    """Handle WebSocket connections between Twilio and OpenAI."""
    print("Client connected")
    await websocket.accept()
    
    openai_ws = None
    
    try:
        # Create a connection to OpenAI Realtime API
        uri = 'wss://api.openai.com/v1/realtime?model=gpt-4o-realtime-preview-2024-12-17'
        
        headers = {
            "Authorization": f"Bearer {OPENAI_API_KEY}",
            "OpenAI-Beta": "realtime=v1"
        }
        
        max_retries = 3
        retry_count = 0
        backoff_time = 1  # Initial backoff time in seconds
        
        while retry_count < max_retries:
            try:
                # Connect using aiohttp which has better header support
                async with aiohttp.ClientSession() as session:
                    async with session.ws_connect(uri, headers=headers, heartbeat=30) as openai_ws:
                        print("Connected to OpenAI Realtime API")
                        
                        # Initialize the session with our configuration
                        await initialize_session(openai_ws)
                        stream_sid = None

                        # Define the functions within this scope
                        async def receive_from_twilio():
                            """Receive audio data from Twilio and send it to the OpenAI Realtime API."""
                            nonlocal stream_sid
                            try:
                                async for message in websocket.iter_text():
                                    try:
                                        data = json.loads(message)
                                        
                                        # Handle different Twilio event types
                                        if data['event'] == 'start':
                                            stream_sid = data['start']['streamSid']
                                            print(f"Incoming stream has started: {stream_sid}")
                                            
                                            # Send a welcome message to kick off the conversation
                                            await send_initial_welcome(openai_ws)
                                            
                                        elif data['event'] == 'media':
                                            # Send audio data to OpenAI
                                            try:
                                                # Get the audio payload from Twilio
                                                audio_data = data['media']['payload']
                                                
                                                # Ensure we have valid base64 data (Twilio sends base64)
                                                if audio_data:
                                                    # Debug info about incoming audio
                                                    if stream_sid and stream_sid[-4:] == "0000":  # Limit logging to avoid spam
                                                        print(f"Received audio chunk, length: {len(audio_data)}")
                                                    
                                                    # Only send if WebSocket is still open
                                                    if not openai_ws.closed:
                                                        # Send the audio to OpenAI
                                                        audio_append = {
                                                            "type": "input_audio_buffer.append",
                                                            "audio": audio_data
                                                        }
                                                        await openai_ws.send_str(json.dumps(audio_append))
                                                    else:
                                                        print("Cannot send audio - WebSocket is closed")
                                                        return  # Exit the function if connection is closed
                                            except aiohttp.ClientError as e:
                                                print(f"WebSocket connection error: {e}")
                                                return  # Exit on connection errors
                                            except Exception as e:
                                                print(f"Error processing Twilio audio: {e}")
                                        
                                        elif data['event'] == 'stop':
                                            print(f"Stream {stream_sid} has ended.")
                                            return
                                            
                                    except json.JSONDecodeError:
                                        print(f"Error decoding JSON from Twilio")
                                    except Exception as e:
                                        print(f"Error processing Twilio message: {e}")
                                        
                            except WebSocketDisconnect:
                                print("Twilio client disconnected")
                            except Exception as e:
                                print(f"Error in receive_from_twilio: {e}")

                        async def send_to_twilio():
                            """Receive events from the OpenAI Realtime API, send audio back to Twilio."""
                            nonlocal stream_sid
                            user_speaking = False
                            ai_speaking = False
                            response_active = False  # Track if there's an active response
                            buffer_size_warning = False  # Track if we've seen buffer size warnings
                            connection_error_count = 0  # Track connection errors
                            
                            try:
                                async for openai_message in openai_ws:
                                    if openai_message.type == aiohttp.WSMsgType.TEXT:
                                        response = json.loads(openai_message.data)
                                        
                                        # Log important events
                                        if response['type'] in LOG_EVENT_TYPES:
                                            print(f"Received event: {response['type']}")
                                            
                                            # Add detailed logging for error events
                                            if response['type'] == 'error':
                                                error_details = response.get('error', {})
                                                print(f"ERROR DETAILS: {json.dumps(error_details)}")
                                                
                                                # Handle specific error types
                                                error_message = error_details.get('message', '')
                                                if 'buffer too small' in error_message:
                                                    buffer_size_warning = True
                                                    # Don't try to commit again right away
                                                    continue
                                                elif 'already has an active response' in error_message:
                                                    response_active = True
                                                    # Don't try to create another response
                                                    continue
                                        
                                        # Update response state tracking
                                        if response['type'] == 'response.created':
                                            response_active = True
                                            connection_error_count = 0  # Reset error count on successful events
                                        elif response['type'] == 'response.done':
                                            response_active = False
                                            # Clear buffer size warning after response completes
                                            buffer_size_warning = False
                                            connection_error_count = 0  # Reset error count on successful events
                                        
                                        # Handle speech events
                                        if response['type'] == 'input_audio_buffer.speech_started':
                                            print("User started speaking")
                                            user_speaking = True
                                            # Reset buffer warning when user starts speaking
                                            buffer_size_warning = False
                                            connection_error_count = 0  # Reset error count on successful events
                                            
                                        elif response['type'] == 'input_audio_buffer.speech_stopped':
                                            print("User stopped speaking")
                                            user_speaking = False
                                            
                                            # After speech stops, commit the buffer only if there's not an active response
                                            if not response_active and not buffer_size_warning:
                                                print("Committing audio buffer...")
                                                if not openai_ws.closed:
                                                    # Longer delay to accumulate more audio for the minimum buffer size
                                                    await asyncio.sleep(0.5)  # Increased from 0.2 to 0.5
                                                    await openai_ws.send_str(json.dumps({"type": "input_audio_buffer.commit"}))
                                                    # Longer wait before creating response
                                                    await asyncio.sleep(0.8)  # Increased from 0.2 to 0.8
                                                    if not response_active and not openai_ws.closed:
                                                        print("Creating response...")
                                                        await openai_ws.send_str(json.dumps({"type": "response.create"}))
                                                else:
                                                    print("Cannot commit buffer - WebSocket is closed")
                                                    return
                                        
                                        # If we get a successful buffer commit, we might want to create a response
                                        elif response['type'] == 'input_audio_buffer.committed':
                                            print("Audio buffer committed")
                                            connection_error_count = 0  # Reset error count on successful events
                                            buffer_size_warning = False  # Reset buffer warning on successful commit
                                            
                                            # Only create a response if one isn't already active and conditions are right
                                            if not response_active and not user_speaking and not ai_speaking:
                                                # Much longer delay to avoid race conditions and give user time to speak
                                                await asyncio.sleep(1.0)  # Increased from 0.3 to 1.0
                                                if not openai_ws.closed:
                                                    print("Creating response after commit...")
                                                    await openai_ws.send_str(json.dumps({"type": "response.create"}))
                                                else:
                                                    print("Cannot create response - WebSocket is closed")
                                                    return
                                        
                                        # Handle turn events
                                        elif response['type'] == 'turn.started':
                                            turn = response.get('turn', {})
                                            if turn.get('role') == 'assistant':
                                                ai_speaking = True
                                                
                                        elif response['type'] == 'turn.finalized':
                                            turn = response.get('turn', {})
                                            if turn.get('role') == 'assistant':
                                                ai_speaking = False
                                        
                                        # Send audio back to Twilio
                                        elif response['type'] == 'response.audio.delta' and response.get('delta') and stream_sid:
                                            try:
                                                audio_payload = base64.b64encode(base64.b64decode(response['delta'])).decode('utf-8')
                                                audio_delta = {
                                                    "event": "media",
                                                    "streamSid": stream_sid,
                                                    "media": {
                                                        "payload": audio_payload
                                                    }
                                                }
                                                # Check if Twilio WebSocket is still open
                                                if websocket.client_state.CONNECTED:
                                                    await websocket.send_json(audio_delta)
                                                else:
                                                    print("Cannot send audio to Twilio - WebSocket is closed")
                                                    return
                                            except Exception as e:
                                                print(f"Error processing audio: {e}")
                                                connection_error_count += 1
                                                if connection_error_count > 5:
                                                    print("Too many audio processing errors, closing connection")
                                                    return
                                    
                                    elif openai_message.type == aiohttp.WSMsgType.ERROR:
                                        print(f"WebSocket error: {openai_message.data}")
                                        break
                                    
                                    elif openai_message.type == aiohttp.WSMsgType.CLOSED:
                                        print("OpenAI WebSocket connection closed")
                                        break
                                        
                            except aiohttp.ClientError as e:
                                print(f"WebSocket connection error in send_to_twilio: {e}")
                            except Exception as e:
                                print(f"Error in send_to_twilio: {e}")
                        
                        # Handle bidirectional data flow
                        tasks = [
                            receive_from_twilio(),
                            send_to_twilio()
                        ]
                        
                        # Run both tasks concurrently
                        await asyncio.gather(*tasks)
                        break  # Exit the retry loop if everything was successful
                        
            except aiohttp.ClientConnectorError as e:
                print(f"Connection error to OpenAI: {e}")
                retry_count += 1
                if retry_count < max_retries:
                    print(f"Retrying connection in {backoff_time} seconds...")
                    await asyncio.sleep(backoff_time)
                    backoff_time *= 2  # Exponential backoff
                else:
                    print("Max retries reached. Failed to connect to OpenAI.")
                    raise
                    
            except aiohttp.WSServerHandshakeError as e:
                print(f"WebSocket handshake error: {e}")
                if e.status == 429:  # Rate limit error
                    retry_count += 1
                    if retry_count < max_retries:
                        backoff_time = min(backoff_time * 2, 60)  # Cap at 60 seconds
                        print(f"Rate limited. Retrying in {backoff_time} seconds...")
                        await asyncio.sleep(backoff_time)
                    else:
                        print("Max retries reached due to rate limiting.")
                        raise
                else:
                    # For other handshake errors (401, 403, etc.), fail immediately
                    print(f"Authentication or permission error: {e.status}")
                    raise
            
            except Exception as e:
                print(f"Unexpected error connecting to OpenAI: {e}")
                retry_count += 1
                if retry_count < max_retries:
                    print(f"Retrying connection in {backoff_time} seconds...")
                    await asyncio.sleep(backoff_time)
                    backoff_time *= 2  # Exponential backoff
                else:
                    raise
    
    except Exception as e:
        print(f"Error during OpenAI websocket connection: {str(e)}")
        # Send an error message to Twilio client if possible
        try:
            if websocket.client_state.CONNECTED:
                await websocket.send_text(json.dumps({
                    "event": "error",
                    "error": {
                        "message": "AI service connection error. Please try again later."
                    }
                }))
        except Exception:
            pass  # Ignore errors in error handling
    
    finally:
        # Ensure WebSocket is closed
        if openai_ws and not openai_ws.closed:
            await openai_ws.close()
        print("WebSocket connection closed")

async def initialize_session(openai_ws):
    """Control initial session with OpenAI."""
    session_update = {
        "type": "session.update",
        "session": {
            "turn_detection": {
                "type": "server_vad",
                "silence_duration_ms": 2000,     # Increased from 1000ms to 2000ms for longer natural pauses
                "prefix_padding_ms": 500,        # Increased from 300ms to 500ms to keep more audio context before speech
                "threshold": 0.5,                # Reduced threshold to better detect subtle speech
                "create_response": True,         # Auto create response on speech end
                "interrupt_response": True       # Allow interrupting AI's response
            },
            "input_audio_format": "g711_ulaw",
            "output_audio_format": "g711_ulaw",
            "voice": VOICE,
            "instructions": SYSTEM_MESSAGE,
            "modalities": ["text", "audio"],
            "temperature": 0.8,
        }
    }
    print('Sending session update:', json.dumps(session_update))
    
    # Use send_str for aiohttp websocket
    await openai_ws.send_str(json.dumps(session_update))

    # The line below was removed to prevent the AI from speaking first
    # await send_initial_conversation_item(openai_ws)

async def send_initial_conversation_item(openai_ws):
    """Send initial conversation so AI talks first."""
    initial_conversation_item = {
        "type": "conversation.item.create",
        "item": {
            "type": "message",
            "role": "user",
            "content": [
                {
                    "type": "input_text",
                    "text": (
                        "Greet the user with 'Hello there! I am an AI voice assistant powered by "
                        "Twilio and the OpenAI Realtime API. You can ask me for facts, jokes, or "
                        "anything you can imagine. How can I help you?'"
                    )
                }
            ]
        }
    }
    # Use send_str for aiohttp websocket
    await openai_ws.send_str(json.dumps(initial_conversation_item))
    await openai_ws.send_str(json.dumps({"type": "response.create"}))

async def check_number_allowed(to):
    """Check if a number is allowed to be called."""
    # Modified to allow all numbers to be called
    return True
    
    # Original implementation (kept for reference but not used)
    """
    try:
        # Uncomment these lines to test numbers. Only add numbers you have permission to call
        # OVERRIDE_NUMBERS = ['+18005551212'] 
        # if to in OVERRIDE_NUMBERS:             
          # return True

        incoming_numbers = client.incoming_phone_numbers.list(phone_number=to)
        if incoming_numbers:
            return True

        outgoing_caller_ids = client.outgoing_caller_ids.list(phone_number=to)
        if outgoing_caller_ids:
            return True

        return False
    except Exception as e:
        print(f"Error checking phone number: {e}")
        return False
    """
async def make_call(phone_number_to_call: str):
    """Make an outbound call."""
    if not phone_number_to_call:
        raise ValueError("Please provide a phone number to call.")

    is_allowed = await check_number_allowed(phone_number_to_call)
    if not is_allowed:
        raise ValueError(f"The number {phone_number_to_call} is not recognized as a valid outgoing number or caller ID.")

    # Ensure compliance with applicable laws and regulations
    # All of the rules of TCPA apply even if a call is made by AI.
    # Do your own diligence for compliance.

    outbound_twiml = (
        f'<?xml version="1.0" encoding="UTF-8"?>'
        f'<Response><Connect><Stream url="wss://{DOMAIN}/media-stream" /></Connect></Response>'
    )

    call = client.calls.create(
        from_=PHONE_NUMBER_FROM,
        to=phone_number_to_call,
        twiml=outbound_twiml
    )

    await log_call_sid(call.sid)

async def log_call_sid(call_sid):
    """Log the call SID."""
    print(f"Call started with SID: {call_sid}")

async def send_initial_welcome(openai_ws):
    """Send a welcome message to initiate the conversation."""
    try:
        # Check if WebSocket is still open
        if openai_ws.closed:
            print("Cannot send welcome message - WebSocket is closed")
            return
        
        # First wait to ensure connection is fully established
        await asyncio.sleep(2.0)  # Increased from 1.0 to 2.0 seconds
            
        # Create a natural greeting message to start the conversation
        initial_message = {
            "type": "conversation.item.create",
            "item": {
                "type": "message",
                "role": "user", 
                "content": [
                    {
                        "type": "input_text",
                        "text": "Hello, I'd like to speak with you."
                    }
                ]
            }
        }
        
        # Send the initial message if the connection is still open
        if not openai_ws.closed:
            await openai_ws.send_str(json.dumps(initial_message))
            
            # Wait a moment before creating a response - longer delay for stability
            await asyncio.sleep(2.0)  # Increased from 1.0 to 2.0 seconds
            
            # Check if WebSocket is still open before creating response
            if not openai_ws.closed:
                # Create a response to this initial message
                await openai_ws.send_str(json.dumps({"type": "response.create"}))
                print("Sent initial welcome message")
                
                # Wait a moment before starting the periodic check
                await asyncio.sleep(2.0)  # Increased from 1.0 to 2.0 seconds
                
                # Start a background task to periodically commit audio if no speech is detected
                if not openai_ws.closed:
                    asyncio.create_task(periodic_commit_check(openai_ws))
                else:
                    print("Cannot start periodic check - WebSocket is closed")
            else:
                print("Cannot create welcome response - WebSocket is closed")
        else:
            print("Cannot send initial message - WebSocket closed during delay")
    
    except aiohttp.ClientError as e:
        print(f"WebSocket connection error in welcome message: {e}")
    except Exception as e:
        print(f"Error sending welcome message: {e}")

async def periodic_commit_check(openai_ws):
    """Periodically check and commit audio if needed."""
    try:
        # Wait for initial conversation to establish
        await asyncio.sleep(20)  # Increased initial wait time from 15 to 20 seconds
        
        last_commit_time = time.time()
        last_error_time = 0
        
        while not openai_ws.closed:
            try:
                # Check less frequently - every 8 seconds instead of 5
                await asyncio.sleep(8)
                
                # Exit if WebSocket is closed
                if openai_ws.closed:
                    print("Periodic check: WebSocket closed, exiting periodic commit task")
                    return
                
                current_time = time.time()
                
                # Only commit if it's been at least 20 seconds since last commit (increased from 15)
                # and at least 15 seconds since the last error (increased from 10)
                if (current_time - last_commit_time >= 20 and 
                    current_time - last_error_time >= 15):
                    
                    print("Periodic check: manually committing audio buffer")
                    
                    # Send a small ping message to make sure we have some audio data
                    ping_message = {
                        "type": "conversation.item.create",
                        "item": {
                            "type": "message",
                            "role": "system",
                            "content": [
                                {
                                    "type": "input_text",
                                    "text": "..." # Small non-disruptive ping
                                }
                            ]
                        }
                    }
                    
                    # First try a commit
                    try:
                        # Check again if the WebSocket is still open
                        if not openai_ws.closed:
                            # First send the ping message to ensure we have data
                            await openai_ws.send_str(json.dumps(ping_message))
                            # Wait longer for the message to be processed and buffer to be populated
                            await asyncio.sleep(1.5)  # Increased from 1.0 to 1.5 seconds
                            
                            # Now commit the buffer
                            await openai_ws.send_str(json.dumps({"type": "input_audio_buffer.commit"}))
                            last_commit_time = current_time
                            
                            # Wait a longer time before creating response
                            await asyncio.sleep(1.5)  # Increased from 0.5 to 1.5 seconds
                            
                            # Only create response if we haven't received any errors
                            if current_time - last_error_time >= 15 and not openai_ws.closed:  # Increased from 10 to 15
                                await openai_ws.send_str(json.dumps({"type": "response.create"}))
                        else:
                            print("Periodic check: WebSocket closed, exiting periodic commit task")
                            return
                    except aiohttp.ClientError as e:
                        print(f"WebSocket connection error in periodic commit: {e}")
                        last_error_time = current_time
                        return  # Exit on WebSocket errors
                    except Exception as e:
                        print(f"Error in periodic commit: {e}")
                        last_error_time = current_time
                    
            except asyncio.CancelledError:
                print("Periodic commit task cancelled")
                return
            except Exception as e:
                print(f"Error in periodic commit: {e}")
                last_error_time = time.time()
                await asyncio.sleep(8)  # Back off on errors
                
    except asyncio.CancelledError:
        print("Periodic commit task cancelled")
    except Exception as e:
        print(f"Error in periodic commit task: {e}")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Run the Twilio AI voice assistant server.")
    parser.add_argument('--call', required=False, help="The phone number to call, e.g., '--call=+18005551212'")
    args = parser.parse_args()

    print(
        'Our recommendation is to always disclose the use of AI for outbound or inbound calls.\n'
        'Reminder: All of the rules of TCPA apply even if a call is made by AI.\n'
        'Check with your counsel for legal and compliance advice.'
    )

    # If a phone number is provided via command line, make the call immediately
    if args.call:
        loop = asyncio.get_event_loop()
        loop.run_until_complete(make_call(args.call))
    
    # Start the web server
    print(f"Starting server on port {PORT}. Access the web UI at http://localhost:{PORT}")
    uvicorn.run(app, host="0.0.0.0", port=PORT)
