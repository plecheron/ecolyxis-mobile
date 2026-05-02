# Ecolyxis — Sustainable AI Chat Platform

Build a complete Flask web application: a LLM chatbot platform called "Ecolyxis". The company uses last-generation hardware (Tesla P40 GPUs) with modern optimizations and green power generation to make AI sustainable and eco-friendly.

## Tech Stack
- Flask + SQLAlchemy + Flask-Login
- SQLite database (in instance/)
- Jinja2 templates + HTMX for dynamic bits
- SSE streaming for chat responses
- Gunicorn-ready (but Flask dev server for now)
- Python venv already exists at ./venv/ with Flask installed

## LLM API
- Endpoint: http://127.0.0.1:8081/v1/ (OpenAI-compatible, llama.cpp turboquant)
- Model: qwen3.6-35b-a3b
- Stream responses via SSE
- System prompt: "You are Ecolyxis AI, a helpful, knowledgeable assistant. You are powered by sustainable computing — recycled hardware running on green energy."

## Database Models

### User
- id (int, PK)
- username (str, unique)
- email (str, unique)  
- password_hash (str)
- created_at (datetime)
- last_login (datetime)

### Thread
- id (int, PK)
- user_id (FK → User)
- title (str, default "New Chat")
- created_at (datetime)
- updated_at (datetime)

### Message
- id (int, PK)
- thread_id (FK → Thread)
- role (str: "user" / "assistant")
- content (text)
- tokens_used (int, nullable)
- created_at (datetime)

## Routes

### Public
- GET / → Landing page (hero, features, CTA to sign up)
- GET /signup → Sign up form
- POST /signup → Create account (validate, hash password, redirect to dashboard)
- GET /login → Login form
- POST /login → Authenticate (Flask-Login)
- POST /logout → Logout

### Authenticated
- GET /dashboard → Thread list + "New Chat" button
- POST /threads → Create new thread (redirect to chat)
- DELETE /threads/<id> → Delete thread + messages (HTMX)
- GET /chat/<id> → Chat view for a thread
- POST /chat/<id>/message → Send message, stream LLM response via SSE

## Pages Design

### Landing (/)
- Hero section: "Sustainable AI. No Compromises." with a leaf/circuit-board motif
- 3 feature cards: "Recycled Hardware" (Tesla P40 GPUs), "Green Energy" (100% renewable), "Modern Optimization" (turboquant inference)
- CTA button: "Get Started — It's Free"
- Clean footer with "Powered by sustainable computing"

### Signup (/signup)
- Username, email, password, confirm password
- Redirect to dashboard on success

### Login (/login)
- Email/username + password
- Redirect to dashboard on success

### Dashboard (/dashboard)
- Top bar with "Ecolyxis" logo + username + logout
- Main area: list of chat threads (title, date, delete button)
- "New Chat" button prominently placed
- Empty state: "Start your first conversation with sustainable AI"

### Chat (/chat/<id>)
- Left sidebar: thread list (same as dashboard, collapsible on mobile)
- Main area: message history (bubbles, user right, assistant left)
- Bottom: input box + send button
- Streaming: tokens appear in real-time via SSE
- Auto-title: after first response, update thread title using first ~50 chars of user message

## Project Structure
```
Ecolyxis/
├── run.py                 # Entry point (python run.py)
├── config.py              # Config class
├── requirements.txt       # All deps
├── venv/                  # Already exists
├── app/
│   ├── __init__.py        # create_app() factory
│   ├── models.py          # All SQLAlchemy models
│   ├── auth.py            # Auth routes (blueprint)
│   ├── chat.py            # Chat routes (blueprint)
│   ├── dashboard.py       # Dashboard routes (blueprint)
│   ├── llm.py             # LLM API client (streaming)
│   └── templates/
│   ├── base.html
│   ├── landing.html
│   ├── auth/
│   │   ├── login.html
│   │   └── signup.html
│   ├── dashboard.html
│   └── chat.html
├── static/
│   ├── css/
│   │   └── style.css
│   └── js/
│       └── chat.js
└── instance/
    └── ecolyxis.db (auto-created)
```

## CSS / Theme
- Dark-ish theme with green accents (#2ecc71, #27ae60, #1a1a2e background)
- Clean, modern, minimal — think Linear meets eco-branding
- Responsive (works on mobile)
- Chat bubbles: user = green tinted right-aligned, assistant = dark card left-aligned
- Smooth animations for streaming text appearance

## Key Implementation Details
1. Use Flask blueprints for auth, dashboard, chat
2. Flask-Login for session management
3. Werkzeug password hashing
4. SSE streaming: POST to /chat/<id>/message returns text/event-stream
5. LLM client: use `requests` with stream=True to llama.cpp, forward chunks as SSE
6. Send last 20 messages as context (or less if thread is new)
7. Auto-create DB tables on first run (db.create_all())
8. The app should run on port 80 (host 0.0.0.0)

## Requirements to install in venv
flask, flask-sqlalchemy, flask-login, werkzeug, requests, gunicorn

Build the complete app. All files. Make it work end-to-end. Install deps in the existing venv at ./venv/. Run db init on startup. The entry point should be `venv/bin/python run.py` serving on 0.0.0.0:80.

When completely finished, run this command to notify me:
openclaw system event --text "Done: Built Ecolyxis Flask chat app with auth, dashboard, streaming chat" --mode now
