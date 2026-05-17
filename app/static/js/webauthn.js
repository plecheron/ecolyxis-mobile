/* ===== WebAuthn Biometric Registration ===== */

function escapeHtml(s) {
    const d = document.createElement('div');
    d.textContent = s;
    return d.innerHTML;
}

function bufferEncode(buffer) {
    const bytes = new Uint8Array(buffer);
    let binary = '';
    bytes.forEach(b => binary += String.fromCharCode(b));
    return btoa(binary).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');
}

function bufferDecode(str) {
    str = str.replace(/-/g, '+').replace(/_/g, '/');
    while (str.length % 4) str += '=';
    const binary = atob(str);
    const bytes = new Uint8Array(binary.length);
    for (let i = 0; i < binary.length; i++) bytes[i] = binary.charCodeAt(i);
    return bytes.buffer;
}

async function loadWebAuthnDevices() {
    const el = document.getElementById('webauthn-devices');
    const btn = document.getElementById('register-biometric-btn');
    if (!window.PublicKeyCredential) {
        el.innerHTML = '<p class="text-error">Biometric login is not supported on this device.</p>';
        btn.style.display = 'none';
        return;
    }
    try {
        const r = await fetch('/webauthn/credentials');
        const creds = await r.json();
        if (creds.length === 0) {
            el.innerHTML = '<p class="text-muted-sm">No devices registered yet.</p>';
        } else {
            el.innerHTML = creds.map(c => '<div class="webauthn-device">' +
                '<div><div class="webauthn-device-name">' + escapeHtml(c.name) + '</div>' +
                '<div class="webauthn-device-date">Added ' + new Date(c.created_at).toLocaleDateString() +
                (c.last_used_at ? ' · Last used ' + new Date(c.last_used_at).toLocaleDateString() : '') + '</div></div>' +
                '<button onclick="removeWebAuthnDevice(' + c.id + ')" title="Remove">🗑️</button></div>').join('');
        }
        btn.style.display = creds.length >= 5 ? 'none' : '';
    } catch (e) {
        el.innerHTML = '<p class="text-error">Failed to load devices.</p>';
    }
}

async function registerBiometric() {
    const btn = document.getElementById('register-biometric-btn');
    btn.disabled = true;
    btn.textContent = 'Waiting for biometric...';
    try {
        const beginResp = await fetch('/webauthn/register-begin', { method: 'POST' });
        if (!beginResp.ok) throw new Error((await beginResp.json()).error || 'Failed');
        const options = await beginResp.json();
        options.challenge = bufferDecode(options.challenge);
        options.user.id = bufferDecode(options.user.id);
        if (options.excludeCredentials) {
            options.excludeCredentials = options.excludeCredentials.map(c => ({
                ...c, id: bufferDecode(c.id)
            }));
        }
        const credential = await navigator.credentials.create({ publicKey: options });
        const deviceName = prompt('Name this device:', 'My ' + (
            navigator.userAgent.includes('iPhone') || navigator.userAgent.includes('iPad') ? 'iPhone' :
            navigator.userAgent.includes('Android') ? 'Android' : 'Device'
        )) || 'My Device';
        const finishData = {
            credential: {
                id: credential.id,
                rawId: bufferEncode(credential.rawId),
                response: {
                    attestationObject: bufferEncode(credential.response.attestationObject),
                    clientDataJSON: bufferEncode(credential.response.clientDataJSON),
                },
                type: credential.type,
                transports: credential.response.getTransports ? credential.response.getTransports() : [],
            },
            name: deviceName,
        };
        const finishResp = await fetch('/webauthn/register-finish', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(finishData),
        });
        const result = await finishResp.json();
        if (result.success) {
            loadWebAuthnDevices();
        } else {
            alert('Registration failed: ' + (result.error || 'Unknown error'));
        }
    } catch (e) {
        if (e.name !== 'NotAllowedError') alert('Registration failed: ' + e.message);
    } finally {
        btn.disabled = false;
        btn.textContent = '+ Add Device';
    }
}

async function removeWebAuthnDevice(id) {
    if (!confirm('Remove this device?')) return;
    await fetch('/webauthn/credentials/' + id, { method: 'DELETE' });
    loadWebAuthnDevices();
}
