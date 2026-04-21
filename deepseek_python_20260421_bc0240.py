# IMPORTANT: monkey_patch must be the FIRST line
from gevent import monkey
monkey.patch_all()  # This must be first!

# Now all other imports
import os
import uuid
from datetime import datetime, timezone
from flask import Flask, render_template, request, redirect, url_for, flash, jsonify, session
from flask_socketio import SocketIO, emit, join_room, leave_room
from flask_login import LoginManager, UserMixin, login_user, logout_user, login_required, current_user
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename
from supabase import create_client, Client
from dotenv import load_dotenv
import pytz
from threading import Timer, Lock
import logging
import traceback
import eventlet
from eventlet import wsgi
import time

# Setup logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

load_dotenv()

app = Flask(__name__)
app.config['SECRET_KEY'] = os.getenv('SECRET_KEY', 'fallback-secret-key-change-me')
app.config['MAX_CONTENT_LENGTH'] = 50 * 1024 * 1024
app.config['SESSION_COOKIE_SECURE'] = False
app.config['SESSION_COOKIE_HTTPONLY'] = True
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'
app.config['PERMANENT_SESSION_LIFETIME'] = 7 * 24 * 3600

# Supabase client
supabase_url = os.getenv('SUPABASE_URL')
supabase_key = os.getenv('SUPABASE_KEY')
if not supabase_url or not supabase_key:
    raise RuntimeError("SUPABASE_URL and SUPABASE_KEY must be set")
supabase: Client = create_client(supabase_url, supabase_key)

# SocketIO - OPTIMIZED FOR SPEED
socketio = SocketIO(
    app, 
    cors_allowed_origins="*", 
    async_mode='gevent',
    ping_timeout=30,  # Reduced from 60 for faster disconnection detection
    ping_interval=15,  # Reduced from 25 for more responsive connection
    max_http_buffer_size=50 * 1024 * 1024,  # Increased to 50MB for video
    engineio_logger=False,  # Disable in production for speed
    logger=False,  # Disable in production for speed
    always_connect=True,  # Better connection handling
    transports=['websocket', 'polling']  # Prefer WebSocket for speed
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
peer_connections = {}  # Track peer connections for better cleanup

# ========== HELPER FUNCTIONS ==========
def get_utc_time():
    return datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')

def format_ist_time(timestamp_str):
    if not timestamp_str:
        return ""
    try:
        clean_timestamp = timestamp_str.replace('Z', '+00:00')
        dt_utc = datetime.fromisoformat(clean_timestamp)
        dt_ist = dt_utc.astimezone(IST)
        return dt_ist.strftime("%b %d, %Y, %I:%M %p")
    except:
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
    try:
        result = supabase.table('chat_users').select('*').eq('id', user_id).execute()
        if result.data:
            u = result.data[0]
            return User(u['id'], u['username'], u['email'], u.get('is_online', False), u.get('last_seen'))
    except Exception as e:
        logger.error(f"Supabase load_user error: {e}")
    return None

# ----------------- Safe helpers -----------------
def supabase_execute_safe(query_func, default_return=None):
    try:
        result = query_func()
        return result.data if result else default_return
    except Exception as e:
        logger.error(f"Supabase error: {e}")
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
        return counts
    except:
        return {}

def get_messages_between(u1, u2, limit=20, offset=0):
    try:
        r = supabase.table('messages').select('*')\
            .or_(f"and(sender_id.eq.{u1},receiver_id.eq.{u2}),and(sender_id.eq.{u2},receiver_id.eq.{u1})")\
            .order('created_at', desc=True)\
            .range(offset, offset+limit-1)\
            .execute()
        return list(reversed(r.data))
    except:
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
        return reactions_by_msg
    except:
        return {}

def save_message(sender_id, receiver_id, msg_type, content, reply_to_id=None, reply_to_content=None):
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
        return r.data[0] if r.data else None
    except:
        return None

def mark_messages_as_read(receiver_id, sender_id):
    try:
        r = supabase.table('messages').update({'is_read': True})\
            .eq('sender_id', sender_id)\
            .eq('receiver_id', receiver_id)\
            .eq('is_read', False)\
            .execute()
        return r.data
    except:
        return []

def edit_message(message_id, user_id, new_content):
    try:
        msg = supabase.table('messages').select('*').eq('id', message_id).execute()
        if msg.data and msg.data[0]['sender_id'] == user_id and msg.data[0]['message_type'] == 'text':
            supabase.table('messages').update({'content': new_content, 'edited': True}).eq('id', message_id).execute()
            return True
    except:
        pass
    return False

def add_reaction(message_id, user_id, reaction):
    try:
        supabase.table('message_reactions').insert({
            'message_id': message_id,
            'user_id': user_id,
            'reaction': reaction
        }).execute()
        return True
    except:
        return False

def remove_reaction(message_id, user_id, reaction):
    try:
        supabase.table('message_reactions').delete()\
            .eq('message_id', message_id)\
            .eq('user_id', user_id)\
            .eq('reaction', reaction)\
            .execute()
        return True
    except:
        return False

def get_reactions_for_message(message_id):
    try:
        r = supabase.table('message_reactions').select('*').eq('message_id', message_id).execute()
        return r.data
    except:
        return []

# ----------------- Call timeout handler -----------------
def handle_call_timeout(caller_id, target_id):
    try:
        with call_lock:
            if caller_id in active_calls and active_calls[caller_id].get('state') == 'calling':
                del active_calls[caller_id]
                socketio.emit('call_timeout', room=caller_id)
            if caller_id in call_timeouts:
                del call_timeouts[caller_id]
    except:
        pass

# ----------------- ROUTES -----------------
@app.route('/')
def index():
    if current_user.is_authenticated:
        return redirect(url_for('users'))
    return redirect(url_for('login'))

@app.route('/check_auth')
def check_auth():
    return jsonify({'authenticated': current_user.is_authenticated})

@app.route('/register', methods=['GET', 'POST'])
def register():
    if current_user.is_authenticated:
        return redirect(url_for('users'))
    if request.method == 'POST':
        username = request.form['username']
        email = request.form['email']
        password = request.form['password']
        
        if get_user_by_username(username):
            flash('Username already exists', 'danger')
            return redirect(url_for('register'))
        if get_user_by_email(email):
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
                flash('Registration successful! Please login.', 'success')
                return redirect(url_for('login'))
        except:
            pass
        flash('Registration failed', 'danger')
    return render_template('register.html')

@app.route('/login', methods=['GET', 'POST'])
def login():
    if current_user.is_authenticated:
        return redirect(url_for('users'))
    
    if request.method == 'POST':
        username = request.form['username']
        password = request.form['password']
        user_data = get_user_by_username(username)
        
        if user_data and check_password_hash(user_data[0]['password_hash'], password):
            user = User(user_data[0]['id'], user_data[0]['username'], user_data[0]['email'], user_data[0]['is_online'])
            login_user(user, remember=True)
            session.permanent = True
            
            try:
                supabase.table('chat_users').update({'is_online': True, 'last_seen': None}).eq('id', user.id).execute()
                socketio.emit('user_status', {'user_id': user.id, 'is_online': True, 'last_seen': None})
            except:
                pass
            
            next_page = request.args.get('next')
            if next_page and next_page.startswith('/'):
                return redirect(next_page)
            return redirect(url_for('users'))
        else:
            flash('Invalid username or password', 'danger')
    
    return render_template('login.html')

@app.route('/logout')
@login_required
def logout():
    try:
        supabase.table('chat_users').update({'is_online': False, 'last_seen': get_utc_time()}).eq('id', current_user.id).execute()
        socketio.emit('user_status', {'user_id': current_user.id, 'is_online': False, 'last_seen': get_utc_time()})
        
        # Clean up calls
        with call_lock:
            if current_user.id in active_calls:
                target_id = active_calls[current_user.id].get('with')
                if target_id and target_id in active_calls:
                    del active_calls[target_id]
                del active_calls[current_user.id]
                if target_id:
                    socketio.emit('call_ended', room=target_id)
    except:
        pass
    
    logout_user()
    flash('Logged out successfully', 'success')
    return redirect(url_for('login'))

@app.route('/users')
@login_required
def users():
    all_users = get_all_users_except(current_user.id)
    unread_counts = get_unread_counts(current_user.id)
    for user in all_users:
        if user.get('last_seen'):
            user['last_seen_formatted'] = format_ist_time(user['last_seen'])
        else:
            user['last_seen_formatted'] = 'recently'
    return render_template('users.html', users=all_users, unread_counts=unread_counts)

@app.route('/chat/<user_id>')
@login_required
def chat(user_id):
    try:
        other_user_data = supabase.table('chat_users').select('*').eq('id', user_id).execute()
        if not other_user_data.data:
            flash('User not found', 'danger')
            return redirect(url_for('users'))
        other_user = other_user_data.data[0]
    except:
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
        socketio.emit('messages_read', {'reader_id': current_user.id, 'sender_id': user_id}, room=user_id)
        unread_after = get_unread_counts(current_user.id).get(user_id, 0)
        socketio.emit('unread_update', {'sender_id': user_id, 'count': unread_after}, room=str(current_user.id))
    
    return render_template('chat.html', other_user=other_user, messages=messages, current_user=current_user)

@app.route('/load_more_messages')
@login_required
def load_more_messages():
    other_user_id = request.args.get('other_user_id')
    offset = int(request.args.get('offset', 0))
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
    if edit_message(message_id, current_user.id, new_content):
        socketio.emit('message_edited', {'message_id': message_id, 'new_content': new_content}, room=receiver_id)
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
    existing = supabase.table('message_reactions').select('*').eq('message_id', message_id).eq('user_id', current_user.id).eq('reaction', reaction).execute()
    if existing.data:
        remove_reaction(message_id, current_user.id, reaction)
    else:
        add_reaction(message_id, current_user.id, reaction)
    new_reactions = get_reactions_for_message(message_id)
    socketio.emit('reaction_updated', {'message_id': message_id, 'reactions': new_reactions}, room=receiver_id)
    socketio.emit('reaction_updated', {'message_id': message_id, 'reactions': new_reactions}, room=str(current_user.id))
    return jsonify({'success': True, 'reactions': new_reactions})

@app.route('/search_messages')
@login_required
def search_messages():
    q = request.args.get('q', '')
    other_user_id = request.args.get('other_user_id')
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
        return jsonify(results)
    except:
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
    except:
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
        socketio.emit('new_message', msg_dict, room=receiver_id)
        socketio.emit('new_message', msg_dict, room=str(current_user.id))
        unread_count = get_unread_counts(receiver_id).get(current_user.id, 0)
        socketio.emit('unread_update', {'sender_id': current_user.id, 'count': unread_count}, room=receiver_id)
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
    except:
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
        socketio.emit('new_message', msg_dict, room=receiver_id)
        socketio.emit('new_message', msg_dict, room=str(current_user.id))
        unread_count = get_unread_counts(receiver_id).get(current_user.id, 0)
        socketio.emit('unread_update', {'sender_id': current_user.id, 'count': unread_count}, room=receiver_id)
        return {'success': True}
    return {'error': 'Save failed'}, 500

@app.route('/audio-call/<user_id>')
@login_required
def audio_call(user_id):
    try:
        other_user_data = supabase.table('chat_users').select('*').eq('id', user_id).execute()
        if not other_user_data.data:
            flash('User not found', 'danger')
            return redirect(url_for('users'))
        return render_template('audio.call.html', other_user=other_user_data.data[0])
    except:
        flash('Error starting call', 'danger')
        return redirect(url_for('users'))

@app.route('/video-call/<user_id>')
@login_required
def video_call(user_id):
    try:
        other_user_data = supabase.table('chat_users').select('*').eq('id', user_id).execute()
        if not other_user_data.data:
            flash('User not found', 'danger')
            return redirect(url_for('users'))
        return render_template('video.call.html', other_user=other_user_data.data[0])
    except:
        flash('Error starting call', 'danger')
        return redirect(url_for('users'))

@app.route('/delete_message/<message_id>', methods=['POST'])
@login_required
def delete_message_route(message_id):
    data = request.get_json()
    delete_for = data.get('delete_for')
    try:
        msg = supabase.table('messages').select('*').eq('id', message_id).execute()
        if not msg.data:
            return jsonify({'error': 'Message not found'}), 404
        msg = msg.data[0]
        if msg['sender_id'] != current_user.id and delete_for == 'everyone':
            return jsonify({'error': 'Not authorized'}), 403
        if delete_for == 'everyone':
            supabase.table('messages').update({'is_deleted': True}).eq('id', message_id).execute()
            socketio.emit('message_deleted', {'message_id': message_id, 'for_everyone': True}, room=msg['sender_id'])
            socketio.emit('message_deleted', {'message_id': message_id, 'for_everyone': True}, room=msg['receiver_id'])
        else:
            socketio.emit('message_deleted', {'message_id': message_id, 'for_everyone': False}, room=current_user.id)
        return jsonify({'success': True})
    except:
        return jsonify({'error': 'Server error'}), 500

# ----------------- SOCKETIO EVENTS - OPTIMIZED -----------------
@socketio.on('connect')
def handle_connect():
    if current_user.is_authenticated:
        join_room(str(current_user.id))
        try:
            supabase.table('chat_users').update({'is_online': True, 'last_seen': None}).eq('id', current_user.id).execute()
            emit('user_status', {'user_id': current_user.id, 'is_online': True, 'last_seen': None}, broadcast=True)
        except:
            pass

@socketio.on('disconnect')
def handle_disconnect():
    if current_user.is_authenticated:
        leave_room(str(current_user.id))
        try:
            supabase.table('chat_users').update({'is_online': False, 'last_seen': get_utc_time()}).eq('id', current_user.id).execute()
            emit('user_status', {'user_id': current_user.id, 'is_online': False, 'last_seen': get_utc_time()}, broadcast=True)
        except:
            pass

@socketio.on('send_message')
def handle_send_message(data):
    if not current_user.is_authenticated:
        return
    
    receiver_id = data['receiver_id']
    content = data['content']
    msg_type = data.get('message_type', 'text')
    reply_to_id = data.get('reply_to_id', None)
    reply_to_content = None
    if reply_to_id:
        try:
            orig = supabase.table('messages').select('content').eq('id', reply_to_id).execute()
            if orig.data:
                reply_to_content = orig.data[0]['content'][:100]
        except:
            pass
    
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
        emit('new_message', msg_dict, room=receiver_id)
        emit('new_message', msg_dict, room=str(current_user.id))
        unread_count = get_unread_counts(receiver_id).get(current_user.id, 0)
        emit('unread_update', {'sender_id': current_user.id, 'count': unread_count}, room=receiver_id)

@socketio.on('edit_message')
def handle_edit_message(data):
    if not current_user.is_authenticated:
        return
    
    if edit_message(data['message_id'], current_user.id, data['new_content']):
        emit('message_edited', {'message_id': data['message_id'], 'new_content': data['new_content']}, room=data['receiver_id'])
        emit('message_edited', {'message_id': data['message_id'], 'new_content': data['new_content']}, room=str(current_user.id))

@socketio.on('react_to_message')
def handle_react(data):
    if not current_user.is_authenticated:
        return
    
    message_id = data['message_id']
    reaction = data['reaction']
    receiver_id = data['receiver_id']
    existing = supabase.table('message_reactions').select('*').eq('message_id', message_id).eq('user_id', current_user.id).eq('reaction', reaction).execute()
    if existing.data:
        remove_reaction(message_id, current_user.id, reaction)
    else:
        add_reaction(message_id, current_user.id, reaction)
    new_reactions = get_reactions_for_message(message_id)
    emit('reaction_updated', {'message_id': message_id, 'reactions': new_reactions}, room=receiver_id)
    emit('reaction_updated', {'message_id': message_id, 'reactions': new_reactions}, room=str(current_user.id))

@socketio.on('mark_read')
def handle_mark_read(data):
    if not current_user.is_authenticated:
        return
    
    sender_id = data['sender_id']
    marked = mark_messages_as_read(current_user.id, sender_id)
    if marked:
        emit('messages_read', {'reader_id': current_user.id, 'sender_id': sender_id}, room=sender_id)
        emit('unread_update', {'sender_id': current_user.id, 'count': 0}, room=sender_id)

@socketio.on('typing')
def handle_typing(data):
    if not current_user.is_authenticated:
        return
    
    emit('user_typing', {'user_id': current_user.id, 'is_typing': data['is_typing']}, room=data['receiver_id'])

# OPTIMIZED WebRTC signaling for better audio/video
@socketio.on('call_user')
def handle_call_user(data):
    target_id = data.get('target_id')
    call_type = data.get('call_type')
    offer = data.get('offer')
    
    if not target_id or not call_type:
        emit('call_error', {'message': 'Missing parameters'}, room=request.sid)
        return
    
    with call_lock:
        if current_user.id in active_calls:
            emit('call_error', {'message': 'You are already in a call'}, room=request.sid)
            return
        
        if target_id in active_calls:
            emit('call_busy', {'message': 'User is on another call'}, room=request.sid)
            return
    
    try:
        target_user = supabase.table('chat_users').select('is_online').eq('id', target_id).execute()
        if not target_user.data or not target_user.data[0].get('is_online', False):
            emit('call_error', {'message': 'User is offline'}, room=request.sid)
            return
    except:
        emit('call_error', {'message': 'Cannot check user status'}, room=request.sid)
        return
    
    with call_lock:
        active_calls[current_user.id] = {'with': target_id, 'type': call_type, 'state': 'calling'}
    
    timeout_timer = Timer(30.0, lambda: handle_call_timeout(current_user.id, target_id))
    timeout_timer.daemon = True
    timeout_timer.start()
    call_timeouts[current_user.id] = timeout_timer
    
    emit('incoming_call', {
        'caller_id': current_user.id,
        'caller_name': current_user.username,
        'call_type': call_type,
        'offer': offer
    }, room=target_id)

@socketio.on('answer_call')
def handle_answer_call(data):
    caller_id = data.get('caller_id')
    answer_sdp = data.get('answer')
    call_type = data.get('call_type')
    
    if not caller_id or not answer_sdp:
        emit('call_error', {'message': 'Missing parameters'}, room=request.sid)
        return
    
    with call_lock:
        if caller_id not in active_calls:
            emit('call_error', {'message': 'Call no longer exists'}, room=request.sid)
            return
        
        if caller_id in call_timeouts:
            try:
                call_timeouts[caller_id].cancel()
                del call_timeouts[caller_id]
            except:
                pass
        
        if caller_id in active_calls:
            active_calls[caller_id]['state'] = 'connected'
        
        active_calls[current_user.id] = {'with': caller_id, 'type': call_type, 'state': 'connected'}
    
    emit('call_answered', {'answer': answer_sdp}, room=caller_id)

@socketio.on('reject_call')
def handle_reject_call(data):
    caller_id = data.get('caller_id')
    
    if caller_id in call_timeouts:
        try:
            call_timeouts[caller_id].cancel()
            del call_timeouts[caller_id]
        except:
            pass
    
    with call_lock:
        if caller_id in active_calls:
            del active_calls[caller_id]
    
    emit('call_rejected', room=caller_id)

@socketio.on('ice_candidate')
def handle_ice_candidate(data):
    target_id = data.get('target_id')
    candidate = data.get('candidate')
    
    if target_id and candidate:
        # Send ICE candidate immediately without delay
        emit('ice_candidate', {'candidate': candidate}, room=target_id)

@socketio.on('end_call')
def handle_end_call(data):
    target_id = data.get('target_id')
    
    if not target_id:
        for uid, call in active_calls.items():
            if uid == current_user.id:
                target_id = call.get('with')
                break
            elif call.get('with') == current_user.id:
                target_id = uid
                break
    
    with call_lock:
        if current_user.id in active_calls:
            del active_calls[current_user.id]
        if target_id and target_id in active_calls:
            del active_calls[target_id]
    
    if current_user.id in call_timeouts:
        try:
            call_timeouts[current_user.id].cancel()
            del call_timeouts[current_user.id]
        except:
            pass
    
    if target_id:
        emit('call_ended', room=target_id)

# ----------------- RUN APPLICATION -----------------
if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    debug_mode = os.environ.get('DEBUG', 'False').lower() == 'true'
    
    socketio.run(
        app, 
        host='0.0.0.0', 
        port=port, 
        debug=debug_mode,
        allow_unsafe_werkzeug=True,
        use_reloader=False  # Disable reloader for better performance
    )