# ARIES Crash Recovery System — Frontend Dashboard

This directory contains the React + TypeScript frontend dashboard for monitoring the ARIES crash recovery engine.

## Tech Stack

- React 18
- TypeScript
- Vite
- Lucide React (Icons)
- Modern Glassmorphic CSS3

## Setup & Running

1. Install dependencies:
   ```bash
   npm install
   ```

2. Start development server:
   ```bash
   npm run dev
   ```
   The dashboard will be served at `http://localhost:5173`.

3. Build for production:
   ```bash
   npm run build
   ```

## Development Proxy

The Vite dev server is pre-configured to proxy `/api/*` requests to the FastAPI backend running at `http://localhost:8000`.
