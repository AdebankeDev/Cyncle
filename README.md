# Cyncle
python -m venv .venv ; .venv\Scripts\Activate.ps1   (or: uv venv)
pip install -r requirements.txt
copy .env.example .env   # then put your GROQ_API_KEY in .env
uvicorn server:app --reload   # http://localhost:8000

Each browser gets its own private database file in DATA_DIR (default ./data).
Deploy with the Dockerfile; mount a persistent disk at DATA_DIR and CHECKPOINT_DB.
