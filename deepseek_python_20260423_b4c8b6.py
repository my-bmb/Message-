# IMPORTANT: monkey_patch must be the FIRST line
from gevent import monkey
monkey.patch_all()

# Now all other imports
import os
import uuid
import re
import math
import json
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
import time
import requests
from urllib3.util.retry import Retry
from requests.adapters import HTTPAdapter

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
app.config['REMEMBER_COOKIE_DURATION'] = 7 * 24 * 3600
app.config['REMEMBER_COOKIE_SECURE'] = False

# Supabase client with retry logic
supabase_url = os.getenv('SUPABASE_URL')
supabase_key = os.getenv('SUPABASE_KEY')
if not supabase_url or not supabase_key:
    raise RuntimeError("SUPABASE_URL and SUPABASE_KEY must be set")

# Create Supabase client
supabase: Client = create_client(supabase_url, supabase_key)

# SocketIO - optimized for production
socketio = SocketIO(
    app, 
    cors_allowed_origins="*", 
    async_mode='gevent',
    ping_timeout=60,
    ping_interval=25,
    max_http_buffer_size=50 * 1024 * 1024,
    engineio_logger=False,
    logger=False,
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
ping_timers = {}

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
    except Exception as e:
        logger.error(f"Format time error: {e}")
        return str(timestamp_str)[:16]

def format_distance(meters):
    if meters is None or meters == 999:
        return "Unknown"
    try:
        meters = float(meters)
        if meters < 1000:
            return f"{int(meters)} m"
        else:
            km = meters / 1000
            if km < 10:
                return f"{km:.1f} km"
            else:
                return f"{int(km)} km"
    except (ValueError, TypeError):
        return "Unknown"

def haversine_distance(lat1, lon1, lat2, lon2):
    R = 6371000
    lat1_rad = math.radians(lat1)
    lat2_rad = math.radians(lat2)
    delta_lat = math.radians(lat2 - lat1)
    delta_lon = math.radians(lon2 - lon1)
    a = math.sin(delta_lat / 2) ** 2 + math.cos(lat1_rad) * math.cos(lat2_rad) * math.sin(delta_lon / 2) ** 2
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
    return R * c

def parse_location(location_str):
    if not location_str or not isinstance(location_str, str):
        return None, None
    match = re.search(r'POINT\(([-\d.]+)\s+([-\d.]+)\)', location_str)
    if match:
        lng = float(match.group(1))
        lat = float(match.group(2))
        return lat, lng
    return None, None

class User(UserMixin):
    def __init__(self, id, username, email, is_online, last_seen=None, profile_pic=None, age=None, bio=None, interests=None):
        self.id = id
        self.username = username
        self.email = email
        self.is_online = is_online
        self.last_seen = last_seen
        self.profile_pic = profile_pic
        self.age = age
        self.bio = bio
        self.interests = interests

@login_manager.user_loader
def load_user(user_id):
    try:
        result = supabase.table('chat_users').select('*').eq('id', user_id).execute()
        if result.data:
            u = result.data[0]
            return User(u['id'], u['username'], u['email'], u.get('is_online', False), u.get('last_seen'), u.get('profile_pic'), u.get('age'), u.get('bio'), u.get('interests'))
    except Exception as e:
        logger.error(f"Supabase load_user error: {str(e)}")
    return None

def supabase_execute_safe(query_func, default_return=None, max_retries=2):
    for attempt in range(max_retries):
        try:
            result = query_func()
            return result.data if result else default_return
        except Exception as e:
            logger.error(f"Supabase error (attempt {attempt+1}): {str(e)}")
            if attempt == max_retries - 1:
                return default_return
            time.sleep(0.5 * (attempt + 1))
    return default_return

def get_user_by_username(username):
    return supabase_execute_safe(lambda: supabase.table('chat_users').select('*').eq('username', username).execute(), [])

def get_user_by_email(email):
    return supabase_execute_safe(lambda: supabase.table('chat_users').select('*').eq('email', email).execute(), [])

def get_nearby_users(user_id, lat=None, lng=None, radius_km=50, limit=50):
    try:
        result = supabase.table('chat_users').select('*').neq('id', user_id).execute()
        users = result.data if result.data else []
        
        if lat is not None and lng is not None:
            for user in users:
                user_lat, user_lng = parse_location(user.get('location'))
                if user_lat is not None and user_lng is not None:
                    distance_meters = haversine_distance(lat, lng, user_lat, user_lng)
                    user['distance_meters'] = distance_meters
                    user['distance_km'] = distance_meters / 1000
                    user['distance_display'] = format_distance(distance_meters)
                else:
                    user['distance_meters'] = None
                    user['distance_km'] = 999999
                    user['distance_display'] = "Unknown"
                
                if user.get('last_seen'):
                    user['last_seen_formatted'] = format_ist_time(user['last_seen'])
                else:
                    user['last_seen_formatted'] = 'recently'
            
            users.sort(key=lambda x: x.get('distance_km', 999999))
            
            if radius_km and radius_km < 999:
                filtered_users = []
                for user in users:
                    if user.get('distance_km', 999999) <= radius_km:
                        filtered_users.append(user)
                    elif user.get('distance_display') == "Unknown":
                        filtered_users.append(user)
                users = filtered_users
        else:
            for user in users:
                user['distance_display'] = "Unknown"
                user['distance_km'] = 999999
                if user.get('last_seen'):
                    user['last_seen_formatted'] = format_ist_time(user['last_seen'])
                else:
                    user['last_seen_formatted'] = 'recently'
            users.sort(key=lambda x: (0 if x.get('is_online') else 1, x.get('username', '')))
        
        return users[:limit]
    except Exception as e:
        logger.error(f"get_nearby_users error: {e}")
        try:
            result = supabase.table('chat_users').select('*').neq('id', user_id).execute()
            users = result.data if result.data else []
            for user in users:
                user['distance_display'] = "Unknown"
                user['distance_km'] = 999999
                if user.get('last_seen'):
                    user['last_seen_formatted'] = format_ist_time(user['last_seen'])
                else:
                    user['last_seen_formatted'] = 'recently'
            return users
        except Exception as fallback_error:
            logger.error(f"Fallback error: {fallback_error}")
            return []

def get_unread_counts(user_id):
    try:
        r = supabase.table('messages').select('sender_id').eq('receiver_id', user_id).eq('is_read', False).execute()
        counts = {}
        for msg in r.data:
            counts[msg['sender_id']] = counts.get(msg['sender_id'], 0) + 1
        return counts
    except Exception as e:
        logger.error(f"Unread count error: {e}")
        return {}

def get_messages_between(u1, u2, limit=20, offset=0):
    try:
        r = supabase.table('messages').select('*')\
            .or_(f"and(sender_id.eq.{u1},receiver_id.eq.{u2}),and(sender_id.eq.{u2},receiver_id.eq.{u1})")\
            .order('created_at', desc=True)\
            .range(offset, offset+limit-1)\
            .execute()
        return list(reversed(r.data))
    except Exception as e:
        logger.error(f"get_messages_between error: {e}")
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
    except Exception as e:
        logger.error(f"get_reactions_for_messages error: {e}")
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
    except Exception as e:
        logger.error(f"save_message error: {e}")
        return None

def mark_messages_as_read(receiver_id, sender_id):
    try:
        r = supabase.table('messages').update({'is_read': True})\
            .eq('sender_id', sender_id)\
            .eq('receiver_id', receiver_id)\
            .eq('is_read', False)\
            .execute()
        return r.data
    except Exception as e:
        logger.error(f"mark_messages_as_read error: {e}")
        return []

def edit_message(message_id, user_id, new_content):
    try:
        msg = supabase.table('messages').select('*').eq('id', message_id).execute()
        if msg.data and msg.data[0]['sender_id'] == user_id and msg.data[0]['message_type'] == 'text':
            supabase.table('messages').update({'content': new_content, 'edited': True}).eq('id', message_id).execute()
            return True
    except Exception as e:
        logger.error(f"edit_message error: {e}")
    return False

def add_reaction(message_id, user_id, reaction):
    try:
        supabase.table('message_reactions').insert({
            'message_id': message_id,
            'user_id': user_id,
            'reaction': reaction
        }).execute()
        return True
    except Exception as e:
        logger.error(f"add_reaction error: {e}")
        return False

def remove_reaction(message_id, user_id, reaction):
    try:
        supabase.table('message_reactions').delete()\
            .eq('message_id', message_id)\
            .eq('user_id', user_id)\
            .eq('reaction', reaction)\
            .execute()
        return True
    except Exception as e:
        logger.error(f"remove_reaction error: {e}")
        return False

def get_reactions_for_message(message_id):
    try:
        r = supabase.table('message_reactions').select('*').eq('message_id', message_id).execute()
        return r.data
    except Exception as e:
        logger.error(f"get_reactions_for_message error: {e}")
        return []

def update_user_status(user_id, is_online):
    try:
        supabase.table('chat_users').update({
            'is_online': is_online, 
            'last_seen': get_utc_time() if not is_online else None
        }).eq('id', user_id).execute()
        socketio.emit('user_status', {
            'user_id': user_id, 
            'is_online': is_online, 
            'last_seen': None if is_online else get_utc_time()
        }, to=None)
    except Exception as e:
        logger.error(f"Status update error: {e}")

def debounced_status_update(user_id, is_online, delay=1.0):
    if user_id in status_update_timers:
        try:
            status_update_timers[user_id].cancel()
        except:
            pass
    timer = Timer(delay, update_user_status, args=[user_id, is_online])
    timer.daemon = True
    timer.start()
    status_update_timers[user_id] = timer

def user_has_location(user_id):
    try:
        result = supabase.table('chat_users').select('location').eq('id', user_id).execute()
        return result.data and result.data[0].get('location') is not None
    except Exception as e:
        logger.error(f"Check location error: {e}")
        return False

def handle_call_timeout(caller_id, target_id):
    try:
        with call_lock:
            if caller_id in active_calls and active_calls[caller_id].get('state') == 'calling':
                del active_calls[caller_id]
                socketio.emit('call_timeout', room=caller_id)
            if caller_id in call_timeouts:
                del call_timeouts[caller_id]
    except Exception as e:
        logger.error(f"Call timeout error: {e}")

# ==================== PROFILE SYSTEM ROUTES (NEW) ====================

@app.route('/profile')
@login_required
def profile():
    try:
        result = supabase.table('chat_users').select('*').eq('id', current_user.id).execute()
        if not result.data:
            flash('User data not found', 'danger')
            return redirect(url_for('users'))
        
        user_data = result.data[0]
        
        if user_data.get('interests'):
            try:
                user_data['interests_list'] = json.loads(user_data['interests'])
            except:
                user_data['interests_list'] = []
        else:
            user_data['interests_list'] = []
        
        if user_data.get('photos'):
            try:
                user_data['photos_list'] = json.loads(user_data['photos'])
            except:
                user_data['photos_list'] = []
        else:
            user_data['photos_list'] = []
        
        if session.get('user_lat') and session.get('user_lng'):
            user_lat, user_lng = parse_location(user_data.get('location'))
            if user_lat and user_lng:
                distance = haversine_distance(session['user_lat'], session['user_lng'], user_lat, user_lng)
                user_data['distance_display'] = format_distance(distance)
        
        user_data['is_verified'] = user_data.get('email_verified', False)
        user_data['last_seen_formatted'] = format_ist_time(user_data.get('last_seen')) if user_data.get('last_seen') else 'recently'
        
        return render_template('profile.html', user=user_data)
    except Exception as e:
        logger.error(f"Profile error: {e}")
        flash('Error loading profile', 'danger')
        return redirect(url_for('users'))

@app.route('/edit_profile', methods=['GET', 'POST'])
@login_required
def edit_profile():
    if request.method == 'POST':
        try:
            username = request.form.get('username', '').strip()
            bio = request.form.get('bio', '').strip()
            age = request.form.get('age', type=int)
            gender = request.form.get('gender', '')
            interests_raw = request.form.get('interests', '')
            
            if not username:
                flash('Username is required', 'danger')
                return redirect(url_for('edit_profile'))
            
            if age and (age < 18 or age > 120):
                flash('Age must be between 18 and 120', 'danger')
                return redirect(url_for('edit_profile'))
            
            if len(bio) > 500:
                flash('Bio cannot exceed 500 characters', 'danger')
                return redirect(url_for('edit_profile'))
            
            interests_list = [i.strip() for i in interests_raw.split(',') if i.strip()]
            interests_json = json.dumps(interests_list) if interests_list else None
            
            profile_pic_url = None
            if 'profile_pic' in request.files:
                file = request.files['profile_pic']
                if file and file.filename:
                    ext = file.filename.rsplit('.', 1)[1].lower()
                    if ext in ['png', 'jpg', 'jpeg', 'gif', 'webp']:
                        filename = f"profile_{uuid.uuid4()}_{secure_filename(file.filename)}"
                        file_path = f"profile_photos/{current_user.id}/{filename}"
                        file_bytes = file.read()
                        supabase.storage.from_('chat-files').upload(file_path, file_bytes)
                        profile_pic_url = supabase.storage.from_('chat-files').get_public_url(file_path)
            
            existing_photos = []
            if 'photos' in request.files:
                photos_files = request.files.getlist('photos')
                for photo in photos_files:
                    if photo and photo.filename:
                        ext = photo.filename.rsplit('.', 1)[1].lower()
                        if ext in ['png', 'jpg', 'jpeg', 'gif', 'webp']:
                            filename = f"gallery_{uuid.uuid4()}_{secure_filename(photo.filename)}"
                            file_path = f"profile_photos/{current_user.id}/{filename}"
                            photo_bytes = photo.read()
                            supabase.storage.from_('chat-files').upload(file_path, photo_bytes)
                            photo_url = supabase.storage.from_('chat-files').get_public_url(file_path)
                            existing_photos.append(photo_url)
            
            existing_user = supabase.table('chat_users').select('photos', 'profile_pic').eq('id', current_user.id).execute()
            if existing_user.data:
                if existing_photos:
                    photos_json = json.dumps(existing_photos)
                else:
                    old_photos = existing_user.data[0].get('photos')
                    photos_json = old_photos if old_photos else json.dumps([])
                if not profile_pic_url:
                    profile_pic_url = existing_user.data[0].get('profile_pic')
            else:
                photos_json = json.dumps([])
            
            update_data = {
                'username': username,
                'bio': bio,
                'age': age,
                'gender': gender,
                'interests': interests_json,
                'profile_pic': profile_pic_url,
                'photos': photos_json
            }
            
            supabase.table('chat_users').update(update_data).eq('id', current_user.id).execute()
            
            flash('Profile updated successfully!', 'success')
            return redirect(url_for('profile'))
        except Exception as e:
            logger.error(f"Edit profile error: {e}")
            flash('Error updating profile', 'danger')
            return redirect(url_for('edit_profile'))
    
    try:
        result = supabase.table('chat_users').select('*').eq('id', current_user.id).execute()
        if not result.data:
            flash('User not found', 'danger')
            return redirect(url_for('users'))
        
        user_data = result.data[0]
        
        if user_data.get('interests'):
            try:
                user_data['interests_str'] = ', '.join(json.loads(user_data['interests']))
            except:
                user_data['interests_str'] = ''
        else:
            user_data['interests_str'] = ''
        
        if user_data.get('photos'):
            try:
                user_data['photos_list'] = json.loads(user_data['photos'])
            except:
                user_data['photos_list'] = []
        else:
            user_data['photos_list'] = []
        
        return render_template('edit_profile.html', user=user_data)
    except Exception as e:
        logger.error(f"Edit profile load error: {e}")
        flash('Error loading profile data', 'danger')
        return redirect(url_for('users'))

# ==================== EXISTING ROUTES (ALL INTACT) ====================

@app.route('/')
def index():
    if current_user.is_authenticated:
        return redirect(url_for('users'))
    return redirect(url_for('login'))

@app.route('/health')
def health():
    return jsonify({'status': 'healthy', 'timestamp': get_utc_time()}), 200

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
        except Exception as e:
            logger.error(f"Registration error: {e}")
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
            session['user_id'] = user.id
            
            try:
                supabase.table('chat_users').update({'is_online': True, 'last_seen': None}).eq('id', user.id).execute()
                socketio.emit('user_status', {'user_id': user.id, 'is_online': True, 'last_seen': None}, to=None)
            except Exception as e:
                logger.error(f"Login status update error: {e}")
            
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
        if current_user.id in status_update_timers:
            try:
                status_update_timers[current_user.id].cancel()
                del status_update_timers[current_user.id]
            except:
                pass
        
        supabase.table('chat_users').update({'is_online': False, 'last_seen': get_utc_time()}).eq('id', current_user.id).execute()
        socketio.emit('user_status', {'user_id': current_user.id, 'is_online': False, 'last_seen': get_utc_time()}, to=None)
        
        with call_lock:
            if current_user.id in active_calls:
                target_id = active_calls[current_user.id].get('with')
                if target_id and target_id in active_calls:
                    del active_calls[target_id]
                del active_calls[current_user.id]
                if target_id:
                    socketio.emit('call_ended', room=target_id)
    except Exception as e:
        logger.error(f"Logout error: {e}")
    
    logout_user()
    session.clear()
    flash('Logged out successfully', 'success')
    return redirect(url_for('login'))

@app.route('/users')
@login_required
def users():
    lat = request.args.get('lat', type=float)
    lng = request.args.get('lng', type=float)
    
    if (lat is None or lng is None) and session.get('user_lat') and session.get('user_lng'):
        lat = session.get('user_lat')
        lng = session.get('user_lng')
        logger.info(f"Using location from session for user {current_user.id}: {lat}, {lng}")
    
    if lat is not None and lng is not None:
        logger.info(f"Getting nearby users for {current_user.id} at {lat}, {lng}")
        all_users = get_nearby_users(current_user.id, lat, lng)
        session['user_lat'] = lat
        session['user_lng'] = lng
    else:
        logger.info(f"No location provided for {current_user.id}, getting all users")
        all_users = get_nearby_users(current_user.id)
    
    unread_counts = get_unread_counts(current_user.id)
    
    for user in all_users:
        user['unread_count'] = unread_counts.get(user['id'], 0)
    
    return render_template('users.html', users=all_users, unread_counts=unread_counts, session=session)

@app.route('/update_location', methods=['POST'])
@login_required
def update_location():
    data = request.get_json()
    lat = data.get('lat')
    lng = data.get('lng')
    
    if lat is None or lng is None:
        return jsonify({'error': 'Latitude and longitude required'}), 400
    
    try:
        location_wkt = f"POINT({lng} {lat})"
        supabase.table('chat_users').update({'location': location_wkt}).eq('id', current_user.id).execute()
        session['user_lat'] = lat
        session['user_lng'] = lng
        
        logger.info(f"📍 Location updated for user {current_user.id}: {lat}, {lng}")
        
        nearby_users = get_nearby_users(current_user.id, lat, lng, radius_km=10, limit=20)
        
        socketio.emit('nearby_users_update', {
            'user_id': current_user.id,
            'location_updated': True,
            'nearby_count': len(nearby_users)
        }, to=None)
        
        return jsonify({'success': True, 'message': 'Location updated'})
    except Exception as e:
        logger.error(f"Update location error: {e}")
        logger.error(traceback.format_exc())
        return jsonify({'error': str(e)}), 500

@app.route('/chat/<user_id>')
@login_required
def chat(user_id):
    try:
        other_user_data = supabase.table('chat_users').select('*').eq('id', user_id).execute()
        if not other_user_data.data:
            flash('User not found', 'danger')
            return redirect(url_for('users'))
        other_user = other_user_data.data[0]
        
        if session.get('user_lat') and session.get('user_lng'):
            user_lat, user_lng = parse_location(other_user.get('location'))
            if user_lat and user_lng:
                distance = haversine_distance(session['user_lat'], session['user_lng'], user_lat, user_lng)
                other_user['distance_display'] = format_distance(distance)
                other_user['distance_km'] = distance / 1000
            else:
                other_user['distance_display'] = "Unknown"
        else:
            other_user['distance_display'] = "Unknown"
    except Exception as e:
        logger.error(f"Chat user fetch error: {e}")
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
    except Exception as e:
        logger.error(f"Search error: {e}")
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
    except Exception as e:
        logger.error(f"Storage upload error: {e}")
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
    except Exception as e:
        logger.error(f"Audio upload error: {e}")
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
    except Exception as e:
        logger.error(f"Audio call error: {e}")
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
    except Exception as e:
        logger.error(f"Video call error: {e}")
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
    except Exception as e:
        logger.error(f"Delete message error: {e}")
        return jsonify({'error': 'Server error'}), 500

# ==================== SOCKETIO EVENTS (ALL INTACT) ====================

@socketio.on('connect')
def handle_connect():
    logger.info(f"Client connected: {request.sid}")
    
    if not current_user.is_authenticated:
        logger.warning(f"Unauthenticated connection attempt: {request.sid}")
        return False
    
    join_room(str(current_user.id))
    
    try:
        supabase.table('chat_users').update({'is_online': True, 'last_seen': None}).eq('id', current_user.id).execute()
        emit('user_status', {'user_id': current_user.id, 'is_online': True, 'last_seen': None}, to=None)
    except Exception as e:
        logger.error(f"Connect status error: {e}")

@socketio.on('disconnect')
def handle_disconnect():
    logger.info(f"Client disconnected: {request.sid}")
    
    if current_user.is_authenticated:
        leave_room(str(current_user.id))
        
        try:
            supabase.table('chat_users').update({'is_online': False, 'last_seen': get_utc_time()}).eq('id', current_user.id).execute()
            emit('user_status', {'user_id': current_user.id, 'is_online': False, 'last_seen': get_utc_time()}, to=None)
        except Exception as e:
            logger.error(f"Disconnect status error: {e}")

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
        except Exception as e:
            logger.error(f"Fetch reply content error: {e}")
    
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

@socketio.on('location_update')
def handle_location_update(data):
    if not current_user.is_authenticated:
        emit('location_error', {'message': 'Not authenticated'}, room=request.sid)
        return
    
    lat = data.get('lat')
    lng = data.get('lng')
    
    if lat is None or lng is None:
        emit('location_error', {'message': 'Missing coordinates'}, room=request.sid)
        return
    
    try:
        location_wkt = f"POINT({lng} {lat})"
        supabase.table('chat_users').update({
            'location': location_wkt
        }).eq('id', current_user.id).execute()
        
        session['user_lat'] = lat
        session['user_lng'] = lng
        
        nearby_users = get_nearby_users(current_user.id, lat, lng, radius_km=10, limit=30)
        
        emit('nearby_users_update', {
            'users': nearby_users,
            'current_location': {'lat': lat, 'lng': lng},
            'nearby_count': len(nearby_users)
        }, room=str(current_user.id))
        
        emit('user_location_changed', {
            'user_id': current_user.id,
            'username': current_user.username,
            'lat': lat,
            'lng': lng
        }, to=None, include_self=False)
        
        logger.info(f"📍 Socket location updated for user {current_user.id}: {lat}, {lng}, found {len(nearby_users)} nearby users")
    except Exception as e:
        logger.error(f"Socket location update error: {e}")
        emit('location_error', {'message': str(e)}, room=request.sid)

@socketio.on('get_nearby_users')
def handle_get_nearby_users(data):
    if not current_user.is_authenticated:
        return
    
    lat = data.get('lat')
    lng = data.get('lng')
    
    if lat is None or lng is None:
        emit('nearby_users_error', {'message': 'Location required'}, room=request.sid)
        return
    
    nearby_users = get_nearby_users(current_user.id, lat, lng, radius_km=10, limit=30)
    emit('nearby_users_list', {'users': nearby_users, 'timestamp': get_utc_time()}, room=request.sid)

@socketio.on('refresh_nearby')
def handle_refresh_nearby(data):
    if not current_user.is_authenticated:
        return
    
    lat = session.get('user_lat')
    lng = session.get('user_lng')
    
    if lat and lng:
        nearby_users = get_nearby_users(current_user.id, lat, lng, radius_km=10, limit=30)
        emit('nearby_users_update', {'users': nearby_users, 'refreshed': True, 'timestamp': get_utc_time()}, room=str(current_user.id))
    else:
        emit('nearby_users_error', {'message': 'No location available'}, room=request.sid)

@socketio.on('network_status')
def handle_network_status(data):
    if not current_user.is_authenticated:
        return
    
    target_id = data.get('target_id')
    needs_turn = data.get('needsTurn')
    
    if target_id:
        logger.info(f"📡 Network status: User {current_user.id} needs_turn={needs_turn} -> sending to {target_id}")
        emit('peer_network_status', {
            'user_id': current_user.id,
            'needs_turn': needs_turn,
            'username': current_user.username
        }, room=target_id)

@socketio.on('check_user_online')
def handle_check_online(data):
    if not current_user.is_authenticated:
        return
    
    target_id = data.get('user_id')
    
    try:
        result = supabase.table('chat_users').select('is_online', 'username').eq('id', target_id).execute()
        if result.data:
            is_online = result.data[0].get('is_online', False)
            username = result.data[0].get('username', 'User')
            rooms = socketio.server.manager.rooms.get('/', {})
            has_active_socket = str(target_id) in rooms
            
            emit('user_online_status', {
                'user_id': target_id,
                'username': username,
                'is_online': is_online,
                'has_active_socket': has_active_socket,
                'can_receive_calls': is_online and has_active_socket
            }, room=request.sid)
            logger.info(f"🔍 Online check for {target_id}: online={is_online}, socket={has_active_socket}")
    except Exception as e:
        logger.error(f"Check online error: {e}")
        emit('user_online_status', {
            'user_id': target_id,
            'username': 'User',
            'is_online': False,
            'has_active_socket': False,
            'can_receive_calls': False
        }, room=request.sid)

@socketio.on('ping_receiver')
def handle_ping_receiver(data):
    if not current_user.is_authenticated:
        return
    
    target_id = data.get('target_id')
    
    emit('call_ping', {
        'from_id': current_user.id,
        'from_name': current_user.username,
        'timestamp': get_utc_time()
    }, room=target_id)
    
    def pong_timeout():
        emit('call_ping_timeout', {'user_id': target_id}, room=request.sid)
    
    timer = Timer(5.0, pong_timeout)
    timer.daemon = True
    timer.start()
    
    global ping_timers
    ping_key = f"{current_user.id}_{target_id}"
    if ping_key in ping_timers:
        try:
            ping_timers[ping_key].cancel()
        except:
            pass
    ping_timers[ping_key] = timer
    logger.info(f"📡 Ping sent from {current_user.id} to {target_id}")

@socketio.on('call_pong')
def handle_call_pong(data):
    if not current_user.is_authenticated:
        return
    
    target_id = data.get('target_id')
    
    global ping_timers
    ping_key = f"{target_id}_{current_user.id}"
    if ping_key in ping_timers:
        try:
            ping_timers[ping_key].cancel()
            del ping_timers[ping_key]
        except:
            pass
    
    emit('call_pong_received', {'user_id': current_user.id}, room=target_id)
    logger.info(f"📡 Pong received from {current_user.id} to {target_id}")

@socketio.on('call_user')
def handle_call_user(data):
    if not current_user.is_authenticated:
        emit('call_error', {'message': 'Not authenticated'}, room=request.sid)
        return
    
    target_id = data.get('target_id')
    call_type = data.get('call_type')
    offer = data.get('offer')
    caller_needs_turn = data.get('caller_needs_turn', False)
    
    if not target_id or not call_type:
        emit('call_error', {'message': 'Missing parameters'}, room=request.sid)
        return
    
    try:
        target_user = supabase.table('chat_users').select('is_online', 'username').eq('id', target_id).execute()
        if not target_user.data or not target_user.data[0].get('is_online', False):
            emit('call_error', {'message': 'User is offline'}, room=request.sid)
            return
    except Exception as e:
        logger.error(f"Call user check error: {e}")
        emit('call_error', {'message': 'Cannot check user status'}, room=request.sid)
        return
    
    with call_lock:
        active_calls[current_user.id] = {'with': target_id, 'type': call_type, 'state': 'calling'}
    
    timeout_timer = Timer(45.0, lambda: handle_call_timeout(current_user.id, target_id))
    timeout_timer.daemon = True
    timeout_timer.start()
    call_timeouts[current_user.id] = timeout_timer
    
    emit('incoming_call', {
        'caller_id': current_user.id,
        'caller_name': current_user.username,
        'call_type': call_type,
        'offer': offer,
        'caller_needs_turn': caller_needs_turn
    }, room=target_id)
    
    logger.info(f"📞 Call from {current_user.id} to {target_id}, caller_needs_turn={caller_needs_turn}")

@socketio.on('answer_call')
def handle_answer_call(data):
    if not current_user.is_authenticated:
        emit('call_error', {'message': 'Not authenticated'}, room=request.sid)
        return
    
    caller_id = data.get('caller_id')
    answer_sdp = data.get('answer')
    call_type = data.get('call_type')
    answerer_needs_turn = data.get('answerer_needs_turn', False)
    
    if not caller_id or not answer_sdp:
        emit('call_error', {'message': 'Missing parameters'}, room=request.sid)
        return
    
    if caller_id in call_timeouts:
        try:
            call_timeouts[caller_id].cancel()
            del call_timeouts[caller_id]
        except:
            pass
    
    with call_lock:
        if caller_id not in active_calls:
            logger.warning(f"⚠️ Late answer from {current_user.id} for caller {caller_id}, re-creating call state")
            active_calls[caller_id] = {'with': current_user.id, 'type': call_type, 'state': 'connected'}
        
        active_calls[current_user.id] = {'with': caller_id, 'type': call_type, 'state': 'connected'}
        
        if caller_id in active_calls:
            active_calls[caller_id]['state'] = 'connected'
    
    emit('call_answered', {'answer': answer_sdp, 'answerer_needs_turn': answerer_needs_turn}, room=caller_id)
    logger.info(f"📞 Call answered by {current_user.id} for caller {caller_id}, answerer_needs_turn={answerer_needs_turn}")

@socketio.on('reject_call')
def handle_reject_call(data):
    if not current_user.is_authenticated:
        return
    
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
    logger.info(f"📞 Call rejected by {current_user.id} for caller {caller_id}")

@socketio.on('ice_candidate')
def handle_ice_candidate(data):
    if not current_user.is_authenticated:
        return
    
    target_id = data.get('target_id')
    candidate = data.get('candidate')
    
    if target_id and candidate:
        emit('ice_candidate', {'candidate': candidate}, room=target_id)
        logger.info(f"❄️ ICE candidate forwarded from {current_user.id} to {target_id}")

@socketio.on('end_call')
def handle_end_call(data):
    if not current_user.is_authenticated:
        return
    
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
    
    global ping_timers
    for key in list(ping_timers.keys()):
        if current_user.id in key or (target_id and target_id in key):
            try:
                ping_timers[key].cancel()
                del ping_timers[key]
            except:
                pass
    
    if target_id:
        emit('call_ended', room=target_id)
    
    logger.info(f"📞 Call ended by {current_user.id}, target: {target_id}")

# ==================== RUN APPLICATION ====================
if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    debug_mode = os.environ.get('DEBUG', 'False').lower() == 'true'
    
    socketio.run(
        app, 
        host='0.0.0.0', 
        port=port, 
        debug=debug_mode,
        allow_unsafe_werkzeug=True
    )