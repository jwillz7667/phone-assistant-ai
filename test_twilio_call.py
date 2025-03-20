import os
from dotenv import load_dotenv
from twilio.rest import Client
import re

load_dotenv()

# Load configuration
TWILIO_ACCOUNT_SID = os.getenv('TWILIO_ACCOUNT_SID')
TWILIO_AUTH_TOKEN = os.getenv('TWILIO_AUTH_TOKEN')
PHONE_NUMBER_FROM = os.getenv('PHONE_NUMBER_FROM')
raw_domain = os.getenv('DOMAIN', '')
DOMAIN = re.sub(r'(^\w+:|^)\/\/|\/+$', '', raw_domain)  # Strip protocols and trailing slashes

def validate_configuration():
    """Check if all required environment variables are set."""
    errors = []
    
    if not TWILIO_ACCOUNT_SID:
        errors.append("TWILIO_ACCOUNT_SID is not set in your .env file")
    
    if not TWILIO_AUTH_TOKEN:
        errors.append("TWILIO_AUTH_TOKEN is not set in your .env file")
    
    if not PHONE_NUMBER_FROM:
        errors.append("PHONE_NUMBER_FROM is not set in your .env file")
    
    if not DOMAIN:
        errors.append("DOMAIN is not set in your .env file")
    
    return errors

def validate_domain():
    """Validate domain format and check if it looks correct."""
    domain_issues = []
    
    # Check if domain contains protocol
    if DOMAIN.startswith(('http:', 'https:', 'ws:', 'wss:')):
        domain_issues.append(f"DOMAIN should not include protocol prefix, but found: {DOMAIN}")
    
    # Check if domain ends with a path (might be valid but unusual)
    if '/' in DOMAIN:
        domain_issues.append(f"DOMAIN contains slashes which might indicate a path: {DOMAIN}")
        
    # Check if domain looks like a valid hostname or IP
    if not re.match(r'^[a-zA-Z0-9.-]+(\:[0-9]+)?$', DOMAIN):
        domain_issues.append(f"DOMAIN doesn't look like a valid hostname or IP: {DOMAIN}")
    
    return domain_issues

def create_twiml():
    """Create and validate the TwiML that will be used."""
    twiml = (
        f'<?xml version="1.0" encoding="UTF-8"?>'
        f'<Response><Connect><Stream url="wss://{DOMAIN}/media-stream" /></Connect></Response>'
    )
    
    # Simple validation
    if 'wss://' not in twiml:
        return None, ["TwiML doesn't contain 'wss://' protocol which is required for secure WebSockets"]
    
    return twiml, []

def make_test_call(phone_number_to_call, twiml):
    """Attempt to make a test call to diagnose issues."""
    try:
        # Initialize Twilio client
        client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
        
        # Make the call
        call = client.calls.create(
            from_=PHONE_NUMBER_FROM,
            to=phone_number_to_call,
            twiml=twiml
        )
        
        return True, call.sid
    except Exception as e:
        return False, str(e)

if __name__ == "__main__":
    print("Twilio Call Test Utility")
    print("========================\n")
    
    # Step 1: Validate configuration
    print("1. Validating configuration...")
    config_errors = validate_configuration()
    
    if config_errors:
        print("❌ Configuration errors found:")
        for error in config_errors:
            print(f"   - {error}")
        print("\nPlease fix these errors in your .env file and try again.")
        exit(1)
    else:
        print("✅ Configuration looks good!")
    
    # Step 2: Validate domain
    print("\n2. Validating domain...")
    domain_issues = validate_domain()
    
    if domain_issues:
        print("⚠️ Potential domain issues:")
        for issue in domain_issues:
            print(f"   - {issue}")
        print("\nYou may want to check your DOMAIN setting in the .env file.")
    else:
        print(f"✅ Domain looks good: {DOMAIN}")
    
    # Step 3: Create and validate TwiML
    print("\n3. Creating TwiML...")
    twiml, twiml_issues = create_twiml()
    
    if twiml_issues:
        print("❌ TwiML issues found:")
        for issue in twiml_issues:
            print(f"   - {issue}")
        print("\nPlease check your TwiML generation logic.")
        exit(1)
    else:
        print("✅ TwiML created successfully:")
        print(f"   {twiml}")
    
    # Step 4: Make test call
    print("\n4. Test call:")
    print("   To make a test call, enter a phone number below.")
    print("   This will use your Twilio account and may incur charges.")
    
    phone_number = input("   Enter phone number to call (e.g., +1234567890) or press Enter to skip: ")
    
    if phone_number:
        print(f"\n   Making test call to {phone_number}...")
        success, result = make_test_call(phone_number, twiml)
        
        if success:
            print(f"   ✅ Call initiated successfully! Call SID: {result}")
            print("   Check your Twilio console for call status and any errors.")
        else:
            print(f"   ❌ Call failed: {result}")
            print("   Check your Twilio credentials and network connectivity.")
    else:
        print("   Skipping test call.")
    
    print("\nDiagnostic completed.") 