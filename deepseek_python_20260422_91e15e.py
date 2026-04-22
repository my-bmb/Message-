# IMPORTANT: monkey_patch must be the FIRST line
from gevent import monkey
monkey.patch_all()

# Now all other imports
import os
import uuid
from datetime import datetime, timezone
from flask import Flask, render_template, request, redirect, url_for, flash, jsonify, session
from flask_socketio import SocketIO, emit, join_room, leave_room, rooms as socket_rooms
from flask_login import LoginManager, UserMixin, login_user, logout_user, login_required, current_user
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename
from supabase import create_client, Client
from dotenv import load_dotenv
import pytz
from threading import Timer, Lock
import logging
import traceback
import time
import requests
import json
from functools import wraps
from urllib3.util.retry import Retry
from requests.adapters import HTTPAdapter

# ========== LOGGING SETUP WITH MICRO DETAILING ==========
class MicroLogger:
    """Custom logger for micro-detailing"""
    def __init__(self):
        self.log_level = logging.INFO
        self.setup_logging()
    
    def setup_logging(self):
        logging.basicConfig(
            level=logging.DEBUG,
            format='[%(asctime)s] %(levelname)s | %(filename)s:%(lineno)d | %(funcName)s() | %(message)s',
            datefmt='%Y-%m-%d %H:%M:%S.%f'
        )
        self.logger = logging.getLogger(__name__)
    
    def _format_message(self, event_type, details, extra=None):
        msg = f"🔍 [{event_type}] {details}"
        if extra:
            msg += f" | extra: {json.dumps(extra, default=str)}"
        return msg
    
    def event(self, event_type, details, extra=None):
        self.logger.info(self._format_message(event_type, details, extra))
    
    def error(self, event_type, details, extra=None):
        self.logger.error(self._format_message(event_type, details, extra))
    
    def debug(self, event_type, details, extra=None):
        self.logger.debug(self._format_message(event_type, details, extra))
    
    def warn(self, event_type, details, extra=None):
        self.logger.warning(self._format_message(event_type, details, extra))

logger = MicroLogger()

load_dotenv()

app = Flask(__name__)
app.config['SECRET_KEY'] = os.getenv('SECRET_KEY', 'fallback-secret-key-change-me')
app.config['MAX_CONTENT_LENGTH'] = 50 * 1024 * 1024
app.config['SESSION_COOKIE_SECURE'] = False
app.config['SESSION_COOKIE_HTTPONLY'] = True
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'
app.config['PERMANENT_SESSION_LIFETIME'] = 7 * 24 * 3600
app.config['REMEMBER_COOKIE_DURATION'] = 7 * 24 * 3600
app.config['REMEMBER_COOKIE_SECURE'] = False

# Supabase client with retry logic
supabase_url = os.getenv('SUPABASE_URL')
supabase_key = os.getenv('SUPABASE_KEY')
if not supabase_url or not supabase_key:
    raise RuntimeError("SUPABASE_URL and SUPABASE_KEY must be set")

session_config = requests.Session()
retries = Retry(
    total=3, 
    backoff_factor=0.5, 
    status_forcelist=[500, 502, 503, 504],
    allowed_methods=["GET", "POST", "PATCH", "DELETE"]
)
session_config.mount('https://', HTTPAdapter(max_retries=retries, pool_connections=10, pool_maxsize=10))

supabase: Client = create_client(supabase_url, supabase_key)

socketio = SocketIO(
    app, 
    cors_allowed_origins="*", 
    async_mode='gevent',
    ping_timeout=30,
    ping_interval=15,
    max_http_buffer_size=50 * 1024 * 1024,
    engineio_logger=True,
    logger=True,
    always_connect=True,
    transports=['websocket', 'polling']
)

login_manager = LoginManager(app)
login_manager.login_view = 'login'
login_manager.login_message = 'Please login to access this page'
login_manager.login_message_category = 'warning'

IST = pytz.timezone('Asia/Kolkata')

# ========== GLOBAL STATE MANAGEMENT ==========
active_calls = {}
call_timeouts = {}
status_update_timers = {}
call_lock = Lock()

# ========== HELPER FUNCTIONS ==========
def get_utc_time():
    return datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')

def get_timestamp_ms():
    """Get current timestamp in milliseconds for micro-detailing"""
    return time.time() * 1000

def format_ist_time(timestamp_str):
    if not timestamp_str:
        return ""
    try:
        clean_timestamp = timestamp_str.replace('Z', '+00:00')
        dt_utc = datetime.fromisoformat(clean_timestamp)
        dt_ist = dt_utc.astimezone(IST)
        return dt_ist.strftime("%b %d, %Y, %I:%M %p")
    except Exception as e:
        logger.error("Format time error", str(e), extra={'timestamp': timestamp_str})
        return str(timestamp_str)[:16]

class User(UserMixin):
    def __init__(self, id, username, email, is_online, last_seen=None):
        self.id = id
        self.username = username
        self.email = email
        self.is_online = is_online
        self.last_seen = last_seen

@login_manager.user_loader
def load_user(user_id):
    logger.event("USER_LOAD", f"Loading user: {user_id}")
    try:
        result = supabase.table('chat_users').select('*').eq('id', user_id).execute()
        if result.data:
            u = result.data[0]
            logger.event("USER_LOAD_SUCCESS", f"User loaded: {u['username']}", extra={'user_id': user_id})
            return User(u['id'], u['username'], u['email'], u.get('is_online', False), u.get('last_seen'))
        else:
            logger.warn("USER_LOAD_FAILED", f"User not found: {user_id}")
    except Exception as e:
        logger.error("USER_LOAD_ERROR", str(e), extra={'user_id': user_id})
    return None

def supabase_execute_safe(query_func, default_return=None, max_retries=2):
    for attempt in range(max_retries):
        try:
            start_time = get_timestamp_ms()
            result = query_func()
            elapsed = get_timestamp_ms() - start_time
            logger.debug("DB_QUERY", f"Query executed in {elapsed:.2f}ms", extra={'attempt': attempt+1})
            return result.data if result else default_return
        except Exception as e:
            logger.error("DB_QUERY_ERROR", str(e), extra={'attempt': attempt+1, 'error_type': type(e).__name__})
            if attempt == max_retries - 1:
                return default_return
            time.sleep(0.5 * (attempt + 1))
    return default_return

def get_user_by_username(username):
    return supabase_execute_safe(lambda: supabase.table('chat_users').select('*').eq('username', username).execute(), [])

def get_user_by_email(email):
    return supabase_execute_safe(lambda: supabase.table('chat_users').select('*').eq('email', email).execute(), [])

def get_all_users_except(user_id):
    return supabase_execute_safe(lambda: supabase.table('chat_users').select('*').neq('id', user_id).execute(), [])

def get_unread_counts(user_id):
    try:
        r = supabase.table('messages').select('sender_id').eq('receiver_id', user_id).eq('is_read', False).execute()
        counts = {}
        for msg in r.data:
            counts[msg['sender_id']] = counts.get(msg['sender_id'], 0) + 1
        logger.debug("UNREAD_COUNTS", f"Found {len(counts)} senders with unread messages", extra={'user_id': user_id})
        return counts
    except Exception as e:
        logger.error("UNREAD_COUNTS_ERROR", str(e), extra={'user_id': user_id})
        return {}

def get_messages_between(u1, u2, limit=20, offset=0):
    try:
        r = supabase.table('messages').select('*')\
            .or_(f"and(sender_id.eq.{u1},receiver_id.eq.{u2}),and(sender_id.eq.{u2},receiver_id.eq.{u1})")\
            .order('created_at', desc=True)\
            .range(offset, offset+limit-1)\
            .execute()
        logger.debug("MESSAGES_FETCH", f"Fetched {len(r.data)} messages between users", extra={'user1': u1, 'user2': u2})
        return list(reversed(r.data))
    except Exception as e:
        logger.error("MESSAGES_FETCH_ERROR", str(e), extra={'user1': u1, 'user2': u2})
        return []

def get_reactions_for_messages(message_ids):
    if not message_ids:
        return {}
    try:
        r = supabase.table('message_reactions').select('*').in_('message_id', message_ids).execute()
        reactions_by_msg = {}
        for react in r.data:
            msg_id = react['message_id']
            if msg_id not in reactions_by_msg:
                reactions_by_msg[msg_id] = []
            reactions_by_msg[msg_id].append(react)
        logger.debug("REACTIONS_FETCH", f"Fetched {len(r.data)} reactions for {len(message_ids)} messages")
        return reactions_by_msg
    except Exception as e:
        logger.error("REACTIONS_FETCH_ERROR", str(e))
        return {}

def save_message(sender_id, receiver_id, msg_type, content, reply_to_id=None, reply_to_content=None):
    logger.event("MESSAGE_SAVE", f"Saving {msg_type} message", extra={'sender': sender_id, 'receiver': receiver_id})
    data = {
        'sender_id': sender_id,
        'receiver_id': receiver_id,
        'message_type': msg_type,
        'content': content,
        'is_read': False,
        'is_deleted': False,
        'reply_to_id': reply_to_id,
        'reply_to_content': reply_to_content,
        'edited': False,
        'created_at': get_utc_time()
    }
    try:
        r = supabase.table('messages').insert(data).execute()
        if r.data:
            logger.event("MESSAGE_SAVE_SUCCESS", f"Message saved with ID: {r.data[0]['id']}")
            return r.data[0]
        else:
            logger.warn("MESSAGE_SAVE_FAILED", "No data returned")
            return None
    except Exception as e:
        logger.error("MESSAGE_SAVE_ERROR", str(e))
        return None

def mark_messages_as_read(receiver_id, sender_id):
    logger.event("MARK_READ", f"Marking messages as read", extra={'receiver': receiver_id, 'sender': sender_id})
    try:
        r = supabase.table('messages').update({'is_read': True})\
            .eq('sender_id', sender_id)\
            .eq('receiver_id', receiver_id)\
            .eq('is_read', False)\
            .execute()
        logger.event("MARK_READ_SUCCESS", f"Marked {len(r.data) if r.data else 0} messages as read")
        return r.data
    except Exception as e:
        logger.error("MARK_READ_ERROR", str(e))
        return []

def edit_message(message_id, user_id, new_content):
    try:
        msg = supabase.table('messages').select('*').eq('id', message_id).execute()
        if msg.data and msg.data[0]['sender_id'] == user_id and msg.data[0]['message_type'] == 'text':
            supabase.table('messages').update({'content': new_content, 'edited': True}).eq('id', message_id).execute()
            logger.event("MESSAGE_EDITED", f"Message {message_id} edited by {user_id}")
            return True
        else:
            logger.warn("MESSAGE_EDIT_FAILED", f"User {user_id} cannot edit message {message_id}")
    except Exception as e:
        logger.error("MESSAGE_EDIT_ERROR", str(e))
    return False

def add_reaction(message_id, user_id, reaction):
    try:
        supabase.table('message_reactions').insert({
            'message_id': message_id,
            'user_id': user_id,
            'reaction': reaction
        }).execute()
        logger.event("REACTION_ADDED", f"{user_id} added {reaction} to {message_id}")
        return True
    except Exception as e:
        logger.error("REACTION_ADD_ERROR", str(e))
        return False

def remove_reaction(message_id, user_id, reaction):
    try:
        supabase.table('message_reactions').delete()\
            .eq('message_id', message_id)\
            .eq('user_id', user_id)\
            .eq('reaction', reaction)\
            .execute()
        logger.event("REACTION_REMOVED", f"{user_id} removed {reaction} from {message_id}")
        return True
    except Exception as e:
        logger.error("REACTION_REMOVE_ERROR", str(e))
        return False

def get_reactions_for_message(message_id):
    try:
        r = supabase.table('message_reactions').select('*').eq('message_id', message_id).execute()
        return r.data
    except Exception as e:
        logger.error("REACTION_FETCH_ERROR", str(e))
        return []

def update_user_status(user_id, is_online):
    logger.event("STATUS_UPDATE", f"Updating user {user_id} status to {is_online}")
    try:
        supabase.table('chat_users').update({
            'is_online': is_online, 
            'last_seen': get_utc_time() if not is_online else None
        }).eq('id', user_id).execute()
        
        socketio.emit('user_status', {
            'user_id': user_id, 
            'is_online': is_online, 
            'last_seen': None if is_online else get_utc_time()
        }, broadcast=True)
        
        logger.event("STATUS_UPDATED", f"Status updated and broadcasted", extra={'user_id': user_id, 'is_online': is_online})
    except Exception as e:
        logger.error("STATUS_UPDATE_ERROR", str(e), extra={'user_id': user_id})

def debounced_status_update(user_id, is_online, delay=1.0):
    if user_id in status_update_timers:
        try:
            status_update_timers[user_id].cancel()
            logger.debug("STATUS_DEBOUNCE", f"Cancelled previous timer for {user_id}")
        except:
            pass
    
    timer = Timer(delay, update_user_status, args=[user_id, is_online])
    timer.daemon = True
    timer.start()
    status_update_timers[user_id] = timer
    logger.debug("STATUS_DEBOUNCE", f"Scheduled status update for {user_id} in {delay}s")

def handle_call_timeout(caller_id, target_id):
    logger.event("CALL_TIMEOUT", f"Call timeout for caller {caller_id} to {target_id}")
    try:
        with call_lock:
            if caller_id in active_calls and active_calls[caller_id].get('state') == 'calling':
                del active_calls[caller_id]
                socketio.emit('call_timeout', room=str(caller_id))
                logger.event("CALL_TIMEOUT_HANDLED", f"Call timeout emitted to {caller_id}")
            if caller_id in call_timeouts:
                del call_timeouts[caller_id]
    except Exception as e:
        logger.error("CALL_TIMEOUT_ERROR", str(e))

# ----------------- ROUTES WITH DETAILED LOGS -----------------
@app.route('/')
def index():
    logger.debug("ROUTE_ACCESS", "Index page accessed")
    if current_user.is_authenticated:
        return redirect(url_for('users'))
    return redirect(url_for('login'))

@app.route('/health')
def health():
    return jsonify({'status': 'healthy', 'timestamp': get_utc_time()}), 200

@app.route('/check_auth')
def check_auth():
    auth_status = current_user.is_authenticated
    logger.debug("AUTH_CHECK", f"Auth status: {auth_status}", extra={'user_id': current_user.id if auth_status else None})
    return jsonify({'authenticated': auth_status})

@app.route('/register', methods=['GET', 'POST'])
def register():
    if current_user.is_authenticated:
        return redirect(url_for('users'))
    if request.method == 'POST':
        username = request.form['username']
        email = request.form['email']
        password = request.form['password']
        
        logger.event("REGISTRATION_ATTEMPT", f"User trying to register: {username}")
        
        if get_user_by_username(username):
            logger.warn("REGISTRATION_FAILED", f"Username already exists: {username}")
            flash('Username already exists', 'danger')
            return redirect(url_for('register'))
        if get_user_by_email(email):
            logger.warn("REGISTRATION_FAILED", f"Email already registered: {email}")
            flash('Email already registered', 'danger')
            return redirect(url_for('register'))
        
        user_data = {
            'username': username,
            'email': email,
            'password_hash': generate_password_hash(password),
            'is_online': False,
            'last_seen': get_utc_time()
        }
        try:
            r = supabase.table('chat_users').insert(user_data).execute()
            if r.data:
                logger.event("REGISTRATION_SUCCESS", f"User registered: {username}", extra={'user_id': r.data[0]['id']})
                flash('Registration successful! Please login.', 'success')
                return redirect(url_for('login'))
        except Exception as e:
            logger.error("REGISTRATION_ERROR", str(e), extra={'username': username})
        flash('Registration failed', 'danger')
    return render_template('register.html')

@app.route('/login', methods=['GET', 'POST'])
def login():
    if current_user.is_authenticated:
        return redirect(url_for('users'))
    
    if request.method == 'POST':
        username = request.form['username']
        password = request.form['password']
        
        logger.event("LOGIN_ATTEMPT", f"User trying to login: {username}")
        
        user_data = get_user_by_username(username)
        
        if user_data and check_password_hash(user_data[0]['password_hash'], password):
            user = User(user_data[0]['id'], user_data[0]['username'], user_data[0]['email'], user_data[0]['is_online'])
            login_user(user, remember=True)
            session.permanent = True
            session['user_id'] = user.id
            
            logger.event("LOGIN_SUCCESS", f"User logged in: {username}", extra={'user_id': user.id})
            
            try:
                supabase.table('chat_users').update({'is_online': True, 'last_seen': None}).eq('id', user.id).execute()
                socketio.emit('user_status', {'user_id': user.id, 'is_online': True, 'last_seen': None})
                logger.event("LOGIN_STATUS_UPDATED", f"Online status updated for {user.id}")
            except Exception as e:
                logger.error("LOGIN_STATUS_ERROR", str(e), extra={'user_id': user.id})
            
            next_page = request.args.get('next')
            if next_page and next_page.startswith('/'):
                return redirect(next_page)
            return redirect(url_for('users'))
        else:
            logger.warn("LOGIN_FAILED", f"Invalid credentials for: {username}")
            flash('Invalid username or password', 'danger')
    
    return render_template('login.html')

@app.route('/logout', methods=['GET', 'POST'])
@login_required
def logout():
    user_id = str(current_user.id)
    username = current_user.username
    
    logger.event("LOGOUT_START", f"Logout initiated for user: {username}", extra={'user_id': user_id, 'method': request.method})
    
    try:
        # Cancel pending timers
        if user_id in status_update_timers:
            try:
                status_update_timers[user_id].cancel()
                del status_update_timers[user_id]
                logger.debug("LOGOUT_CLEANUP", f"Cancelled status timer for {user_id}")
            except Exception as e:
                logger.warn("LOGOUT_CLEANUP_ERROR", f"Timer cancel error: {e}")

        # End active calls
        with call_lock:
            if user_id in active_calls:
                call_data = active_calls[user_id]
                target_id = call_data.get('with')
                logger.event("LOGOUT_ACTIVE_CALL", f"User had active call with {target_id}", extra={'call_type': call_data.get('type')})
                
                if target_id:
                    socketio.emit('call_ended', room=str(target_id))
                    logger.event("LOGOUT_CALL_ENDED", f"Call ended emitted to {target_id}")
                
                del active_calls[user_id]
                logger.debug("LOGOUT_CALL_CLEANUP", f"Removed user from active_calls")

        # Update offline status
        update_user_status(user_id, False)
        logger.event("LOGOUT_STATUS_UPDATED", f"User marked offline: {user_id}")

    except Exception as e:
        logger.error("LOGOUT_ERROR", str(e), extra={'user_id': user_id, 'traceback': traceback.format_exc()})

    logout_user()
    session.clear()
    
    logger.event("LOGOUT_COMPLETE", f"User logged out successfully: {username}")
    flash('Logged out successfully', 'success')
    return redirect(url_for('login'))

@app.route('/users')
@login_required
def users():
    logger.debug("ROUTE_ACCESS", f"Users page accessed by {current_user.username}", extra={'user_id': current_user.id})
    all_users = get_all_users_except(current_user.id)
    unread_counts = get_unread_counts(current_user.id)
    for user in all_users:
        if user.get('last_seen'):
            user['last_seen_formatted'] = format_ist_time(user['last_seen'])
        else:
            user['last_seen_formatted'] = 'recently'
    logger.debug("USERS_FETCH", f"Fetched {len(all_users)} users for display")
    return render_template('users.html', users=all_users, unread_counts=unread_counts)

@app.route('/chat/<user_id>')
@login_required
def chat(user_id):
    logger.event("CHAT_ACCESS", f"User {current_user.id} accessing chat with {user_id}")
    try:
        other_user_data = supabase.table('chat_users').select('*').eq('id', user_id).execute()
        if not other_user_data.data:
            logger.warn("CHAT_USER_NOT_FOUND", f"Target user {user_id} not found")
            flash('User not found', 'danger')
            return redirect(url_for('users'))
        other_user = other_user_data.data[0]
    except Exception as e:
        logger.error("CHAT_USER_FETCH_ERROR", str(e), extra={'user_id': user_id})
        flash('Error loading user', 'danger')
        return redirect(url_for('users'))

    messages = get_messages_between(current_user.id, user_id, limit=20, offset=0)
    message_ids = [msg['id'] for msg in messages]
    reactions_by_msg = get_reactions_for_messages(message_ids)
    
    for msg in messages:
        if msg.get('is_deleted'):
            msg['content'] = 'This message was deleted'
            msg['message_type'] = 'text'
        msg['reactions'] = reactions_by_msg.get(msg['id'], [])
    
    marked = mark_messages_as_read(current_user.id, user_id)
    if marked:
        socketio.emit('messages_read', {'reader_id': current_user.id, 'sender_id': user_id}, room=str(user_id))
        unread_after = get_unread_counts(current_user.id).get(user_id, 0)
        socketio.emit('unread_update', {'sender_id': user_id, 'count': unread_after}, room=str(current_user.id))
        logger.event("MESSAGES_MARKED_READ", f"Marked messages from {user_id} as read")
    
    return render_template('chat.html', other_user=other_user, messages=messages, current_user=current_user)

@app.route('/load_more_messages')
@login_required
def load_more_messages():
    other_user_id = request.args.get('other_user_id')
    offset = int(request.args.get('offset', 0))
    logger.debug("LOAD_MORE_MESSAGES", f"Loading messages with offset {offset}", extra={'other_user': other_user_id})
    messages = get_messages_between(current_user.id, other_user_id, limit=20, offset=offset)
    if messages:
        message_ids = [msg['id'] for msg in messages]
        reactions_by_msg = get_reactions_for_messages(message_ids)
        for msg in messages:
            if msg.get('is_deleted'):
                msg['content'] = 'This message was deleted'
                msg['message_type'] = 'text'
            msg['reactions'] = reactions_by_msg.get(msg['id'], [])
    return jsonify(messages)

@app.route('/edit_message', methods=['POST'])
@login_required
def edit_message_route():
    data = request.get_json()
    message_id = data['message_id']
    new_content = data['content']
    receiver_id = data['receiver_id']
    logger.event("EDIT_MESSAGE_API", f"Editing message {message_id}", extra={'user': current_user.id})
    if edit_message(message_id, current_user.id, new_content):
        socketio.emit('message_edited', {'message_id': message_id, 'new_content': new_content}, room=str(receiver_id))
        socketio.emit('message_edited', {'message_id': message_id, 'new_content': new_content}, room=str(current_user.id))
        return jsonify({'success': True})
    return jsonify({'error': 'Cannot edit'}), 400

@app.route('/react_to_message', methods=['POST'])
@login_required
def react_to_message():
    data = request.get_json()
    message_id = data['message_id']
    reaction = data['reaction']
    receiver_id = data['receiver_id']
    logger.event("REACT_API", f"User {current_user.id} reacting to {message_id} with {reaction}")
    existing = supabase.table('message_reactions').select('*').eq('message_id', message_id).eq('user_id', current_user.id).eq('reaction', reaction).execute()
    if existing.data:
        remove_reaction(message_id, current_user.id, reaction)
    else:
        add_reaction(message_id, current_user.id, reaction)
    new_reactions = get_reactions_for_message(message_id)
    socketio.emit('reaction_updated', {'message_id': message_id, 'reactions': new_reactions}, room=str(receiver_id))
    socketio.emit('reaction_updated', {'message_id': message_id, 'reactions': new_reactions}, room=str(current_user.id))
    return jsonify({'success': True, 'reactions': new_reactions})

@app.route('/search_messages')
@login_required
def search_messages():
    q = request.args.get('q', '')
    other_user_id = request.args.get('other_user_id')
    logger.debug("SEARCH_MESSAGES", f"Searching for '{q}'", extra={'other_user': other_user_id})
    if not q or not other_user_id:
        return jsonify([])
    try:
        r = supabase.table('messages').select('*')\
            .or_(f"and(sender_id.eq.{current_user.id},receiver_id.eq.{other_user_id}),and(sender_id.eq.{other_user_id},receiver_id.eq.{current_user.id})")\
            .ilike('content', f'%{q}%')\
            .order('created_at', desc=True)\
            .limit(30)\
            .execute()
        results = []
        for msg in r.data:
            if msg.get('is_deleted'):
                continue
            results.append({
                'id': msg['id'],
                'content': msg['content'],
                'sender_id': msg['sender_id'],
                'formatted_time': format_ist_time(msg['created_at'])
            })
        logger.debug("SEARCH_RESULTS", f"Found {len(results)} results")
        return jsonify(results)
    except Exception as e:
        logger.error("SEARCH_ERROR", str(e))
        return jsonify([])

@app.route('/upload', methods=['POST'])
@login_required
def upload_file():
    if 'file' not in request.files:
        return {'error': 'No file'}, 400
    file = request.files['file']
    receiver_id = request.form.get('receiver_id')
    if file.filename == '' or not receiver_id:
        return {'error': 'Missing data'}, 400
    ext = file.filename.rsplit('.', 1)[1].lower()
    if ext in ['png','jpg','jpeg','gif','webp']:
        msg_type = 'image'
    elif ext in ['mp4','webm','avi','mov','mkv']:
        msg_type = 'video'
    elif ext in ['mp3','wav','ogg','m4a','aac']:
        msg_type = 'audio'
    else:
        msg_type = 'file'
    file_path = f"chat_uploads/{uuid.uuid4()}_{secure_filename(file.filename)}"
    file_bytes = file.read()
    try:
        supabase.storage.from_('chat-files').upload(file_path, file_bytes)
        public_url = supabase.storage.from_('chat-files').get_public_url(file_path)
        logger.event("FILE_UPLOAD", f"File uploaded: {file_path}", extra={'type': msg_type})
    except Exception as e:
        logger.error("STORAGE_UPLOAD_ERROR", str(e))
        return {'error': 'Upload failed'}, 500
    message = save_message(current_user.id, receiver_id, msg_type, public_url)
    if message:
        msg_dict = {
            'id': message['id'],
            'sender_id': message['sender_id'],
            'receiver_id': message['receiver_id'],
            'message_type': message['message_type'],
            'content': message['content'],
            'is_read': message['is_read'],
            'created_at': message['created_at'],
            'reactions': []
        }
        socketio.emit('new_message', msg_dict, room=str(receiver_id))
        socketio.emit('new_message', msg_dict, room=str(current_user.id))
        unread_count = get_unread_counts(receiver_id).get(current_user.id, 0)
        socketio.emit('unread_update', {'sender_id': current_user.id, 'count': unread_count}, room=str(receiver_id))
        return {'success': True, 'url': public_url, 'type': msg_type}
    return {'error': 'Save failed'}, 500

@app.route('/upload_audio', methods=['POST'])
@login_required
def upload_audio():
    if 'audio' not in request.files:
        return {'error': 'No audio'}, 400
    audio = request.files['audio']
    receiver_id = request.form.get('receiver_id')
    if not receiver_id:
        return {'error': 'Missing receiver'}, 400
    file_path = f"chat_uploads/{uuid.uuid4()}_voice.wav"
    try:
        supabase.storage.from_('chat-files').upload(file_path, audio.read())
        public_url = supabase.storage.from_('chat-files').get_public_url(file_path)
        logger.event("AUDIO_UPLOAD", f"Audio uploaded: {file_path}")
    except Exception as e:
        logger.error("AUDIO_UPLOAD_ERROR", str(e))
        return {'error': 'Upload failed'}, 500
    message = save_message(current_user.id, receiver_id, 'audio', public_url)
    if message:
        msg_dict = {
            'id': message['id'],
            'sender_id': message['sender_id'],
            'receiver_id': message['receiver_id'],
            'message_type': 'audio',
            'content': public_url,
            'is_read': False,
            'created_at': message['created_at'],
            'reactions': []
        }
        socketio.emit('new_message', msg_dict, room=str(receiver_id))
        socketio.emit('new_message', msg_dict, room=str(current_user.id))
        unread_count = get_unread_counts(receiver_id).get(current_user.id, 0)
        socketio.emit('unread_update', {'sender_id': current_user.id, 'count': unread_count}, room=str(receiver_id))
        return {'success': True}
    return {'error': 'Save failed'}, 500

@app.route('/audio-call/<user_id>')
@login_required
def audio_call(user_id):
    logger.event("AUDIO_CALL_PAGE", f"User {current_user.id} accessing audio call with {user_id}")
    try:
        other_user_data = supabase.table('chat_users').select('*').eq('id', user_id).execute()
        if not other_user_data.data:
            flash('User not found', 'danger')
            return redirect(url_for('users'))
        return render_template('audio.call.html', other_user=other_user_data.data[0])
    except Exception as e:
        logger.error("AUDIO_CALL_ERROR", str(e))
        flash('Error starting call', 'danger')
        return redirect(url_for('users'))

@app.route('/video-call/<user_id>')
@login_required
def video_call(user_id):
    logger.event("VIDEO_CALL_PAGE", f"User {current_user.id} accessing video call with {user_id}")
    try:
        other_user_data = supabase.table('chat_users').select('*').eq('id', user_id).execute()
        if not other_user_data.data:
            flash('User not found', 'danger')
            return redirect(url_for('users'))
        return render_template('video.call.html', other_user=other_user_data.data[0])
    except Exception as e:
        logger.error("VIDEO_CALL_ERROR", str(e))
        flash('Error starting call', 'danger')
        return redirect(url_for('users'))

@app.route('/delete_message/<message_id>', methods=['POST'])
@login_required
def delete_message_route(message_id):
    data = request.get_json()
    delete_for = data.get('delete_for')
    logger.event("DELETE_MESSAGE", f"User {current_user.id} deleting message {message_id}", extra={'delete_for': delete_for})
    try:
        msg = supabase.table('messages').select('*').eq('id', message_id).execute()
        if not msg.data:
            return jsonify({'error': 'Message not found'}), 404
        msg = msg.data[0]
        if msg['sender_id'] != current_user.id and delete_for == 'everyone':
            return jsonify({'error': 'Not authorized'}), 403
        if delete_for == 'everyone':
            supabase.table('messages').update({'is_deleted': True}).eq('id', message_id).execute()
            socketio.emit('message_deleted', {'message_id': message_id, 'for_everyone': True}, room=str(msg['sender_id']))
            socketio.emit('message_deleted', {'message_id': message_id, 'for_everyone': True}, room=str(msg['receiver_id']))
            logger.event("MESSAGE_DELETED_EVERYONE", f"Message {message_id} deleted for everyone")
        else:
            socketio.emit('message_deleted', {'message_id': message_id, 'for_everyone': False}, room=str(current_user.id))
            logger.event("MESSAGE_DELETED_SELF", f"Message {message_id} deleted only for self")
        return jsonify({'success': True})
    except Exception as e:
        logger.error("DELETE_MESSAGE_ERROR", str(e))
        return jsonify({'error': 'Server error'}), 500

# ----------------- SOCKETIO EVENTS WITH MICRO-DETAILING -----------------
@socketio.on('connect')
def handle_connect():
    sid = request.sid
    logger.event("SOCKET_CONNECT", f"New socket connection", extra={'sid': sid, 'timestamp_ms': get_timestamp_ms()})
    
    try:
        if current_user.is_authenticated:
            user_id = str(current_user.id)
            username = current_user.username
            
            logger.event("SOCKET_AUTHENTICATED", f"Authenticated user connecting", extra={'user_id': user_id, 'username': username, 'sid': sid})
            
            # Join personal room
            join_room(user_id)
            logger.event("ROOM_JOINED", f"User joined personal room", extra={'user_id': user_id, 'room': user_id, 'sid': sid})
            
            # Check current rooms
            current_rooms = socket_rooms()
            logger.debug("ACTIVE_ROOMS", f"Current rooms for user", extra={'user_id': user_id, 'rooms': list(current_rooms)})
            
            # Update status
            update_user_status(user_id, True)
            
            # Send confirmation to frontend
            emit('joined_room', {
                'user_id': user_id,
                'status': 'joined',
                'sid': sid,
                'timestamp': get_timestamp_ms()
            }, room=sid)
            
            logger.event("CONNECT_COMPLETE", f"User fully connected", extra={'user_id': user_id, 'sid': sid})
        else:
            logger.warn("SOCKET_UNAUTHENTICATED", f"Unauthenticated connection attempt", extra={'sid': sid})
            return False
            
    except Exception as e:
        logger.error("SOCKET_CONNECT_ERROR", str(e), extra={'sid': sid, 'traceback': traceback.format_exc()})
        return False

@socketio.on('disconnect')
def handle_disconnect():
    sid = request.sid
    logger.event("SOCKET_DISCONNECT", f"Socket disconnected", extra={'sid': sid, 'timestamp_ms': get_timestamp_ms()})
    
    try:
        if current_user.is_authenticated:
            user_id = str(current_user.id)
            username = current_user.username
            
            logger.event("USER_DISCONNECTING", f"Authenticated user disconnecting", extra={'user_id': user_id, 'username': username, 'sid': sid})
            
            # Leave room
            leave_room(user_id)
            logger.event("ROOM_LEFT", f"User left personal room", extra={'user_id': user_id, 'room': user_id})
            
            # Update offline status
            update_user_status(user_id, False)
            
            logger.event("DISCONNECT_COMPLETE", f"User fully disconnected", extra={'user_id': user_id})
        else:
            logger.debug("DISCONNECT_UNAUTH", f"Unauthenticated socket disconnected", extra={'sid': sid})
            
    except Exception as e:
        logger.error("SOCKET_DISCONNECT_ERROR", str(e), extra={'sid': sid, 'traceback': traceback.format_exc()})

@socketio.on('send_message')
def handle_send_message(data):
    if not current_user.is_authenticated:
        logger.warn("SEND_MESSAGE_UNAUTH", "Unauthenticated user tried to send message")
        return
    
    receiver_id = data['receiver_id']
    content = data['content']
    msg_type = data.get('message_type', 'text')
    
    logger.event("SEND_MESSAGE", f"User sending message", extra={
        'sender': current_user.id,
        'receiver': receiver_id,
        'type': msg_type,
        'content_length': len(content) if content else 0,
        'timestamp_ms': get_timestamp_ms()
    })
    
    reply_to_id = data.get('reply_to_id', None)
    reply_to_content = None
    if reply_to_id:
        try:
            orig = supabase.table('messages').select('content').eq('id', reply_to_id).execute()
            if orig.data:
                reply_to_content = orig.data[0]['content'][:100]
                logger.debug("REPLY_FETCHED", f"Reply content fetched for {reply_to_id}")
        except Exception as e:
            logger.error("REPLY_FETCH_ERROR", str(e))
    
    message = save_message(current_user.id, receiver_id, msg_type, content, reply_to_id, reply_to_content)
    if message:
        msg_dict = {
            'id': message['id'],
            'sender_id': message['sender_id'],
            'receiver_id': message['receiver_id'],
            'message_type': message['message_type'],
            'content': message['content'],
            'is_read': message['is_read'],
            'created_at': message['created_at'],
            'reply_to_id': message.get('reply_to_id'),
            'reply_to_content': message.get('reply_to_content'),
            'edited': False,
            'reactions': []
        }
        
        emit('new_message', msg_dict, room=str(receiver_id))
        emit('new_message', msg_dict, room=str(current_user.id))
        
        logger.event("MESSAGE_BROADCAST", f"Message broadcasted", extra={
            'message_id': message['id'],
            'receiver_room': receiver_id,
            'sender_room': current_user.id
        })
        
        unread_count = get_unread_counts(receiver_id).get(current_user.id, 0)
        emit('unread_update', {'sender_id': current_user.id, 'count': unread_count}, room=str(receiver_id))
        
        logger.debug("UNREAD_UPDATED", f"Unread count for receiver: {unread_count}")
    else:
        logger.error("MESSAGE_SAVE_FAILED", "Failed to save message to database")

@socketio.on('edit_message')
def handle_edit_message(data):
    if not current_user.is_authenticated:
        return
    
    logger.event("EDIT_MESSAGE_SOCKET", f"User editing message", extra={
        'user': current_user.id,
        'message_id': data['message_id']
    })
    
    if edit_message(data['message_id'], current_user.id, data['new_content']):
        emit('message_edited', {'message_id': data['message_id'], 'new_content': data['new_content']}, room=str(data['receiver_id']))
        emit('message_edited', {'message_id': data['message_id'], 'new_content': data['new_content']}, room=str(current_user.id))
        logger.event("MESSAGE_EDITED_SUCCESS", f"Message {data['message_id']} edited successfully")

@socketio.on('react_to_message')
def handle_react(data):
    if not current_user.is_authenticated:
        return
    
    logger.event("REACT_SOCKET", f"User reacting to message", extra={
        'user': current_user.id,
        'message_id': data['message_id'],
        'reaction': data['reaction']
    })
    
    message_id = data['message_id']
    reaction = data['reaction']
    receiver_id = data['receiver_id']
    existing = supabase.table('message_reactions').select('*').eq('message_id', message_id).eq('user_id', current_user.id).eq('reaction', reaction).execute()
    if existing.data:
        remove_reaction(message_id, current_user.id, reaction)
        logger.debug("REACTION_REMOVED", f"Reaction {reaction} removed")
    else:
        add_reaction(message_id, current_user.id, reaction)
        logger.debug("REACTION_ADDED", f"Reaction {reaction} added")
    
    new_reactions = get_reactions_for_message(message_id)
    emit('reaction_updated', {'message_id': message_id, 'reactions': new_reactions}, room=str(receiver_id))
    emit('reaction_updated', {'message_id': message_id, 'reactions': new_reactions}, room=str(current_user.id))

@socketio.on('mark_read')
def handle_mark_read(data):
    if not current_user.is_authenticated:
        return
    
    sender_id = data['sender_id']
    logger.event("MARK_READ_SOCKET", f"User marking messages as read", extra={
        'reader': current_user.id,
        'sender': sender_id
    })
    
    marked = mark_messages_as_read(current_user.id, sender_id)
    if marked:
        emit('messages_read', {'reader_id': current_user.id, 'sender_id': sender_id}, room=str(sender_id))
        emit('unread_update', {'sender_id': current_user.id, 'count': 0}, room=str(sender_id))
        logger.event("MARK_READ_SUCCESS", f"Messages marked read from {sender_id}")

@socketio.on('typing')
def handle_typing(data):
    if not current_user.is_authenticated:
        return
    
    logger.debug("TYPING_EVENT", f"User typing status", extra={
        'user': current_user.id,
        'is_typing': data['is_typing'],
        'target': data['receiver_id']
    })
    
    emit('user_typing', {'user_id': current_user.id, 'is_typing': data['is_typing']}, room=str(data['receiver_id']))

# ========== WEBRTC SIGNALING WITH MICRO-DETAILING ==========
@socketio.on('call_user')
def handle_call_user(data):
    if not current_user.is_authenticated:
        logger.warn("CALL_UNAUTH", "Unauthenticated user tried to initiate call")
        emit('call_error', {'message': 'Not authenticated'}, room=request.sid)
        return
    
    target_id = data.get('target_id')
    call_type = data.get('call_type')
    offer = data.get('offer')
    
    caller_id = str(current_user.id)
    receiver_id = str(target_id)
    
    logger.event("CALL_INITIATED", f"Call initiated", extra={
        'caller_id': caller_id,
        'caller_name': current_user.username,
        'receiver_id': receiver_id,
        'call_type': call_type,
        'has_offer': offer is not None,
        'timestamp_ms': get_timestamp_ms()
    })
    
    # Validation checks
    if not target_id or not call_type:
        logger.warn("CALL_MISSING_PARAMS", "Missing call parameters", extra={'target_id': target_id, 'call_type': call_type})
        emit('call_error', {'message': 'Missing parameters'}, room=request.sid)
        return
    
    # Check if caller is already in a call
    with call_lock:
        if current_user.id in active_calls:
            logger.warn("CALL_ALREADY_ACTIVE", f"Caller already in a call", extra={'caller_id': caller_id, 'active_call': active_calls[current_user.id]})
            emit('call_error', {'message': 'You are already in a call'}, room=request.sid)
            return
        
        if target_id in active_calls:
            logger.warn("CALL_RECEIVER_BUSY", f"Receiver is already in a call", extra={'receiver_id': receiver_id, 'active_call': active_calls[target_id]})
            emit('call_busy', {'message': 'User is on another call'}, room=request.sid)
            return
    
    # Check if receiver is online
    try:
        target_user = supabase.table('chat_users').select('is_online').eq('id', target_id).execute()
        if not target_user.data or not target_user.data[0].get('is_online', False):
            logger.warn("CALL_RECEIVER_OFFLINE", f"Receiver is offline", extra={'receiver_id': receiver_id})
            emit('call_error', {'message': 'User is offline'}, room=request.sid)
            return
        else:
            logger.debug("CALL_RECEIVER_ONLINE", f"Receiver is online", extra={'receiver_id': receiver_id})
    except Exception as e:
        logger.error("CALL_STATUS_CHECK_ERROR", str(e), extra={'receiver_id': receiver_id})
        emit('call_error', {'message': 'Cannot check user status'}, room=request.sid)
        return
    
    # Register the call
    with call_lock:
        active_calls[current_user.id] = {'with': target_id, 'type': call_type, 'state': 'calling', 'started_at': get_timestamp_ms()}
        logger.event("CALL_REGISTERED", f"Call registered in active_calls", extra={'caller_id': caller_id, 'call_data': active_calls[current_user.id]})
    
    # Set timeout
    timeout_timer = Timer(30.0, lambda: handle_call_timeout(current_user.id, target_id))
    timeout_timer.daemon = True
    timeout_timer.start()
    call_timeouts[current_user.id] = timeout_timer
    logger.debug("CALL_TIMEOUT_SET", f"30 second timeout set for call", extra={'caller_id': caller_id})
    
    # Send incoming call to receiver
    logger.event("SENDING_INCOMING_CALL", f"Sending incoming call event", extra={
        'from': caller_id,
        'to': receiver_id,
        'room': receiver_id,
        'call_type': call_type
    })
    
    emit('incoming_call', {
        'caller_id': current_user.id,
        'caller_name': current_user.username,
        'call_type': call_type,
        'offer': offer,
        'timestamp': get_timestamp_ms()
    }, room=receiver_id)
    
    logger.event("INCOMING_CALL_SENT", f"Incoming call event emitted successfully", extra={'receiver_room': receiver_id})

@socketio.on('answer_call')
def handle_answer_call(data):
    if not current_user.is_authenticated:
        logger.warn("ANSWER_UNAUTH", "Unauthenticated user tried to answer call")
        emit('call_error', {'message': 'Not authenticated'}, room=request.sid)
        return
    
    caller_id = data.get('caller_id')
    answer_sdp = data.get('answer')
    call_type = data.get('call_type')
    
    logger.event("CALL_ANSWERED", f"Call answered", extra={
        'answerer_id': current_user.id,
        'caller_id': caller_id,
        'has_answer': answer_sdp is not None,
        'call_type': call_type,
        'timestamp_ms': get_timestamp_ms()
    })
    
    if not caller_id or not answer_sdp:
        logger.warn("ANSWER_MISSING_PARAMS", "Missing answer parameters")
        emit('call_error', {'message': 'Missing parameters'}, room=request.sid)
        return
    
    with call_lock:
        if caller_id not in active_calls:
            logger.warn("ANSWER_CALL_NOT_FOUND", f"Call not found for caller {caller_id}", extra={'active_calls': list(active_calls.keys())})
            emit('call_error', {'message': 'Call no longer exists'}, room=request.sid)
            return
        
        # Cancel timeout
        if caller_id in call_timeouts:
            try:
                call_timeouts[caller_id].cancel()
                del call_timeouts[caller_id]
                logger.debug("CALL_TIMEOUT_CANCELLED", f"Timeout cancelled for caller {caller_id}")
            except:
                pass
        
        # Update call states
        if caller_id in active_calls:
            active_calls[caller_id]['state'] = 'connected'
            active_calls[caller_id]['answered_at'] = get_timestamp_ms()
            logger.event("CALL_STATE_UPDATED", f"Caller state updated to connected", extra={'caller_id': caller_id})
        
        active_calls[current_user.id] = {'with': caller_id, 'type': call_type, 'state': 'connected', 'started_at': get_timestamp_ms()}
        logger.event("ANSWERER_REGISTERED", f"Answerer registered in active_calls", extra={'answerer_id': current_user.id})
    
    # Send answer to caller
    emit('call_answered', {'answer': answer_sdp, 'timestamp': get_timestamp_ms()}, room=str(caller_id))
    logger.event("ANSWER_SENT", f"Answer sent to caller", extra={'caller_room': caller_id})

@socketio.on('reject_call')
def handle_reject_call(data):
    if not current_user.is_authenticated:
        return
    
    caller_id = data.get('caller_id')
    
    logger.event("CALL_REJECTED", f"Call rejected", extra={
        'rejecter_id': current_user.id,
        'caller_id': caller_id,
        'timestamp_ms': get_timestamp_ms()
    })
    
    # Cancel timeout
    if caller_id in call_timeouts:
        try:
            call_timeouts[caller_id].cancel()
            del call_timeouts[caller_id]
            logger.debug("TIMEOUT_CANCELLED_ON_REJECT", f"Timeout cancelled for {caller_id}")
        except:
            pass
    
    # Remove from active calls
    with call_lock:
        if caller_id in active_calls:
            del active_calls[caller_id]
            logger.debug("CALL_REMOVED_ON_REJECT", f"Removed caller {caller_id} from active_calls")
    
    # Send rejection
    emit('call_rejected', {'timestamp': get_timestamp_ms()}, room=str(caller_id))
    logger.event("REJECTION_SENT", f"Rejection sent to caller", extra={'caller_room': caller_id})

@socketio.on('webrtc_offer')
def handle_webrtc_offer(data):
    if not current_user.is_authenticated:
        return
    
    target = data.get('target')
    
    logger.event("WEBRTC_OFFER", f"WebRTC offer sent", extra={
        'sender': current_user.id,
        'target': target,
        'timestamp_ms': get_timestamp_ms()
    })
    
    emit('webrtc_offer', data, room=str(target))
    logger.debug("OFFER_FORWARDED", f"Offer forwarded to room {target}")

@socketio.on('webrtc_answer')
def handle_webrtc_answer(data):
    if not current_user.is_authenticated:
        return
    
    target = data.get('target')
    
    logger.event("WEBRTC_ANSWER", f"WebRTC answer sent", extra={
        'sender': current_user.id,
        'target': target,
        'timestamp_ms': get_timestamp_ms()
    })
    
    emit('webrtc_answer', data, room=str(target))
    logger.debug("ANSWER_FORWARDED", f"Answer forwarded to room {target}")

@socketio.on('ice_candidate')
def handle_ice_candidate(data):
    if not current_user.is_authenticated:
        return
    
    target_id = data.get('target_id')
    candidate = data.get('candidate')
    
    logger.event("ICE_CANDIDATE", f"ICE candidate sent", extra={
        'sender': current_user.id,
        'target': target_id,
        'has_candidate': candidate is not None,
        'timestamp_ms': get_timestamp_ms()
    })
    
    if target_id and candidate:
        emit('ice_candidate', {'candidate': candidate, 'sender': current_user.id, 'timestamp': get_timestamp_ms()}, room=str(target_id))
        logger.debug("ICE_FORWARDED", f"ICE candidate forwarded to room {target_id}")
    else:
        logger.warn("ICE_MISSING_DATA", f"Missing target_id or candidate", extra={'target_id': target_id, 'has_candidate': candidate is not None})

@socketio.on('end_call')
def handle_end_call(data):
    if not current_user.is_authenticated:
        return
    
    target_id = data.get('target_id')
    
    logger.event("CALL_ENDED", f"Call ended by user", extra={
        'ender_id': current_user.id,
        'explicit_target': target_id,
        'timestamp_ms': get_timestamp_ms()
    })
    
    # Find target if not provided
    if not target_id:
        for uid, call in active_calls.items():
            if uid == current_user.id:
                target_id = call.get('with')
                logger.debug("TARGET_FOUND_FROM_CALLER", f"Found target {target_id} from caller's active call")
                break
            elif call.get('with') == current_user.id:
                target_id = uid
                logger.debug("TARGET_FOUND_FROM_RECEIVER", f"Found target {target_id} from receiver's active call")
                break
    
    # Clean up active calls
    with call_lock:
        if current_user.id in active_calls:
            call_data = active_calls[current_user.id]
            logger.event("CALL_CLEANUP_CALLER", f"Removing caller from active_calls", extra={'call_data': call_data})
            del active_calls[current_user.id]
        
        if target_id and target_id in active_calls:
            call_data = active_calls[target_id]
            logger.event("CALL_CLEANUP_TARGET", f"Removing target from active_calls", extra={'call_data': call_data})
            del active_calls[target_id]
    
    # Cancel timeout if exists
    if current_user.id in call_timeouts:
        try:
            call_timeouts[current_user.id].cancel()
            del call_timeouts[current_user.id]
            logger.debug("TIMEOUT_CANCELLED_ON_END", f"Timeout cancelled for {current_user.id}")
        except:
            pass
    
    # Notify other party
    if target_id:
        emit('call_ended', {'ended_by': current_user.id, 'timestamp': get_timestamp_ms()}, room=str(target_id))
        logger.event("CALL_END_NOTIFICATION", f"Call end notification sent", extra={'target_room': target_id})

# Helper function to get active rooms (for debugging)
def get_active_rooms():
    """Get all active rooms for debugging"""
    try:
        rooms = socketio.server.rooms(socketio.server)
        logger.debug("ACTIVE_ROOMS_QUERY", f"Active rooms: {rooms}")
        return rooms
    except Exception as e:
        logger.error("ACTIVE_ROOMS_ERROR", str(e))
        return set()

# ----------------- RUN APPLICATION -----------------
if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    debug_mode = os.environ.get('DEBUG', 'False').lower() == 'true'
    
    print("=" * 80)
    print("🚀 CHAT APPLICATION STARTING WITH MICRO-DETAILING LOGGING")
    print("=" * 80)
    print(f"📡 Server will run on: http://0.0.0.0:{port}")
    print(f"🐛 Debug mode: {debug_mode}")
    print(f"📝 Log level: DEBUG (micro-detailing enabled)")
    print("=" * 80)
    print("")
    
    socketio.run(
        app, 
        host='0.0.0.0', 
        port=port, 
        debug=debug_mode,
        allow_unsafe_werkzeug=True
    )