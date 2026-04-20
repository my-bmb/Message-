// static/js/call-notification.js - Professional Version

let callNotificationActive = false;
let currentCallData = null;
let notificationTimeout = null;

function showCallNotification(callerId, callerName, callType, offer) {
    // Don't show if already on call page
    const isOnCallPage = window.location.pathname.includes('/audio-call/') || window.location.pathname.includes('/video-call/');
    if (isOnCallPage) return;
    
    // Don't show duplicate notifications
    if (callNotificationActive) return;
    
    callNotificationActive = true;
    currentCallData = { callerId, callerName, callType, offer };

    // Remove existing notification if any
    let notifDiv = document.getElementById('global-call-notification');
    if (notifDiv) notifDiv.remove();

    // Create new notification
    notifDiv = document.createElement('div');
    notifDiv.id = 'global-call-notification';
    notifDiv.style.cssText = `
        position: fixed;
        top: 20px;
        right: 20px;
        left: auto;
        background: linear-gradient(135deg, #1a1a2e 0%, #16213e 100%);
        color: white;
        border-radius: 16px;
        padding: 16px 20px;
        z-index: 10000;
        box-shadow: 0 10px 30px rgba(0,0,0,0.3);
        display: flex;
        align-items: center;
        gap: 16px;
        font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
        min-width: 280px;
        max-width: 350px;
        backdrop-filter: blur(10px);
        border: 1px solid rgba(255,255,255,0.15);
        animation: slideInRight 0.3s ease-out;
        cursor: pointer;
    `;
    
    // Add animation styles if not present
    if (!document.querySelector('#call-notification-styles')) {
        const style = document.createElement('style');
        style.id = 'call-notification-styles';
        style.textContent = `
            @keyframes slideInRight {
                from {
                    transform: translateX(100%);
                    opacity: 0;
                }
                to {
                    transform: translateX(0);
                    opacity: 1;
                }
            }
            @keyframes ring {
                0% { transform: rotate(0); }
                25% { transform: rotate(10deg); }
                50% { transform: rotate(-10deg); }
                75% { transform: rotate(5deg); }
                100% { transform: rotate(0); }
            }
            .call-avatar {
                width: 48px;
                height: 48px;
                background: linear-gradient(135deg, #00b4db, #0083b0);
                border-radius: 50%;
                display: flex;
                align-items: center;
                justify-content: center;
                font-size: 24px;
                animation: ring 1s infinite;
            }
            .call-info {
                flex: 1;
            }
            .caller-name {
                font-weight: 600;
                font-size: 16px;
                margin-bottom: 4px;
            }
            .call-type {
                font-size: 12px;
                opacity: 0.8;
                display: flex;
                align-items: center;
                gap: 4px;
            }
            .call-buttons {
                display: flex;
                gap: 10px;
            }
            .accept-btn, .reject-btn {
                width: 40px;
                height: 40px;
                border-radius: 50%;
                border: none;
                cursor: pointer;
                transition: transform 0.2s, opacity 0.2s;
                display: flex;
                align-items: center;
                justify-content: center;
                font-size: 18px;
            }
            .accept-btn {
                background: linear-gradient(135deg, #00b09b, #96c93d);
                color: white;
            }
            .reject-btn {
                background: linear-gradient(135deg, #cb356b, #bd3f32);
                color: white;
            }
            .accept-btn:hover, .reject-btn:hover {
                transform: scale(1.05);
            }
            .accept-btn:active, .reject-btn:active {
                transform: scale(0.95);
            }
            @media (max-width: 480px) {
                .call-avatar { width: 40px; height: 40px; font-size: 20px; }
                .accept-btn, .reject-btn { width: 36px; height: 36px; font-size: 16px; }
                .caller-name { font-size: 14px; }
                .call-type { font-size: 10px; }
            }
        `;
        document.head.appendChild(style);
    }
    
    // Get avatar initial
    const initial = callerName ? callerName.charAt(0).toUpperCase() : '?';
    const callTypeIcon = callType === 'audio' ? '📞' : '📹';
    const callTypeText = callType === 'audio' ? 'Audio Call' : 'Video Call';
    
    notifDiv.innerHTML = `
        <div class="call-avatar">
            <span>${escapeHtml(initial)}</span>
        </div>
        <div class="call-info">
            <div class="caller-name">${escapeHtml(callerName)}</div>
            <div class="call-type">
                <span>${callTypeIcon}</span>
                <span>Incoming ${callTypeText}</span>
            </div>
        </div>
        <div class="call-buttons">
            <button class="accept-btn" id="acceptCallNotif" title="Accept">
                <i class="fas fa-phone-alt"></i>
            </button>
            <button class="reject-btn" id="rejectCallNotif" title="Reject">
                <i class="fas fa-times"></i>
            </button>
        </div>
    `;
    
    document.body.appendChild(notifDiv);
    
    // Auto hide after 30 seconds
    notificationTimeout = setTimeout(() => {
        if (notifDiv) {
            notifDiv.style.display = 'none';
            callNotificationActive = false;
            setTimeout(() => {
                if (notifDiv) notifDiv.remove();
            }, 300);
        }
    }, 30000);
    
    // Accept call handler
    const acceptBtn = document.getElementById('acceptCallNotif');
    if (acceptBtn) {
        acceptBtn.onclick = (e) => {
            e.stopPropagation();
            if (notificationTimeout) clearTimeout(notificationTimeout);
            notifDiv.style.display = 'none';
            callNotificationActive = false;
            sessionStorage.setItem('pendingOffer', JSON.stringify({ callerId, callType, offer }));
            // Small delay to ensure clean navigation
            setTimeout(() => {
                window.location.href = `/${callType}-call/${callerId}?role=receiver`;
            }, 100);
        };
    }
    
    // Reject call handler
    const rejectBtn = document.getElementById('rejectCallNotif');
    if (rejectBtn) {
        rejectBtn.onclick = (e) => {
            e.stopPropagation();
            if (notificationTimeout) clearTimeout(notificationTimeout);
            notifDiv.style.display = 'none';
            callNotificationActive = false;
            if (typeof socket !== 'undefined' && socket.connected) {
                socket.emit('reject_call', { caller_id: callerId });
            }
            setTimeout(() => {
                if (notifDiv) notifDiv.remove();
            }, 300);
        };
    }
    
    // Click anywhere on notification (but not buttons) - can be used to focus
    notifDiv.addEventListener('click', (e) => {
        if (e.target.closest('.accept-btn') || e.target.closest('.reject-btn')) return;
        // Just focus on the notification, don't auto-accept
    });
}

function escapeHtml(str) {
    if (!str) return '';
    return str.replace(/[&<>]/g, m => {
        if (m === '&') return '&amp;';
        if (m === '<') return '&lt;';
        if (m === '>') return '&gt;';
        return m;
    });
}

// Initialize socket event listener when socket is available
function initCallNotifications() {
    if (typeof socket !== 'undefined' && socket) {
        // Remove existing listener to avoid duplicates
        socket.off('incoming_call');
        
        socket.on('incoming_call', (data) => {
            console.log('📞 Incoming call from:', data.caller_name);
            showCallNotification(data.caller_id, data.caller_name, data.call_type, data.offer);
        });
        
        console.log('✅ Call notifications initialized');
    } else {
        console.warn('⚠️ Socket not defined, retrying in 1 second...');
        setTimeout(initCallNotifications, 1000);
    }
}

// Start initialization when DOM is ready
if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', initCallNotifications);
} else {
    initCallNotifications();
}