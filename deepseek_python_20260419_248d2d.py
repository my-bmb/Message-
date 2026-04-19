import os
import uuid
from datetime import datetime
from flask import Flask, render_template, request, redirect, url_for, flash
from flask_socketio import SocketIO, emit, join_room, leave_room
from flask_login import LoginManager, UserMixin, login_user, logout_user, login_required, current_user
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename
from supabase import create_client, Client
from dotenv import load_dotenv
import pytz

load_dotenv()

app = Flask(__name__)
app.config['SECRET_KEY'] = os.getenv('SECRET_KEY', 'fallback-secret-key-change-me')
app.config['MAX_CONTENT_LENGTH'] = 50 * 1024 * 1024

supabase: Client = create_client(os.getenv('SUPABASE_URL'), os.getenv('SUPABASE_KEY'))
socketio = SocketIO(app, cors_allowed_origins="*")
login_manager = LoginManager(app)
login_manager.login_view = 'login'

# IST timezone
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
    def __init__(self, id, username, email, is_online):
        self.id = id
        self.username = username
        self.email = email
        self.is_online = is_online

@login_manager.user_loader
def load_user(user_id):
    result = supabase.table('chat_users').select('*').eq('id', user_id).execute()
    if result.data:
        u = result.data[0]
        return User(u['id'], u['username'], u['email'], u.get('is_online', False))
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

def save_message(sender_id, receiver_id, msg_type, content):
    data = {
        'sender_id': sender_id,
        'receiver_id': receiver_id,
        'message_type': msg_type,
        'content': content,
        'is_read': False,
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
            'is_online': False
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
            supabase.table('chat_users').update({'is_online': True}).eq('id', user.id).execute()
            socketio.emit('user_status', {'user_id': user.id, 'is_online': True})
            return redirect(url_for('users'))
        else:
            flash('Invalid username or password', 'danger')
    return render_template('login.html')

@app.route('/logout')
@login_required
def logout():
    supabase.table('chat_users').update({'is_online': False}).eq('id', current_user.id).execute()
    socketio.emit('user_status', {'user_id': current_user.id, 'is_online': False})
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
    marked = mark_messages_as_read(current_user.id, user_id)
    if marked:
        socketio.emit('messages_read', {'reader_id': current_user.id, 'sender_id': user_id}, room=user_id)
    for msg in messages:
        msg['formatted_time'] = format_ist_time(msg['created_at'])
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

# ----------------- SocketIO Signaling for WebRTC -----------------
@socketio.on('connect')
def handle_connect():
    if current_user.is_authenticated:
        join_room(str(current_user.id))
        emit('user_status', {'user_id': current_user.id, 'is_online': True}, broadcast=True)

@socketio.on('disconnect')
def handle_disconnect():
    if current_user.is_authenticated:
        leave_room(str(current_user.id))
        emit('user_status', {'user_id': current_user.id, 'is_online': False}, broadcast=True)

@socketio.on('send_message')
def handle_send_message(data):
    receiver_id = data['receiver_id']
    content = data['content']
    msg_type = data.get('message_type', 'text')
    message = save_message(current_user.id, receiver_id, msg_type, content)
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
    emit('incoming_call', {
        'caller_id': current_user.id,
        'caller_name': current_user.username,
        'call_type': data['call_type'],
        'offer': data['offer']
    }, room=target_id)

@socketio.on('answer_call')
def handle_answer_call(data):
    emit('call_answered', {'answer': data['answer']}, room=data['target_id'])

@socketio.on('ice_candidate')
def handle_ice_candidate(data):
    emit('ice_candidate', {'candidate': data['candidate']}, room=data['target_id'])

@socketio.on('end_call')
def handle_end_call(data):
    emit('call_ended', room=data['target_id'])

if __name__ == '__main__':
    socketio.run(app, host='0.0.0.0', port=int(os.environ.get('PORT', 5000)), debug=False)