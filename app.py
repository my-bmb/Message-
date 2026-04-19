import os
import uuid
from datetime import datetime
from flask import Flask, render_template, request, redirect, url_for, flash, session
from flask_socketio import SocketIO, emit, join_room, leave_room
from flask_login import LoginManager, UserMixin, login_user, logout_user, login_required, current_user
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename
from supabase import create_client, Client
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__)
app.config['SECRET_KEY'] = os.getenv('SECRET_KEY')
app.config['MAX_CONTENT_LENGTH'] = 50 * 1024 * 1024

# Supabase client
supabase: Client = create_client(os.getenv('SUPABASE_URL'), os.getenv('SUPABASE_KEY'))

socketio = SocketIO(app, cors_allowed_origins="*")
login_manager = LoginManager(app)
login_manager.login_view = 'login'

# Custom User class for Flask-Login
class User(UserMixin):
    def __init__(self, id, username, email, is_online):
        self.id = id
        self.username = username
        self.email = email
        self.is_online = is_online

@login_manager.user_loader
def load_user(user_id):
    # UPDATED: using 'chat_users' table
    result = supabase.table('chat_users').select('*').eq('id', user_id).execute()
    if result.data:
        user_data = result.data[0]
        return User(
            id=user_data['id'],
            username=user_data['username'],
            email=user_data['email'],
            is_online=user_data.get('is_online', False)
        )
    return None

# Helper functions (all updated to 'chat_users')
def get_user_by_username(username):
    result = supabase.table('chat_users').select('*').eq('username', username).execute()
    return result.data[0] if result.data else None

def get_user_by_email(email):
    result = supabase.table('chat_users').select('*').eq('email', email).execute()
    return result.data[0] if result.data else None

def get_all_users_except(user_id):
    result = supabase.table('chat_users').select('*').neq('id', user_id).execute()
    return result.data

def get_messages_between(user1_id, user2_id):
    result = supabase.table('messages').select('*')\
        .or_(f"and(sender_id.eq.{user1_id},receiver_id.eq.{user2_id}),and(sender_id.eq.{user2_id},receiver_id.eq.{user1_id})")\
        .order('created_at').execute()
    return result.data

def save_message(sender_id, receiver_id, message_type, content):
    data = {
        'sender_id': sender_id,
        'receiver_id': receiver_id,
        'message_type': message_type,
        'content': content,
        'is_read': False,
        'created_at': datetime.utcnow().isoformat()
    }
    result = supabase.table('messages').insert(data).execute()
    return result.data[0] if result.data else None

# Routes
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
        
        # Insert into 'chat_users'
        user_data = {
            'username': username,
            'email': email,
            'password_hash': generate_password_hash(password),
            'is_online': False
        }
        result = supabase.table('chat_users').insert(user_data).execute()
        if result.data:
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
            # Update online status in 'chat_users'
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
    if ext in ['png', 'jpg', 'jpeg', 'gif', 'webp']:
        msg_type = 'image'
    elif ext in ['mp4', 'webm', 'avi', 'mov']:
        msg_type = 'video'
    elif ext in ['mp3', 'wav', 'ogg', 'm4a']:
        msg_type = 'audio'
    else:
        msg_type = 'file'
    
    file_path = f"chat_uploads/{uuid.uuid4()}_{secure_filename(file.filename)}"
    file_bytes = file.read()
    supabase.storage.from_('chat-files').upload(file_path, file_bytes)
    public_url = supabase.storage.from_('chat-files').get_public_url(file_path)
    
    message = save_message(current_user.id, receiver_id, msg_type, public_url)
    if message:
        socketio.emit('new_message', {
            'id': message['id'],
            'sender_id': message['sender_id'],
            'receiver_id': message['receiver_id'],
            'message_type': message['message_type'],
            'content': message['content'],
            'created_at': message['created_at']
        }, room=receiver_id)
        return {'success': True, 'url': public_url, 'type': msg_type}
    return {'error': 'Save failed'}, 500

# SocketIO events (unchanged logic, but table names already updated in helpers)
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
        emit('new_message', {
            'id': message['id'],
            'sender_id': message['sender_id'],
            'receiver_id': message['receiver_id'],
            'message_type': message['message_type'],
            'content': message['content'],
            'created_at': message['created_at']
        }, room=receiver_id)
        emit('new_message', { ...same... }, room=str(current_user.id))

@socketio.on('typing')
def handle_typing(data):
    emit('user_typing', {'user_id': current_user.id, 'is_typing': data['is_typing']}, room=data['receiver_id'])

# WebRTC signaling (no change)
@socketio.on('call_user')
def handle_call(data):
    target_room = data['target_id']
    emit('incoming_call', {
        'caller_id': current_user.id,
        'caller_name': current_user.username,
        'offer': data['offer']
    }, room=target_room)

@socketio.on('answer_call')
def handle_answer(data):
    emit('call_answered', {'answer': data['answer']}, room=data['target_id'])

@socketio.on('ice_candidate')
def handle_ice(data):
    emit('ice_candidate', {'candidate': data['candidate']}, room=data['target_id'])

@socketio.on('end_call')
def handle_end_call(data):
    emit('call_ended', room=data['target_id'])

if __name__ == '__main__':
    socketio.run(app, debug=True)
