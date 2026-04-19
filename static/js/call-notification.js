// static/js/call-notification.js
let callNotificationActive = false;
let currentCallData = null;

function showCallNotification(callerId, callerName, callType, offer) {
    if (callNotificationActive) return;
    callNotificationActive = true;
    currentCallData = { callerId, callerName, callType, offer };

    let notifDiv = document.getElementById('global-call-notification');
    if (!notifDiv) {
        notifDiv = document.createElement('div');
        notifDiv.id = 'global-call-notification';
        notifDiv.style.cssText = `
            position: fixed;
            bottom: 20px;
            right: 20px;
            left: auto;
            background: #0f3460;
            color: white;
            border-radius: 12px;
            padding: 15px 20px;
            z-index: 10000;
            box-shadow: 0 4px 12px rgba(0,0,0,0.3);
            display: flex;
            align-items: center;
            gap: 15px;
            font-family: 'Segoe UI', sans-serif;
            max-width: 300px;
            backdrop-filter: blur(10px);
            border: 1px solid rgba(255,255,255,0.2);
        `;
        document.body.appendChild(notifDiv);
    }

    notifDiv.innerHTML = `
        <div style="flex-grow:1">
            <i class="fas fa-phone-alt"></i> <strong>${escapeHtml(callerName)}</strong><br>
            <small>${callType === 'audio' ? 'Audio' : 'Video'} call...</small>
        </div>
        <div>
            <button id="acceptCallNotif" style="background:#2ecc71; border:none; border-radius:50%; width:40px; height:40px; margin-right:8px; cursor:pointer;">
                <i class="fas fa-check" style="color:white;"></i>
            </button>
            <button id="rejectCallNotif" style="background:#e74c3c; border:none; border-radius:50%; width:40px; height:40px; cursor:pointer;">
                <i class="fas fa-times" style="color:white;"></i>
            </button>
        </div>
    `;
    notifDiv.style.display = 'flex';

    document.getElementById('acceptCallNotif').onclick = () => {
        notifDiv.style.display = 'none';
        callNotificationActive = false;
        sessionStorage.setItem('pendingOffer', JSON.stringify({ callerId, callType, offer }));
        window.location.href = `/${callType}-call/${callerId}?role=receiver`;
    };

    document.getElementById('rejectCallNotif').onclick = () => {
        notifDiv.style.display = 'none';
        callNotificationActive = false;
        if (typeof socket !== 'undefined') {
            socket.emit('reject_call', { caller_id: callerId });
        }
    };
}

function escapeHtml(str) {
    if (!str) return '';
    return str.replace(/[&<>]/g, m => ({'&':'&amp;','<':'&lt;','>':'&gt;'}[m]));
}

if (typeof socket !== 'undefined') {
    socket.on('incoming_call', (data) => {
        const isOnCallPage = window.location.pathname.includes('/audio-call/') || window.location.pathname.includes('/video-call/');
        if (!isOnCallPage) {
            showCallNotification(data.caller_id, data.caller_name, data.call_type, data.offer);
        }
    });
} else {
    console.warn('Socket not defined, call notifications disabled');
          }
