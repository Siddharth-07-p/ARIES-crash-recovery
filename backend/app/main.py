"""FastAPI application entry point for ARIES Crash Recovery System."""

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

app = FastAPI(
    title="ARIES Crash Recovery System API",
    description="Backend API foundation for educational ARIES-inspired transactional crash recovery engine",
    version="0.1.0",
)

# CORS configuration for local frontend development
origins = [
    "http://localhost:5173",
    "http://127.0.0.1:5173",
    "http://localhost:3000",
    "http://127.0.0.1:3000",
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/")
def root():
    """Root endpoint for API discovery."""
    return {
        "service": "ARIES Crash Recovery System",
        "status": "online",
        "docs_url": "/docs",
        "health_url": "/api/health",
    }


@app.get("/api/health")
def health_check():
    """Health check endpoint for status monitoring and frontend connectivity verification."""
    return {
        "status": "ok",
        "service": "ARIES Crash Recovery System",
        "version": "0.1.0",
        "message": "ARIES backend foundation is operational",
    }
