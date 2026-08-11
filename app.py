import os
import re
import json
import base64
import sqlite3
import logging
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.base import MIMEBase
from email import encoders
from pathlib import Path
from datetime import datetime, date, timezone
from functools import wraps

import requests
import stripe
import anthropic
from requests.auth import HTTPBasicAuth
from flask import (
    Flask, g, render_template, request, redirect, url_for, flash, session, send_file,
    send_from_directory
)
from werkzeug.security import generate_password_hash, check_password_hash
from apscheduler.schedulers.background import BackgroundScheduler

import gmail_poller
import fax_poller

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get('STORAGE_DIR', BASE_DIR)) / 'data'
DATA_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = DATA_DIR / 'callback_manager.db'
DOCS_DIR = DATA_DIR / 'documents'
DOCS_DIR.mkdir(parents=True, exist_ok=True)
REFERRAL_DIR = DATA_DIR / 'referral_attachments'
REFERRAL_DIR.mkdir(parents=True, exist_ok=True)
FAX_DIR = DATA_DIR / 'faxes'
FAX_DIR.mkdir(parents=True, exist_ok=True)

HOSPITALS = ['Dee Why Endoscopy', 'Mater Hospital', 'East Sydney Private Hospital']
DOC_TYPES = [
    ('colonoscopy_prep', 'Colonoscopy Prep'),
    ('gastroscopy_prep', 'Gastroscopy Prep'),
    ('ifc', 'Informed Consent Form (IFC)'),
]

# Standalone documents that aren't tied to a hospital's procedure paperwork -
# just the two consult/clinic room info sheets used for the welcome email.
EXTRA_DOCS = [
    ('nbh_consult_room_info', 'NBH Consultation Room Info'),
    ('mater_clinic_room_info', 'Mater Clinic Room Info'),
]


def _hospital_slug(name):
    return re.sub(r'[^a-z0-9]+', '_', name.lower()).strip('_')


def _to_e164(number):
    """Convert an Australian number to E.164 (+61...)."""
    digits = ''.join(filter(str.isdigit, str(number)))
    if digits.startswith('61'):
        return '+' + digits
    if digits.startswith('0'):
        return '+61' + digits[1:]
    if len(digits) == 9:
        return '+61' + digits
    return '+' + digits


def _send_sms(to_number, message, alpha_tag=None):
    """Send via ClickSend (same API/pattern as review_sender). Returns None on
    success, or an error message string on failure. Pass alpha_tag to override
    the default sender name (e.g. a patient-facing tag vs. the staff one)."""
    username = cfg('clicksend_username')
    api_key = cfg('clicksend_api_key')
    if not username or not api_key:
        return 'ClickSend is not set up yet — configure it under Settings first.'
    alpha_tag = alpha_tag or cfg('sms_alpha_tag') or 'CallbackMgr'

    payload = {'messages': [{
        'source': 'sdk',
        'from': alpha_tag,
        'body': message,
        'to': _to_e164(to_number),
    }]}
    try:
        resp = requests.post(
            'https://rest.clicksend.com/v3/sms/send',
            json=payload,
            auth=HTTPBasicAuth(username, api_key),
            timeout=15,
        )
        result = resp.json()
    except Exception as e:
        logger.exception('SMS send failed')
        return str(e)

    if result.get('response_code') != 'SUCCESS':
        return result.get('response_msg', str(result))
    return None


def _send_email(to_addr, subject, body, attachments=None):
    """Send via the practice's Gmail (SMTP, same app-password used for reading
    Solium mail). attachments is a list of (path, filename) pairs. Returns None
    on success, or an error message string on failure."""
    gmail_address = cfg('gmail_address')
    gmail_password = cfg('gmail_app_password')
    if not gmail_address or not gmail_password:
        return 'Gmail is not set up yet — configure it under Settings first.'

    msg = MIMEMultipart()
    msg['From'] = gmail_address
    msg['To'] = to_addr
    msg['Subject'] = subject
    msg.attach(MIMEText(body, 'plain'))

    for attachment_path, attachment_name in (attachments or []):
        with open(attachment_path, 'rb') as f:
            part = MIMEBase('application', 'pdf')
            part.set_payload(f.read())
        encoders.encode_base64(part)
        part.add_header('Content-Disposition', f'attachment; filename="{attachment_name}"')
        msg.attach(part)

    try:
        with smtplib.SMTP_SSL('smtp.gmail.com', 465, timeout=15) as smtp:
            smtp.login(gmail_address, gmail_password)
            smtp.send_message(msg)
    except Exception as e:
        logger.exception('Email send failed')
        return str(e)
    return None

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s %(message)s',
    handlers=[
        logging.StreamHandler(),  # so Railway's log viewer actually shows this
        logging.FileHandler(str(BASE_DIR / 'error.log')),
    ],
)
logger = logging.getLogger('callback_manager')

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', 'dev-secret-change-me')
app.config['TEMPLATES_AUTO_RELOAD'] = True

OUTCOME_LABELS = {
    'completed': 'Completed',
    'message_for_doctor': 'Forward to Dr Tu',  # legacy label, for archived tasks from before this changed
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    display_name TEXT NOT NULL,
    role TEXT NOT NULL DEFAULT 'actioneer',
    rate_per_task REAL,
    token_rate REAL,
    hourly_rate REAL,
    clinic_hourly_rate REAL,
    phone_number TEXT,
    is_doctor INTEGER NOT NULL DEFAULT 0,
    active INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    patient_name TEXT,
    phone_number TEXT,
    message_text TEXT,
    source_label TEXT,
    attachment_filename TEXT,
    intake_source TEXT,
    intake_kind TEXT,
    urgency TEXT,
    gmail_message_id TEXT UNIQUE,
    status TEXT NOT NULL DEFAULT 'open',
    claimed_by_id INTEGER,
    claimed_at TEXT,
    outcome_type TEXT,
    outcome_note TEXT,
    actioned_by_id INTEGER,
    actioned_at TEXT,
    pay_amount REAL,
    payroll_run_id INTEGER,
    doctor_handled_at TEXT,
    doctor_handled_by_id INTEGER,
    pending_question_for INTEGER
);

CREATE TABLE IF NOT EXISTS task_notes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id INTEGER NOT NULL,
    author_id INTEGER,
    created_at TEXT NOT NULL,
    note TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS config (
    key TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS payroll_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    period_start TEXT,
    period_end TEXT,
    total_amount REAL,
    paid_at TEXT
);

CREATE TABLE IF NOT EXISTS payments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id INTEGER,
    user_id INTEGER NOT NULL,
    amount REAL NOT NULL,
    minutes REAL,
    reason TEXT NOT NULL,
    created_at TEXT NOT NULL,
    payroll_run_id INTEGER
);

CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    author_id INTEGER,
    recipient_id INTEGER,
    created_at TEXT NOT NULL,
    body TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS email_templates (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    subject TEXT NOT NULL,
    body TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS fax_documents (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    received_at TEXT NOT NULL,
    from_number TEXT,
    pdf_filename TEXT NOT NULL,
    gmail_message_id TEXT UNIQUE,
    category TEXT,
    patient_name TEXT,
    notes TEXT,
    filed_by_id INTEGER,
    filed_at TEXT,
    linked_task_id INTEGER,
    ai_category TEXT,
    ai_patient_name TEXT,
    ai_suggested_action TEXT,
    ai_reasoning TEXT,
    ai_analyzed_at TEXT
);

CREATE TABLE IF NOT EXISTS paid_questions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    patient_name TEXT NOT NULL,
    patient_mobile TEXT NOT NULL,
    patient_email TEXT NOT NULL,
    clinic_seen TEXT NOT NULL,
    request_type TEXT NOT NULL,
    question_text TEXT NOT NULL,
    amount_cents INTEGER NOT NULL,
    stripe_checkout_session_id TEXT,
    stripe_payment_intent_id TEXT,
    status TEXT NOT NULL DEFAULT 'awaiting_payment',
    answer_text TEXT,
    decided_by_id INTEGER,
    decided_at TEXT,
    linked_task_id INTEGER
);
"""


def get_db():
    db = getattr(g, '_database', None)
    if db is None:
        db = g._database = sqlite3.connect(str(DB_PATH))
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA foreign_keys = ON')
    return db


@app.teardown_appcontext
def close_db(exception):
    db = getattr(g, '_database', None)
    if db is not None:
        db.close()


def _migrate(db):
    """Add columns introduced after tasks/users already existed in the wild."""
    existing_task_cols = {row['name'] for row in db.execute('PRAGMA table_info(tasks)').fetchall()}
    for col, decl in [('doctor_handled_at', 'TEXT'), ('doctor_handled_by_id', 'INTEGER'),
                       ('pending_question_for', 'INTEGER'), ('attachment_filename', 'TEXT'),
                       ('intake_source', 'TEXT'), ('intake_kind', 'TEXT'), ('urgency', 'TEXT')]:
        if col not in existing_task_cols:
            db.execute(f'ALTER TABLE tasks ADD COLUMN {col} {decl}')

    existing_user_cols = {row['name'] for row in db.execute('PRAGMA table_info(users)').fetchall()}
    if 'token_rate' not in existing_user_cols:
        db.execute('ALTER TABLE users ADD COLUMN token_rate REAL')
    if 'is_doctor' not in existing_user_cols:
        db.execute('ALTER TABLE users ADD COLUMN is_doctor INTEGER NOT NULL DEFAULT 0')
    if 'hourly_rate' not in existing_user_cols:
        db.execute('ALTER TABLE users ADD COLUMN hourly_rate REAL')
    if 'clinic_hourly_rate' not in existing_user_cols:
        db.execute('ALTER TABLE users ADD COLUMN clinic_hourly_rate REAL')
    if 'phone_number' not in existing_user_cols:
        db.execute('ALTER TABLE users ADD COLUMN phone_number TEXT')

    existing_payment_cols = {row['name'] for row in db.execute('PRAGMA table_info(payments)').fetchall()}
    if 'minutes' not in existing_payment_cols:
        db.execute('ALTER TABLE payments ADD COLUMN minutes REAL')

    existing_message_cols = {row['name'] for row in db.execute('PRAGMA table_info(messages)').fetchall()}
    if 'recipient_id' not in existing_message_cols:
        db.execute('ALTER TABLE messages ADD COLUMN recipient_id INTEGER')

    existing_fax_cols = {row['name'] for row in db.execute('PRAGMA table_info(fax_documents)').fetchall()}
    for col, decl in [('ai_category', 'TEXT'), ('ai_patient_name', 'TEXT'),
                       ('ai_suggested_action', 'TEXT'), ('ai_reasoning', 'TEXT'),
                       ('ai_analyzed_at', 'TEXT')]:
        if col not in existing_fax_cols:
            db.execute(f'ALTER TABLE fax_documents ADD COLUMN {col} {decl}')

    # task_id used to be required - relax it so clinic/phone time can be logged
    # without being tied to a specific callback task. SQLite can't drop a NOT
    # NULL constraint in place, so rebuild the table.
    task_id_notnull = next(
        (row['notnull'] for row in db.execute('PRAGMA table_info(payments)').fetchall()
         if row['name'] == 'task_id'),
        0,
    )
    if task_id_notnull:
        db.execute('''
            CREATE TABLE payments_new (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id INTEGER,
                user_id INTEGER NOT NULL,
                amount REAL NOT NULL,
                minutes REAL,
                reason TEXT NOT NULL,
                created_at TEXT NOT NULL,
                payroll_run_id INTEGER
            )
        ''')
        db.execute(
            'INSERT INTO payments_new (id, task_id, user_id, amount, minutes, reason, created_at, payroll_run_id) '
            'SELECT id, task_id, user_id, amount, minutes, reason, created_at, payroll_run_id FROM payments'
        )
        db.execute('DROP TABLE payments')
        db.execute('ALTER TABLE payments_new RENAME TO payments')

    # Warren predates the 'delegate' role - promote him if he's still 'actioneer'.
    db.execute("UPDATE users SET role = 'delegate' WHERE display_name = 'Warren' AND role = 'actioneer'")
    # Pay moved from per-task rates to an hourly rate - give Warren a default
    # if he doesn't have one yet (old per-task fields are no longer used).
    db.execute("UPDATE users SET hourly_rate = 30.0 WHERE display_name = 'Warren' AND hourly_rate IS NULL")
    db.execute("UPDATE users SET clinic_hourly_rate = 33.0 WHERE display_name = 'Warren' AND clinic_hourly_rate IS NULL")
    # Sally and Dr Tu should have the same interface - both full admins.
    db.execute("UPDATE users SET role = 'admin' WHERE display_name = 'Sally' AND role = 'actioneer'")
    # 'Forward to Dr Tu' needs to know which admin is actually the doctor.
    db.execute("UPDATE users SET is_doctor = 1 WHERE display_name = 'Dr Jeffrey Tu'")
    db.commit()


def init_db():
    db = sqlite3.connect(str(DB_PATH))
    db.row_factory = sqlite3.Row
    db.executescript(SCHEMA)
    db.commit()
    _migrate(db)

    existing = db.execute('SELECT COUNT(*) AS n FROM users').fetchone()['n']
    if existing == 0:
        seed = [
            ('Dr Jeffrey Tu', 'admin', None, None, 1),
            ('Sally', 'admin', None, None, 0),
            ('Warren', 'delegate', 30.0, 33.0, 0),
        ]
        for display_name, role, hourly_rate, clinic_hourly_rate, is_doctor in seed:
            db.execute(
                'INSERT INTO users (display_name, role, hourly_rate, clinic_hourly_rate, is_doctor) '
                'VALUES (?, ?, ?, ?, ?)',
                (display_name, role, hourly_rate, clinic_hourly_rate, is_doctor),
            )
        db.commit()
    db.close()


def cfg(key, default=None):
    row = get_db().execute('SELECT value FROM config WHERE key = ?', (key,)).fetchone()
    return row['value'] if row else default


def set_cfg(key, value):
    db = get_db()
    db.execute(
        'INSERT INTO config (key, value) VALUES (?, ?) '
        'ON CONFLICT(key) DO UPDATE SET value = excluded.value',
        (key, value),
    )
    db.commit()


def current_user():
    uid = session.get('user_id')
    if not uid:
        return None
    return get_db().execute('SELECT * FROM users WHERE id = ?', (uid,)).fetchone()


# 'admin'/'actioneer' see the full incoming queue and can assign work to a
# 'delegate' (Warren) - a delegate only ever sees tasks assigned to them.
FULL_ACCESS_ROLES = ('admin', 'actioneer')


def _can_manage_task(task):
    if session.get('role') in FULL_ACCESS_ROLES:
        return True
    return task['claimed_by_id'] == session.get('user_id')


def _parse_minutes(user, raw):
    """Returns (minutes_or_None, error_message_or_None). Required if the user
    is on an hourly rate, optional (and ignored) otherwise."""
    raw = (raw or '').strip()
    if not raw:
        if user['hourly_rate']:
            return None, "Log how many minutes you spent - that's what you get paid on."
        return None, None
    try:
        return float(raw), None
    except ValueError:
        return None, 'Minutes needs to be a number.'


def _log_time(db, task_id, user, minutes, now):
    """Record active minutes spent, paid at the user's hourly rate (locked in
    at the rate in effect right now, so later rate changes don't rewrite
    history). No-op if they're not on an hourly rate."""
    if not minutes or not user['hourly_rate']:
        return
    amount = round(minutes / 60.0 * user['hourly_rate'], 2)
    db.execute(
        'INSERT INTO payments (task_id, user_id, amount, minutes, reason, created_at) '
        'VALUES (?, ?, ?, ?, ?, ?)',
        (task_id, user['id'], amount, minutes, 'time', now),
    )


def _log_standalone_time(db, user, minutes, rate, reason, now):
    """Same idea as _log_time but not tied to a specific task - e.g. clinic
    time. rate is locked in at whatever's passed, same as _log_time."""
    if not minutes or not rate:
        return
    amount = round(minutes / 60.0 * rate, 2)
    db.execute(
        'INSERT INTO payments (task_id, user_id, amount, minutes, reason, created_at) '
        'VALUES (?, ?, ?, ?, ?, ?)',
        (None, user['id'], amount, minutes, reason, now),
    )


@app.before_request
def require_login():
    if request.endpoint is None or request.endpoint in (
        'login', 'static', 'service_worker', 'ask_form', 'ask_success', 'ask_webhook',
    ):
        return None
    if not session.get('user_id'):
        return redirect(url_for('login', next=request.path))
    return None


@app.route('/sw.js')
def service_worker():
    # Served from the root (not /static/) so its scope covers the whole app,
    # not just /static/ - a service worker's default scope is its own directory.
    return send_from_directory(app.static_folder, 'sw.js', mimetype='application/javascript')


def admin_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if session.get('role') != 'admin':
            flash('That page is admin-only.', 'warning')
            return redirect(url_for('queue'))
        return view(*args, **kwargs)
    return wrapped


@app.context_processor
def inject_globals():
    user = current_user()
    all_users = []
    unfiled_fax_count = 0
    pending_paid_qa_count = 0
    if user:
        all_users = get_db().execute(
            'SELECT id, display_name FROM users WHERE active = 1 ORDER BY display_name'
        ).fetchall()
        if user['role'] in FULL_ACCESS_ROLES:
            unfiled_fax_count = get_db().execute(
                "SELECT COUNT(*) AS n FROM fax_documents WHERE category IS NULL"
            ).fetchone()['n']
            pending_paid_qa_count = get_db().execute(
                "SELECT COUNT(*) AS n FROM paid_questions WHERE status = 'pending_review'"
            ).fetchone()['n']
    return {
        'current_user': user,
        'outcome_labels': OUTCOME_LABELS,
        'all_users': all_users,
        'endoscopy_manager_url': cfg('endoscopy_manager_url', '') if user else '',
        'practice_manager_url': cfg('practice_manager_url', '') if user else '',
        'unfiled_fax_count': unfiled_fax_count,
        'pending_paid_qa_count': pending_paid_qa_count,
    }


# ---------- auth ----------
# One shared password gets anyone past the door; a "who's acting" picker on
# login (and a quick switcher in the nav) is what attributes tasks to a
# specific person for the archive/payroll records.

@app.route('/login', methods=['GET', 'POST'])
def login():
    db = get_db()
    users = db.execute(
        'SELECT * FROM users WHERE active = 1 ORDER BY role, display_name'
    ).fetchall()
    first_run = not cfg('shared_password_hash')

    if request.method == 'POST':
        if first_run:
            password = request.form.get('password', '')
            confirm = request.form.get('password_confirm', '')
            if not password or password != confirm:
                flash('Passwords must match and not be empty.', 'danger')
            else:
                set_cfg('shared_password_hash', generate_password_hash(password))
                flash('Password set — log in below.', 'success')
            return redirect(url_for('login'))

        password = request.form.get('password', '')
        user_id = request.form.get('user_id')
        user = db.execute('SELECT * FROM users WHERE id = ? AND active = 1', (user_id,)).fetchone()
        if user and check_password_hash(cfg('shared_password_hash'), password):
            session['user_id'] = user['id']
            session['role'] = user['role']
            session['display_name'] = user['display_name']
            return redirect(request.args.get('next') or url_for('queue'))
        flash('Incorrect password.', 'danger')

    return render_template('login.html', first_run=first_run, users=users)


@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login'))


@app.route('/switch-user', methods=['POST'])
def switch_user():
    user_id = request.form.get('user_id')
    user = get_db().execute('SELECT * FROM users WHERE id = ? AND active = 1', (user_id,)).fetchone()
    if user:
        session['user_id'] = user['id']
        session['role'] = user['role']
        session['display_name'] = user['display_name']
    return redirect(request.referrer or url_for('queue'))


# ---------- messages (general noticeboard, not tied to a task) ----------

@app.route('/messages', methods=['GET', 'POST'])
def messages_page():
    db = get_db()
    if request.method == 'POST':
        body = request.form.get('body', '').strip()
        recipient_id = request.form.get('recipient_id', '').strip() or None
        if recipient_id and not db.execute(
            'SELECT 1 FROM users WHERE id = ? AND active = 1', (recipient_id,)
        ).fetchone():
            recipient_id = None
        if body:
            db.execute(
                'INSERT INTO messages (author_id, recipient_id, created_at, body) VALUES (?, ?, ?, ?)',
                (session['user_id'], recipient_id, datetime.now(timezone.utc).isoformat(), body),
            )
            db.commit()
            flash('Message sent.' if recipient_id else 'Message posted.', 'success')
        return redirect(url_for('messages_page'))

    # Everyone sees public posts; direct messages are only visible to the
    # sender and the recipient - not a broadcast, so nobody else's DMs show up.
    rows = db.execute(
        "SELECT m.*, u.display_name AS author_name, r.display_name AS recipient_name FROM messages m "
        "LEFT JOIN users u ON u.id = m.author_id "
        "LEFT JOIN users r ON r.id = m.recipient_id "
        "WHERE m.recipient_id IS NULL OR m.recipient_id = ? OR m.author_id = ? "
        "ORDER BY m.created_at DESC LIMIT 200",
        (session['user_id'], session['user_id']),
    ).fetchall()
    recipients = db.execute(
        "SELECT id, display_name FROM users WHERE active = 1 AND id != ? ORDER BY display_name",
        (session['user_id'],),
    ).fetchall()
    return render_template('messages.html', messages=rows, recipients=recipients)


@app.route('/messages/<int:message_id>/delete', methods=['POST'])
def delete_message(message_id):
    db = get_db()
    message = db.execute('SELECT * FROM messages WHERE id = ?', (message_id,)).fetchone()
    if not message:
        flash('Message not found.', 'warning')
        return redirect(url_for('messages_page'))
    uid = session.get('user_id')
    if (session.get('role') not in FULL_ACCESS_ROLES
            and message['author_id'] != uid and message['recipient_id'] != uid):
        flash('You can only delete your own messages.', 'warning')
        return redirect(url_for('messages_page'))
    db.execute('DELETE FROM messages WHERE id = ?', (message_id,))
    db.commit()
    flash('Message deleted.', 'success')
    return redirect(url_for('messages_page'))


# ---------- document library (prep sheets + IFC per hospital) ----------

@app.route('/documents')
def documents_page():
    grid = []
    for hosp in HOSPITALS:
        slug = _hospital_slug(hosp)
        docs = []
        for key, label in DOC_TYPES:
            docs.append({
                'key': key,
                'label': label,
                'exists': (DOCS_DIR / slug / f'{key}.pdf').exists(),
            })
        grid.append({'hospital': hosp, 'slug': slug, 'docs': docs})
    extra = [
        {'key': key, 'label': label, 'exists': (DOCS_DIR / f'{key}.pdf').exists()}
        for key, label in EXTRA_DOCS
    ]
    return render_template(
        'documents.html', grid=grid, doc_types=DOC_TYPES, extra=extra,
        can_upload=session.get('role') in FULL_ACCESS_ROLES,
    )


@app.route('/documents/upload', methods=['POST'])
def upload_document():
    if session.get('role') not in FULL_ACCESS_ROLES:
        flash('Only Dr Tu or Sally can upload documents.', 'warning')
        return redirect(url_for('documents_page'))
    hospital = request.form.get('hospital', '')
    doc_key = request.form.get('doc_key', '')
    valid_keys = {k for k, _ in DOC_TYPES}
    f = request.files.get('file')
    if hospital not in HOSPITALS or doc_key not in valid_keys or not f or not f.filename:
        flash('Choose a hospital, document type, and PDF file.', 'warning')
        return redirect(url_for('documents_page'))
    folder = DOCS_DIR / _hospital_slug(hospital)
    folder.mkdir(parents=True, exist_ok=True)
    f.save(str(folder / f'{doc_key}.pdf'))
    flash(f'Uploaded {dict(DOC_TYPES).get(doc_key, doc_key)} for {hospital}.', 'success')
    return redirect(url_for('documents_page'))


@app.route('/documents/upload-extra', methods=['POST'])
def upload_extra_document():
    if session.get('role') not in FULL_ACCESS_ROLES:
        flash('Only Dr Tu or Sally can upload documents.', 'warning')
        return redirect(url_for('documents_page'))
    doc_key = request.form.get('doc_key', '')
    valid_keys = {k for k, _ in EXTRA_DOCS}
    f = request.files.get('file')
    if doc_key not in valid_keys or not f or not f.filename:
        flash('Choose a document type and PDF file.', 'warning')
        return redirect(url_for('documents_page'))
    f.save(str(DOCS_DIR / f'{doc_key}.pdf'))
    flash(f'Uploaded {dict(EXTRA_DOCS).get(doc_key, doc_key)}.', 'success')
    return redirect(url_for('documents_page'))


# ---------- booking checklist (quick reference for new-patient calls) ----------

@app.route('/checklist')
def checklist_page():
    return render_template('checklist.html')


# ---------- stats (counts by how a task came in) ----------

INTAKE_LABELS = {
    ('solium', 'callback_request'): 'Solium — callback requests',
    ('solium', 'appointment_new'): 'Solium — new appointments',
    ('solium', 'appointment_followup'): 'Solium — follow-up appointments',
    ('halaxy', 'appointment_new'): 'Halaxy — new appointments',
    ('halaxy', 'appointment_followup'): 'Halaxy — follow-up appointments',
}


@app.route('/stats')
@admin_required
def stats_page():
    db = get_db()

    def _counts(since=None):
        query = "SELECT intake_source, intake_kind, COUNT(*) AS n FROM tasks"
        params = []
        if since:
            query += " WHERE created_at >= ?"
            params.append(since)
        query += " GROUP BY intake_source, intake_kind"
        rows = db.execute(query, params).fetchall()
        breakdown = []
        other = 0
        for r in rows:
            key = (r['intake_source'], r['intake_kind'])
            if key in INTAKE_LABELS:
                breakdown.append({'label': INTAKE_LABELS[key], 'count': r['n']})
            else:
                other += r['n']
        breakdown.sort(key=lambda x: -x['count'])
        if other:
            breakdown.append({'label': 'Manual / referral drop / other', 'count': other})
        total = sum(b['count'] for b in breakdown)
        return breakdown, total

    month_start = datetime.now(timezone.utc).replace(day=1, hour=0, minute=0, second=0, microsecond=0).isoformat()
    month_breakdown, month_total = _counts(since=month_start)
    all_breakdown, all_total = _counts()

    return render_template(
        'stats.html',
        month_breakdown=month_breakdown, month_total=month_total,
        all_breakdown=all_breakdown, all_total=all_total,
    )


@app.route('/documents/<hospital_slug>/<doc_key>')
def view_document(hospital_slug, doc_key):
    path = DOCS_DIR / hospital_slug / f'{doc_key}.pdf'
    if not path.exists():
        flash('Not uploaded yet.', 'warning')
        return redirect(url_for('documents_page'))
    return send_file(str(path), mimetype='application/pdf')


@app.route('/documents/extra/<doc_key>')
def view_extra_document(doc_key):
    path = DOCS_DIR / f'{doc_key}.pdf'
    if not path.exists():
        flash('Not uploaded yet.', 'warning')
        return redirect(url_for('documents_page'))
    return send_file(str(path), mimetype='application/pdf')


# ---------- task queue ----------

def _time_ago(iso_str):
    if not iso_str:
        return None
    then = datetime.fromisoformat(iso_str)
    delta = datetime.now(timezone.utc) - then
    mins = int(delta.total_seconds() // 60)
    if mins < 1:
        return 'just now'
    if mins < 60:
        return f'{mins}m ago'
    hours = mins // 60
    if hours < 24:
        return f'{hours}h ago'
    return f'{hours // 24}d ago'


URGENCY_LEVELS = {
    'red': 'Within hours',
    'yellow': 'Within 24 hours',
    'green': 'Can wait',
}
_URGENCY_ORDER_SQL = "CASE t.urgency WHEN 'red' THEN 0 WHEN 'yellow' THEN 1 WHEN 'green' THEN 2 ELSE 3 END"


@app.route('/')
def queue():
    db = get_db()
    is_delegate = session.get('role') not in FULL_ACCESS_ROLES
    valid_views = ('mine', 'questions') if is_delegate else ('untouched', 'mine', 'questions')
    view = request.args.get('view', 'mine' if is_delegate else 'untouched')
    if view not in valid_views:
        view = valid_views[0]

    if view == 'questions':
        rows = db.execute(
            "SELECT t.*, u.display_name AS claimed_by_name FROM tasks t "
            "LEFT JOIN users u ON u.id = t.claimed_by_id "
            f"WHERE t.pending_question_for = ? ORDER BY {_URGENCY_ORDER_SQL}, t.created_at ASC",
            (session['user_id'],),
        ).fetchall()
    elif view == 'mine':
        rows = db.execute(
            "SELECT t.*, u.display_name AS claimed_by_name FROM tasks t "
            "LEFT JOIN users u ON u.id = t.claimed_by_id "
            "WHERE t.status = 'claimed' AND t.claimed_by_id = ? "
            f"ORDER BY {_URGENCY_ORDER_SQL}, t.claimed_at ASC",
            (session['user_id'],),
        ).fetchall()
    else:  # untouched (full-access only)
        rows = db.execute(
            "SELECT t.*, u.display_name AS claimed_by_name FROM tasks t "
            "LEFT JOIN users u ON u.id = t.claimed_by_id "
            "WHERE t.status = 'open' "
            f"ORDER BY {_URGENCY_ORDER_SQL}, t.created_at ASC"
        ).fetchall()

    tasks = []
    for r in rows:
        task = dict(r)
        task['claimed_ago'] = _time_ago(r['claimed_at'])
        notes = db.execute(
            "SELECT tn.*, u.display_name AS author_name FROM task_notes tn "
            "LEFT JOIN users u ON u.id = tn.author_id "
            "WHERE tn.task_id = ? ORDER BY tn.created_at DESC",
            (r['id'],),
        ).fetchall()
        task['notes'] = notes
        tasks.append(task)

    sources = sorted({t['source_label'] for t in tasks if t['source_label']})
    untouched_count = mine_count = 0
    if is_delegate:
        # A delegate can only hand back to (or ask) a full-access person, never
        # sideways to another delegate.
        handoff_targets = db.execute(
            "SELECT id, display_name FROM users WHERE active = 1 AND role IN ('admin', 'actioneer') "
            "ORDER BY display_name"
        ).fetchall()
    else:
        handoff_targets = db.execute(
            "SELECT id, display_name FROM users WHERE active = 1 AND id != ? ORDER BY display_name",
            (session['user_id'],),
        ).fetchall()
        untouched_count = db.execute("SELECT COUNT(*) AS n FROM tasks WHERE status = 'open'").fetchone()['n']
        mine_count = db.execute(
            "SELECT COUNT(*) AS n FROM tasks WHERE status = 'claimed' AND claimed_by_id = ?",
            (session['user_id'],),
        ).fetchone()['n']
    questions_count = db.execute(
        "SELECT COUNT(*) AS n FROM tasks WHERE pending_question_for = ?", (session['user_id'],)
    ).fetchone()['n']

    notify_targets = []
    if not is_delegate:
        notify_targets = db.execute(
            "SELECT id, display_name FROM users WHERE active = 1 AND id != ? "
            "AND phone_number IS NOT NULL AND phone_number != '' ORDER BY display_name",
            (session['user_id'],),
        ).fetchall()

    return render_template(
        'queue.html', tasks=tasks, sources=sources, handoff_targets=handoff_targets,
        is_delegate=is_delegate, view=view, untouched_count=untouched_count, mine_count=mine_count,
        questions_count=questions_count, notify_targets=notify_targets, urgency_levels=URGENCY_LEVELS,
    )


@app.route('/task/<int:task_id>/claim', methods=['POST'])
def claim_task(task_id):
    if session.get('role') not in FULL_ACCESS_ROLES:
        flash('Only Dr Tu or Sally can pull tasks from the untouched pool — ask them to hand it to you.', 'warning')
        return redirect(url_for('queue'))
    db = get_db()
    task = db.execute('SELECT * FROM tasks WHERE id = ?', (task_id,)).fetchone()
    if not task or task['status'] != 'open':
        flash('That task is no longer available.', 'warning')
        return redirect(url_for('queue'))
    db.execute(
        "UPDATE tasks SET status = 'claimed', claimed_by_id = ?, claimed_at = ? WHERE id = ?",
        (session['user_id'], datetime.now(timezone.utc).isoformat(), task_id),
    )
    db.commit()
    return redirect(url_for('queue', view='mine'))


@app.route('/task/<int:task_id>/urgency', methods=['POST'])
def set_urgency(task_id):
    if session.get('role') not in FULL_ACCESS_ROLES:
        flash('Only Dr Tu or Sally can set urgency.', 'warning')
        return redirect(url_for('queue'))
    db = get_db()
    task = db.execute('SELECT * FROM tasks WHERE id = ?', (task_id,)).fetchone()
    urgency = request.form.get('urgency', '').strip()
    if not task or task['status'] == 'done':
        flash('That task is already resolved.', 'warning')
        return redirect(url_for('queue'))
    if urgency not in URGENCY_LEVELS:
        flash('Choose a valid urgency.', 'warning')
        return redirect(url_for('queue'))
    db.execute('UPDATE tasks SET urgency = ? WHERE id = ?', (urgency, task_id))
    db.commit()
    flash(f'Marked {URGENCY_LEVELS[urgency]}.', 'success')
    return redirect(url_for('queue'))


@app.route('/task/<int:task_id>/handoff', methods=['POST'])
def handoff_task(task_id):
    db = get_db()
    task = db.execute('SELECT * FROM tasks WHERE id = ?', (task_id,)).fetchone()
    if not task or task['status'] == 'done':
        flash('That task is already resolved.', 'warning')
        return redirect(url_for('queue'))
    if not _can_manage_task(task):
        flash('That task is not assigned to you.', 'warning')
        return redirect(url_for('queue'))

    user = current_user()
    target_id = request.form.get('target_id')
    instructions = request.form.get('instructions', '').strip()
    target = db.execute("SELECT * FROM users WHERE id = ? AND active = 1", (target_id,)).fetchone()
    if not target:
        flash('Choose who to hand this off to.', 'warning')
        return redirect(url_for('queue'))
    if user['role'] == 'delegate' and target['role'] not in FULL_ACCESS_ROLES:
        flash('You can only hand off to Dr Tu or Sally.', 'warning')
        return redirect(url_for('queue'))

    # Anti-ping-pong: a delegate must have logged at least one real attempt on
    # this task before handing it off - stops "claim, bounce, claim, bounce"
    # churning out paid minutes for zero actual effort.
    if user['role'] == 'delegate':
        logged = db.execute(
            'SELECT COUNT(*) AS n FROM task_notes WHERE task_id = ? AND author_id = ?',
            (task_id, user['id']),
        ).fetchone()['n']
        if not logged:
            flash('Log at least one attempt first (what you tried) before handing this off.', 'warning')
            return redirect(url_for('queue'))

    minutes_val, error = _parse_minutes(user, request.form.get('minutes'))
    if error:
        flash(error, 'warning')
        return redirect(url_for('queue'))

    urgency = request.form.get('urgency', '').strip()
    set_urgency_now = user['role'] in FULL_ACCESS_ROLES and urgency in URGENCY_LEVELS

    now = datetime.now(timezone.utc).isoformat()
    if set_urgency_now:
        db.execute(
            "UPDATE tasks SET status = 'claimed', claimed_by_id = ?, claimed_at = ?, "
            "pending_question_for = NULL, urgency = ? WHERE id = ?",
            (target['id'], now, urgency, task_id),
        )
    else:
        db.execute(
            "UPDATE tasks SET status = 'claimed', claimed_by_id = ?, claimed_at = ?, "
            "pending_question_for = NULL WHERE id = ?",
            (target['id'], now, task_id),
        )
    note = (f"Handed to {target['display_name']} by {user['display_name']}: {instructions}" if instructions
            else f"Handed to {target['display_name']} by {user['display_name']} — "
                 "no specific instructions, just call and find out what's needed.")
    if set_urgency_now:
        note += f' [{URGENCY_LEVELS[urgency]}]'
    db.execute(
        'INSERT INTO task_notes (task_id, author_id, created_at, note) VALUES (?, ?, ?, ?)',
        (task_id, user['id'], now, note),
    )
    _log_time(db, task_id, user, minutes_val, now)
    db.commit()
    paid_note = f' ({minutes_val:g} min logged)' if minutes_val else ''
    flash(f"Handed off to {target['display_name']}{paid_note}.", 'success')
    return redirect(url_for('queue'))


@app.route('/task/<int:task_id>/notify-urgent', methods=['POST'])
def notify_urgent(task_id):
    if session.get('role') not in FULL_ACCESS_ROLES:
        flash('Only Dr Tu or Sally can send urgent notifications.', 'warning')
        return redirect(url_for('queue'))
    db = get_db()
    task = db.execute('SELECT * FROM tasks WHERE id = ?', (task_id,)).fetchone()
    target_id = request.form.get('target_id', '').strip()
    note = request.form.get('note', '').strip()
    target = db.execute('SELECT * FROM users WHERE id = ? AND active = 1', (target_id,)).fetchone()
    if not task or task['status'] == 'done' or not target:
        flash('Could not send that notification.', 'warning')
        return redirect(url_for('queue'))
    if not target['phone_number']:
        flash(f'{target["display_name"]} has no phone number saved — add one under Users first.', 'warning')
        return redirect(url_for('queue'))

    # Deliberately no patient details in the text itself - SMS isn't secure.
    # It's just a "look now" ping; the real detail lives in the app.
    message = f"Urgent from {session.get('display_name')}: please check Callback Manager " \
              "for something that needs action in the next few hours."
    if note:
        message += f' ({note})'
    error = _send_sms(target['phone_number'], message)
    if error:
        flash(f'Could not send SMS: {error}', 'danger')
        return redirect(url_for('queue'))

    now = datetime.now(timezone.utc).isoformat()
    db.execute(
        'INSERT INTO task_notes (task_id, author_id, created_at, note) VALUES (?, ?, ?, ?)',
        (task_id, session['user_id'], now, f"Sent an urgent SMS to {target['display_name']}."),
    )
    db.commit()
    flash(f"Urgent SMS sent to {target['display_name']}.", 'success')
    return redirect(url_for('queue'))


@app.route('/task/<int:task_id>/confirm-appointment', methods=['GET', 'POST'])
def confirm_appointment(task_id):
    if session.get('role') not in FULL_ACCESS_ROLES:
        flash('Only Dr Tu or Sally can send appointment confirmations.', 'warning')
        return redirect(url_for('queue'))
    db = get_db()
    task = db.execute('SELECT * FROM tasks WHERE id = ?', (task_id,)).fetchone()
    if not task or task['intake_source'] != 'solium' or task['intake_kind'] != 'appointment_new':
        flash('That option is only for new Solium AI-booked appointments.', 'warning')
        return redirect(url_for('queue'))
    if not task['phone_number']:
        flash('No phone number on this task to text.', 'warning')
        return redirect(url_for('queue'))

    reply_email = cfg('patient_reply_email', '')
    when_match = re.search(r'\bon\s+(.+)$', task['message_text'] or '', re.IGNORECASE)
    appointment_when = when_match.group(1).strip().rstrip('.') if when_match else (task['message_text'] or '').strip()

    default_message = f"Welcome to Shore Gastroenterology. Your appointment is confirmed on {appointment_when}."
    if reply_email:
        default_message += (
            f' Please reply by email to {reply_email} with your name, DOB, and email address, '
            'and send your referral (PDF or screenshot photo) to the same address so we can '
            'also send you our clinic information.'
        )

    if request.method == 'POST':
        message = request.form.get('message', '').strip()
        if not message:
            flash('Message cannot be empty.', 'danger')
            return render_template('confirm_appointment.html', task=task, default_message=message)
        error = _send_sms(task['phone_number'], message, alpha_tag=cfg('patient_sms_alpha_tag', 'DrJeffreyTu'))
        if error:
            flash(f'Could not send SMS: {error}', 'danger')
            return render_template('confirm_appointment.html', task=task, default_message=message)
        now = datetime.now(timezone.utc).isoformat()
        db.execute(
            'INSERT INTO task_notes (task_id, author_id, created_at, note) VALUES (?, ?, ?, ?)',
            (task_id, session['user_id'], now, f'Sent appointment-confirmation SMS to patient: "{message}"'),
        )
        db.commit()
        flash('Confirmation SMS sent to patient.', 'success')
        return redirect(url_for('queue'))

    return render_template('confirm_appointment.html', task=task, default_message=default_message)


@app.route('/task/<int:task_id>/unclaim', methods=['POST'])
def unclaim_task(task_id):
    db = get_db()
    task = db.execute('SELECT * FROM tasks WHERE id = ?', (task_id,)).fetchone()
    if task and task['status'] == 'claimed' and _can_manage_task(task):
        db.execute(
            "UPDATE tasks SET status = 'open', claimed_by_id = NULL, claimed_at = NULL, "
            "pending_question_for = NULL WHERE id = ?",
            (task_id,),
        )
        db.execute(
            'INSERT INTO task_notes (task_id, author_id, created_at, note) VALUES (?, ?, ?, ?)',
            (task_id, session['user_id'], datetime.now(timezone.utc).isoformat(),
             f"Released back to the open queue by {session.get('display_name')}."),
        )
        db.commit()
    return redirect(url_for('queue'))


@app.route('/task/<int:task_id>/note', methods=['POST'])
def add_task_note(task_id):
    db = get_db()
    task = db.execute('SELECT * FROM tasks WHERE id = ?', (task_id,)).fetchone()
    note = request.form.get('note', '').strip()
    if not task or task['status'] == 'done':
        flash('That task is already resolved.', 'warning')
        return redirect(url_for('queue'))
    if not _can_manage_task(task):
        flash('Claim this task first before logging an attempt.', 'warning')
        return redirect(url_for('queue'))
    if not note:
        flash('Enter a note before logging it.', 'warning')
        return redirect(url_for('queue'))

    user = current_user()
    minutes_val, error = _parse_minutes(user, request.form.get('minutes'))
    if error:
        flash(error, 'warning')
        return redirect(url_for('queue'))

    now = datetime.now(timezone.utc).isoformat()
    db.execute(
        'INSERT INTO task_notes (task_id, author_id, created_at, note) VALUES (?, ?, ?, ?)',
        (task_id, user['id'], now, note),
    )
    _log_time(db, task_id, user, minutes_val, now)
    db.commit()
    time_note = f' ({minutes_val:g} min logged)' if minutes_val else ''
    flash(f'Attempt logged{time_note} — task stays claimed and active.', 'success')
    return redirect(url_for('queue'))


# Asking a question never changes who owns the task - it's purely a side
# channel so the owner can get input without palming the work off.

@app.route('/task/<int:task_id>/ask', methods=['POST'])
def ask_question(task_id):
    db = get_db()
    task = db.execute('SELECT * FROM tasks WHERE id = ?', (task_id,)).fetchone()
    target_id = request.form.get('target_id', '').strip()
    question = request.form.get('question', '').strip()
    if not task or task['status'] == 'done':
        flash('That task is already resolved.', 'warning')
        return redirect(url_for('queue'))
    if not _can_manage_task(task):
        flash('That task is not assigned to you.', 'warning')
        return redirect(url_for('queue'))
    if task['pending_question_for']:
        flash("There's already a question pending on this task — wait for that answer first.", 'warning')
        return redirect(url_for('queue'))
    target = db.execute('SELECT * FROM users WHERE id = ? AND active = 1', (target_id,)).fetchone()
    if not target or not question:
        flash('Choose who to ask and type your question.', 'warning')
        return redirect(url_for('queue'))

    user = current_user()
    now = datetime.now(timezone.utc).isoformat()
    db.execute('UPDATE tasks SET pending_question_for = ? WHERE id = ?', (target['id'], task_id))
    db.execute(
        'INSERT INTO task_notes (task_id, author_id, created_at, note) VALUES (?, ?, ?, ?)',
        (task_id, user['id'], now, f"Question for {target['display_name']}: {question}"),
    )
    db.commit()
    flash(f"Question sent to {target['display_name']} — still your task, they're just answering.", 'success')
    return redirect(url_for('queue'))


@app.route('/task/<int:task_id>/answer', methods=['POST'])
def answer_question(task_id):
    db = get_db()
    task = db.execute('SELECT * FROM tasks WHERE id = ?', (task_id,)).fetchone()
    answer = request.form.get('answer', '').strip()
    if not task or task['pending_question_for'] != session.get('user_id'):
        flash('No question waiting for you on that task.', 'warning')
        return redirect(url_for('queue', view='questions'))
    if not answer:
        flash('Type an answer first.', 'warning')
        return redirect(url_for('queue', view='questions'))

    user = current_user()
    now = datetime.now(timezone.utc).isoformat()
    db.execute('UPDATE tasks SET pending_question_for = NULL WHERE id = ?', (task_id,))
    db.execute(
        'INSERT INTO task_notes (task_id, author_id, created_at, note) VALUES (?, ?, ?, ?)',
        (task_id, user['id'], now, f"Answer from {user['display_name']}: {answer}"),
    )
    db.commit()
    flash('Answer sent back.', 'success')
    return redirect(url_for('queue', view='questions'))


@app.route('/task/<int:task_id>/resolve', methods=['GET', 'POST'])
def resolve_task(task_id):
    db = get_db()
    task = db.execute('SELECT * FROM tasks WHERE id = ?', (task_id,)).fetchone()
    if not task or task['status'] == 'done':
        flash('That task is already resolved.', 'warning')
        return redirect(url_for('queue'))
    if not _can_manage_task(task):
        flash('That task is not assigned to you.', 'warning')
        return redirect(url_for('queue'))
    notes = db.execute(
        "SELECT tn.*, u.display_name AS author_name FROM task_notes tn "
        "LEFT JOIN users u ON u.id = tn.author_id "
        "WHERE tn.task_id = ? ORDER BY tn.created_at ASC",
        (task_id,),
    ).fetchall()

    if request.method == 'POST':
        outcome_note = request.form.get('outcome_note', '').strip()
        user = current_user()
        minutes_val, error = _parse_minutes(user, request.form.get('minutes'))
        if error:
            flash(error, 'warning')
            return render_template('resolve.html', task=task, notes=notes)

        now = datetime.now(timezone.utc).isoformat()
        db.execute(
            "UPDATE tasks SET status = 'done', outcome_type = 'completed', outcome_note = ?, "
            "actioned_by_id = ?, actioned_at = ?, pending_question_for = NULL, "
            "claimed_by_id = COALESCE(claimed_by_id, ?) WHERE id = ?",
            (outcome_note, user['id'], now, user['id'], task_id),
        )
        _log_time(db, task_id, user, minutes_val, now)
        db.commit()
        flash('Task resolved and archived.', 'success')
        return redirect(url_for('queue'))

    return render_template('resolve.html', task=task, notes=notes)


@app.route('/task/<int:task_id>/quick-archive', methods=['POST'])
def quick_archive_task(task_id):
    """One-click archive for Dr Tu/Sally - skips the outcome-note form for
    tasks that don't need one on record. Not available to delegates."""
    if session.get('role') not in FULL_ACCESS_ROLES:
        flash('Only Dr Tu or Sally can quick-archive.', 'warning')
        return redirect(url_for('queue'))
    db = get_db()
    task = db.execute('SELECT * FROM tasks WHERE id = ?', (task_id,)).fetchone()
    if not task or task['status'] == 'done':
        flash('That task is already resolved.', 'warning')
        return redirect(url_for('queue'))
    user = current_user()
    now = datetime.now(timezone.utc).isoformat()
    db.execute(
        "UPDATE tasks SET status = 'done', outcome_type = 'completed', outcome_note = ?, "
        "actioned_by_id = ?, actioned_at = ?, pending_question_for = NULL, "
        "claimed_by_id = COALESCE(claimed_by_id, ?) WHERE id = ?",
        ('Quick-archived from the queue.', user['id'], now, user['id'], task_id),
    )
    db.commit()
    flash('Task archived.', 'success')
    return redirect(url_for('queue'))


@app.route('/task/<int:task_id>/sms', methods=['GET', 'POST'])
def text_patient(task_id):
    """One-way SMS to the patient - there's no inbound handling, so the
    message always needs to give them another way to actually reply."""
    db = get_db()
    task = db.execute('SELECT * FROM tasks WHERE id = ?', (task_id,)).fetchone()
    if not task or task['status'] == 'done':
        flash('That task is already resolved.', 'warning')
        return redirect(url_for('queue'))
    if not _can_manage_task(task):
        flash('That task is not assigned to you.', 'warning')
        return redirect(url_for('queue'))

    reply_email = cfg('patient_reply_email', '')
    default_message = ''
    if reply_email:
        default_message = f'Reply by email to {reply_email} if you need to get back to us.'

    if request.method == 'POST':
        phone_number = request.form.get('phone_number', '').strip() or task['phone_number']
        message = request.form.get('message', '').strip()
        if not phone_number:
            flash('Enter a phone number to text.', 'danger')
            return render_template('text_patient.html', task=task, default_message=message)
        if not message:
            flash('Message cannot be empty.', 'danger')
            return render_template('text_patient.html', task=task, default_message=message)
        error = _send_sms(phone_number, message, alpha_tag=cfg('patient_sms_alpha_tag', 'DrJeffreyTu'))
        if error:
            flash(f'Could not send SMS: {error}', 'danger')
            return render_template('text_patient.html', task=task, default_message=message)
        now = datetime.now(timezone.utc).isoformat()
        if not task['phone_number']:
            db.execute('UPDATE tasks SET phone_number = ? WHERE id = ?', (phone_number, task_id))
        db.execute(
            'INSERT INTO task_notes (task_id, author_id, created_at, note) VALUES (?, ?, ?, ?)',
            (task_id, session['user_id'], now, f'Sent SMS to patient: "{message}"'),
        )
        db.commit()
        flash('SMS sent to patient.', 'success')
        return redirect(url_for('queue'))

    return render_template('text_patient.html', task=task, default_message=default_message)


@app.route('/task/<int:task_id>/email', methods=['GET', 'POST'])
def email_task(task_id):
    db = get_db()
    task = db.execute('SELECT * FROM tasks WHERE id = ?', (task_id,)).fetchone()
    if not task or task['status'] == 'done':
        flash('That task is already resolved.', 'warning')
        return redirect(url_for('queue'))
    if not _can_manage_task(task):
        flash('That task is not assigned to you.', 'warning')
        return redirect(url_for('queue'))
    templates = [dict(t) for t in db.execute('SELECT * FROM email_templates ORDER BY name').fetchall()]

    if request.method == 'POST':
        to_addr = request.form.get('to', '').strip()
        subject = request.form.get('subject', '').strip()
        body = request.form.get('body', '').strip()
        selected = request.form.getlist('attachments')
        selected_extra = request.form.getlist('extra_attachments')

        if not to_addr or not subject or not body:
            flash('Fill in the recipient, subject, and message.', 'danger')
            return render_template('email_task.html', task=task, hospitals=HOSPITALS,
                                    doc_types=DOC_TYPES, extra_docs=EXTRA_DOCS, templates=templates)

        doc_labels = dict(DOC_TYPES)
        attachments = []
        missing = []
        for item in selected:
            hospital, _, doc_key = item.partition('|')
            doc_label = doc_labels.get(doc_key)
            if not hospital or not doc_label:
                continue
            display_name = f'{hospital} - {doc_label}.pdf'
            path = DOCS_DIR / _hospital_slug(hospital) / f'{doc_key}.pdf'
            if path.exists():
                attachments.append((path, display_name))
            else:
                missing.append(display_name)

        extra_labels = dict(EXTRA_DOCS)
        for doc_key in selected_extra:
            doc_label = extra_labels.get(doc_key)
            if not doc_label:
                continue
            display_name = f'{doc_label}.pdf'
            path = DOCS_DIR / f'{doc_key}.pdf'
            if path.exists():
                attachments.append((path, display_name))
            else:
                missing.append(display_name)

        error = _send_email(to_addr, subject, body, attachments)
        if error:
            flash(f'Could not send email: {error}', 'danger')
            return render_template('email_task.html', task=task, hospitals=HOSPITALS,
                                    doc_types=DOC_TYPES, extra_docs=EXTRA_DOCS, templates=templates)

        user = current_user()
        note = f'Emailed {to_addr} — "{subject}"'
        if attachments:
            note += f' (attached {", ".join(name for _, name in attachments)})'
        if missing:
            note += f' (not uploaded, skipped: {", ".join(missing)})'
        db.execute(
            'INSERT INTO task_notes (task_id, author_id, created_at, note) VALUES (?, ?, ?, ?)',
            (task_id, user['id'], datetime.now(timezone.utc).isoformat(), note),
        )
        db.commit()
        if missing:
            flash(f'Email sent, but skipped {len(missing)} document(s) not uploaded yet: {", ".join(missing)}', 'warning')
        else:
            flash('Email sent.', 'success')
        return redirect(url_for('queue'))

    return render_template('email_task.html', task=task, hospitals=HOSPITALS,
                            doc_types=DOC_TYPES, extra_docs=EXTRA_DOCS, templates=templates)


@app.route('/compose', methods=['GET', 'POST'])
def compose_email():
    """A general-purpose "email anyone" page - not tied to a callback task,
    for anything that doesn't fit the patient-callback workflow."""
    db = get_db()
    templates = [dict(t) for t in db.execute('SELECT * FROM email_templates ORDER BY name').fetchall()]

    if request.method == 'POST':
        to_addr = request.form.get('to', '').strip()
        subject = request.form.get('subject', '').strip()
        body = request.form.get('body', '').strip()
        selected = request.form.getlist('attachments')
        selected_extra = request.form.getlist('extra_attachments')

        if not to_addr or not subject or not body:
            flash('Fill in the recipient, subject, and message.', 'danger')
            return render_template('compose_email.html', hospitals=HOSPITALS,
                                    doc_types=DOC_TYPES, extra_docs=EXTRA_DOCS, templates=templates)

        doc_labels = dict(DOC_TYPES)
        attachments = []
        missing = []
        for item in selected:
            hospital, _, doc_key = item.partition('|')
            doc_label = doc_labels.get(doc_key)
            if not hospital or not doc_label:
                continue
            display_name = f'{hospital} - {doc_label}.pdf'
            path = DOCS_DIR / _hospital_slug(hospital) / f'{doc_key}.pdf'
            if path.exists():
                attachments.append((path, display_name))
            else:
                missing.append(display_name)

        extra_labels = dict(EXTRA_DOCS)
        for doc_key in selected_extra:
            doc_label = extra_labels.get(doc_key)
            if not doc_label:
                continue
            display_name = f'{doc_label}.pdf'
            path = DOCS_DIR / f'{doc_key}.pdf'
            if path.exists():
                attachments.append((path, display_name))
            else:
                missing.append(display_name)

        error = _send_email(to_addr, subject, body, attachments)
        if error:
            flash(f'Could not send email: {error}', 'danger')
            return render_template('compose_email.html', hospitals=HOSPITALS,
                                    doc_types=DOC_TYPES, extra_docs=EXTRA_DOCS, templates=templates)

        user = current_user()
        note = f'{user["display_name"]} emailed {to_addr} — "{subject}"'
        if attachments:
            note += f' (attached {", ".join(name for _, name in attachments)})'
        if missing:
            note += f' (not uploaded, skipped: {", ".join(missing)})'
        db.execute(
            'INSERT INTO messages (author_id, created_at, body) VALUES (?, ?, ?)',
            (user['id'], datetime.now(timezone.utc).isoformat(), note),
        )
        db.commit()
        if missing:
            flash(f'Email sent, but skipped {len(missing)} document(s) not uploaded yet: {", ".join(missing)}', 'warning')
        else:
            flash('Email sent.', 'success')
        return redirect(url_for('compose_email'))

    return render_template('compose_email.html', hospitals=HOSPITALS,
                            doc_types=DOC_TYPES, extra_docs=EXTRA_DOCS, templates=templates)


@app.route('/task/<int:task_id>/reopen', methods=['POST'])
def reopen_task(task_id):
    db = get_db()
    task = db.execute('SELECT * FROM tasks WHERE id = ?', (task_id,)).fetchone()
    if not task or task['status'] != 'done':
        flash('That task is not archived.', 'warning')
        return redirect(url_for('archive'))
    already_paid = db.execute(
        'SELECT COUNT(*) AS n FROM payments WHERE task_id = ? AND payroll_run_id IS NOT NULL',
        (task_id,),
    ).fetchone()['n']
    if already_paid:
        flash("Can't reopen — a payment for this has already been included in a paid payroll run.", 'danger')
        return redirect(url_for('archive'))
    if session.get('role') not in FULL_ACCESS_ROLES and task['actioned_by_id'] != session.get('user_id'):
        flash('Only the person who resolved this (or Dr Tu/Sally) can reopen it.', 'warning')
        return redirect(url_for('archive'))

    db.execute('DELETE FROM payments WHERE task_id = ? AND payroll_run_id IS NULL', (task_id,))
    db.execute(
        "UPDATE tasks SET status = 'open', claimed_by_id = NULL, claimed_at = NULL, "
        "outcome_type = NULL, outcome_note = NULL, actioned_by_id = NULL, actioned_at = NULL, "
        "doctor_handled_at = NULL, doctor_handled_by_id = NULL WHERE id = ?",
        (task_id,),
    )
    db.execute(
        'INSERT INTO task_notes (task_id, author_id, created_at, note) VALUES (?, ?, ?, ?)',
        (task_id, session['user_id'], datetime.now(timezone.utc).isoformat(),
         f"Reopened by {session.get('display_name')} — was previously resolved as "
         f"\"{OUTCOME_LABELS.get(task['outcome_type'], task['outcome_type'])}\"."),
    )
    db.commit()
    flash('Reopened — back in the open queue.', 'success')
    return redirect(url_for('queue'))


@app.route('/archive')
def archive():
    db = get_db()
    q = request.args.get('q', '').strip()
    query = (
        "SELECT t.*, u.display_name AS actioned_by_name, "
        "(SELECT COUNT(*) FROM payments p WHERE p.task_id = t.id AND p.payroll_run_id IS NOT NULL) AS paid_count "
        "FROM tasks t LEFT JOIN users u ON u.id = t.actioned_by_id WHERE t.status = 'done'"
    )
    params = []
    if session.get('role') not in FULL_ACCESS_ROLES:
        query += " AND t.actioned_by_id = ?"
        params.append(session['user_id'])
    if q:
        query += " AND (t.patient_name LIKE ? OR t.phone_number LIKE ? OR t.source_label LIKE ?)"
        like = f'%{q}%'
        params += [like, like, like]
    query += " ORDER BY t.actioned_at DESC LIMIT 200"
    tasks = db.execute(query, params).fetchall()
    return render_template('archive.html', tasks=tasks, q=q)


# ---------- time logging (not tied to a specific task) ----------

@app.route('/log-time', methods=['GET', 'POST'])
def log_time_page():
    user = current_user()
    if not user['hourly_rate'] and not user['clinic_hourly_rate']:
        flash("You're not set up for hourly pay, so there's nothing to log here.", 'warning')
        return redirect(url_for('queue'))
    db = get_db()

    if request.method == 'POST':
        kind = request.form.get('kind')
        raw_minutes = request.form.get('minutes', '').strip()
        try:
            minutes = float(raw_minutes)
        except ValueError:
            minutes = 0
        if minutes <= 0:
            flash('Enter how many minutes you spent.', 'danger')
            return redirect(url_for('log_time_page'))

        now = datetime.now(timezone.utc).isoformat()
        if kind == 'phone' and user['hourly_rate']:
            _log_standalone_time(db, user, minutes, user['hourly_rate'], 'phone_time', now)
            db.commit()
            flash(f'Logged {minutes:g} min of telephone task time.', 'success')
        elif kind == 'clinic' and user['clinic_hourly_rate']:
            _log_standalone_time(db, user, minutes, user['clinic_hourly_rate'], 'clinic_time', now)
            db.commit()
            flash(f'Logged {minutes:g} min of clinic time.', 'success')
        else:
            flash('Could not log that.', 'danger')
        return redirect(url_for('log_time_page'))

    totals = db.execute(
        "SELECT COALESCE(SUM(amount), 0) AS total, COALESCE(SUM(minutes), 0) AS minutes "
        "FROM payments WHERE user_id = ? AND payroll_run_id IS NULL",
        (user['id'],),
    ).fetchone()
    recent = db.execute(
        "SELECT * FROM payments WHERE user_id = ? ORDER BY created_at DESC LIMIT 15",
        (user['id'],),
    ).fetchall()
    return render_template('log_time.html', user=user, totals=totals, recent=recent)


# ---------- admin ----------
# (Needs Dr Tu used to be a separate page; forwarded tasks now just land in the
# doctor's own "My tasks" box like any other hand-off, so it's gone.)


@app.route('/admin/payroll', methods=['GET', 'POST'])
@admin_required
def payroll():
    db = get_db()
    if request.method == 'POST':
        action = request.form.get('action', 'mark_paid')

        if action == 'mark_paid':
            user_id = request.form.get('user_id')
            unpaid = db.execute(
                'SELECT * FROM payments WHERE user_id = ? AND payroll_run_id IS NULL',
                (user_id,),
            ).fetchall()
            if unpaid:
                total = sum(p['amount'] for p in unpaid)
                period_start = min(p['created_at'] for p in unpaid)
                period_end = max(p['created_at'] for p in unpaid)
                now = datetime.now(timezone.utc).isoformat()
                cur = db.execute(
                    'INSERT INTO payroll_runs (user_id, period_start, period_end, total_amount, paid_at) '
                    'VALUES (?, ?, ?, ?, ?)',
                    (user_id, period_start, period_end, total, now),
                )
                run_id = cur.lastrowid
                db.execute(
                    "UPDATE payments SET payroll_run_id = ? WHERE id IN ({})".format(
                        ','.join(str(p['id']) for p in unpaid)
                    ),
                    (run_id,),
                )
                db.commit()
                flash(f'Marked ${total:.2f} as paid.', 'success')

        elif action == 'adjust_payment':
            payment_id = request.form.get('payment_id')
            new_amount = request.form.get('amount', '').strip()
            try:
                new_amount = round(float(new_amount), 2)
            except ValueError:
                flash('Amount needs to be a number.', 'danger')
                return redirect(url_for('payroll'))
            payment = db.execute(
                'SELECT * FROM payments WHERE id = ? AND payroll_run_id IS NULL', (payment_id,)
            ).fetchone()
            if not payment:
                flash('Could not find that entry - it may already be paid.', 'warning')
            else:
                db.execute('UPDATE payments SET amount = ? WHERE id = ?', (new_amount, payment_id))
                db.commit()
                flash(f'Adjusted entry to ${new_amount:.2f}.', 'success')

        elif action == 'delete_payment':
            payment_id = request.form.get('payment_id')
            deleted = db.execute(
                'DELETE FROM payments WHERE id = ? AND payroll_run_id IS NULL', (payment_id,)
            )
            db.commit()
            if deleted.rowcount:
                flash('Entry removed.', 'success')
            else:
                flash('Could not find that entry - it may already be paid.', 'warning')

        elif action == 'update_rates':
            user_id = request.form.get('user_id')
            hourly_rate = request.form.get('hourly_rate', '').strip()
            clinic_hourly_rate = request.form.get('clinic_hourly_rate', '').strip()
            hourly_rate_val = float(hourly_rate) if hourly_rate else None
            clinic_hourly_rate_val = float(clinic_hourly_rate) if clinic_hourly_rate else None
            db.execute(
                'UPDATE users SET hourly_rate = ?, clinic_hourly_rate = ? WHERE id = ?',
                (hourly_rate_val, clinic_hourly_rate_val, user_id),
            )
            db.commit()
            flash('Rates updated.', 'success')

        return redirect(url_for('payroll'))

    payees = db.execute(
        "SELECT * FROM users WHERE (hourly_rate IS NOT NULL OR clinic_hourly_rate IS NOT NULL) AND active = 1"
    ).fetchall()
    totals = []
    for u in payees:
        row = db.execute(
            "SELECT COALESCE(SUM(amount), 0) AS total, COALESCE(SUM(minutes), 0) AS minutes, "
            "COUNT(*) AS n FROM payments WHERE user_id = ? AND payroll_run_id IS NULL",
            (u['id'],),
        ).fetchone()
        entries = db.execute(
            "SELECT * FROM payments WHERE user_id = ? AND payroll_run_id IS NULL ORDER BY created_at DESC",
            (u['id'],),
        ).fetchall()
        totals.append({
            'user': u,
            'total': row['total'],
            'minutes': row['minutes'],
            'count': row['n'],
            'entries': entries,
        })
    history = db.execute(
        "SELECT r.*, u.display_name FROM payroll_runs r JOIN users u ON u.id = r.user_id "
        "ORDER BY r.paid_at DESC LIMIT 50"
    ).fetchall()
    return render_template('payroll.html', totals=totals, history=history)


@app.route('/admin/users', methods=['GET', 'POST'])
@admin_required
def admin_users():
    db = get_db()
    if request.method == 'POST':
        action = request.form.get('action')
        if action == 'create':
            display_name = request.form.get('display_name', '').strip()
            role = request.form.get('role', 'actioneer')
            rate = request.form.get('hourly_rate', '').strip()
            rate_val = float(rate) if rate else None
            clinic_rate = request.form.get('clinic_hourly_rate', '').strip()
            clinic_rate_val = float(clinic_rate) if clinic_rate else None
            phone = request.form.get('phone_number', '').strip()
            if not display_name:
                flash('Display name is required.', 'danger')
            else:
                db.execute(
                    'INSERT INTO users (display_name, role, hourly_rate, clinic_hourly_rate, phone_number) '
                    'VALUES (?, ?, ?, ?, ?)',
                    (display_name, role, rate_val, clinic_rate_val, phone or None),
                )
                db.commit()
                flash(f'Added {display_name}.', 'success')
        elif action == 'update':
            user_id = request.form.get('user_id')
            role = request.form.get('role', 'actioneer')
            rate = request.form.get('hourly_rate', '').strip()
            rate_val = float(rate) if rate else None
            clinic_rate = request.form.get('clinic_hourly_rate', '').strip()
            clinic_rate_val = float(clinic_rate) if clinic_rate else None
            phone = request.form.get('phone_number', '').strip()
            active = 1 if request.form.get('active') == 'on' else 0
            is_doctor = 1 if request.form.get('is_doctor') == 'on' else 0
            if is_doctor:
                db.execute('UPDATE users SET is_doctor = 0 WHERE id != ?', (user_id,))
            db.execute(
                'UPDATE users SET role = ?, hourly_rate = ?, clinic_hourly_rate = ?, phone_number = ?, '
                'active = ?, is_doctor = ? WHERE id = ?',
                (role, rate_val, clinic_rate_val, phone or None, active, is_doctor, user_id),
            )
            db.commit()
            flash('User updated.', 'success')
        return redirect(url_for('admin_users'))

    users = db.execute('SELECT * FROM users ORDER BY role, display_name').fetchall()
    return render_template('admin_users.html', users=users)


@app.route('/admin/email-templates', methods=['GET', 'POST'])
@admin_required
def email_templates_page():
    db = get_db()
    if request.method == 'POST':
        action = request.form.get('action')
        if action == 'create':
            name = request.form.get('name', '').strip()
            subject = request.form.get('subject', '').strip()
            body = request.form.get('body', '').strip()
            if not name or not subject or not body:
                flash('Fill in a name, subject, and body.', 'danger')
            else:
                db.execute(
                    'INSERT INTO email_templates (name, subject, body, created_at) VALUES (?, ?, ?, ?)',
                    (name, subject, body, datetime.now(timezone.utc).isoformat()),
                )
                db.commit()
                flash(f'Added template "{name}".', 'success')
        elif action == 'delete':
            db.execute('DELETE FROM email_templates WHERE id = ?', (request.form.get('template_id'),))
            db.commit()
            flash('Template deleted.', 'success')
        return redirect(url_for('email_templates_page'))

    templates = db.execute('SELECT * FROM email_templates ORDER BY name').fetchall()
    return render_template('email_templates.html', templates=templates)


@app.route('/admin/settings', methods=['GET', 'POST'])
@admin_required
def admin_settings():
    if request.method == 'POST':
        action = request.form.get('action')
        if action == 'save':
            set_cfg('gmail_address', request.form.get('gmail_address', '').strip())
            new_password = request.form.get('gmail_app_password', '').strip()
            if new_password:
                set_cfg('gmail_app_password', new_password)
            set_cfg('poll_interval_seconds', request.form.get('poll_interval_seconds', '90').strip())
            set_cfg('gmail_folder', request.form.get('gmail_folder', 'INBOX').strip() or 'INBOX')
            set_cfg('endoscopy_manager_url', request.form.get('endoscopy_manager_url', '').strip())
            set_cfg('practice_manager_url', request.form.get('practice_manager_url', '').strip())
            set_cfg('clicksend_username', request.form.get('clicksend_username', '').strip())
            new_api_key = request.form.get('clicksend_api_key', '').strip()
            if new_api_key:
                set_cfg('clicksend_api_key', new_api_key)
            set_cfg('sms_alpha_tag', request.form.get('sms_alpha_tag', '').strip() or 'CallbackMgr')
            set_cfg('patient_reply_email', request.form.get('patient_reply_email', '').strip())
            set_cfg('patient_sms_alpha_tag', request.form.get('patient_sms_alpha_tag', '').strip() or 'DrJeffreyTu')
            flash('Settings saved.', 'success')
        elif action == 'poll_now':
            count, error = poll_gmail()
            if error:
                flash(f'Poll failed: {error}', 'danger')
            else:
                flash(f'Poll complete: {count} new task(s) imported.', 'success')
        elif action == 'save_fax':
            set_cfg('fax_gmail_address', request.form.get('fax_gmail_address', '').strip())
            new_fax_password = request.form.get('fax_gmail_app_password', '').strip()
            if new_fax_password:
                set_cfg('fax_gmail_app_password', new_fax_password)
            set_cfg('fax_gmail_folder', request.form.get('fax_gmail_folder', 'INBOX').strip() or 'INBOX')
            flash('Fax settings saved.', 'success')
        elif action == 'poll_fax_now':
            count, error = poll_fax_inbox()
            if error:
                flash(f'Fax poll failed: {error}', 'danger')
            else:
                flash(f'Fax poll complete: {count} new fax(es) imported.', 'success')
        elif action == 'save_stripe':
            fee = request.form.get('paid_qa_fee_aud', '').strip()
            try:
                fee_val = round(float(fee), 2) if fee else 25.0
            except ValueError:
                fee_val = 25.0
            set_cfg('paid_qa_fee_aud', str(fee_val))
            set_cfg('stripe_publishable_key', request.form.get('stripe_publishable_key', '').strip())
            new_secret_key = request.form.get('stripe_secret_key', '').strip()
            if new_secret_key:
                set_cfg('stripe_secret_key', new_secret_key)
            new_webhook_secret = request.form.get('stripe_webhook_secret', '').strip()
            if new_webhook_secret:
                set_cfg('stripe_webhook_secret', new_webhook_secret)
            flash('Paid Q&A settings saved.', 'success')
        elif action == 'save_ai':
            new_ai_key = request.form.get('anthropic_api_key', '').strip()
            if new_ai_key:
                set_cfg('anthropic_api_key', new_ai_key)
            flash('AI fax analysis settings saved.', 'success')
        elif action == 'change_shared_password':
            new_password = request.form.get('new_password', '')
            confirm = request.form.get('new_password_confirm', '')
            if not new_password or new_password != confirm:
                flash('Passwords must match and not be empty.', 'danger')
            else:
                set_cfg('shared_password_hash', generate_password_hash(new_password))
                flash('Shared login password updated.', 'success')
        elif action == 'send_test_email':
            test_to = request.form.get('test_email_to', '').strip()
            if not test_to:
                flash('Enter an address to send the test to.', 'warning')
            else:
                error = _send_email(
                    test_to,
                    'Callback Manager — test email',
                    'This is a test email from Callback Manager, sent to confirm outgoing '
                    'email is working correctly. Safe to ignore or delete.',
                )
                if error:
                    flash(f'Test email failed: {error}', 'danger')
                else:
                    flash(f'Test email sent to {test_to} — check that it arrived.', 'success')
        elif action == 'seed_training_data':
            added = seed_training_patients()
            if added:
                flash(f'Added {added} training patient(s) to the Untouched queue.', 'success')
            else:
                flash('Training patients are already there — nothing new to add.', 'warning')
        elif action == 'send_test_sms':
            test_to = request.form.get('test_sms_to', '').strip()
            if not test_to:
                flash('Enter a phone number to send the test to.', 'warning')
            else:
                error = _send_sms(
                    test_to,
                    'Callback Manager test SMS - if you got this, urgent notifications work. Ignore.',
                )
                if error:
                    flash(f'Test SMS failed: {error}', 'danger')
                else:
                    flash(f'Test SMS sent to {test_to} — check it arrived.', 'success')
        return redirect(url_for('admin_settings'))

    return render_template(
        'admin_settings.html',
        gmail_address=cfg('gmail_address', ''),
        has_password=bool(cfg('gmail_app_password')),
        poll_interval_seconds=cfg('poll_interval_seconds', '90'),
        gmail_folder=cfg('gmail_folder', 'INBOX'),
        endoscopy_manager_url=cfg('endoscopy_manager_url', ''),
        practice_manager_url=cfg('practice_manager_url', ''),
        clicksend_username=cfg('clicksend_username', ''),
        has_clicksend_key=bool(cfg('clicksend_api_key')),
        sms_alpha_tag=cfg('sms_alpha_tag', 'CallbackMgr'),
        patient_reply_email=cfg('patient_reply_email', ''),
        patient_sms_alpha_tag=cfg('patient_sms_alpha_tag', 'DrJeffreyTu'),
        fax_gmail_address=cfg('fax_gmail_address', ''),
        has_fax_password=bool(cfg('fax_gmail_app_password')),
        fax_gmail_folder=cfg('fax_gmail_folder', 'INBOX'),
        paid_qa_fee_aud=cfg('paid_qa_fee_aud', '25.00'),
        stripe_publishable_key=cfg('stripe_publishable_key', ''),
        has_stripe_secret_key=bool(cfg('stripe_secret_key')),
        has_stripe_webhook_secret=bool(cfg('stripe_webhook_secret')),
        has_anthropic_key=bool(cfg('anthropic_api_key')),
    )


# ---------- training data ----------

TRAINING_PATIENTS = [
    ('TEST Patient - Nausea After Prep', '+61400000401',
     'Patient took the bowel prep solution last night and is now experiencing nausea, dizziness, '
     'and stomach cramps. She is worried this is not normal and wants a clinician to call her back today.',
     'Mater Hospital', '<training-nausea@example.com>'),
    (None, '+61400000402',
     'AI phone system booked a Colonoscopy appointment for Thursday 20 August 2026 at Dee Why '
     'Endoscopy. No patient name or email captured. Please call to confirm the booking details, '
     "obtain the patient's email address, and send them the clinic confirmation letter.",
     'Dee Why Endoscopy', '<training-ai-booking@example.com>'),
    ('TEST Patient - Bowel Prep Request', '+61400000403',
     'Patient is asking for the bowel prep instructions to be emailed to her ahead of her '
     'upcoming colonoscopy. She does not have a copy and would like it sent as soon as possible.',
     'Dee Why Endoscopy', '<training-bowel-prep@example.com>'),
    ('TEST Patient - Reschedule Request', '+61400000404',
     'Patient needs to reschedule her colonoscopy next Tuesday - she has a work conflict and is '
     'asking for the next available date, preferably a Friday.',
     'Dee Why Endoscopy', '<training-reschedule@example.com>'),
    ('TEST Patient - Billing Question', '+61400000405',
     'Patient is asking how much out-of-pocket cost to expect for her upcoming colonoscopy and '
     'whether Medicare/private health will cover it.',
     'East Sydney Private Hospital', '<training-billing@example.com>'),
]


def seed_training_patients():
    """Insert the fixed set of training/demo patients into the Untouched queue.
    Safe to call repeatedly - duplicates are skipped via the unique message id."""
    db = get_db()
    now = datetime.now(timezone.utc).isoformat()
    added = 0
    for patient_name, phone, message, source_label, message_id in TRAINING_PATIENTS:
        try:
            db.execute(
                'INSERT INTO tasks (created_at, patient_name, phone_number, message_text, '
                'source_label, gmail_message_id, status) VALUES (?, ?, ?, ?, ?, ?, ?)',
                (now, patient_name, phone, message, source_label, message_id, 'open'),
            )
            added += 1
        except sqlite3.IntegrityError:
            continue
    db.commit()
    return added


# ---------- referral drop (manual task intake, for anything not from Solium) ----------

@app.route('/referral-drop', methods=['GET', 'POST'])
def referral_drop():
    if session.get('role') not in FULL_ACCESS_ROLES:
        flash('Only Dr Tu or Sally can drop a referral in.', 'warning')
        return redirect(url_for('queue'))
    db = get_db()

    if request.method == 'POST':
        patient_name = request.form.get('patient_name', '').strip()
        phone_number = request.form.get('phone_number', '').strip()
        message_text = request.form.get('message_text', '').strip()
        assign_to = request.form.get('assign_to', '').strip()
        f = request.files.get('attachment')

        if not message_text:
            flash('Add at least a short description of what this is.', 'danger')
            return redirect(url_for('referral_drop'))

        now = datetime.now(timezone.utc).isoformat()
        claimed_by_id = int(assign_to) if assign_to else None
        cur = db.execute(
            'INSERT INTO tasks (created_at, patient_name, phone_number, message_text, source_label, '
            'status, claimed_by_id, claimed_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
            (now, patient_name or None, phone_number or None, message_text, 'Referral drop',
             'claimed' if claimed_by_id else 'open', claimed_by_id, now if claimed_by_id else None),
        )
        task_id = cur.lastrowid

        if f and f.filename:
            ext = os.path.splitext(f.filename)[1] or '.pdf'
            filename = f'{task_id}{ext}'
            f.save(str(REFERRAL_DIR / filename))
            db.execute('UPDATE tasks SET attachment_filename = ? WHERE id = ?', (filename, task_id))

        db.commit()
        if claimed_by_id:
            flash('Referral dropped in and assigned.', 'success')
        else:
            flash('Referral dropped into the Untouched pool.', 'success')
        return redirect(url_for('referral_drop'))

    targets = db.execute(
        "SELECT id, display_name FROM users WHERE active = 1 ORDER BY display_name"
    ).fetchall()
    return render_template('referral_drop.html', targets=targets)


@app.route('/referral-drop/<int:task_id>/attachment')
def view_referral_attachment(task_id):
    db = get_db()
    task = db.execute('SELECT attachment_filename FROM tasks WHERE id = ?', (task_id,)).fetchone()
    if not task or not task['attachment_filename']:
        flash('No attachment for that referral.', 'warning')
        return redirect(url_for('queue'))
    path = REFERRAL_DIR / task['attachment_filename']
    if not path.exists():
        flash('Attachment file is missing.', 'warning')
        return redirect(url_for('queue'))
    return send_file(str(path))


# ---------- fax inbox (VOIP.net fax-to-email triage) ----------

FAX_CATEGORIES = {
    'pathology': 'Pathology',
    'radiology': 'Radiology',
    'referral': 'Referral',
}

FAX_AI_ACTION_LABELS = {
    'scope': 'Book scope directly',
    'consult': 'Suggest consult',
}


def _analyze_fax_with_ai(pdf_bytes):
    """Asks Claude to read the fax and suggest a filing category and patient
    name, and - for referrals - whether the letter reads as scope-appropriate
    (explicit endoscopy/colonoscopy/gastroscopy request, screening/surveillance/
    FOBT, or a symptom commonly booked straight to a procedure such as PR
    bleeding, dysphagia, or dyspepsia) or should go to a consult first
    (functional complaints like SIBO/IBS, a second-opinion request, or
    anything unclear). This is a suggestion for staff to act on, not an
    automatic booking."""
    api_key = cfg('anthropic_api_key')
    if not api_key:
        raise RuntimeError('Anthropic API key is not set - configure it under Settings first.')
    client = anthropic.Anthropic(api_key=api_key)
    pdf_b64 = base64.b64encode(pdf_bytes).decode('ascii')
    prompt = (
        'You are triaging an incoming fax for a gastroenterology practice (Dr Jeffrey Tu). '
        'Read the document and respond with ONLY a JSON object, no other text, with these keys:\n'
        '"category": one of "pathology", "radiology", "referral", or "other" if you truly cannot tell\n'
        '"patient_name": the patient\'s full name as written, or null if not found\n'
        '"suggested_action": only relevant if category is "referral" - one of "scope" or "consult", '
        'else null.\n'
        '  Use "scope" if the referring GP explicitly requests endoscopy, colonoscopy, or gastroscopy, '
        'OR the letter is about screening, surveillance, or a positive FOBT, OR the presenting symptom '
        'is one commonly booked straight to a procedure (e.g. PR bleeding, dysphagia, dyspepsia).\n'
        '  Use "consult" if it is a functional complaint (e.g. SIBO, IBS), a request for a second '
        'opinion, or you are not confident a direct procedure booking is appropriate.\n'
        '"reasoning": one short sentence explaining the category/action choice'
    )
    response = client.messages.create(
        model='claude-sonnet-5',
        max_tokens=500,
        messages=[{
            'role': 'user',
            'content': [
                {'type': 'document', 'source': {'type': 'base64', 'media_type': 'application/pdf', 'data': pdf_b64}},
                {'type': 'text', 'text': prompt},
            ],
        }],
    )
    text = ''.join(block.text for block in response.content if block.type == 'text')
    match = re.search(r'\{.*\}', text, re.DOTALL)
    if not match:
        raise RuntimeError('Could not parse the AI response.')
    data = json.loads(match.group(0))
    category = data.get('category') if data.get('category') in FAX_CATEGORIES else None
    suggested_action = data.get('suggested_action') if data.get('suggested_action') in FAX_AI_ACTION_LABELS else None
    return {
        'category': category,
        'patient_name': (data.get('patient_name') or '').strip() or None,
        'suggested_action': suggested_action,
        'reasoning': (data.get('reasoning') or '').strip() or None,
    }


@app.route('/fax-inbox/<int:fax_id>/analyze', methods=['POST'])
def analyze_fax(fax_id):
    if session.get('role') not in FULL_ACCESS_ROLES:
        flash('Only Dr Tu or Sally can analyse faxes.', 'warning')
        return redirect(url_for('queue'))
    db = get_db()
    fax = db.execute('SELECT * FROM fax_documents WHERE id = ?', (fax_id,)).fetchone()
    if not fax:
        flash('Fax not found.', 'warning')
        return redirect(url_for('fax_inbox_page'))
    pdf_path = FAX_DIR / fax['pdf_filename']
    if not pdf_path.exists():
        flash('Fax file is missing.', 'warning')
        return redirect(url_for('fax_inbox_page'))

    try:
        result = _analyze_fax_with_ai(pdf_path.read_bytes())
    except Exception as e:
        logger.exception('AI fax analysis failed')
        flash(f'AI analysis failed: {e}', 'danger')
        return redirect(url_for('fax_inbox_page'))

    now = datetime.now(timezone.utc).isoformat()
    db.execute(
        'UPDATE fax_documents SET ai_category = ?, ai_patient_name = ?, ai_suggested_action = ?, '
        'ai_reasoning = ?, ai_analyzed_at = ? WHERE id = ?',
        (result['category'], result['patient_name'], result['suggested_action'], result['reasoning'], now, fax_id),
    )
    db.commit()
    flash('AI analysis complete.', 'success')
    return redirect(url_for('fax_inbox_page'))


@app.route('/fax-inbox')
def fax_inbox_page():
    if session.get('role') not in FULL_ACCESS_ROLES:
        flash('Only Dr Tu or Sally can access the fax inbox.', 'warning')
        return redirect(url_for('queue'))
    db = get_db()
    unfiled = db.execute(
        "SELECT * FROM fax_documents WHERE category IS NULL ORDER BY received_at ASC"
    ).fetchall()
    targets = db.execute(
        "SELECT id, display_name FROM users WHERE active = 1 ORDER BY display_name"
    ).fetchall()
    return render_template('fax_inbox.html', faxes=unfiled, categories=FAX_CATEGORIES, targets=targets,
                            action_labels=FAX_AI_ACTION_LABELS, has_ai_key=bool(cfg('anthropic_api_key')))


@app.route('/fax-inbox/archive')
def fax_archive_page():
    if session.get('role') not in FULL_ACCESS_ROLES:
        flash('Only Dr Tu or Sally can access the fax archive.', 'warning')
        return redirect(url_for('queue'))
    db = get_db()
    q = request.args.get('q', '').strip()
    query = "SELECT * FROM fax_documents WHERE category IN ('pathology', 'radiology')"
    params = []
    if q:
        query += " AND patient_name LIKE ?"
        params.append(f'%{q}%')
    query += " ORDER BY filed_at DESC LIMIT 200"
    filed = db.execute(query, params).fetchall()
    return render_template('fax_archive.html', faxes=filed, categories=FAX_CATEGORIES, q=q)


@app.route('/fax-inbox/<int:fax_id>/view')
def view_fax(fax_id):
    if session.get('role') not in FULL_ACCESS_ROLES:
        flash('Only Dr Tu or Sally can access the fax inbox.', 'warning')
        return redirect(url_for('queue'))
    db = get_db()
    fax = db.execute('SELECT pdf_filename FROM fax_documents WHERE id = ?', (fax_id,)).fetchone()
    if not fax or not fax['pdf_filename']:
        flash('Fax not found.', 'warning')
        return redirect(url_for('fax_inbox_page'))
    path = FAX_DIR / fax['pdf_filename']
    if not path.exists():
        flash('Fax file is missing.', 'warning')
        return redirect(url_for('fax_inbox_page'))
    return send_file(str(path), mimetype='application/pdf')


@app.route('/fax-inbox/<int:fax_id>/file', methods=['POST'])
def file_fax(fax_id):
    if session.get('role') not in FULL_ACCESS_ROLES:
        flash('Only Dr Tu or Sally can file faxes.', 'warning')
        return redirect(url_for('queue'))
    db = get_db()
    fax = db.execute('SELECT * FROM fax_documents WHERE id = ?', (fax_id,)).fetchone()
    if not fax:
        flash('Fax not found.', 'warning')
        return redirect(url_for('fax_inbox_page'))

    category = request.form.get('category', '')
    patient_name = request.form.get('patient_name', '').strip()
    notes = request.form.get('notes', '').strip()
    if category not in FAX_CATEGORIES:
        flash('Choose a category.', 'danger')
        return redirect(url_for('fax_inbox_page'))

    now = datetime.now(timezone.utc).isoformat()
    linked_task_id = None

    if category == 'referral':
        phone_number = request.form.get('phone_number', '').strip()
        assign_to = request.form.get('assign_to', '').strip()
        claimed_by_id = int(assign_to) if assign_to else None
        message = f'Referral from fax (received from {fax["from_number"] or "unknown number"}).'
        if notes:
            message += f' {notes}'
        cur = db.execute(
            'INSERT INTO tasks (created_at, patient_name, phone_number, message_text, source_label, '
            'status, claimed_by_id, claimed_at, attachment_filename) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)',
            (now, patient_name or None, phone_number or None, message, 'Fax referral',
             'claimed' if claimed_by_id else 'open', claimed_by_id, now if claimed_by_id else None,
             fax['pdf_filename']),
        )
        linked_task_id = cur.lastrowid
        # The task views its attachment via REFERRAL_DIR, so copy the PDF there under the new task's id.
        task_filename = f'{linked_task_id}.pdf'
        (REFERRAL_DIR / task_filename).write_bytes((FAX_DIR / fax['pdf_filename']).read_bytes())
        db.execute('UPDATE tasks SET attachment_filename = ? WHERE id = ?', (task_filename, linked_task_id))

    db.execute(
        'UPDATE fax_documents SET category = ?, patient_name = ?, notes = ?, filed_by_id = ?, '
        'filed_at = ?, linked_task_id = ? WHERE id = ?',
        (category, patient_name or None, notes or None, session['user_id'], now, linked_task_id, fax_id),
    )
    db.commit()
    flash(f'Filed as {FAX_CATEGORIES[category]}.', 'success')
    return redirect(url_for('fax_inbox_page'))


# ---------- paid Q&A / script requests (public-facing, Stripe) ----------
# Public flow: patient fills /ask -> Stripe Checkout authorises (not charges) their
# card -> /ask/success confirms -> request sits pending_review for Dr Tu/Sally.
# Internal flow: /paid-qa/<id>/answer captures the hold (actually charges) once
# answered; /paid-qa/<id>/decline releases the hold with no charge and creates a
# normal callback task instead.

PAID_QA_CLINICS = ['Mater Hospital', 'Northern Beaches Hospital']
PAID_QA_TYPES = {'question': 'Clinical question', 'script': 'Script request'}


def _paid_qa_fee_cents():
    try:
        return max(50, int(round(float(cfg('paid_qa_fee_aud', '25.00')) * 100)))
    except ValueError:
        return 2500


@app.route('/ask', methods=['GET', 'POST'])
def ask_form():
    fee = cfg('paid_qa_fee_aud', '25.00')
    if request.method == 'POST':
        patient_name = request.form.get('patient_name', '').strip()
        patient_mobile = request.form.get('patient_mobile', '').strip()
        patient_email = request.form.get('patient_email', '').strip()
        clinic_seen = request.form.get('clinic_seen', '').strip()
        request_type = request.form.get('request_type', '').strip()
        question_text = request.form.get('question_text', '').strip()
        consent = request.form.get('consent') == 'on'

        errors = []
        if not patient_name or not patient_mobile or not patient_email:
            errors.append('Please fill in your name, mobile, and email.')
        if clinic_seen not in PAID_QA_CLINICS:
            errors.append('This service is only for existing Shore Gastroenterology patients seen '
                           'at Mater Hospital or Northern Beaches Hospital.')
        if request_type not in PAID_QA_TYPES:
            errors.append('Choose whether this is a question or a script request.')
        if not question_text:
            errors.append('Please enter your question or script request.')
        if not consent:
            errors.append('You need to agree to the fee before continuing.')
        if not cfg('stripe_secret_key'):
            errors.append('This service is not available right now — please call the practice instead.')

        if errors:
            for e in errors:
                flash(e, 'danger')
            return render_template('ask.html', fee=fee, clinics=PAID_QA_CLINICS, types=PAID_QA_TYPES, form=request.form)

        db = get_db()
        now = datetime.now(timezone.utc).isoformat()
        amount_cents = _paid_qa_fee_cents()
        cur = db.execute(
            'INSERT INTO paid_questions (created_at, patient_name, patient_mobile, patient_email, '
            'clinic_seen, request_type, question_text, amount_cents, status) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)',
            (now, patient_name, patient_mobile, patient_email, clinic_seen, request_type,
             question_text, amount_cents, 'awaiting_payment'),
        )
        pq_id = cur.lastrowid
        db.commit()

        stripe.api_key = cfg('stripe_secret_key')
        try:
            checkout_session = stripe.checkout.Session.create(
                mode='payment',
                payment_method_types=['card'],
                line_items=[{
                    'price_data': {
                        'currency': 'aud',
                        'product_data': {'name': f'Shore Gastroenterology — {PAID_QA_TYPES[request_type]}'},
                        'unit_amount': amount_cents,
                    },
                    'quantity': 1,
                }],
                payment_intent_data={'capture_method': 'manual'},
                customer_email=patient_email,
                success_url=url_for('ask_success', _external=True) + '?session_id={CHECKOUT_SESSION_ID}',
                cancel_url=url_for('ask_form', _external=True),
                metadata={'paid_question_id': str(pq_id)},
            )
        except Exception as e:
            logger.exception('Stripe checkout session creation failed')
            flash(f'Could not start payment: {e}', 'danger')
            return render_template('ask.html', fee=fee, clinics=PAID_QA_CLINICS, types=PAID_QA_TYPES, form=request.form)

        db.execute(
            'UPDATE paid_questions SET stripe_checkout_session_id = ? WHERE id = ?',
            (checkout_session.id, pq_id),
        )
        db.commit()
        return redirect(checkout_session.url, code=303)

    return render_template('ask.html', fee=fee, clinics=PAID_QA_CLINICS, types=PAID_QA_TYPES, form={})


def _confirm_paid_question_payment(checkout_session_id):
    """Looks up the Stripe session; if the card was successfully authorised
    (payment_intent status requires_capture), marks the matching paid_questions
    row pending_review. Idempotent — safe to call from both the success redirect
    and the webhook."""
    db = get_db()
    row = db.execute(
        'SELECT * FROM paid_questions WHERE stripe_checkout_session_id = ?', (checkout_session_id,)
    ).fetchone()
    if not row or row['status'] != 'awaiting_payment':
        return row
    stripe.api_key = cfg('stripe_secret_key')
    checkout_session = stripe.checkout.Session.retrieve(checkout_session_id, expand=['payment_intent'])
    payment_intent = checkout_session.payment_intent
    if payment_intent and payment_intent.status == 'requires_capture':
        db.execute(
            "UPDATE paid_questions SET status = 'pending_review', stripe_payment_intent_id = ? WHERE id = ?",
            (payment_intent.id, row['id']),
        )
        db.commit()
    return db.execute('SELECT * FROM paid_questions WHERE id = ?', (row['id'],)).fetchone()


@app.route('/ask/success')
def ask_success():
    checkout_session_id = request.args.get('session_id', '')
    row = None
    if checkout_session_id:
        try:
            row = _confirm_paid_question_payment(checkout_session_id)
        except Exception:
            logger.exception('Could not confirm paid question payment')
    return render_template('ask_success.html', confirmed=bool(row and row['status'] == 'pending_review'))


@app.route('/ask/webhook', methods=['POST'])
def ask_webhook():
    webhook_secret = cfg('stripe_webhook_secret')
    if not webhook_secret:
        # No signing secret configured yet — the /ask/success redirect is the
        # primary confirmation path, so just no-op rather than trust an
        # unverified payload.
        return ('', 200)
    payload = request.get_data()
    sig_header = request.headers.get('Stripe-Signature', '')
    try:
        event = stripe.Webhook.construct_event(payload, sig_header, webhook_secret)
    except Exception:
        logger.exception('Stripe webhook signature verification failed')
        return ('', 400)

    if event['type'] == 'checkout.session.completed':
        checkout_session_id = event['data']['object']['id']
        try:
            _confirm_paid_question_payment(checkout_session_id)
        except Exception:
            logger.exception('Could not confirm paid question payment from webhook')
    return ('', 200)


@app.route('/paid-qa')
def paid_qa_page():
    if session.get('role') not in FULL_ACCESS_ROLES:
        flash('Only Dr Tu or Sally can access Paid Q&A.', 'warning')
        return redirect(url_for('queue'))
    db = get_db()
    pending = db.execute(
        "SELECT * FROM paid_questions WHERE status = 'pending_review' ORDER BY created_at ASC"
    ).fetchall()
    history = db.execute(
        "SELECT * FROM paid_questions WHERE status IN ('answered', 'declined') "
        "ORDER BY decided_at DESC LIMIT 30"
    ).fetchall()
    return render_template('paid_qa.html', questions=pending, history=history, types=PAID_QA_TYPES)


@app.route('/paid-qa/<int:pq_id>/answer', methods=['GET', 'POST'])
def paid_qa_answer(pq_id):
    if session.get('role') not in FULL_ACCESS_ROLES:
        flash('Only Dr Tu or Sally can answer Paid Q&A.', 'warning')
        return redirect(url_for('queue'))
    db = get_db()
    pq = db.execute('SELECT * FROM paid_questions WHERE id = ?', (pq_id,)).fetchone()
    if not pq or pq['status'] != 'pending_review':
        flash('That request is not awaiting an answer.', 'warning')
        return redirect(url_for('paid_qa_page'))

    if request.method == 'POST':
        answer_text = request.form.get('answer_text', '').strip()
        if not answer_text:
            flash('Write an answer before sending.', 'danger')
            return render_template('paid_qa_answer.html', pq=pq, types=PAID_QA_TYPES)

        stripe.api_key = cfg('stripe_secret_key')
        try:
            stripe.PaymentIntent.capture(pq['stripe_payment_intent_id'])
        except Exception as e:
            logger.exception('Stripe capture failed')
            flash(f'Could not charge the patient: {e}', 'danger')
            return render_template('paid_qa_answer.html', pq=pq, types=PAID_QA_TYPES)

        now = datetime.now(timezone.utc).isoformat()
        db.execute(
            "UPDATE paid_questions SET status = 'answered', answer_text = ?, decided_by_id = ?, "
            "decided_at = ? WHERE id = ?",
            (answer_text, session['user_id'], now, pq_id),
        )
        db.commit()

        email_error = _send_email(
            pq['patient_email'],
            'Your question to Shore Gastroenterology',
            f"Hi {pq['patient_name']},\n\nThanks for your question:\n\n\"{pq['question_text']}\"\n\n"
            f"Dr Tu's answer:\n\n{answer_text}\n\nThis was charged at "
            f"${pq['amount_cents'] / 100:.2f} as agreed when you submitted your request.",
        )
        if email_error:
            flash(f'Charged and saved, but the email to the patient failed: {email_error}', 'warning')
        else:
            flash('Answer sent and payment captured.', 'success')
        return redirect(url_for('paid_qa_page'))

    return render_template('paid_qa_answer.html', pq=pq, types=PAID_QA_TYPES)


@app.route('/paid-qa/<int:pq_id>/decline', methods=['POST'])
def paid_qa_decline(pq_id):
    if session.get('role') not in FULL_ACCESS_ROLES:
        flash('Only Dr Tu or Sally can decline Paid Q&A.', 'warning')
        return redirect(url_for('queue'))
    db = get_db()
    pq = db.execute('SELECT * FROM paid_questions WHERE id = ?', (pq_id,)).fetchone()
    if not pq or pq['status'] != 'pending_review':
        flash('That request is not awaiting a decision.', 'warning')
        return redirect(url_for('paid_qa_page'))

    reason = request.form.get('reason', '').strip()
    stripe.api_key = cfg('stripe_secret_key')
    try:
        stripe.PaymentIntent.cancel(pq['stripe_payment_intent_id'])
    except Exception as e:
        logger.exception('Stripe cancel failed')
        flash(f'Could not release the hold: {e}', 'danger')
        return redirect(url_for('paid_qa_page'))

    now = datetime.now(timezone.utc).isoformat()
    message = f"Paid Q&A referred to appointment (not charged). Original request: {pq['question_text']}"
    if reason:
        message += f'\n\nReason given: {reason}'
    cur = db.execute(
        'INSERT INTO tasks (created_at, patient_name, phone_number, message_text, source_label, status) '
        'VALUES (?, ?, ?, ?, ?, ?)',
        (now, pq['patient_name'], pq['patient_mobile'], message, 'Paid Q&A — needs appointment', 'open'),
    )
    linked_task_id = cur.lastrowid

    db.execute(
        "UPDATE paid_questions SET status = 'declined', decided_by_id = ?, decided_at = ?, "
        "linked_task_id = ? WHERE id = ?",
        (session['user_id'], now, linked_task_id, pq_id),
    )
    db.commit()

    email_error = _send_email(
        pq['patient_email'],
        'Your question to Shore Gastroenterology',
        f"Hi {pq['patient_name']},\n\nDr Tu has reviewed your question and believes it's best "
        "addressed with an appointment rather than by message. You have not been charged for "
        "this request. Our team will be in touch to help arrange a booking.",
    )
    if email_error:
        flash(f'Released the hold, but the email to the patient failed: {email_error}', 'warning')
    else:
        flash('Declined — not charged, patient notified, and a booking task was created.', 'success')
    return redirect(url_for('paid_qa_page'))


# ---------- Gmail polling ----------

def poll_gmail():
    """Fetch new Solium/Halaxy emails and insert them as tasks. Returns (count_imported, error_or_None)."""
    db = sqlite3.connect(str(DB_PATH))
    db.row_factory = sqlite3.Row
    try:
        gmail_address = db.execute(
            "SELECT value FROM config WHERE key = 'gmail_address'"
        ).fetchone()
        gmail_password = db.execute(
            "SELECT value FROM config WHERE key = 'gmail_app_password'"
        ).fetchone()
        gmail_folder = db.execute(
            "SELECT value FROM config WHERE key = 'gmail_folder'"
        ).fetchone()
        gmail_address = gmail_address['value'] if gmail_address else None
        gmail_password = gmail_password['value'] if gmail_password else None
        gmail_folder = gmail_folder['value'] if gmail_folder else 'INBOX'
        if not gmail_address or not gmail_password:
            return 0, 'Gmail is not set up yet — configure it under Settings first.'

        existing_ids = {
            row['gmail_message_id']
            for row in db.execute(
                'SELECT gmail_message_id FROM tasks WHERE gmail_message_id IS NOT NULL'
            ).fetchall()
        }
        try:
            new_emails = gmail_poller.fetch_new_patient_emails(
                gmail_address, gmail_password, existing_ids, folder=gmail_folder
            )
        except Exception as e:
            logger.exception('Gmail poll failed')
            return 0, str(e)

        count = 0
        for item in new_emails:
            try:
                db.execute(
                    'INSERT INTO tasks (created_at, patient_name, phone_number, message_text, '
                    'source_label, gmail_message_id, status, intake_source, intake_kind) '
                    'VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)',
                    (
                        datetime.now(timezone.utc).isoformat(),
                        item['patient_name'],
                        item['phone_number'],
                        item['message_text'],
                        item['source_label'],
                        item['message_id'],
                        'open',
                        item.get('intake_source'),
                        item.get('intake_kind'),
                    ),
                )
                count += 1
            except sqlite3.IntegrityError:
                continue
        db.commit()
        return count, None
    finally:
        db.close()


def poll_fax_inbox():
    """Fetch new VOIP.net fax notifications and save them as unfiled fax
    documents. Returns (count_imported, error_or_None)."""
    db = sqlite3.connect(str(DB_PATH))
    db.row_factory = sqlite3.Row
    try:
        fax_address = db.execute("SELECT value FROM config WHERE key = 'fax_gmail_address'").fetchone()
        fax_password = db.execute("SELECT value FROM config WHERE key = 'fax_gmail_app_password'").fetchone()
        fax_folder = db.execute("SELECT value FROM config WHERE key = 'fax_gmail_folder'").fetchone()
        fax_address = fax_address['value'] if fax_address else None
        fax_password = fax_password['value'] if fax_password else None
        fax_folder = fax_folder['value'] if fax_folder else 'INBOX'
        if not fax_address or not fax_password:
            return 0, 'Fax inbox is not set up yet — configure it under Settings first.'

        existing_ids = {
            row['gmail_message_id']
            for row in db.execute(
                'SELECT gmail_message_id FROM fax_documents WHERE gmail_message_id IS NOT NULL'
            ).fetchall()
        }
        try:
            new_faxes = fax_poller.fetch_new_faxes(fax_address, fax_password, existing_ids, folder=fax_folder)
        except Exception as e:
            logger.exception('Fax poll failed')
            return 0, str(e)

        count = 0
        for item in new_faxes:
            now = datetime.now(timezone.utc).isoformat()
            try:
                cur = db.execute(
                    'INSERT INTO fax_documents (received_at, from_number, pdf_filename, gmail_message_id) '
                    'VALUES (?, ?, ?, ?)',
                    (now, item['from_number'], '', item['message_id']),
                )
            except sqlite3.IntegrityError:
                continue
            fax_id = cur.lastrowid
            filename = f'{fax_id}.pdf'
            (FAX_DIR / filename).write_bytes(item['pdf_bytes'])
            db.execute('UPDATE fax_documents SET pdf_filename = ? WHERE id = ?', (filename, fax_id))
            count += 1
        db.commit()
        return count, None
    finally:
        db.close()


def start_scheduler():
    scheduler = BackgroundScheduler()
    db = sqlite3.connect(str(DB_PATH))
    row = db.execute("SELECT value FROM config WHERE key = 'poll_interval_seconds'").fetchone()
    db.close()
    interval = int(row[0]) if row and row[0] else 90
    scheduler.add_job(poll_gmail, 'interval', seconds=interval, id='gmail_poll', replace_existing=True)
    scheduler.add_job(poll_fax_inbox, 'interval', seconds=interval, id='fax_poll', replace_existing=True)
    scheduler.start()
    return scheduler


init_db()

if os.environ.get('WERKZEUG_RUN_MAIN') != 'true' or not app.debug:
    _scheduler = start_scheduler()


if __name__ == '__main__':
    app.run(debug=False, host='0.0.0.0', port=int(os.environ.get('PORT', 5010)))
