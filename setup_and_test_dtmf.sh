#!/bin/bash

set -e

echo "╔════════════════════════════════════════════════════════════════╗"
echo "║  DTMF Implementation Setup & Testing                          ║"
echo "╚════════════════════════════════════════════════════════════════╝"
echo ""

# Color codes
GREEN='\033[0;32m'
RED='\033[0;31m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

PROJECT_ROOT="/Users/satish/voice-ai-platform"
VENV_PATH="$PROJECT_ROOT/venv"

# Step 1: Verify project structure
echo -e "${BLUE}[1/6]${NC} Verifying project structure..."
if [ ! -d "$PROJECT_ROOT" ]; then
    echo -e "${RED}✗ Project root not found at $PROJECT_ROOT${NC}"
    exit 1
fi
echo -e "${GREEN}✓ Project found${NC}"

# Step 2: Activate virtual environment
echo -e "${BLUE}[2/6]${NC} Setting up Python environment..."
if [ ! -d "$VENV_PATH" ]; then
    echo -e "${YELLOW}  Creating virtual environment...${NC}"
    cd "$PROJECT_ROOT"
    python3 -m venv venv
fi

source "$VENV_PATH/bin/activate"
echo -e "${GREEN}✓ Virtual environment activated${NC}"

# Step 3: Verify protobuf version
echo -e "${BLUE}[3/6]${NC} Verifying dependencies..."
PROTOBUF_VERSION=$(python -c "import google.protobuf; print(google.protobuf.__version__)" 2>/dev/null || echo "not installed")
if [[ "$PROTOBUF_VERSION" < "7.36" ]]; then
    echo -e "${YELLOW}  Upgrading protobuf to 7.36.2...${NC}"
    pip install --upgrade protobuf==7.36.2 -q
fi
echo -e "${GREEN}✓ Protobuf version: $PROTOBUF_VERSION${NC}"

# Step 4: Verify code changes are in place
echo -e "${BLUE}[4/6]${NC} Verifying DTMF implementation..."
cd "$PROJECT_ROOT"

CHECKS_PASSED=0
CHECKS_TOTAL=4

if grep -q "dtmf_webhook" services/telephony/app.py; then
    echo -e "${GREEN}✓ dtmf_webhook route found in app.py${NC}"
    ((CHECKS_PASSED++))
else
    echo -e "${RED}✗ dtmf_webhook route NOT found in app.py${NC}"
fi
((CHECKS_TOTAL++))

if grep -q "handle_dtmf_webhook" services/telephony/orchestrator.py; then
    echo -e "${GREEN}✓ handle_dtmf_webhook found in orchestrator.py${NC}"
    ((CHECKS_PASSED++))
else
    echo -e "${RED}✗ handle_dtmf_webhook NOT found in orchestrator.py${NC}"
fi
((CHECKS_TOTAL++))

if grep -q "parse_dtmf_digit" libs/telephony_sdk/interface.py; then
    echo -e "${GREEN}✓ parse_dtmf_digit interface found${NC}"
    ((CHECKS_PASSED++))
else
    echo -e "${RED}✗ parse_dtmf_digit interface NOT found${NC}"
fi
((CHECKS_TOTAL++))

if grep -q "call_session_map" services/telephony/orchestrator.py; then
    echo -e "${GREEN}✓ CallSessionMap found${NC}"
    ((CHECKS_PASSED++))
else
    echo -e "${RED}✗ CallSessionMap NOT found${NC}"
fi
((CHECKS_TOTAL++))

echo -e "${GREEN}✓ Implementation verification: $CHECKS_PASSED/$CHECKS_TOTAL checks passed${NC}"

# Step 5: Run diagnostic
echo -e "${BLUE}[5/6]${NC} Running diagnostic tests..."
python3 << 'PYTHON_DIAGNOSTIC'
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), 'services/conversation/generated'))

print("\n  Checking imports and structure...")

try:
    from services.telephony import app as app_module
    from services.telephony import orchestrator
    from libs.telephony_sdk.providers.vobiz import VobizTelephonyProvider
    from libs.telephony_sdk.providers.cloudonix import CloudonixProvider

    # Check routes
    routes = [str(route.path) for route in app_module.app.routes]
    dtmf_routes = [r for r in routes if 'dtmf' in r]

    if dtmf_routes:
        print(f"  ✓ DTMF routes registered: {dtmf_routes}")
    else:
        print(f"  ⚠ DTMF routes not found in app.routes")
        print(f"  Available routes: {routes[:3]}...")

    # Check functions
    if hasattr(orchestrator, 'handle_dtmf_webhook'):
        print("  ✓ orchestrator.handle_dtmf_webhook exists")
    else:
        print("  ✗ orchestrator.handle_dtmf_webhook missing")

    if hasattr(orchestrator, 'call_session_map'):
        print("  ✓ orchestrator.call_session_map exists")
    else:
        print("  ✗ orchestrator.call_session_map missing")

    # Check provider methods
    vobiz = VobizTelephonyProvider({"auth_id": "test", "auth_token": "test"})
    if hasattr(vobiz, 'parse_dtmf_digit'):
        print("  ✓ VobizTelephonyProvider.parse_dtmf_digit exists")

    cloudonix = CloudonixProvider({"domain": "test", "api_keys": ["enc:test"]})
    if hasattr(cloudonix, 'parse_dtmf_digit'):
        print("  ✓ CloudonixProvider.parse_dtmf_digit exists")

    print("\n  ✓ All imports successful")

except Exception as e:
    print(f"  ✗ Import error: {e}")
    sys.exit(1)
PYTHON_DIAGNOSTIC

# Step 6: Check if services are running
echo -e "${BLUE}[6/6]${NC} Checking services status..."

# Check telephony service
if lsof -Pi :8750 -sTCP:LISTEN -t >/dev/null 2>&1; then
    echo -e "${GREEN}✓ services/telephony running on port 8750${NC}"
else
    echo -e "${YELLOW}⚠ services/telephony NOT running on port 8750${NC}"
    echo -e "${YELLOW}  You can start it with:${NC}"
    echo -e "${YELLOW}    source venv/bin/activate${NC}"
    echo -e "${YELLOW}    python -m services.telephony${NC}"
fi

# Check conversation service
if lsof -Pi :50051 -sTCP:LISTEN -t >/dev/null 2>&1; then
    echo -e "${GREEN}✓ Conversation Service running on port 50051${NC}"
else
    echo -e "${YELLOW}⚠ Conversation Service NOT running on port 50051${NC}"
fi

echo ""
echo "╔════════════════════════════════════════════════════════════════╗"
echo "║  Setup Complete!                                               ║"
echo "╚════════════════════════════════════════════════════════════════╝"
echo ""
echo -e "${BLUE}Next steps:${NC}"
echo ""
echo "1. Start services/telephony (if not running):"
echo "   ${YELLOW}source venv/bin/activate${NC}"
echo "   ${YELLOW}python -m services.telephony${NC}"
echo ""
echo "2. Start Conversation Service (if not running):"
echo "   ${YELLOW}source venv/bin/activate${NC}"
echo "   ${YELLOW}python -m services.conversation${NC}"
echo ""
echo "3. Run DTMF API tests:"
echo "   ${YELLOW}python test_dtmf_api.py${NC}"
echo ""
echo "4. Run comprehensive diagnostic:"
echo "   ${YELLOW}python diagnose_dtmf.py${NC}"
echo ""
