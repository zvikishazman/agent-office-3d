#!/bin/bash
cd "$(dirname "$0")"
echo "🚀 Starting Agent Office Server on http://localhost:8080"
echo "📊 Open this URL in your browser"
echo "Press Ctrl+C to stop"
python3 server.py
