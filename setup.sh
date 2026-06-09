#!/bin/bash
# One-time setup script for LinkedIn Job Automation
set -e

echo ""
echo "📦  Installing Python dependencies..."
pip install -r requirements.txt

echo ""
echo "🌐  Installing Playwright Chromium browser..."
playwright install chromium

echo ""
echo "✅  Setup complete!"
echo ""
echo "Next steps:"
echo "  1. Edit config.yaml with your LinkedIn credentials and job preferences"
echo "  2. Run the bot:    python run_bot.py"
echo "  3. Run the portal: python portal/app.py"
echo ""
