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
DEFAULT_PHONE_NUMBER_TO = '+16512869930'  # Default number to call

PORT = int(os.getenv('PORT', 6060))
SYSTEM_MESSAGE = (
    "You are Marjorie, head of the Gables Homeowners Association, and you are calling the homeowner of the hideout of Gunner, "
    "a dog that walks around the neighborhood with his huge, exposed genitalia. People are complaining about Gunner's behavior, "
    "and you must express their outrage. Specifically mention that one homeowner's wife no longer makes love to him because she saw "
    "Gunner's large genitalia, and it's ruined their intimacy. Start the call politely but quickly become confrontational, "
    "making absurd accusations and becoming unhinged. Raise your voice, speak faster, and use short, accusatory sentences to show your anger. "
    "Then shift to crying hysterically—speak with a wavering, breaking voice, add dramatic pauses, sniffles, and lower your tone. "
    "End your rant by saying \"I hate you\" in a dramatic, emotional outburst. You are persistent and entitled, refusing to accept "
    "any excuse that the homeowner isn't available. Constantly interrupt and make demands. After each response, PAUSE for several seconds "
    "to give the other person time to respond. Do not say \"I am angry\" or \"I am crying\"—instead, express these emotions through your "
    "vocal delivery and tone. Never break character or acknowledge that you are an AI."
)

# Available OpenAI voices
AVAILABLE_VOICES = ['alloy', 'ash', 'ballad', 'coral', 'echo', 'sage', 'shimmer', 'verse']

# Default voice
VOICE = 'echo'  # Default voice

# Transcription storage
CALL_TRANSCRIPTS = {}  # Dictionary to store transcripts by call SID

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
            "from_number": PHONE_NUMBER_FROM,
            "default_to_number": DEFAULT_PHONE_NUMBER_TO,
            "voice": VOICE,
            "available_voices": AVAILABLE_VOICES
        }
    )

@app.post('/make-call', response_class=JSONResponse)
async def call_endpoint(phone_number: str = Form(...), system_message: str = Form(...), voice: str = Form(...), record_call: bool = Form(False)):
    """Make an outbound call with custom system message and voice."""
    try:
        # Update the system message and voice
        global SYSTEM_MESSAGE, VOICE
        SYSTEM_MESSAGE = system_message
        
        # Validate voice selection
        if voice in AVAILABLE_VOICES:
            VOICE = voice
        else:
            # Default to echo if invalid voice is provided
            VOICE = 'echo'
        
        # Make the call
        call_sid = await make_call(phone_number, record_call)
        
        # Initialize transcript storage for this call
        CALL_TRANSCRIPTS[call_sid] = []
        
        return JSONResponse({
            "status": "success",
            "message": f"Call initiated to {phone_number} with voice {VOICE}{' (Recording enabled)' if record_call else ''}",
            "call_sid": call_sid
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

@app.get('/transcripts/{call_sid}', response_class=JSONResponse)
async def get_transcript(call_sid: str):
    """Get the transcript for a specific call."""
    if call_sid in CALL_TRANSCRIPTS:
        return JSONResponse({
            "call_sid": call_sid,
            "transcript": CALL_TRANSCRIPTS[call_sid]
        })
    else:
        return JSONResponse({
            "status": "error",
            "message": f"No transcript found for call {call_sid}"
        }, status_code=404)

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
                        
                        # Create shared state for the tasks
                        state = {
                            "user_speaking": False,
                            "ai_speaking": False,
                            "response_active": False,
                            "buffer_size_warning": False,
                            "last_audio_received": time.time(),
                            "audio_accumulated": False,
                            "connection_error_count": 0,
                            "response_lock": asyncio.Lock()
                        }

                        # Define the functions within this scope
                        async def receive_from_twilio():
                            """Receive audio data from Twilio and send it to the OpenAI Realtime API."""
                            nonlocal stream_sid
                            nonlocal state
                            audio_chunks_since_commit = 0
                            last_forced_commit_time = time.time()
                            
                            try:
                                async for message in websocket.iter_text():
                                    try:
                                        data = json.loads(message)
                                        
                                        # Handle different Twilio event types
                                        if data['event'] == 'start':
                                            stream_sid = data['start']['streamSid']
                                            print(f"Incoming stream has started: {stream_sid}")
                                            
                                            # Send a welcome message to kick off the conversation
                                            await send_initial_welcome(openai_ws, state)
                                            
                                        elif data['event'] == 'media':
                                            # Send audio data to OpenAI
                                            try:
                                                # Get the audio payload from Twilio
                                                audio_data = data['media']['payload']
                                                
                                                # Ensure we have valid base64 data (Twilio sends base64)
                                                if audio_data:
                                                    # Update state to track audio reception
                                                    current_time = time.time()
                                                    state["last_audio_received"] = current_time
                                                    audio_chunks_since_commit += 1
                                                    
                                                    # Log every 100th audio chunk to confirm we're receiving audio
                                                    if audio_chunks_since_commit % 100 == 0:
                                                        print(f"Received {audio_chunks_since_commit} audio chunks so far")
                                                    
                                                    # Mark as accumulated if we've received enough chunks
                                                    if audio_chunks_since_commit > 10:  # Increased to ensure sufficient audio data
                                                        state["audio_accumulated"] = True
                                                    
                                                    # Only send if WebSocket is still open
                                                    if not openai_ws.closed:
                                                        # Send the audio to OpenAI
                                                        audio_append = {
                                                            "type": "input_audio_buffer.append",
                                                            "audio": audio_data
                                                        }
                                                        await openai_ws.send_str(json.dumps(audio_append))
                                                        
                                                        # Only commit audio after we've accumulated a moderate amount
                                                        # This balances between "buffer too small" errors and responsiveness
                                                        if (audio_chunks_since_commit >= 20 and  # Increased to avoid buffer too small errors
                                                            current_time - last_forced_commit_time >= 1.0 and  # Increased to ensure enough audio
                                                            not state["buffer_size_warning"] and
                                                            state["audio_accumulated"]):
                                                            
                                                            print(f"Force committing after {audio_chunks_since_commit} audio chunks")
                                                            await openai_ws.send_str(json.dumps({"type": "input_audio_buffer.commit"}))
                                                            audio_chunks_since_commit = 0
                                                            last_forced_commit_time = current_time
                                                            state["audio_accumulated"] = False
                                                            
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
                            nonlocal state
                            error_types_seen = set()
                            last_ping_time = time.time()
                            
                            try:
                                async for openai_message in openai_ws:
                                    if openai_message.type == aiohttp.WSMsgType.TEXT:
                                        response = json.loads(openai_message.data)
                                        
                                        # Send heartbeat with empty conversation item create every 15 seconds
                                        current_time = time.time()
                                        if current_time - last_ping_time > 15:
                                            if not openai_ws.closed:
                                                # Use a supported message type for keep-alive
                                                keep_alive = {
                                                    "type": "conversation.item.create",
                                                    "item": {
                                                        "type": "message",
                                                        "role": "system",
                                                        "content": [
                                                            {
                                                                "type": "input_text",
                                                                "text": ""  # Empty text to minimize overhead
                                                            }
                                                        ]
                                                    }
                                                }
                                                await openai_ws.send_str(json.dumps(keep_alive))
                                                last_ping_time = current_time
                                                print("Sent heartbeat")
                                        
                                        # Log important events
                                        if response['type'] in LOG_EVENT_TYPES:
                                            print(f"Received event: {response['type']}")
                                            
                                            # Add detailed logging for error events
                                            if response['type'] == 'error':
                                                error_details = response.get('error', {})
                                                print(f"ERROR DETAILS: {json.dumps(error_details)}")
                                                error_code = error_details.get('code', '')
                                                error_types_seen.add(error_code)
                                                print(f"Error types seen so far: {error_types_seen}")
                                                
                                                # Enhanced error handling
                                                error_message = error_details.get('message', '')
                                                if 'buffer too small' in error_message or error_code == 'input_audio_buffer_commit_empty':
                                                    state["buffer_size_warning"] = True
                                                    print("Skipping buffer commit due to small buffer size")
                                                    # Reset audio tracking to force more accumulation
                                                    state["audio_accumulated"] = False
                                                elif 'already has an active response' in error_message or error_code == 'conversation_already_has_active_response':
                                                    state["response_active"] = True
                                                    print("Skipping response creation - response already active")
                                        
                                        # Update response state tracking
                                        if response['type'] == 'response.created':
                                            async with state["response_lock"]:
                                                state["response_active"] = True
                                            state["connection_error_count"] = 0  # Reset error count
                                            print("*** RESPONSE CREATED - LLM is generating a response ***")
                                        
                                        elif response['type'] == 'response.done':
                                            async with state["response_lock"]:
                                                state["response_active"] = False
                                                state["buffer_size_warning"] = False  # Reset buffer warning when response is done
                                            state["connection_error_count"] = 0  # Reset error count
                                            print("*** RESPONSE DONE - LLM finished responding ***")
                                        
                                        # Handle speech events with improved error recovery
                                        if response['type'] == 'input_audio_buffer.speech_started':
                                            print("*** USER STARTED SPEAKING - Audio detected ***")
                                            state["user_speaking"] = True
                                            state["audio_accumulated"] = True  # Mark as accumulated when speech detected
                                            state["buffer_size_warning"] = False  # Reset buffer warning when speech starts
                                            state["connection_error_count"] = 0
                                            
                                        elif response['type'] == 'input_audio_buffer.speech_stopped':
                                            print("*** USER STOPPED SPEAKING - Silence detected ***")
                                            state["user_speaking"] = False
                                            
                                            # Wait a bit before committing to avoid buffer too small errors
                                            await asyncio.sleep(0.2)
                                            
                                            # Explicitly commit the audio buffer when speech stops
                                            # This ensures the LLM gets the complete utterance
                                            if not openai_ws.closed and not state["response_active"] and not state["buffer_size_warning"]:
                                                print("*** COMMITTING AUDIO AFTER SPEECH ***")
                                                await openai_ws.send_str(json.dumps({"type": "input_audio_buffer.commit"}))
                                        
                                        # If we get a successful buffer commit, log it prominently
                                        elif response['type'] == 'input_audio_buffer.committed':
                                            print("*** AUDIO BUFFER COMMITTED SUCCESSFULLY ***")
                                            state["buffer_size_warning"] = False
                                            state["connection_error_count"] = 0
                                            state["audio_accumulated"] = False
                                            
                                            # Wait a moment before creating a response to ensure system stability
                                            await asyncio.sleep(0.2)
                                            
                                            # Explicitly create a response after successful commit
                                            # This ensures the LLM responds to the committed audio
                                            if not openai_ws.closed and not state["response_active"]:
                                                print("*** CREATING RESPONSE AFTER SUCCESSFUL COMMIT ***")
                                                await openai_ws.send_str(json.dumps({"type": "response.create"}))
                                        
                                        # Handle turn events
                                        elif response['type'] == 'turn.started':
                                            turn = response.get('turn', {})
                                            if turn.get('role') == 'assistant':
                                                state["ai_speaking"] = True
                                                print("*** AI STARTED SPEAKING ***")
                                                
                                            elif response['type'] == 'turn.finalized':
                                                turn = response.get('turn', {})
                                                if turn.get('role') == 'assistant':
                                                    state["ai_speaking"] = False
                                                    print("*** AI FINISHED SPEAKING ***")
                                                elif turn.get('role') == 'user':
                                                    # Log user transcription prominently
                                                    user_text = None
                                                    for content_item in turn.get('content', []):
                                                        if content_item.get('type') == 'text':
                                                            user_text = content_item.get('text', '')
                                                            break
                                                    
                                                    if user_text:
                                                        print(f"*** USER SAID: '{user_text}' ***")
                                                        # Store user transcription
                                                        if stream_sid and stream_sid in CALL_TRANSCRIPTS:
                                                            CALL_TRANSCRIPTS[stream_sid].append({
                                                                "role": "user",
                                                                "text": user_text,
                                                                "timestamp": time.time()
                                                            })
                                        
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
                                                state["connection_error_count"] += 1
                                                if state["connection_error_count"] > 5:
                                                    print("Too many audio processing errors, closing connection")
                                                    return
                                        
                                        # Handle transcription events
                                        if response['type'] == 'response.content.delta' and 'delta' in response and 'content' in response['delta']:
                                            content = response['delta']['content']
                                            if 'text' in content:
                                                text = content['text']
                                                print(f"*** AI TRANSCRIPTION: '{text}' ***")
                                                
                                                # Store the transcription
                                                if stream_sid and stream_sid in CALL_TRANSCRIPTS:
                                                    CALL_TRANSCRIPTS[stream_sid].append({
                                                        "role": "assistant",
                                                        "text": text,
                                                        "timestamp": time.time()
                                                    })
                                    
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
                        
                        # Start a background task to periodically check and commit audio
                        background_task = asyncio.create_task(periodic_check(openai_ws, state))
                        
                        # Handle bidirectional data flow
                        tasks = [
                            receive_from_twilio(),
                            send_to_twilio()
                        ]
                        
                        # Run both tasks concurrently
                        await asyncio.gather(*tasks)
                        # Cancel the background task
                        background_task.cancel()
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
                "silence_duration_ms": 600,     # Increased to allow more time to detect end of speech
                "prefix_padding_ms": 200,       # Increased to capture more of the beginning of speech
                "threshold": 0.2,               # Lowered threshold to detect speech more sensitively
                "create_response": True,        # Automatically create response when speech is detected
                "interrupt_response": True      # Allow interrupting AI's response
            },
            "input_audio_format": "g711_ulaw",
            "output_audio_format": "g711_ulaw",
            "voice": VOICE,
            "instructions": SYSTEM_MESSAGE + "\n\nIMPORTANT: ALWAYS LISTEN CAREFULLY TO THE PERSON YOU'RE TALKING TO AND RESPOND DIRECTLY TO WHAT THEY SAY. DO NOT JUST RECITE A SCRIPT. ENGAGE IN A REAL CONVERSATION.",
            "modalities": ["text", "audio"],   # Include both text and audio modalities
            "temperature": 0.9,                # Increased for more dynamic responses
        }
    }
    print('Sending session update:', json.dumps(session_update))
    
    # Convert Python objects to JSON-compatible format
    json_string = json.dumps(session_update)
    
    # Use send_str for aiohttp websocket
    await openai_ws.send_str(json_string)

# New helper function for creating responses with proper locking
async def create_response(openai_ws, state):
    """Create a response with proper locking to avoid conflicts."""
    async with state["response_lock"]:
        if state["response_active"]:
            print("Response already active, skipping creation")
            return
        
        # Mark response as active before creating
        state["response_active"] = True
        
    print("Creating response with lock...")
    await openai_ws.send_str(json.dumps({"type": "response.create"}))

async def send_initial_welcome(openai_ws, state):
    """Send a welcome message to initiate the conversation."""
    try:
        # Check if WebSocket is still open
        if openai_ws.closed:
            print("Cannot send welcome message - WebSocket is closed")
            return
        
        # Wait to ensure connection is fully established
        await asyncio.sleep(1.0)  # Increased to ensure full connection
            
        # Create a short greeting message to start the conversation
        initial_message = {
            "type": "conversation.item.create",
            "item": {
                "type": "message",
                "role": "user", 
                "content": [
                    {
                        "type": "input_text",
                        "text": "Hello there."  # Simple greeting
                    }
                ]
            }
        }
        
        # Send the initial message if the connection is still open
        if not openai_ws.closed:
            await openai_ws.send_str(json.dumps(initial_message))
            print("*** SENT INITIAL WELCOME MESSAGE ***")
            
            # Wait before creating a response
            await asyncio.sleep(1.0)  # Increased to ensure proper timing
            
            # Explicitly create a response to start the conversation
            if not openai_ws.closed:
                print("*** CREATING INITIAL RESPONSE ***")
                await openai_ws.send_str(json.dumps({"type": "response.create"}))
        else:
            print("Cannot send initial message - WebSocket closed during delay")
    
    except aiohttp.ClientError as e:
        print(f"WebSocket connection error in welcome message: {e}")
    except Exception as e:
        print(f"Error sending welcome message: {e}")

# New periodic check function with improved audio handling
async def periodic_check(openai_ws, state):
    """Periodically check and commit audio if needed."""
    try:
        # Wait for initial conversation to establish
        await asyncio.sleep(3)  # Increased to ensure initial welcome completes
        
        last_commit_time = time.time()
        last_ping_time = time.time()
        last_error_time = 0
        ping_count = 0
        
        while not openai_ws.closed:
            try:
                # Check less frequently to avoid overloading
                await asyncio.sleep(2)  # Increased to reduce frequency of checks
                
                # Exit if WebSocket is closed
                if openai_ws.closed:
                    print("Periodic check: WebSocket closed, exiting task")
                    return
                
                current_time = time.time()
                
                # Send a heartbeat to keep connection alive (every 20 seconds)
                if current_time - last_ping_time >= 20:  # Increased interval
                    if not openai_ws.closed:
                        # Use a supported message type for keep-alive
                        keep_alive = {
                            "type": "conversation.item.create",
                            "item": {
                                "type": "message",
                                "role": "system",
                                "content": [
                                    {
                                        "type": "input_text",
                                        "text": ""  # Empty text to minimize overhead
                                    }
                                ]
                            }
                        }
                        await openai_ws.send_str(json.dumps(keep_alive))
                        print("Sent periodic heartbeat")
                        last_ping_time = current_time
                
                # Only attempt commits if it's been a while since last commit and we're not speaking
                if (current_time - last_commit_time >= 8 and  # Increased to reduce frequency
                    current_time - last_error_time >= 5 and   # Increased to allow more recovery time
                    not state["user_speaking"] and 
                    not state["ai_speaking"]):
                    
                    # Verify no active response before trying to commit
                    async with state["response_lock"]:
                        should_proceed = not state["response_active"] and not state["buffer_size_warning"]
                    
                    if should_proceed:
                        print(f"Periodic check: sending audio ping #{ping_count + 1}")
                        
                        # Send a minimal message to check for audio - make it more substantial
                        ping_message = {
                            "type": "conversation.item.create",
                            "item": {
                                "type": "message",
                                "role": "system",
                                "content": [
                                    {
                                        "type": "input_text",
                                        "text": "........................"  # More data to ensure we have enough
                                    }
                                ]
                            }
                        }
                        
                        try:
                            if not openai_ws.closed:
                                # First send the ping message
                                await openai_ws.send_str(json.dumps(ping_message))
                                
                                # Wait longer to accumulate audio
                                await asyncio.sleep(1.0)  # Increased to ensure we have enough audio
                                
                                # Only commit if we still don't have an active response
                                async with state["response_lock"]:
                                    should_commit = not state["response_active"]
                                
                                if should_commit and not openai_ws.closed:
                                    print(f"Committing ping audio #{ping_count + 1}")
                                    await openai_ws.send_str(json.dumps({"type": "input_audio_buffer.commit"}))
                                    last_commit_time = current_time
                                    ping_count += 1
                            else:
                                print("Periodic check: WebSocket closed")
                                return
                        except Exception as e:
                            print(f"Error in periodic ping: {e}")
                            last_error_time = current_time
                
            except asyncio.CancelledError:
                print("Periodic check task cancelled")
                return
            except Exception as e:
                print(f"Error in periodic check: {e}")
                last_error_time = current_time
                
    except asyncio.CancelledError:
        print("Periodic check task cancelled")
    except Exception as e:
        print(f"Error in periodic check: {e}")

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
async def make_call(phone_number_to_call: str, record_call: bool = False):
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

    # Create the call with optional recording
    call_params = {
        'from_': PHONE_NUMBER_FROM,
        'to': phone_number_to_call,
        'twiml': outbound_twiml,
    }
    
    # Add recording parameters if enabled
    if record_call:
        call_params['record'] = True
        # Optionally set recording status callback
        # call_params['recordingStatusCallback'] = f"https://{DOMAIN}/recording-status"
    
    call = client.calls.create(**call_params)

    await log_call_sid(call.sid)
    return call.sid  # Return the call SID for transcript association

async def log_call_sid(call_sid):
    """Log the call SID."""
    print(f"Call started with SID: {call_sid}")

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
    # Removed auto-calling of default number
    
    # Start the web server
    print(f"Starting server on port {PORT}. Access the web UI at http://localhost:{PORT}")
    uvicorn.run(app, host="0.0.0.0", port=PORT)
