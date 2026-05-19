/* ===== Dashboard Sidebar Toggle ===== */

function openSidebar() {
    document.getElementById('sidebar').classList.add('open');
    document.getElementById('sidebar-overlay').classList.add('active');
}

function closeSidebar() {
    document.getElementById('sidebar').classList.remove('open');
    document.getElementById('sidebar-overlay').classList.remove('active');
}

/* ===== Thread Rename (double-click on dashboard cards) ===== */
function startDashRename(e, tId, el) {
    e.preventDefault();
    e.stopPropagation();
    const current = el.textContent;
    const input = document.createElement('input');
    input.type = 'text';
    input.className = 'thread-rename-input';
    input.value = current;
    input.maxLength = 200;
    el.replaceWith(input);
    input.focus();
    input.select();

    async function save() {
        const newTitle = input.value.trim();
        const h3 = document.createElement('h3');
        h3.setAttribute('ondblclick', "startDashRename(event, '" + tId + "', this)");
        if (newTitle && newTitle !== current) {
            try {
                const resp = await fetch('/chat/' + tId + '/rename', {
                    method: 'PATCH',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ title: newTitle })
                });
                if (resp.ok) {
                    h3.textContent = newTitle;
                } else {
                    h3.textContent = current;
                }
            } catch (err) {
                h3.textContent = current;
            }
        } else {
            h3.textContent = current;
        }
        input.replaceWith(h3);
    }

    input.addEventListener('blur', save);
    input.addEventListener('keydown', function(ev) {
        if (ev.key === 'Enter') { ev.preventDefault(); input.blur(); }
        if (ev.key === 'Escape') { input.value = current; input.blur(); }
    });
}

/* ===== Biometric Modal ===== */

function showBiometricSettings() {
    document.getElementById('biometric-modal').style.display = 'flex';
    loadWebAuthnDevices();
}

function closeBiometricSettings() {
    document.getElementById('biometric-modal').style.display = 'none';
}
