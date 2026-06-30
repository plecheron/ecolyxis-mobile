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

/* ===== Multi-Select & Bulk Delete ===== */

let selectMode = false;
let selectedThreads = new Set();

function toggleSelectMode() {
    selectMode = !selectMode;
    selectedThreads.clear();
    const btn = document.getElementById('select-mode-btn');
    const bar = document.getElementById('bulk-action-bar');
    const cards = document.querySelectorAll('.thread-card');

    if (selectMode) {
        btn.classList.add('active');
        btn.textContent = '✕ Cancel';
        cards.forEach(c => c.classList.add('selectable'));
    } else {
        btn.classList.remove('active');
        btn.textContent = '☑️ Select';
        cards.forEach(c => {
            c.classList.remove('selectable', 'selected');
            const cb = c.querySelector('.thread-checkbox');
            if (cb) cb.checked = false;
        });
        bar.classList.remove('visible');
    }
    updateBulkCount();
}

function toggleThreadSelect(threadId) {
    const card = document.getElementById('thread-' + threadId);
    if (!card) return;
    const cb = card.querySelector('.thread-checkbox');

    if (selectedThreads.has(threadId)) {
        selectedThreads.delete(threadId);
        card.classList.remove('selected');
        if (cb) cb.checked = false;
    } else {
        selectedThreads.add(threadId);
        card.classList.add('selected');
        if (cb) cb.checked = true;
    }
    updateBulkCount();
}

function updateBulkCount() {
    const countEl = document.getElementById('bulk-selected-count');
    const bar = document.getElementById('bulk-action-bar');
    const deleteBtn = document.getElementById('bulk-delete-btn');
    if (countEl) countEl.textContent = selectedThreads.size;
    if (bar) bar.classList.toggle('visible', selectedThreads.size > 0);
    if (deleteBtn) deleteBtn.disabled = selectedThreads.size === 0;
}

async function bulkDelete() {
    if (selectedThreads.size === 0) return;
    const count = selectedThreads.size;
    if (!confirm('Delete ' + count + ' conversation' + (count > 1 ? 's' : '') + '? This cannot be undone.')) return;

    const deleteBtn = document.getElementById('bulk-delete-btn');
    deleteBtn.disabled = true;
    deleteBtn.textContent = 'Deleting…';

    try {
        const csrfToken = document.querySelector('meta[name=csrf-token]').content;
        const resp = await fetch('/threads/bulk-delete', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json', 'X-CSRFToken': csrfToken },
            body: JSON.stringify({ thread_ids: Array.from(selectedThreads) }),
        });
        const data = await resp.json();
        if (resp.ok) {
            // Remove deleted cards from DOM
            selectedThreads.forEach(id => {
                const card = document.getElementById('thread-' + id);
                if (card) card.remove();
            });
            selectedThreads.clear();
            toggleSelectMode();

            // Show empty state if no threads left
            const grid = document.getElementById('thread-grid');
            if (grid && grid.children.length === 0) {
                location.reload();
            }
        } else {
            alert('Error: ' + (data.error || 'Unknown error'));
            deleteBtn.disabled = false;
            deleteBtn.textContent = '🗑️ Delete Selected';
        }
    } catch (err) {
        alert('Failed to delete: ' + err.message);
        deleteBtn.disabled = false;
        deleteBtn.textContent = '🗑️ Delete Selected';
    }
}
