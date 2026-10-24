# Email Extractor (web)

Enter an email + app password + date range, get the senders (name, address, subject, date) and download CSV.

## Run locally
    python -m venv venv
    venv\Scripts\activate        # Windows  (Mac/Linux: source venv/bin/activate)
    pip install -r requirements.txt
    python app.py                # open http://127.0.0.1:5000

## Deploy (Render)
Build command: `pip install -r requirements.txt`
Start command: `gunicorn app:app --timeout 120 --workers 2`

Credentials are used for a single IMAP session and never stored or logged.
