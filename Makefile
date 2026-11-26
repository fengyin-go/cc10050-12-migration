.PHONY: install backend frontend verify verify-handoff

install:
	cd backend && python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
	cd frontend && npm ci

backend:
	cd backend && ./run.sh

frontend:
	cd frontend && npm run dev

verify:
	cd backend && .venv/bin/python -m app.verify

verify-handoff:
	cd backend && .venv/bin/python -I scripts/verify_handoff.py
