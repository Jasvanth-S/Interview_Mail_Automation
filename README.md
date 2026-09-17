# Gateway Interview Mailer

Streamlit + Gmail SMTP automation for interview scheduling emails.

## Files

- `app.py` — main Streamlit application
- `template.html` — responsive Gateway HTML email template with dynamic placeholders
- `.streamlit/secrets.toml` — Gmail accounts and SMTP defaults (never commit)
- `.streamlit/config.toml` — Streamlit runtime/theme configuration
- `requirements.txt` — dependency
- `.gitignore` — prevents credentials and logs from entering Git
- `logs/` — run-level CSV logs created automatically

## Run

```bash
python -m venv .venv
# Windows
.venv\\Scripts\\activate
# macOS/Linux
# source .venv/bin/activate

pip install -r requirements.txt
streamlit run app.py
```

## Gmail credentials

Use a Gmail App Password rather than your normal account password. The Google account must have 2-Step Verification enabled.

Configure accounts in `.streamlit/secrets.toml` with nested TOML tables. The UI lets you select a saved sender or choose **Manual Gmail + App Password**.

## Recipient input

Paste either:

```text
Candidate Name, candidate@example.com
Another Candidate, another@example.com
```

or upload a UTF-8 CSV with `name,email` columns.

## SMTP

The default is Gmail SMTP over implicit TLS on port 465. Port 587 is also supported by changing `smtp_port`; the app automatically uses STARTTLS for non-465 ports.

## Deliverability

This app uses multipart/alternative email, TLS, a plain-text fallback, valid RFC-style message headers and pacing between messages. These practices do not guarantee inbox placement. Domain authentication, sender reputation, recipient validity and complaint/bounce levels remain important.
