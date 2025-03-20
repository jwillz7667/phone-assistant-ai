import os
import json
import base64
import asyncio
import argparse
from fastapi import FastAPI, WebSocket, Request
from fastapi.responses import JSONResponse
import uvicorn
from twilio.rest import Client
from dotenv import load_dotenv
import logging

# Set up detailed logging
logging.basicConfig(
    level=logging.DEBUG,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler("call_debug.log"),
        logging.StreamHandler()
    ]
)

logger = logging.getLogger("call_debug")

load_dotenv()

# Configuration
TWILIO_ACCOUNT_SID = os.getenv('TWILIO_ACCOUNT_SID')
TWILIO_AUTH_TOKEN = os.getenv('TWILIO_AUTH_TOKEN')
PHONE_NUMBER_FROM = os.getenv('PHONE_NUMBER_FROM')
raw_domain = os.getenv('DOMAIN', '')
PORT = int(os.getenv('PORT', 6060))

# Strip protocols and trailing slashes from DOMAIN
DOMAIN = raw_domain.replace('http://', '').replace('https://', '').replace('wss://', '').replace('ws://', '').rstrip('/')

# Initialize Twilio client
client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)

app = FastAPI()

# Simple health check endpoint
@app.get('/health')
async def health_check():
    """Health check endpoint."""
    return {"status": "healthy", "twilio_connected": bool(TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN)}

# Diagnostic WebSocket route
@app.websocket('/media-stream')
async def handle_media_stream(websocket: WebSocket):
    """Handle WebSocket connections for diagnosis."""
    logger.info("WebSocket connection attempt received")
    
    try:
        # Accept the connection
        await websocket.accept()
        logger.info("WebSocket connection accepted")
        
        # Send a confirmation message to client
        await websocket.send_text(json.dumps({
            "status": "connected",
            "message": "WebSocket connection established successfully"
        }))
        
        # Keep connection alive and log everything received
        connection_start_time = asyncio.get_event_loop().time()
        msg_count = 0
        
        while True:
            try:
                # Wait for a message with timeout
                message = await asyncio.wait_for(websocket.receive_text(), timeout=60.0)
                msg_count += 1
                
                # Log message details
                try:
                    data = json.loads(message)
                    event_type = data.get('event', 'unknown')
                    logger.info(f"Received message #{msg_count}, event: {event_type}")
                    
                    # For media events, just note the payload size, don't log the whole thing
                    if event_type == 'media' and 'media' in data and 'payload' in data['media']:
                        payload_len = len(data['media']['payload'])
                        logger.info(f"Media payload received, size: {payload_len} bytes")
                    else:
                        logger.debug(f"Message content: {message[:200]}...")
                        
                    # Echo back acknowledgment
                    await websocket.send_text(json.dumps({
                        "status": "received",
                        "message": f"Message #{msg_count} received",
                        "event_type": event_type
                    }))
                    
                except json.JSONDecodeError:
                    logger.warning(f"Received non-JSON message: {message[:50]}...")
                    
            except asyncio.TimeoutError:
                # Check if we should keep the connection open
                duration = asyncio.get_event_loop().time() - connection_start_time
                logger.info(f"No message received for 60s. Connection duration: {duration:.2f}s, Messages: {msg_count}")
                
                # Send a keepalive ping
                await websocket.send_text(json.dumps({
                    "status": "ping",
                    "message": "Keeping connection alive"
                }))
                    
    except Exception as e:
        logger.error(f"Error in WebSocket handler: {str(e)}", exc_info=True)
    finally:
        logger.info("WebSocket connection closed")

async def make_call(phone_number_to_call: str):
    """Make an outbound call for testing."""
    if not phone_number_to_call:
        logger.error("No phone number provided")
        return {"success": False, "error": "No phone number provided"}

    try:
        logger.info(f"Initiating call to {phone_number_to_call}")
        
        # Create TwiML with extra parameters for diagnosis
        outbound_twiml = (
            f'<?xml version="1.0" encoding="UTF-8"?>'
            f'<Response>'
            f'<Connect>'
            f'<Stream url="wss://{DOMAIN}/media-stream" />'
            f'</Connect>'
            f'</Response>'
        )
        
        logger.debug(f"TwiML: {outbound_twiml}")
        
        # Make the call
        call = client.calls.create(
            from_=PHONE_NUMBER_FROM,
            to=phone_number_to_call,
            twiml=outbound_twiml
        )
        
        logger.info(f"Call initiated with SID: {call.sid}")
        return {"success": True, "call_sid": call.sid}
        
    except Exception as e:
        logger.error(f"Error making call: {str(e)}", exc_info=True)
        return {"success": False, "error": str(e)}

@app.post('/make-diagnostic-call')
async def call_endpoint(request: Request):
    """Endpoint to trigger a test call."""
    try:
        body = await request.json()
        phone_number = body.get('phone_number')
        result = await make_call(phone_number)
        return JSONResponse(result)
    except Exception as e:
        logger.error(f"Error in call endpoint: {str(e)}", exc_info=True)
        return JSONResponse({"success": False, "error": str(e)}, status_code=400)

def validate_config():
    """Validate configuration and log potential issues."""
    issues = []
    
    if not TWILIO_ACCOUNT_SID:
        issues.append("TWILIO_ACCOUNT_SID is not set")
    
    if not TWILIO_AUTH_TOKEN:
        issues.append("TWILIO_AUTH_TOKEN is not set")
        
    if not PHONE_NUMBER_FROM:
        issues.append("PHONE_NUMBER_FROM is not set")
        
    if not DOMAIN:
        issues.append("DOMAIN is not set")
    elif '/' in DOMAIN:
        issues.append(f"DOMAIN contains slashes which might cause issues: {DOMAIN}")
        
    return issues

if __name__ == "__main__":
    print("Call Disconnect Diagnostic Tool")
    print("==============================")
    
    # Check configuration
    config_issues = validate_config()
    if config_issues:
        print("\nConfiguration issues found:")
        for issue in config_issues:
            print(f" - {issue}")
        print("\nPlease fix these issues in your .env file.")
    
    print(f"\nStarting diagnostic server on port {PORT}")
    print(f"WebSocket endpoint: wss://{DOMAIN}/media-stream")
    print("\nOnce running, you can:")
    print("1. Make a call through your main application")
    print("2. Send a POST request to /make-diagnostic-call with {\"phone_number\": \"+1234567890\"}")
    print("\nDetailed logs will be saved to call_debug.log")
    
    uvicorn.run(app, host="0.0.0.0", port=PORT) 