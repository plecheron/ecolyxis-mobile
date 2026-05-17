/* ===== Dashboard Sidebar Toggle ===== */

function openSidebar() {
    document.getElementById('sidebar').classList.add('open');
    document.getElementById('sidebar-overlay').classList.add('active');
}

function closeSidebar() {
    document.getElementById('sidebar').classList.remove('open');
    document.getElementById('sidebar-overlay').classList.remove('active');
}

/* ===== Biometric Modal ===== */

function showBiometricSettings() {
    document.getElementById('biometric-modal').style.display = 'flex';
    loadWebAuthnDevices();
}

function closeBiometricSettings() {
    document.getElementById('biometric-modal').style.display = 'none';
}
