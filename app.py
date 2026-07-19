import os
import re
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
from requests.auth import HTTPBasicAuth
from flask import (
    Flask, g, render_template, request, redirect, url_for, flash, session, send_file,
    send_from_directory
)
from werkzeug.security import generate_password_hash, check_password_hash
from apscheduler.schedulers.background import BackgroundScheduler

import gmail_poller

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get('STORAGE_DIR', BASE_DIR)) / 'data'
DATA_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = DATA_DIR / 'callback_manager.db'
DOCS_DIR = DATA_DIR / 'documents'
DOCS_DIR.mkdir(parents=True, exist_ok=True)

HOSPITALS = ['Dee Why Endoscopy', 'Mater Hospital', 'East Sydney Private Hospital']
DOC_TYPES = [
    ('colonoscopy_prep', 'Colonoscopy Prep'),
    ('gastroscopy_prep', 'Gastroscopy Prep'),
    ('ifc', 'Informed Consent Form (IFC)'),
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


def _send_sms(to_number, message):
    """Send via ClickSend (same API/pattern as review_sender). Returns None on
    success, or an error message string on failure."""
    username = cfg('clicksend_username')
    api_key = cfg('clicksend_api_key')
    if not username or not api_key:
        return 'ClickSend is not set up yet — configure it under Settings first.'
    alpha_tag = cfg('sms_alpha_tag') or 'CallbackMgr'

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
    filename=str(BASE_DIR / 'error.log'),
    level=logging.INFO,
    format='%(asctime)s %(levelname)s %(message)s',
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
    task_id INTEGER NOT NULL,
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
                       ('pending_question_for', 'INTEGER')]:
        if col not in existing_task_cols:
            db.execute(f'ALTER TABLE tasks ADD COLUMN {col} {decl}')

    existing_user_cols = {row['name'] for row in db.execute('PRAGMA table_info(users)').fetchall()}
    if 'token_rate' not in existing_user_cols:
        db.execute('ALTER TABLE users ADD COLUMN token_rate REAL')
    if 'is_doctor' not in existing_user_cols:
        db.execute('ALTER TABLE users ADD COLUMN is_doctor INTEGER NOT NULL DEFAULT 0')
    if 'hourly_rate' not in existing_user_cols:
        db.execute('ALTER TABLE users ADD COLUMN hourly_rate REAL')
    if 'phone_number' not in existing_user_cols:
        db.execute('ALTER TABLE users ADD COLUMN phone_number TEXT')

    existing_payment_cols = {row['name'] for row in db.execute('PRAGMA table_info(payments)').fetchall()}
    if 'minutes' not in existing_payment_cols:
        db.execute('ALTER TABLE payments ADD COLUMN minutes REAL')

    # Warren predates the 'delegate' role - promote him if he's still 'actioneer'.
    db.execute("UPDATE users SET role = 'delegate' WHERE display_name = 'Warren' AND role = 'actioneer'")
    # Pay moved from per-task rates to an hourly rate - give Warren a default
    # if he doesn't have one yet (old per-task fields are no longer used).
    db.execute("UPDATE users SET hourly_rate = 30.0 WHERE display_name = 'Warren' AND hourly_rate IS NULL")
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
            ('Dr Jeffrey Tu', 'admin', None, 1),
            ('Sally', 'admin', None, 0),
            ('Warren', 'delegate', 30.0, 0),
        ]
        for display_name, role, hourly_rate, is_doctor in seed:
            db.execute(
                'INSERT INTO users (display_name, role, hourly_rate, is_doctor) VALUES (?, ?, ?, ?)',
                (display_name, role, hourly_rate, is_doctor),
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


@app.before_request
def require_login():
    if request.endpoint is None or request.endpoint in ('login', 'static', 'service_worker'):
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
    if user:
        all_users = get_db().execute(
            'SELECT id, display_name FROM users WHERE active = 1 ORDER BY display_name'
        ).fetchall()
    return {
        'current_user': user,
        'outcome_labels': OUTCOME_LABELS,
        'all_users': all_users,
        'endoscopy_manager_url': cfg('endoscopy_manager_url', '') if user else '',
        'practice_manager_url': cfg('practice_manager_url', '') if user else '',
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
        if body:
            db.execute(
                'INSERT INTO messages (author_id, created_at, body) VALUES (?, ?, ?)',
                (session['user_id'], datetime.now(timezone.utc).isoformat(), body),
            )
            db.commit()
            flash('Message posted.', 'success')
        return redirect(url_for('messages_page'))

    rows = db.execute(
        "SELECT m.*, u.display_name AS author_name FROM messages m "
        "LEFT JOIN users u ON u.id = m.author_id ORDER BY m.created_at DESC LIMIT 200"
    ).fetchall()
    return render_template('messages.html', messages=rows)


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
    return render_template(
        'documents.html', grid=grid,
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


@app.route('/documents/<hospital_slug>/<doc_key>')
def view_document(hospital_slug, doc_key):
    path = DOCS_DIR / hospital_slug / f'{doc_key}.pdf'
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
            "WHERE t.pending_question_for = ? ORDER BY t.created_at ASC",
            (session['user_id'],),
        ).fetchall()
    elif view == 'mine':
        rows = db.execute(
            "SELECT t.*, u.display_name AS claimed_by_name FROM tasks t "
            "LEFT JOIN users u ON u.id = t.claimed_by_id "
            "WHERE t.status = 'claimed' AND t.claimed_by_id = ? "
            "ORDER BY t.claimed_at ASC",
            (session['user_id'],),
        ).fetchall()
    else:  # untouched (full-access only)
        rows = db.execute(
            "SELECT t.*, u.display_name AS claimed_by_name FROM tasks t "
            "LEFT JOIN users u ON u.id = t.claimed_by_id "
            "WHERE t.status = 'open' "
            "ORDER BY t.created_at ASC"
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
        questions_count=questions_count, notify_targets=notify_targets,
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


@app.route('/task/<int:task_id>/handoff', methods=['POST'])
def handoff_task(task_id):
    if session.get('role') not in FULL_ACCESS_ROLES:
        flash('Only Dr Tu or Sally can hand off tasks.', 'warning')
        return redirect(url_for('queue'))
    db = get_db()
    task = db.execute('SELECT * FROM tasks WHERE id = ?', (task_id,)).fetchone()
    target_id = request.form.get('target_id')
    instructions = request.form.get('instructions', '').strip()
    target = db.execute(
        "SELECT * FROM users WHERE id = ? AND active = 1", (target_id,)
    ).fetchone()
    if not task or task['status'] == 'done' or not target:
        flash('Could not hand off that task.', 'warning')
        return redirect(url_for('queue'))

    db.execute(
        "UPDATE tasks SET status = 'claimed', claimed_by_id = ?, claimed_at = ?, "
        "pending_question_for = NULL WHERE id = ?",
        (target['id'], datetime.now(timezone.utc).isoformat(), task_id),
    )
    note = (f"Handed to {target['display_name']} by {session.get('display_name')}: {instructions}" if instructions
            else f"Handed to {target['display_name']} by {session.get('display_name')} — "
                 "no specific instructions, just call and find out what's needed.")
    db.execute(
        'INSERT INTO task_notes (task_id, author_id, created_at, note) VALUES (?, ?, ?, ?)',
        (task_id, session['user_id'], datetime.now(timezone.utc).isoformat(), note),
    )
    db.commit()
    flash(f"Handed off to {target['display_name']}.", 'success')
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


@app.route('/task/<int:task_id>/forward-to-doctor', methods=['POST'])
def forward_to_doctor(task_id):
    db = get_db()
    task = db.execute('SELECT * FROM tasks WHERE id = ?', (task_id,)).fetchone()
    reason = request.form.get('reason', '').strip()
    if not task or task['status'] == 'done':
        flash('That task is already resolved.', 'warning')
        return redirect(url_for('queue'))
    if not _can_manage_task(task):
        flash('That task is not assigned to you.', 'warning')
        return redirect(url_for('queue'))
    if not reason:
        flash('Add a note for Dr Tu — he needs enough detail to call the patient himself.', 'danger')
        return redirect(url_for('queue'))
    doctor = db.execute('SELECT * FROM users WHERE is_doctor = 1 AND active = 1').fetchone()
    if not doctor:
        flash('No one is set as the doctor yet — set this under Users first.', 'danger')
        return redirect(url_for('queue'))

    user = current_user()
    minutes_val, error = _parse_minutes(user, request.form.get('minutes'))
    if error:
        flash(error, 'warning')
        return redirect(url_for('queue'))

    now = datetime.now(timezone.utc).isoformat()
    db.execute(
        "UPDATE tasks SET status = 'claimed', claimed_by_id = ?, claimed_at = ?, "
        "pending_question_for = NULL WHERE id = ?",
        (doctor['id'], now, task_id),
    )
    db.execute(
        'INSERT INTO task_notes (task_id, author_id, created_at, note) VALUES (?, ?, ?, ?)',
        (task_id, user['id'], now, f"Forwarded to {doctor['display_name']}: {reason}"),
    )
    _log_time(db, task_id, user, minutes_val, now)
    db.commit()
    flash(f'Forwarded to {doctor["display_name"]}.', 'success')
    return redirect(url_for('queue'))


@app.route('/task/<int:task_id>/unable', methods=['POST'])
def unable_to_complete(task_id):
    db = get_db()
    task = db.execute('SELECT * FROM tasks WHERE id = ?', (task_id,)).fetchone()
    reason = request.form.get('reason', '').strip()
    target_id = request.form.get('target_id', '').strip()
    if not task or task['status'] == 'done':
        flash('That task is already resolved.', 'warning')
        return redirect(url_for('queue'))
    if not _can_manage_task(task):
        flash('That task is not assigned to you.', 'warning')
        return redirect(url_for('queue'))

    user = current_user()

    # Warren's paid for this, so he owes a reason every time. Dr Tu and Sally
    # aren't paid for it and work together closely enough not to need one.
    if user['role'] == 'delegate' and not reason:
        flash('Add a reason so whoever picks this up next has context.', 'warning')
        return redirect(url_for('queue'))

    # Anti-ping-pong: a delegate must have logged at least one real attempt on
    # this task before they're allowed to hand it back — stops "claim, bounce,
    # claim, bounce" churning out paid minutes for zero actual effort.
    if user['role'] == 'delegate':
        logged = db.execute(
            'SELECT COUNT(*) AS n FROM task_notes WHERE task_id = ? AND author_id = ?',
            (task_id, user['id']),
        ).fetchone()['n']
        if not logged:
            flash('Log at least one attempt first (what you tried) before handing this back.', 'warning')
            return redirect(url_for('queue'))

    minutes_val, error = _parse_minutes(user, request.form.get('minutes'))
    if error:
        flash(error, 'warning')
        return redirect(url_for('queue'))

    now = datetime.now(timezone.utc).isoformat()
    target = None
    if target_id:
        target = db.execute('SELECT * FROM users WHERE id = ? AND active = 1', (target_id,)).fetchone()

    if target:
        db.execute(
            "UPDATE tasks SET status = 'claimed', claimed_by_id = ?, claimed_at = ?, "
            "pending_question_for = NULL WHERE id = ?",
            (target['id'], now, task_id),
        )
        destination = f"to {target['display_name']}"
    else:
        db.execute(
            "UPDATE tasks SET status = 'open', claimed_by_id = NULL, claimed_at = NULL, "
            "pending_question_for = NULL WHERE id = ?",
            (task_id,),
        )
        destination = "to the Untouched pool"

    note = f"Unable to complete — handed back {destination} by {user['display_name']}"
    note += f": {reason}" if reason else " (no reason given)."
    db.execute(
        'INSERT INTO task_notes (task_id, author_id, created_at, note) VALUES (?, ?, ?, ?)',
        (task_id, user['id'], now, note),
    )
    _log_time(db, task_id, user, minutes_val, now)
    paid_note = f' ({minutes_val:g} min logged)' if minutes_val else ''
    db.commit()
    dest_label = target['display_name'] if target else 'the Untouched pool'
    flash(f'Handed back to {dest_label}{paid_note}.', 'success')
    return redirect(url_for('queue'))


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

        if not to_addr or not subject or not body:
            flash('Fill in the recipient, subject, and message.', 'danger')
            return render_template('email_task.html', task=task, hospitals=HOSPITALS,
                                    doc_types=DOC_TYPES, templates=templates)

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

        error = _send_email(to_addr, subject, body, attachments)
        if error:
            flash(f'Could not send email: {error}', 'danger')
            return render_template('email_task.html', task=task, hospitals=HOSPITALS,
                                    doc_types=DOC_TYPES, templates=templates)

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
                            doc_types=DOC_TYPES, templates=templates)


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


# ---------- admin ----------
# (Needs Dr Tu used to be a separate page; forwarded tasks now just land in the
# doctor's own "My tasks" box like any other hand-off, so it's gone.)


@app.route('/admin/payroll', methods=['GET', 'POST'])
@admin_required
def payroll():
    db = get_db()
    if request.method == 'POST':
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
        return redirect(url_for('payroll'))

    payees = db.execute(
        "SELECT * FROM users WHERE hourly_rate IS NOT NULL AND active = 1"
    ).fetchall()
    totals = []
    for u in payees:
        row = db.execute(
            "SELECT COALESCE(SUM(amount), 0) AS total, COALESCE(SUM(minutes), 0) AS minutes, "
            "COUNT(*) AS n FROM payments WHERE user_id = ? AND payroll_run_id IS NULL",
            (u['id'],),
        ).fetchone()
        totals.append({
            'user': u,
            'total': row['total'],
            'minutes': row['minutes'],
            'count': row['n'],
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
            phone = request.form.get('phone_number', '').strip()
            if not display_name:
                flash('Display name is required.', 'danger')
            else:
                db.execute(
                    'INSERT INTO users (display_name, role, hourly_rate, phone_number) VALUES (?, ?, ?, ?)',
                    (display_name, role, rate_val, phone or None),
                )
                db.commit()
                flash(f'Added {display_name}.', 'success')
        elif action == 'update':
            user_id = request.form.get('user_id')
            role = request.form.get('role', 'actioneer')
            rate = request.form.get('hourly_rate', '').strip()
            rate_val = float(rate) if rate else None
            phone = request.form.get('phone_number', '').strip()
            active = 1 if request.form.get('active') == 'on' else 0
            is_doctor = 1 if request.form.get('is_doctor') == 'on' else 0
            if is_doctor:
                db.execute('UPDATE users SET is_doctor = 0 WHERE id != ?', (user_id,))
            db.execute(
                'UPDATE users SET role = ?, hourly_rate = ?, phone_number = ?, active = ?, is_doctor = ? WHERE id = ?',
                (role, rate_val, phone or None, active, is_doctor, user_id),
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
            flash('Settings saved.', 'success')
        elif action == 'poll_now':
            count = poll_gmail()
            flash(f'Poll complete: {count} new task(s) imported.', 'success')
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


# ---------- Gmail polling ----------

def poll_gmail():
    """Fetch new Solium emails and insert them as tasks. Returns count imported."""
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
            return 0

        existing_ids = {
            row['gmail_message_id']
            for row in db.execute(
                'SELECT gmail_message_id FROM tasks WHERE gmail_message_id IS NOT NULL'
            ).fetchall()
        }
        try:
            new_emails = gmail_poller.fetch_new_solium_emails(
                gmail_address, gmail_password, existing_ids, folder=gmail_folder
            )
        except Exception:
            logger.exception('Gmail poll failed')
            return 0

        count = 0
        for item in new_emails:
            try:
                db.execute(
                    'INSERT INTO tasks (created_at, patient_name, phone_number, message_text, '
                    'source_label, gmail_message_id, status) VALUES (?, ?, ?, ?, ?, ?, ?)',
                    (
                        datetime.now(timezone.utc).isoformat(),
                        item['patient_name'],
                        item['phone_number'],
                        item['message_text'],
                        item['source_label'],
                        item['message_id'],
                        'open',
                    ),
                )
                count += 1
            except sqlite3.IntegrityError:
                continue
        db.commit()
        return count
    finally:
        db.close()


def start_scheduler():
    scheduler = BackgroundScheduler()
    db = sqlite3.connect(str(DB_PATH))
    row = db.execute("SELECT value FROM config WHERE key = 'poll_interval_seconds'").fetchone()
    db.close()
    interval = int(row[0]) if row and row[0] else 90
    scheduler.add_job(poll_gmail, 'interval', seconds=interval, id='gmail_poll', replace_existing=True)
    scheduler.start()
    return scheduler


init_db()

if os.environ.get('WERKZEUG_RUN_MAIN') != 'true' or not app.debug:
    _scheduler = start_scheduler()


if __name__ == '__main__':
    app.run(debug=False, host='0.0.0.0', port=int(os.environ.get('PORT', 5010)))
