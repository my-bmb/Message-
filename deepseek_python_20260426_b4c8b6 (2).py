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

# ========== LIVE CHAT STORAGE ==========
# Note: Primary storage is now in database, this is just cache
LIVE_CHAT_CACHE_SIZE = 100  # Keep 100 latest messages in cache
live_chat_cache = []  # Cache for quick access

# ========== GROUP VIDEO CALL STORAGE ==========
active_group_calls = {}
group_call_participants = {}
GROUP_CALL_MAX_PARTICIPANTS = 10  # Max 10 people in a group call

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

def get_nearby_users(user_id, lat=None, lng=None, radius_km=None, limit=50):
    """Get all users with distance calculation - NO FILTERING applied"""
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
            
            # Sort by distance only - NO FILTERING
            users.sort(key=lambda x: x.get('distance_km', 999999))
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

# ==================== LIVE CHAT DATABASE FUNCTIONS ====================

def save_live_message(sender_id, sender_name, content, msg_type='text', file_name=None, file_size=None, duration=None, reply_to_id=None, reply_to_content=None):
    """Save live chat message to database"""
    data = {
        'sender_id': sender_id,
        'sender_name': sender_name,
        'content': content,
        'message_type': msg_type,
        'file_name': file_name,
        'file_size': file_size,
        'duration': duration,
        'reply_to_id': reply_to_id,
        'reply_to_content': reply_to_content,
        'is_read': False,
        'edited': False,
        'is_deleted': False,
        'created_at': get_utc_time(),
        'reactions': []
    }
    try:
        # Insert into database
        result = supabase.table('live_chat_messages').insert(data).execute()
        if result.data:
            saved_msg = result.data[0]
            # Update cache
            _update_live_chat_cache(saved_msg)
            return saved_msg
    except Exception as e:
        logger.error(f"Save live message to DB error: {e}")
    return None

def _update_live_chat_cache(message):
    """Update in-memory cache with latest message (internal function)"""
    global live_chat_cache
    live_chat_cache.append(message)
    # Keep only last LIVE_CHAT_CACHE_SIZE messages in cache
    if len(live_chat_cache) > LIVE_CHAT_CACHE_SIZE:
        live_chat_cache = live_chat_cache[-LIVE_CHAT_CACHE_SIZE:]

def get_live_messages(limit=20, offset=0, from_cache=False):
    """Get live chat messages from database"""
    global live_chat_cache
    try:
        # Try to get from cache first for recent messages
        if from_cache and offset == 0 and len(live_chat_cache) >= limit:
            return live_chat_cache[-limit:]
        
        # Get from database
        result = supabase.table('live_chat_messages')\
            .select('*')\
            .eq('is_deleted', False)\
            .order('created_at', desc=False)\
            .range(offset, offset + limit - 1)\
            .execute()
        
        messages = result.data if result.data else []
        
        # Update cache with these messages
        for msg in messages:
            if msg not in live_chat_cache:
                live_chat_cache.append(msg)
        
        # Trim cache
        if len(live_chat_cache) > LIVE_CHAT_CACHE_SIZE:
            live_chat_cache = live_chat_cache[-LIVE_CHAT_CACHE_SIZE:]
        
        return messages
    except Exception as e:
        logger.error(f"Get live messages from DB error: {e}")
        return []

def get_total_live_messages_count():
    """Get total count of live chat messages"""
    try:
        result = supabase.table('live_chat_messages')\
            .select('id', count='exact')\
            .eq('is_deleted', False)\
            .execute()
        return result.count if hasattr(result, 'count') else len(live_chat_cache)
    except Exception as e:
        logger.error(f"Get live messages count error: {e}")
        return len(live_chat_cache)

def edit_live_message_in_db(message_id, user_id, new_content):
    """Edit a live chat message in database"""
    global live_chat_cache
    try:
        # Check if user owns the message
        result = supabase.table('live_chat_messages')\
            .select('sender_id')\
            .eq('id', message_id)\
            .execute()
        
        if result.data and result.data[0]['sender_id'] == user_id:
            # Update the message
            supabase.table('live_chat_messages')\
                .update({'content': new_content, 'edited': True})\
                .eq('id', message_id)\
                .execute()
            
            # Update cache
            for i, msg in enumerate(live_chat_cache):
                if msg.get('id') == message_id:
                    live_chat_cache[i]['content'] = new_content
                    live_chat_cache[i]['edited'] = True
                    break
            return True
    except Exception as e:
        logger.error(f"Edit live message in DB error: {e}")
    return False

def delete_live_message_in_db(message_id, user_id, delete_for='everyone'):
    """Delete a live chat message in database"""
    global live_chat_cache
    try:
        if delete_for == 'everyone':
            # Soft delete for everyone
            supabase.table('live_chat_messages')\
                .update({'is_deleted': True, 'content': 'This message was deleted'})\
                .eq('id', message_id)\
                .execute()
        else:
            # Check if user owns the message for 'me' deletion
            result = supabase.table('live_chat_messages')\
                .select('sender_id')\
                .eq('id', message_id)\
                .execute()
            
            if result.data and result.data[0]['sender_id'] == user_id:
                supabase.table('live_chat_messages')\
                    .update({'is_deleted': True, 'content': 'This message was deleted'})\
                    .eq('id', message_id)\
                    .execute()
            else:
                return False
        
        # Update cache
        for i, msg in enumerate(live_chat_cache):
            if msg.get('id') == message_id:
                live_chat_cache[i]['is_deleted'] = True
                if delete_for == 'everyone':
                    live_chat_cache[i]['content'] = 'This message was deleted'
                break
        return True
    except Exception as e:
        logger.error(f"Delete live message in DB error: {e}")
    return False

def add_reaction_to_live_message(message_id, user_id, user_name, reaction):
    """Add or remove reaction from live message in database"""
    global live_chat_cache
    try:
        # Get current reactions
        result = supabase.table('live_chat_messages')\
            .select('reactions')\
            .eq('id', message_id)\
            .execute()
        
        if not result.data:
            return False
        
        current_reactions = result.data[0].get('reactions', [])
        if isinstance(current_reactions, str):
            current_reactions = json.loads(current_reactions) if current_reactions else []
        
        # Check if reaction exists
        existing_idx = None
        for idx, r in enumerate(current_reactions):
            if r.get('user_id') == user_id and r.get('reaction') == reaction:
                existing_idx = idx
                break
        
        if existing_idx is not None:
            # Remove reaction
            current_reactions.pop(existing_idx)
        else:
            # Add reaction
            current_reactions.append({
                'user_id': user_id,
                'user_name': user_name,
                'reaction': reaction
            })
        
        # Update database
        supabase.table('live_chat_messages')\
            .update({'reactions': json.dumps(current_reactions)})\
            .eq('id', message_id)\
            .execute()
        
        # Update cache
        for i, msg in enumerate(live_chat_cache):
            if msg.get('id') == message_id:
                live_chat_cache[i]['reactions'] = current_reactions
                break
        
        return current_reactions
    except Exception as e:
        logger.error(f"Add reaction to live message error: {e}")
        return None

# ==================== LIVE CHAT ROUTES ====================

@app.route('/live-chat')
@login_required
def live_chat():
    """Live chat page - multiple users can chat together"""
    try:
        # Get all online users for sidebar
        result = supabase.table('chat_users')\
            .select('id, username, profile_pic, is_online, last_seen')\
            .neq('id', current_user.id)\
            .execute()
        
        users = result.data if result.data else []
        
        # Format user status
        for user in users:
            if user.get('is_online'):
                user['status_text'] = 'Online'
                user['status_color'] = '#2ecc71'
            else:
                last_seen = format_ist_time(user.get('last_seen')) if user.get('last_seen') else 'recently'
                user['status_text'] = f'Last seen {last_seen}'
                user['status_color'] = '#95a5a6'
        
        # Count total online users (including current user)
        online_count = len([u for u in users if u.get('is_online')]) + 1
        
        # Get recent live chat messages from database
        recent_messages = get_live_messages(limit=20, offset=0)
        total_messages = get_total_live_messages_count()
        has_more = total_messages > 20
        
        # Format messages for display
        formatted_messages = []
        for msg in recent_messages:
            if not msg.get('is_deleted'):
                formatted_messages.append({
                    'id': msg['id'],
                    'sender_id': msg['sender_id'],
                    'sender_name': msg['sender_name'],
                    'content': msg['content'],
                    'message_type': msg['message_type'],
                    'created_at': msg['created_at'],
                    'formatted_time': format_ist_time(msg['created_at']),
                    'is_read': msg.get('is_read', False),
                    'edited': msg.get('edited', False),
                    'reply_to_id': msg.get('reply_to_id'),
                    'reply_to_content': msg.get('reply_to_content'),
                    'reactions': json.loads(msg.get('reactions', '[]')) if isinstance(msg.get('reactions'), str) else msg.get('reactions', [])
                })
        
        return render_template('live.chat.html', 
                             current_user=current_user,
                             users=users,
                             online_count=online_count,
                             messages=formatted_messages,
                             has_more=has_more,
                             total_messages=total_messages)
    except Exception as e:
        logger.error(f"Live chat error: {e}")
        flash('Error loading live chat', 'danger')
        return redirect(url_for('users'))

@app.route('/live-chat/messages')
@login_required
def get_live_chat_messages():
    """Get paginated live chat messages from database"""
    try:
        offset = int(request.args.get('offset', 0))
        limit = 20
        
        messages = get_live_messages(limit=limit, offset=offset)
        total_messages = get_total_live_messages_count()
        
        formatted_messages = []
        for msg in messages:
            if not msg.get('is_deleted'):
                formatted_messages.append({
                    'id': msg['id'],
                    'sender_id': msg['sender_id'],
                    'sender_name': msg['sender_name'],
                    'content': msg['content'],
                    'message_type': msg['message_type'],
                    'created_at': msg['created_at'],
                    'formatted_time': format_ist_time(msg['created_at']),
                    'is_read': msg.get('is_read', False),
                    'edited': msg.get('edited', False),
                    'reply_to_id': msg.get('reply_to_id'),
                    'reply_to_content': msg.get('reply_to_content'),
                    'reactions': json.loads(msg.get('reactions', '[]')) if isinstance(msg.get('reactions'), str) else msg.get('reactions', [])
                })
        
        return jsonify({
            'success': True,
            'messages': formatted_messages,
            'current_user_id': current_user.id,
            'current_user_name': current_user.username,
            'total_messages': total_messages,
            'has_more': (offset + limit) < total_messages,
            'loaded_count': len(formatted_messages)
        })
    except Exception as e:
        logger.error(f"Get live messages error: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/live-chat/send', methods=['POST'])
@login_required
def send_live_chat_message():
    """Send message to live chat and save to database"""
    try:
        data = request.get_json()
        content = data.get('content')
        msg_type = data.get('message_type', 'text')
        reply_to_id = data.get('reply_to_id')
        reply_to_content = data.get('reply_to_content')
        
        if not content and msg_type == 'text':
            return jsonify({'success': False, 'error': 'Message content required'}), 400
        
        # Save to database
        saved_msg = save_live_message(
            sender_id=current_user.id,
            sender_name=current_user.username,
            content=content,
            msg_type=msg_type,
            reply_to_id=reply_to_id,
            reply_to_content=reply_to_content
        )
        
        if saved_msg:
            # Format message for response and socket
            message_data = {
                'id': saved_msg['id'],
                'sender_id': saved_msg['sender_id'],
                'sender_name': saved_msg['sender_name'],
                'content': saved_msg['content'],
                'message_type': saved_msg['message_type'],
                'created_at': saved_msg['created_at'],
                'formatted_time': format_ist_time(saved_msg['created_at']),
                'is_read': saved_msg.get('is_read', False),
                'edited': saved_msg.get('edited', False),
                'reply_to_id': saved_msg.get('reply_to_id'),
                'reply_to_content': saved_msg.get('reply_to_content'),
                'reactions': json.loads(saved_msg.get('reactions', '[]')) if isinstance(saved_msg.get('reactions'), str) else saved_msg.get('reactions', [])
            }
            
            # Broadcast to all users in live chat room
            socketio.emit('live_message', message_data, to='live_chat')
            
            return jsonify({'success': True, 'message': message_data})
        else:
            return jsonify({'success': False, 'error': 'Failed to save message'}), 500
            
    except Exception as e:
        logger.error(f"Send live message error: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/live-chat/upload', methods=['POST'])
@login_required
def upload_live_chat_file():
    """Upload file to live chat and save to database"""
    try:
        if 'file' not in request.files:
            return jsonify({'success': False, 'error': 'No file provided'}), 400
        
        file = request.files['file']
        if file.filename == '':
            return jsonify({'success': False, 'error': 'No file selected'}), 400
        
        ext = file.filename.rsplit('.', 1)[1].lower() if '.' in file.filename else ''
        
        if ext in ['png', 'jpg', 'jpeg', 'gif', 'webp']:
            msg_type = 'image'
        elif ext in ['mp4', 'webm', 'avi', 'mov', 'mkv']:
            msg_type = 'video'
        elif ext in ['mp3', 'wav', 'ogg', 'm4a', 'aac']:
            msg_type = 'audio'
        else:
            msg_type = 'file'
        
        file_path = f"live_chat_uploads/{uuid.uuid4()}_{secure_filename(file.filename)}"
        file_bytes = file.read()
        
        try:
            supabase.storage.from_('chat-files').upload(file_path, file_bytes)
            public_url = supabase.storage.from_('chat-files').get_public_url(file_path)
        except Exception as e:
            logger.error(f"Storage upload error: {e}")
            return jsonify({'success': False, 'error': 'Upload failed'}), 500
        
        # Save to database
        saved_msg = save_live_message(
            sender_id=current_user.id,
            sender_name=current_user.username,
            content=public_url,
            msg_type=msg_type,
            file_name=file.filename,
            file_size=len(file_bytes)
        )
        
        if saved_msg:
            message_data = {
                'id': saved_msg['id'],
                'sender_id': saved_msg['sender_id'],
                'sender_name': saved_msg['sender_name'],
                'content': saved_msg['content'],
                'message_type': saved_msg['message_type'],
                'created_at': saved_msg['created_at'],
                'formatted_time': format_ist_time(saved_msg['created_at']),
                'is_read': saved_msg.get('is_read', False),
                'edited': saved_msg.get('edited', False),
                'reactions': [],
                'file_name': file.filename,
                'file_size': len(file_bytes)
            }
            
            socketio.emit('live_message', message_data, to='live_chat')
            return jsonify({'success': True, 'message': message_data})
        else:
            return jsonify({'success': False, 'error': 'Failed to save message'}), 500
            
    except Exception as e:
        logger.error(f"Upload live file error: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/live-chat/record-audio', methods=['POST'])
@login_required
def upload_live_chat_audio():
    """Upload recorded audio to live chat and save to database"""
    try:
        if 'audio' not in request.files:
            return jsonify({'success': False, 'error': 'No audio provided'}), 400
        
        audio = request.files['audio']
        
        file_path = f"live_chat_audio/{uuid.uuid4()}_voice_recording.webm"
        audio_bytes = audio.read()
        
        try:
            supabase.storage.from_('chat-files').upload(file_path, audio_bytes)
            public_url = supabase.storage.from_('chat-files').get_public_url(file_path)
        except Exception as e:
            logger.error(f"Audio upload error: {e}")
            return jsonify({'success': False, 'error': 'Upload failed'}), 500
        
        duration = request.form.get('duration', 0)
        if duration:
            try:
                duration = int(duration)
            except:
                duration = 0
        
        # Save to database
        saved_msg = save_live_message(
            sender_id=current_user.id,
            sender_name=current_user.username,
            content=public_url,
            msg_type='voice',
            duration=duration
        )
        
        if saved_msg:
            message_data = {
                'id': saved_msg['id'],
                'sender_id': saved_msg['sender_id'],
                'sender_name': saved_msg['sender_name'],
                'content': saved_msg['content'],
                'message_type': 'voice',
                'created_at': saved_msg['created_at'],
                'formatted_time': format_ist_time(saved_msg['created_at']),
                'is_read': saved_msg.get('is_read', False),
                'edited': saved_msg.get('edited', False),
                'reactions': [],
                'duration': duration
            }
            
            socketio.emit('live_message', message_data, to='live_chat')
            return jsonify({'success': True, 'message': message_data})
        else:
            return jsonify({'success': False, 'error': 'Failed to save message'}), 500
            
    except Exception as e:
        logger.error(f"Upload audio error: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/live-chat/edit', methods=['POST'])
@login_required
def edit_live_message():
    """Edit a message in live chat"""
    try:
        data = request.get_json()
        message_id = data.get('message_id')
        new_content = data.get('content')
        
        if edit_live_message_in_db(message_id, current_user.id, new_content):
            socketio.emit('live_message_edited', {
                'message_id': message_id,
                'new_content': new_content
            }, to='live_chat')
            return jsonify({'success': True})
        
        return jsonify({'success': False, 'error': 'Message not found or unauthorized'}), 404
    except Exception as e:
        logger.error(f"Edit live message error: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/live-chat/delete', methods=['POST'])
@login_required
def delete_live_message():
    """Delete a message in live chat"""
    try:
        data = request.get_json()
        message_id = data.get('message_id')
        delete_for = data.get('delete_for', 'everyone')
        
        if delete_live_message_in_db(message_id, current_user.id, delete_for):
            if delete_for == 'everyone':
                socketio.emit('live_message_deleted', {
                    'message_id': message_id,
                    'for_everyone': True
                }, to='live_chat')
            else:
                socketio.emit('live_message_deleted', {
                    'message_id': message_id,
                    'for_everyone': False,
                    'user_id': current_user.id
                }, room=str(current_user.id))
            return jsonify({'success': True})
        
        return jsonify({'success': False, 'error': 'Message not found or unauthorized'}), 404
    except Exception as e:
        logger.error(f"Delete live message error: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/live-chat/react', methods=['POST'])
@login_required
def react_to_live_message():
    """Add/remove reaction to live message"""
    try:
        data = request.get_json()
        message_id = data.get('message_id')
        reaction = data.get('reaction')
        
        updated_reactions = add_reaction_to_live_message(
            message_id, 
            current_user.id, 
            current_user.username, 
            reaction
        )
        
        if updated_reactions is not None:
            socketio.emit('live_reaction_updated', {
                'message_id': message_id,
                'reactions': updated_reactions
            }, to='live_chat')
            return jsonify({'success': True, 'reactions': updated_reactions})
        
        return jsonify({'success': False, 'error': 'Message not found'}), 404
    except Exception as e:
        logger.error(f"React to live message error: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/live-users')
@login_required
def live_users():
    """Show online users only - ultra compact"""
    try:
        result = supabase.table('chat_users')\
            .select('id, username, profile_pic, is_online, last_seen, bio')\
            .neq('id', current_user.id)\
            .eq('is_online', True)\
            .execute()
        
        users = result.data if result.data else []
        total_online = len(users) + 1
        
        return render_template('live.users.html', 
                             users=users, 
                             current_user=current_user,
                             total_online=total_online)
    except Exception as e:
        logger.error(f"Live users error: {e}")
        flash('Error loading users', 'danger')
        return redirect(url_for('users'))

# ==================== GROUP VIDEO CALL ROUTES ====================

@app.route('/group-video-call')
@login_required
def group_video_call():
    """Group video call page - multiple users can join"""
    try:
        result = supabase.table('chat_users')\
            .select('id, username, profile_pic, is_online')\
            .neq('id', current_user.id)\
            .eq('is_online', True)\
            .execute()
        
        online_users = result.data if result.data else []
        
        call_id = session.get('active_group_call_id')
        participants = []
        if call_id and call_id in group_call_participants:
            participants = group_call_participants[call_id]
        
        return render_template('group.video.call.html', 
                             current_user=current_user,
                             online_users=online_users,
                             participants=participants,
                             call_id=call_id)
    except Exception as e:
        logger.error(f"Group video call error: {e}")
        flash('Error loading group video call', 'danger')
        return redirect(url_for('users'))

@app.route('/api/group-call/create', methods=['POST'])
@login_required
def create_group_call():
    """Create a new group video call"""
    try:
        call_id = str(uuid.uuid4())[:8]
        active_group_calls[call_id] = {
            'host_id': current_user.id,
            'host_name': current_user.username,
            'created_at': get_utc_time(),
            'is_active': True,
            'participant_count': 1
        }
        group_call_participants[call_id] = [{
            'user_id': current_user.id,
            'user_name': current_user.username,
            'joined_at': get_utc_time()
        }]
        
        session['active_group_call_id'] = call_id
        
        return jsonify({
            'success': True,
            'call_id': call_id,
            'join_url': f"/group-video-call/join/{call_id}"
        })
    except Exception as e:
        logger.error(f"Create group call error: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/group-video-call/join/<call_id>')
@login_required
def join_group_call(call_id):
    """Join an existing group video call"""
    try:
        if call_id not in active_group_calls or not active_group_calls[call_id]['is_active']:
            flash('Call has ended or does not exist', 'danger')
            return redirect(url_for('group_video_call'))
        
        if call_id in group_call_participants:
            existing = [p for p in group_call_participants[call_id] if p['user_id'] == current_user.id]
            if not existing and len(group_call_participants[call_id]) < GROUP_CALL_MAX_PARTICIPANTS:
                group_call_participants[call_id].append({
                    'user_id': current_user.id,
                    'user_name': current_user.username,
                    'joined_at': get_utc_time()
                })
                active_group_calls[call_id]['participant_count'] = len(group_call_participants[call_id])
        
        session['active_group_call_id'] = call_id
        
        participants = group_call_participants.get(call_id, [])
        
        result = supabase.table('chat_users')\
            .select('id, username, profile_pic, is_online')\
            .neq('id', current_user.id)\
            .eq('is_online', True)\
            .execute()
        online_users = result.data if result.data else []
        
        return render_template('group.video.call.html', 
                             current_user=current_user,
                             online_users=online_users,
                             participants=participants,
                             call_id=call_id,
                             is_join=True)
    except Exception as e:
        logger.error(f"Join group call error: {e}")
        flash('Error joining call', 'danger')
        return redirect(url_for('group_video_call'))

@app.route('/api/group-call/end', methods=['POST'])
@login_required
def end_group_call():
    """End a group video call"""
    try:
        data = request.get_json()
        call_id = data.get('call_id')
        
        if call_id and call_id in active_group_calls:
            if active_group_calls[call_id]['host_id'] == current_user.id:
                active_group_calls[call_id]['is_active'] = False
                socketio.emit('group_call_ended', {'call_id': call_id}, room=f'group_call_{call_id}')
                
                def cleanup():
                    if call_id in active_group_calls:
                        del active_group_calls[call_id]
                    if call_id in group_call_participants:
                        del group_call_participants[call_id]
                Timer(10, cleanup).start()
        
        if session.get('active_group_call_id'):
            del session['active_group_call_id']
        
        return jsonify({'success': True})
    except Exception as e:
        logger.error(f"End group call error: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/group-call/leave', methods=['POST'])
@login_required
def leave_group_call():
    """Leave a group video call"""
    try:
        data = request.get_json()
        call_id = data.get('call_id')
        
        if call_id and call_id in group_call_participants:
            group_call_participants[call_id] = [p for p in group_call_participants[call_id] if p['user_id'] != current_user.id]
            
            if call_id in active_group_calls:
                active_group_calls[call_id]['participant_count'] = len(group_call_participants[call_id])
            
            socketio.emit('participant_left', {
                'user_id': current_user.id,
                'user_name': current_user.username
            }, room=f'group_call_{call_id}')
            
            if len(group_call_participants[call_id]) == 0 and call_id in active_group_calls:
                active_group_calls[call_id]['is_active'] = False
        
        if session.get('active_group_call_id') == call_id:
            del session['active_group_call_id']
        
        return jsonify({'success': True})
    except Exception as e:
        logger.error(f"Leave group call error: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500

# ==================== PROFILE SYSTEM ROUTES ====================

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

@app.route('/user/profile/<user_id>')
@login_required
def view_user_profile(user_id):
    """View another user's profile"""
    try:
        result = supabase.table('chat_users').select('*').eq('id', user_id).execute()
        if not result.data:
            flash('User not found', 'danger')
            return redirect(url_for('users'))
        
        user_data = result.data[0]
        
        if user_data['id'] == current_user.id:
            return redirect(url_for('profile'))
        
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
        
        distance_display = None
        if session.get('user_lat') and session.get('user_lng'):
            user_lat, user_lng = parse_location(user_data.get('location'))
            if user_lat and user_lng:
                distance = haversine_distance(session['user_lat'], session['user_lng'], user_lat, user_lng)
                distance_display = format_distance(distance)
        
        user_data['viewer_distance'] = distance_display
        user_data['last_seen_formatted'] = format_ist_time(user_data.get('last_seen')) if user_data.get('last_seen') else 'recently'
        
        return render_template('view_profile.html', 
                             profile_user=user_data, 
                             current_user=current_user,
                             distance=distance_display)
    except Exception as e:
        logger.error(f"View user profile error: {e}")
        flash('Error loading profile', 'danger')
        return redirect(url_for('users'))

# ==================== EXISTING ROUTES ====================

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
        
        socketio.emit('leave_live_chat')
        
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
        logger.info(f"Getting all users with distance calculation for {current_user.id} at {lat}, {lng}")
        all_users = get_nearby_users(current_user.id, lat, lng, radius_km=None)
        session['user_lat'] = lat
        session['user_lng'] = lng
    else:
        logger.info(f"No location provided for {current_user.id}, getting all users")
        all_users = get_nearby_users(current_user.id, radius_km=None)
    
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
        
        nearby_users = get_nearby_users(current_user.id, lat, lng, radius_km=None, limit=20)
        
        socketio.emit('nearby_users_update', {
            'user_id': current_user.id,
            'location_updated': True,
            'nearby_count': len(nearby_users)
        }, to=None)
        
        return jsonify({'success': True, 'message': 'Location updated'})
    except Exception as e:
        logger.error(f"Update location error: {e}")
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

# ==================== SOCKETIO EVENTS ====================

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

# ==================== LIVE CHAT SOCKET EVENTS ====================

@socketio.on('join_live_chat')
def handle_join_live_chat():
    """User joins the live chat room"""
    if current_user.is_authenticated:
        join_room('live_chat')
        logger.info(f"User {current_user.username} joined live chat")
        
        notification = {
            'type': 'join',
            'user_id': current_user.id,
            'user_name': current_user.username,
            'message': f"✨ {current_user.username} joined the live chat",
            'timestamp': get_utc_time(),
            'formatted_time': format_ist_time(get_utc_time())
        }
        emit('live_user_join_leave', notification, room='live_chat')

@socketio.on('leave_live_chat')
def handle_leave_live_chat():
    """User leaves the live chat room"""
    if current_user.is_authenticated:
        leave_room('live_chat')
        logger.info(f"User {current_user.username} left live chat")
        
        notification = {
            'type': 'leave',
            'user_id': current_user.id,
            'user_name': current_user.username,
            'message': f"🚪 {current_user.username} left the live chat",
            'timestamp': get_utc_time(),
            'formatted_time': format_ist_time(get_utc_time())
        }
        emit('live_user_join_leave', notification, room='live_chat')

@socketio.on('live_typing')
def handle_live_typing(data):
    """Handle typing indicator in live chat"""
    if current_user.is_authenticated:
        emit('live_typing_indicator', {
            'user_id': current_user.id,
            'user_name': current_user.username,
            'is_typing': data.get('is_typing', False)
        }, room='live_chat', include_self=False)

# ==================== GROUP VIDEO CALL SOCKET EVENTS ====================

@socketio.on('join_group_call_room')
def handle_join_group_call_room(data):
    """Join group call WebRTC room"""
    if current_user.is_authenticated:
        call_id = data.get('call_id')
        if call_id:
            room_name = f'group_call_{call_id}'
            join_room(room_name)
            logger.info(f"User {current_user.username} joined group call room {call_id}")
            
            emit('user_joined_call', {
                'user_id': current_user.id,
                'user_name': current_user.username
            }, room=room_name, include_self=False)

@socketio.on('leave_group_call_room')
def handle_leave_group_call_room(data):
    """Leave group call WebRTC room"""
    if current_user.is_authenticated:
        call_id = data.get('call_id')
        if call_id:
            room_name = f'group_call_{call_id}'
            leave_room(room_name)
            
            emit('user_left_call', {
                'user_id': current_user.id,
                'user_name': current_user.username
            }, room=room_name)

@socketio.on('group_call_offer')
def handle_group_call_offer(data):
    """Handle WebRTC offer in group call"""
    if current_user.is_authenticated:
        target_id = data.get('target_id')
        call_id = data.get('call_id')
        offer = data.get('offer')
        
        emit('group_call_offer_received', {
            'from_id': current_user.id,
            'from_name': current_user.username,
            'offer': offer
        }, room=f'group_call_{call_id}', skip_sid=request.sid)

@socketio.on('group_call_answer')
def handle_group_call_answer(data):
    """Handle WebRTC answer in group call"""
    if current_user.is_authenticated:
        target_id = data.get('target_id')
        call_id = data.get('call_id')
        answer = data.get('answer')
        
        emit('group_call_answer_received', {
            'from_id': current_user.id,
            'from_name': current_user.username,
            'answer': answer
        }, room=target_id)

@socketio.on('group_call_ice_candidate')
def handle_group_call_ice_candidate(data):
    """Handle ICE candidates for group call"""
    if current_user.is_authenticated:
        target_id = data.get('target_id')
        call_id = data.get('call_id')
        candidate = data.get('candidate')
        
        emit('group_call_ice_received', {
            'from_id': current_user.id,
            'candidate': candidate
        }, room=target_id)

@socketio.on('toggle_video')
def handle_toggle_video(data):
    """Handle video toggle in group call"""
    if current_user.is_authenticated:
        call_id = data.get('call_id')
        enabled = data.get('enabled', True)
        
        emit('participant_video_toggle', {
            'user_id': current_user.id,
            'user_name': current_user.username,
            'video_enabled': enabled
        }, room=f'group_call_{call_id}', include_self=False)

@socketio.on('toggle_audio')
def handle_toggle_audio(data):
    """Handle audio toggle in group call"""
    if current_user.is_authenticated:
        call_id = data.get('call_id')
        enabled = data.get('enabled', True)
        
        emit('participant_audio_toggle', {
            'user_id': current_user.id,
            'user_name': current_user.username,
            'audio_enabled': enabled
        }, room=f'group_call_{call_id}', include_self=False)

@socketio.on('screen_share')
def handle_screen_share(data):
    """Handle screen sharing in group call"""
    if current_user.is_authenticated:
        call_id = data.get('call_id')
        enabled = data.get('enabled', False)
        
        emit('participant_screen_share', {
            'user_id': current_user.id,
            'user_name': current_user.username,
            'screen_enabled': enabled
        }, room=f'group_call_{call_id}', include_self=False)

# ==================== LOCATION & CALL SOCKET EVENTS ====================

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
        supabase.table('chat_users').update({'location': location_wkt}).eq('id', current_user.id).execute()
        
        session['user_lat'] = lat
        session['user_lng'] = lng
        
        nearby_users = get_nearby_users(current_user.id, lat, lng, radius_km=None, limit=30)
        
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
    
    nearby_users = get_nearby_users(current_user.id, lat, lng, radius_km=None, limit=30)
    emit('nearby_users_list', {'users': nearby_users, 'timestamp': get_utc_time()}, room=request.sid)

@socketio.on('refresh_nearby')
def handle_refresh_nearby(data):
    if not current_user.is_authenticated:
        return
    
    lat = session.get('user_lat')
    lng = session.get('user_lng')
    
    if lat and lng:
        nearby_users = get_nearby_users(current_user.id, lat, lng, radius_km=None, limit=30)
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