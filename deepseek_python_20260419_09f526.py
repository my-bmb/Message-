import os
import uuid
from datetime import datetime
from flask import Flask, render_template, request, redirect, url_for, flash, jsonify
from flask_socketio import SocketIO, emit, join_room, leave_room
from flask_login import LoginManager, UserMixin, login_user, logout_user, login_required, current_user
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename
from supabase import create_client, Client
from dotenv import load_dotenv
import pytz
from threading import Timer

load_dotenv()

app = Flask(__name__)
app.config['SECRET_KEY'] = os.getenv('SECRET_KEY', 'fallback-secret-key-change-me')
app.config['MAX_CONTENT_LENGTH'] = 50 * 1024 * 1024

supabase: Client = create_client(os.getenv('SUPABASE_URL'), os.getenv('SUPABASE_KEY'))
socketio = SocketIO(app, cors_allowed_origins="*")
login_manager = LoginManager(app)
login_manager.login_view = 'login'

IST = pytz.timezone('Asia/Kolkata')

def format_ist_time(timestamp_str):
    if not timestamp_str:
        return ""
    try:
        dt_utc = datetime.fromisoformat(timestamp_str.replace('Z', '+00:00'))
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
    result = supabase.table('chat_users').select('*').eq('id', user_id).execute()
    if result.data:
        u = result.data[0]
        return User(u['id'], u['username'], u['email'], u.get('is_online', False), u.get('last_seen'))
    return None

def get_user_by_username(username):
    r = supabase.table('chat_users').select('*').eq('username', username).execute()
    return r.data[0] if r.data else None

def get_user_by_email(email):
    r = supabase.table('chat_users').select('*').eq('email', email).execute()
    return r.data[0] if r.data else None

def get_all_users_except(user_id):
    r = supabase.table('chat_users').select('*').neq('id', user_id).execute()
    return r.data

def get_messages_between(u1, u2):
    r = supabase.table('messages').select('*')\
        .or_(f"and(sender_id.eq.{u1},receiver_id.eq.{u2}),and(sender_id.eq.{u2},receiver_id.eq.{u1})")\
        .order('created_at').execute()
    return r.data

def save_message(sender_id, receiver_id, msg_type, content, reply_to_id=None):
    data = {
        'sender_id': sender_id,
        'receiver_id': receiver_id,
        'message_type': msg_type,
        'content': content,
        'is_read': False,
        'is_deleted': False,
        'reply_to_id': reply_to_id,
        'created_at': datetime.utcnow().isoformat()
    }
    r = supabase.table('messages').insert(data).execute()
    return r.data[0] if r.data else None

def mark_messages_as_read(receiver_id, sender_id):
    r = supabase.table('messages')\
        .update({'is_read': True})\
        .eq('sender_id', sender_id)\
        .eq('receiver_id', receiver_id)\
        .eq('is_read', False)\
        .execute()
    return r.data

def delete_message_for_everyone(message_id, user_id):
    # Only sender can delete for everyone
    msg = supabase.table('messages').select('*').eq('id', message_id).execute()
    if msg.data and msg.data[0]['sender_id'] == user_id:
        supabase.table('messages').update({'is_deleted': True}).eq('id', message_id).execute()
        return True
    return False

# Call state tracking
active_calls = {}
call_timeouts = {}

# ----------------- Routes -----------------
@app.route('/')
def index():
    if current_user.is_authenticated:
        return redirect(url_for('users'))
    return redirect(url_for('login'))

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
            'last_seen': datetime.utcnow().isoformat()
        }
        r = supabase.table('chat_users').insert(user_data).execute()
        if r.data:
            flash('Registration successful! Please login.', 'success')
            return redirect(url_for('login'))
        else:
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
        if user_data and check_password_hash(user_data['password_hash'], password):
            user = User(user_data['id'], user_data['username'], user_data['email'], user_data['is_online'])
            login_user(user)
            supabase.table('chat_users').update({'is_online': True, 'last_seen': datetime.utcnow().isoformat()}).eq('id', user.id).execute()
            socketio.emit('user_status', {'user_id': user.id, 'is_online': True, 'last_seen': None})
            return redirect(url_for('users'))
        else:
            flash('Invalid username or password', 'danger')
    return render_template('login.html')

@app.route('/logout')
@login_required
def logout():
    supabase.table('chat_users').update({'is_online': False, 'last_seen': datetime.utcnow().isoformat()}).eq('id', current_user.id).execute()
    socketio.emit('user_status', {'user_id': current_user.id, 'is_online': False, 'last_seen': datetime.utcnow().isoformat()})
    logout_user()
    flash('Logged out successfully', 'success')
    return redirect(url_for('login'))

@app.route('/users')
@login_required
def users():
    all_users = get_all_users_except(current_user.id)
    return render_template('users.html', users=all_users)

@app.route('/chat/<user_id>')
@login_required
def chat(user_id):
    other_user_data = supabase.table('chat_users').select('*').eq('id', user_id).execute()
    if not other_user_data.data:
        flash('User not found', 'danger')
        return redirect(url_for('users'))
    other_user = other_user_data.data[0]
    messages = get_messages_between(current_user.id, user_id)
    # Filter out deleted messages for everyone (except show "message deleted" placeholder)
    for msg in messages:
        if msg.get('is_deleted'):
            msg['content'] = 'This message was deleted'
            msg['message_type'] = 'text'
        msg['formatted_time'] = format_ist_time(msg['created_at'])
    marked = mark_messages_as_read(current_user.id, user_id)
    if marked:
        socketio.emit('messages_read', {'reader_id': current_user.id, 'sender_id': user_id}, room=user_id)
    return render_template('chat.html', other_user=other_user, messages=messages)

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
    supabase.storage.from_('chat-files').upload(file_path, file_bytes)
    public_url = supabase.storage.from_('chat-files').get_public_url(file_path)
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
            'formatted_time': format_ist_time(message['created_at'])
        }
        socketio.emit('new_message', msg_dict, room=receiver_id)
        socketio.emit('new_message', msg_dict, room=str(current_user.id))
        return {'success': True, 'url': public_url, 'type': msg_type}
    return {'error': 'Save failed'}, 500

@app.route('/audio-call/<user_id>')
@login_required
def audio_call(user_id):
    other_user_data = supabase.table('chat_users').select('*').eq('id', user_id).execute()
    if not other_user_data.data:
        flash('User not found', 'danger')
        return redirect(url_for('users'))
    return render_template('audio.call.html', other_user=other_user_data.data[0])

@app.route('/video-call/<user_id>')
@login_required
def video_call(user_id):
    other_user_data = supabase.table('chat_users').select('*').eq('id', user_id).execute()
    if not other_user_data.data:
        flash('User not found', 'danger')
        return redirect(url_for('users'))
    return render_template('video.call.html', other_user=other_user_data.data[0])

@app.route('/delete_message/<message_id>', methods=['POST'])
@login_required
def delete_message_route(message_id):
    data = request.get_json()
    delete_for = data.get('delete_for')  # 'me' or 'everyone'
    msg = supabase.table('messages').select('*').eq('id', message_id).execute()
    if not msg.data:
        return jsonify({'error': 'Message not found'}), 404
    msg = msg.data[0]
    if msg['sender_id'] != current_user.id and delete_for == 'everyone':
        return jsonify({'error': 'Not authorized'}), 403
    if delete_for == 'everyone':
        supabase.table('messages').update({'is_deleted': True}).eq('id', message_id).execute()
        # Notify both users
        socketio.emit('message_deleted', {'message_id': message_id, 'for_everyone': True}, room=msg['sender_id'])
        socketio.emit('message_deleted', {'message_id': message_id, 'for_everyone': True}, room=msg['receiver_id'])
    else:  # delete for me only
        # We can implement a separate 'deleted_for' list, but for simplicity we just hide on client.
        # We'll send an event to the current user only.
        socketio.emit('message_deleted', {'message_id': message_id, 'for_everyone': False}, room=current_user.id)
    return jsonify({'success': True})

# ----------------- SocketIO Signaling for WebRTC -----------------
@socketio.on('connect')
def handle_connect():
    if current_user.is_authenticated:
        join_room(str(current_user.id))
        # Update online status
        supabase.table('chat_users').update({'is_online': True, 'last_seen': datetime.utcnow().isoformat()}).eq('id', current_user.id).execute()
        emit('user_status', {'user_id': current_user.id, 'is_online': True, 'last_seen': None}, broadcast=True)

@socketio.on('disconnect')
def handle_disconnect():
    if current_user.is_authenticated:
        leave_room(str(current_user.id))
        supabase.table('chat_users').update({'is_online': False, 'last_seen': datetime.utcnow().isoformat()}).eq('id', current_user.id).execute()
        emit('user_status', {'user_id': current_user.id, 'is_online': False, 'last_seen': datetime.utcnow().isoformat()}, broadcast=True)

@socketio.on('send_message')
def handle_send_message(data):
    receiver_id = data['receiver_id']
    content = data['content']
    msg_type = data.get('message_type', 'text')
    reply_to_id = data.get('reply_to_id', None)
    message = save_message(current_user.id, receiver_id, msg_type, content, reply_to_id)
    if message:
        msg_dict = {
            'id': message['id'],
            'sender_id': message['sender_id'],
            'receiver_id': message['receiver_id'],
            'message_type': message['message_type'],
            'content': message['content'],
            'is_read': message['is_read'],
            'created_at': message['created_at'],
            'formatted_time': format_ist_time(message['created_at']),
            'reply_to_id': message.get('reply_to_id')
        }
        emit('new_message', msg_dict, room=receiver_id)
        emit('new_message', msg_dict, room=str(current_user.id))

@socketio.on('mark_read')
def handle_mark_read(data):
    sender_id = data['sender_id']
    marked = mark_messages_as_read(current_user.id, sender_id)
    if marked:
        emit('messages_read', {'reader_id': current_user.id, 'sender_id': sender_id}, room=sender_id)

@socketio.on('typing')
def handle_typing(data):
    emit('user_typing', {'user_id': current_user.id, 'is_typing': data['is_typing']}, room=data['receiver_id'])

# WebRTC signaling events
@socketio.on('call_user')
def handle_call_user(data):
    target_id = data['target_id']
    call_type = data['call_type']
    # Check if target is online
    target_user = supabase.table('chat_users').select('is_online').eq('id', target_id).execute()
    if not target_user.data or not target_user.data[0]['is_online']:
        emit('call_error', {'message': 'User is offline'}, room=request.sid)
        return
    if target_id in active_calls:
        emit('call_busy', {'message': 'User is on another call'}, room=request.sid)
        return
    # Store pending call
    active_calls[current_user.id] = {'with': target_id, 'type': call_type, 'state': 'calling'}
    timeout_timer = Timer(30.0, lambda: handle_call_timeout(current_user.id, target_id))
    timeout_timer.start()
    call_timeouts[current_user.id] = timeout_timer
    # Notify receiver
    emit('incoming_call', {
        'caller_id': current_user.id,
        'caller_name': current_user.username,
        'call_type': call_type,
        'offer': data['offer']
    }, room=target_id)

@socketio.on('answer_call')
def handle_answer_call(data):
    caller_id = data['caller_id']
    answer_sdp = data['answer']
    if caller_id in call_timeouts:
        call_timeouts[caller_id].cancel()
        del call_timeouts[caller_id]
    active_calls[caller_id]['state'] = 'connected'
    active_calls[current_user.id] = {'with': caller_id, 'type': data['call_type'], 'state': 'connected'}
    emit('call_answered', {'answer': answer_sdp}, room=caller_id)

@socketio.on('reject_call')
def handle_reject_call(data):
    caller_id = data['caller_id']
    if caller_id in call_timeouts:
        call_timeouts[caller_id].cancel()
        del call_timeouts[caller_id]
    if caller_id in active_calls:
        del active_calls[caller_id]
    emit('call_rejected', room=caller_id)

@socketio.on('ice_candidate')
def handle_ice_candidate(data):
    emit('ice_candidate', {'candidate': data['candidate']}, room=data['target_id'])

@socketio.on('end_call')
def handle_end_call(data):
    target_id = data.get('target_id')
    if not target_id:
        for uid, call in active_calls.items():
            if uid == current_user.id:
                target_id = call['with']
                break
            elif call['with'] == current_user.id:
                target_id = uid
                break
    if target_id:
        if current_user.id in active_calls:
            del active_calls[current_user.id]
        if target_id in active_calls:
            del active_calls[target_id]
        emit('call_ended', room=target_id)
    if current_user.id in call_timeouts:
        call_timeouts[current_user.id].cancel()
        del call_timeouts[current_user.id]

def handle_call_timeout(caller_id, target_id):
    if caller_id in active_calls and active_calls[caller_id]['state'] == 'calling':
        del active_calls[caller_id]
        socketio.emit('call_timeout', room=caller_id)

def save_call_log(caller_id, receiver_id, call_type, duration_seconds, status):
    data = {
        'caller_id': caller_id,
        'receiver_id': receiver_id,
        'call_type': call_type,
        'duration': duration_seconds,
        'status': status,
        'timestamp': datetime.utcnow().isoformat()
    }
    supabase.table('call_logs').insert(data).execute()

if __name__ == '__main__':
    socketio.run(app, host='0.0.0.0', port=int(os.environ.get('PORT', 5000)), debug=False)