# ARIES Crash Recovery System — Backend

This directory contains the Python backend foundation for the ARIES-inspired crash recovery database internals project.

## Requirements

- Python 3.11+
- Virtual environment (`venv`)

## Setup

1. Create a virtual environment:
   ```bash
   python -m venv .venv
   ```

2. Activate the virtual environment:
   - **Windows (PowerShell):**
     ```powershell
     .venv\Scripts\Activate.ps1
     ```
   - **Windows (Command Prompt):**
     ```cmd
     .venv\Scripts\activate.bat
     ```
   - **Linux / macOS:**
     ```bash
     source .venv/bin/activate
     ```

3. Install dependencies:
   ```bash
   pip install -r requirements.txt
   ```

## Running the Server

Start the FastAPI development server:
```bash
uvicorn app.main:app --reload --port 8000
```

The API will be accessible at:
- Root: `http://localhost:8000/`
- Health Check: `http://localhost:8000/api/health`
- Swagger Docs: `http://localhost:8000/docs`
- ReDoc: `http://localhost:8000/redoc`

## Running Tests

Execute the test suite using `pytest`:
```bash
pytest
```
