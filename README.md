# Phone Assistant AI

A voice calling system using Twilio and OpenAI's Realtime API to create interactive AI phone calls with realistic conversational capabilities.

## Features

- **Realtime Voice Conversations**: Leverages OpenAI's Realtime API for natural and responsive voice interactions
- **Twilio Integration**: Uses Twilio's Voice API to place and receive phone calls
- **WebSocket Communication**: Bidirectional audio streaming between Twilio and OpenAI
- **Customizable AI Persona**: Configure how the AI assistant behaves and speaks
- **Voice Activity Detection**: Smart detection of speech to manage conversation turns
- **Error Recovery**: Robust handling of connection issues and API errors

## Technical Architecture

This application uses:

- **FastAPI** for the web server framework
- **OpenAI Realtime API** for AI voice processing and responses
- **Twilio Voice API** for telephony capabilities
- **WebSocket** for bidirectional streaming
- **Asyncio** for concurrent task management

## Prerequisites

- Python 3.8+
- OpenAI API key with access to the Realtime API
- Twilio account with Voice capabilities
- A publicly accessible URL (for Twilio webhooks)

## Environment Variables

Create a `.env` file with the following variables:

```
OPENAI_API_KEY=your_openai_api_key
TWILIO_ACCOUNT_SID=your_twilio_account_sid
TWILIO_AUTH_TOKEN=your_twilio_auth_token
PHONE_NUMBER_FROM=your_twilio_phone_number
DOMAIN=your_public_domain_with_protocol
PORT=6060
```

## Installation

1. Clone the repository:
   ```
   git clone https://github.com/jwillz7667/phone-assistant-ai.git
   cd phone-assistant-ai
   ```

2. Install dependencies:
   ```
   pip install -r requirements.txt
   ```

3. Start the server:
   ```
   python main.py
   ```

## Usage

### Making Calls

1. Access the web interface at `http://localhost:6060`
2. Enter a phone number and optional system message
3. Click "Make Call" to initiate the call

### Customizing the AI Assistant

Edit the `SYSTEM_MESSAGE` variable in `main.py` to change the AI's behavior, personality, and instructions.
You can also change the `VOICE` variable to select different voice options (e.g., 'echo', 'alloy', 'nova', 'shimmer', 'onyx').

## API Endpoints

- `GET /`: Web interface for making calls
- `POST /make-call`: API endpoint to initiate a call
- `GET /health`: Health check endpoint
- `WebSocket /media-stream`: WebSocket endpoint for bidirectional audio streaming

## Technical Implementation Details

### OpenAI Realtime API

The application uses OpenAI's Realtime API for:
- Real-time speech-to-text processing
- AI-generated conversational responses
- Text-to-speech synthesis

The WebSocket connection to OpenAI enables:
- Low-latency bidirectional communication
- Input audio buffer management
- Turn detection using Voice Activity Detection (VAD)

### Twilio Integration

The application integrates with Twilio for:
- Initiating outbound calls
- Receiving and sending audio streams
- Managing call state and control

### Error Handling

The system includes robust error handling for:
- WebSocket connection issues
- Buffer size management
- Rate limiting
- Authentication errors
- Audio processing errors

## Troubleshooting

Common issues:

1. **WebSocket Connection Errors**: Ensure your OpenAI API key has access to the Realtime API.
2. **Buffer Too Small Errors**: The system has built-in retry mechanisms for this.
3. **Twilio Webhook Issues**: Ensure your domain is publicly accessible and correctly configured.

## License

This project is licensed under the MIT License - see the LICENSE file for details.

## Acknowledgements

- OpenAI for the Realtime API
- Twilio for the Voice API
- FastAPI for the web framework 