"""
Production entrypoint: `python -m webapp`.

Binds to all interfaces on $PORT (Railway sets it; 8000 locally) -- read
here rather than in the start command, so it works whether or not the
host runs that command through a shell that expands $PORT.
"""

import os

import uvicorn

if __name__ == "__main__":
    uvicorn.run("webapp.main:app", host="0.0.0.0", port=int(os.environ.get("PORT", "8000")))
