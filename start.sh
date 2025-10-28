#!/bin/bash
echo "🚀 Starting Render deployment..."

# Load .env if exists
if [ -f .env ]; then
  echo "🔧 Loading environment variables from .env..."
  export $(grep -v '^#' .env | xargs)
else
  echo "⚠️ .env file not found!"
  exit 1
fi

# Show key environment values for debug
echo "✅ Environment Loaded:"
echo "IG_USERNAME: $IG_USERNAME"
echo "GROQ_API_KEY: ${GROQ_API_KEY:0:8}********"

# Run the bot
echo "🤖 Launching bot..."
python3 igbot.py
