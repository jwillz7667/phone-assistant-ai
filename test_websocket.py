import asyncio
import websockets
import os
from dotenv import load_dotenv

load_dotenv()

# Get domain from .env file, same as your main application
DOMAIN = os.getenv('DOMAIN', '')
# Strip protocols and trailing slashes
DOMAIN = DOMAIN.replace('http://', '').replace('https://', '').replace('wss://', '').replace('ws://', '').rstrip('/')

async def test_websocket_connection():
    """Test if the WebSocket endpoint is accessible and responding correctly."""
    url = f"wss://{DOMAIN}/media-stream"
    print(f"Attempting to connect to: {url}")
    
    try:
        # Attempt to connect with a timeout
        async with websockets.connect(url, ping_interval=None, ping_timeout=None) as websocket:
            print("Connection established successfully!")
            print("WebSocket handshake completed - your endpoint is working.")
            
            # Try sending a simple message
            await websocket.send('{"test": "Hello, server!"}')
            print("Test message sent.")
            
            # Wait for a response with timeout
            try:
                response = await asyncio.wait_for(websocket.recv(), timeout=5.0)
                print(f"Received response: {response}")
            except asyncio.TimeoutError:
                print("No response received within timeout, but connection is working.")
                
    except websockets.exceptions.InvalidStatusCode as e:
        print(f"Connection failed with status code: {e.status_code}")
        print(f"Error message: {str(e)}")
        print("\nThis indicates your server is responding, but not accepting WebSocket upgrade requests.")
        print("Check that your FastAPI WebSocket route is correctly configured.")
        
    except websockets.exceptions.InvalidHandshake as e:
        print(f"WebSocket handshake failed: {str(e)}")
        print("\nYour server may not be properly configured for WebSocket connections.")
        
    except ConnectionRefusedError:
        print("Connection refused. Your server might not be running or the port is closed.")
        print("Check that your server is running and the port is open to external connections.")
        
    except Exception as e:
        print(f"Connection failed with error: {type(e).__name__}: {str(e)}")

if __name__ == "__main__":
    # Check if DOMAIN is properly set
    if not DOMAIN:
        print("ERROR: DOMAIN environment variable is not set in your .env file.")
        print("Please set it to your server's domain name without protocol prefix.")
        exit(1)
        
    print(f"Testing WebSocket connection to: {DOMAIN}/media-stream")
    asyncio.run(test_websocket_connection()) 