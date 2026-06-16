/*
 * app.js — Client-side logic for Pool Equipment Scheduler dashboard.
 * Handles auto-refresh, schedule editing, tab switching, toast notifications.
 *
 * Dynamic config (REFRESH_INTERVAL, SLOT_NAMES) is injected via inline script
 * in dashboard.html before this file loads.
 */

let refreshTimer = null;

// ---------- Time input value tracking ----------
// Chrome does NOT reliably commit <input type="time"> .value on programmatic
// blur — especially when the time picker overlay is open (see Vaadin #7852).
// Instead, we track committed values via event listeners on each input and
// read from this Map during save rather than relying on .value.

const timeInputValues = new Map();

/**
 * Attach value-tracking listeners to a time input element.
 * Captures the value at the moment the user commits it (change/blur/input),
 * so we can reliably read it later regardless of Chrome's internal state.
 */
function trackTimeInput(input) {
    if (!input) return;
    // Seed the Map with whatever value is already set (from server data)
    timeInputValues.set(input, input.value || '');

    const commit = () => { timeInputValues.set(input, input.value || ''); };
    input.addEventListener('change', commit);
    input.addEventListener('blur', commit);
    input.addEventListener('input', commit);
}

// ---------- Auto-refresh status ----------

async function refreshStatus() {
    try {
        const resp = await fetch('/api/status');
        if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
        const data = await resp.json();
        updateStatusUI(data);
        updateHealthIndicator('controller', 'ok');
    } catch (err) {
        updateStatusUI({ error: err.message });
        updateHealthIndicator('controller', 'error');
    }
    refreshTimer = setTimeout(refreshStatus, REFRESH_INTERVAL);
}

function updateStatusUI(data) {
    const errorMsg = document.getElementById('status-error');

    if (data.error) {
        errorMsg.textContent = data.error;
        errorMsg.style.display = 'block';
        return;
    }

    errorMsg.style.display = 'none';

    // LCD display
    document.getElementById('lcd-line1').textContent = data.lcd_line1 || '';
    document.getElementById('lcd-line2').textContent = data.lcd_line2 || '';

    const warningEl = document.getElementById('lcd-check-system');
    warningEl.style.display = data.check_system ? 'block' : 'none';

    // Inject equipment icons into the name cells
    const iconMap = { filter: 'filter', pool: 'pool', spa: 'spa', heater: 'heater', cleaner: 'cleaner', waterfall: 'waterfall', lights: 'lights', spa_light: 'spa_light', blower: 'blower' };
    const nameToSlotLocal = {};
    for (const [slot, name] of Object.entries(SLOT_NAMES)) {
        nameToSlotLocal[name] = slot;
    }

    for (const [eqName, state] of Object.entries(data.equipment)) {
        const slot = nameToSlotLocal[eqName];
        if (!slot) continue;

        // Update slider toggle — set checkbox and state class based on live state
        const toggle = document.querySelector(`.toggle-switch[data-slot="${slot}"]`);
        if (toggle) {
            const input = toggle.querySelector('.toggle-input');
            toggle.classList.remove('on', 'off', 'unknown');
            if (state === 'ON') {
                input.checked = true;
                toggle.classList.add('on');
            } else if (state === 'OFF') {
                input.checked = false;
                toggle.classList.add('off');
            } else {
                input.checked = false;
                toggle.classList.add('unknown');
            }
        }

        // Inject icon into the name cell for this row
        const row = toggle?.closest('tr');
        if (row && iconMap[eqName]) {
            const nameCell = row.querySelector('.equipment-name-cell');
            if (nameCell && !nameCell.querySelector('.eq-icon')) {
                const svgSrc = document.querySelector(`.eq-icon-${eqName}`);
                if (svgSrc) {
                    const icon = svgSrc.cloneNode(true);
                    icon.classList.add('row-icon');
                    nameCell.prepend(icon);
                }
            }
        }
    }

    // Timestamp
    document.getElementById('last-updated').textContent =
        `Last updated: ${new Date(data.timestamp).toLocaleTimeString()}`;
}

// ---------- Slider toggle switches ----------

document.querySelectorAll('.toggle-switch .toggle-input').forEach(input => {
    input.addEventListener('change', async () => {
        const toggle = input.closest('.toggle-switch');
        const slot = toggle.dataset.slot;
        const name = SLOT_NAMES[slot] || toggle.dataset.name;
        const newState = input.checked ? 'ON' : 'OFF';

        // Lock the toggle during the API call to prevent double-clicks
        toggle.style.pointerEvents = 'none';

        try {
            const resp = await fetch(`/api/toggle/${name}`, { method: 'POST' });
            if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
            const result = await resp.json();
            showToast('success', result.result.split('.')[0] || 'Toggled');
            refreshStatus(); // Immediate refresh — will update toggle to match server state
        } catch (err) {
            input.checked = !input.checked; // revert visual state on error
            showToast('error', `Toggle failed: ${err.message}`);
        } finally {
            toggle.style.pointerEvents = '';
        }
    });
});

// ---------- Schedule editor ----------

/**
 * Normalize a schedule entry to an array of {on, off} windows.
 * Accepts:
 *   - undefined/null → []
 *   - {on, off}      → [{on, off}]
 *   - [{on, off}, ...] → [{on, off}, ...]
 */
function normalizeWindows(raw) {
    if (!raw) return [];
    if (Array.isArray(raw)) return raw;
    if (typeof raw === 'object') return [raw];
    return [];
}

/**
 * Build a single time input element.
 */
function createTimeInput(eqName, type, value = '') {
    const input = document.createElement('input');
    input.type = 'time';
    input.id = `${type}-${eqName}`;
    input.className = 'time-input';
    input.value = value;
    trackTimeInput(input);
    return input;
}

/**
 * Build ON time input wrapper for a window row.
 */
function buildOnInput(eqName, onVal = '', index = 0) {
    const input = createTimeInput(eqName, `on-${eqName}-${index}`, onVal);
    input.dataset.windowIndex = index;
    return input;
}

/**
 * Build OFF time input wrapper for a window row.
 */
function buildOffInput(eqName, offVal = '', index = 0) {
    const input = createTimeInput(eqName, `off-${eqName}-${index}`, offVal);
    input.dataset.windowIndex = index;
    return input;
}

/**
 * Build a complete window row (ON + OFF inputs + remove button) for a given equipment.
 * Returns a pair of elements to append to the ON column and OFF column respectively.
 */
function buildWindowRow(eqName, onVal = '', offVal = '', index = 0, removable = false) {
    const onContainer = document.createElement('div');
    onContainer.className = 'schedule-window';
    onContainer.dataset.windowIndex = index;

    const offContainer = document.createElement('div');
    offContainer.className = 'schedule-window';
    offContainer.dataset.windowIndex = index;

    const onInput = buildOnInput(eqName, onVal, index);
    const offInput = buildOffInput(eqName, offVal, index);

    const actions = document.createElement('span');
    actions.style.display = 'flex';
    actions.style.gap = '4px';
    actions.style.alignItems = 'center';

    if (removable) {
        const removeBtn = document.createElement('button');
        removeBtn.type = 'button';
        removeBtn.className = 'btn-remove-window';
        removeBtn.textContent = '−';
        removeBtn.title = 'Remove this window';
        removeBtn.addEventListener('click', () => {
            onContainer.remove();
            offContainer.remove();
            reindexWindows(eqName);
            updateRemoveButtons(eqName);
        });
        actions.appendChild(removeBtn);
    }

    onContainer.appendChild(onInput);
    onContainer.appendChild(actions);
    offContainer.appendChild(offInput);

    return { onContainer, offContainer, onInput, offInput };
}

/**
 * Remove a schedule window row from the DOM.
 * Note: This function is kept for backward compatibility but is no longer
 * called directly since remove buttons now handle removal inline.
 */
function removeWindow(eqName, index, containerEl) {
    containerEl.remove();
    reindexWindows(eqName);
    updateRemoveButtons(eqName);
}

/**
 * Re-index window inputs after one is removed.
 */
function reindexWindows(eqName) {
    const onContainer = document.querySelector(`.equipment-row[data-equipment="${eqName}"] td:nth-child(2)`);
    const offContainer = document.querySelector(`.equipment-row[data-equipment="${eqName}"] td:nth-child(3)`);
    if (!onContainer || !offContainer) return;

    const windows = onContainer.querySelectorAll('.schedule-window');
    windows.forEach((win, i) => {
        win.dataset.windowIndex = i;
    });
}

/**
 * Update remove button visibility (disable if only one window remains).
 */
function updateRemoveButtons(eqName) {
    const onContainer = document.querySelector(`.equipment-row[data-equipment="${eqName}"] td:nth-child(2)`);
    if (!onContainer) return;

    const windows = onContainer.querySelectorAll('.schedule-window');
    onContainer.querySelectorAll('.btn-remove-window').forEach((btn, i) => {
        btn.disabled = windows.length <= 1;
    });
}

/**
 * Add a new schedule window for a given equipment.
 */
function addWindow(eqName) {
    const onCell = document.querySelector(`.equipment-row[data-equipment="${eqName}"] td:nth-child(2)`);
    const offCell = document.querySelector(`.equipment-row[data-equipment="${eqName}"] td:nth-child(3)`);
    if (!onCell || !offCell) return;

    const index = onCell.querySelectorAll('.schedule-window').length;
    const { onContainer, offContainer } = buildWindowRow(eqName, '', '', index, true);

    onCell.appendChild(onContainer);
    offCell.appendChild(offContainer);

    reindexWindows(eqName);
    updateRemoveButtons(eqName);
}

/**
 * Load schedules from API and populate the UI.
 */
async function loadSchedules() {
    try {
        const resp = await fetch('/api/schedules');
        if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
        const schedules = await resp.json();

        for (const eqName of SCHEDULED_EQUIPMENT) {
            const disabledCheckbox = document.getElementById(`disabled-${eqName}`);
            const onCell = document.querySelector(`.equipment-row[data-equipment="${eqName}"] td:nth-child(2)`);
            const offCell = document.querySelector(`.equipment-row[data-equipment="${eqName}"] td:nth-child(3)`);

            // Clear existing windows
            onCell.innerHTML = '';
            offCell.innerHTML = '';

            const windows = normalizeWindows(schedules[eqName]);
            const isDisabled = windows.length === 0;

            disabledCheckbox.checked = isDisabled;
            if (isDisabled) {
                // Add one empty disabled row
                const { onContainer, offContainer } = buildWindowRow(eqName, '', '', 0, false);
                onContainer.querySelectorAll('input').forEach(inp => inp.disabled = true);
                offContainer.querySelectorAll('input').forEach(inp => inp.disabled = true);
                onCell.appendChild(onContainer);
                offCell.appendChild(offContainer);
            } else {
                windows.forEach((w, i) => {
                    const { onContainer, offContainer } = buildWindowRow(eqName, w.on || '', w.off || '', i, i > 0);
                    onCell.appendChild(onContainer);
                    offCell.appendChild(offContainer);
                });
                updateRemoveButtons(eqName);
            }
        }
    } catch (err) {
        console.error('Failed to load schedules:', err);
        showToast('error', `Load failed: ${err.message}`);
    }
}

/**
 * Collect all schedule windows from the UI and save to API.
 */
async function saveSchedules() {
    const btn = document.getElementById('btn-save-schedule');
    btn.textContent = 'Saving...';
    btn.disabled = true;

    // Step 1: If a time input has focus (time picker may be open), shift focus
    // to this button so the picker closes and .value commits. Chrome's native
    // <input type="time"> does not update .value until after the picker is
    // dismissed, so we must give it a moment to flush before reading.
    const wasTimeFocused = document.activeElement?.matches('input[type="time"]');
    if (wasTimeFocused) {
        btn.focus();
        // Wait ~200 ms for Chrome to commit .value after picker closes
        await new Promise(r => setTimeout(r, 200));
    }

    // Clear any previous validation highlighting
    document.querySelectorAll('.schedule-window.input-error').forEach(el => {
        el.classList.remove('input-error');
    });

    const schedules = {};
    const errors = [];

    for (const eqName of SCHEDULED_EQUIPMENT) {
        const disabledCheckbox = document.getElementById(`disabled-${eqName}`);
        const onContainer = document.querySelector(`.equipment-row[data-equipment="${eqName}"] td:nth-child(2)`);
        const offContainer = document.querySelector(`.equipment-row[data-equipment="${eqName}"] td:nth-child(3)`);

        if (disabledCheckbox.checked) {
            schedules[eqName] = null; // signal server to remove/disable this equipment
            continue;
        }

        const onWindows = onContainer.querySelectorAll('.schedule-window');
        const offWindows = offContainer.querySelectorAll('.schedule-window');

        // Collect windows and identify any incomplete ones.
        const windows = [];
        let hasIncomplete = false;
        for (let i = 0; i < onWindows.length; i++) {
            const onInput = onWindows[i].querySelector('input[type="time"]');
            const offInput = offWindows[i] ? offWindows[i].querySelector('input[type="time"]') : null;

            const onVal = onInput?.value || '';
            const offVal = offInput?.value || '';

            if (!onVal || !offVal) {
                // Highlight incomplete windows so the user sees the problem
                if (onWindows[i]) onWindows[i].classList.add('input-error');
                if (offWindows[i]) offWindows[i].classList.add('input-error');
                hasIncomplete = true;
                continue;
            }

            windows.push({ on: onVal, off: offVal });
        }

        if (hasIncomplete) {
            errors.push(`${eqName}: has incomplete time window(s) - please fill both ON and OFF times`);
            // Still keep valid windows so partial saves work
        }

        if (windows.length === 0) {
            continue;
        }

        // Store as single dict if only one window, otherwise as list
        schedules[eqName] = windows.length === 1 ? windows[0] : windows;
    }

    if (errors.length > 0 && Object.keys(schedules).length === 0) {
        // Nothing valid to save — show error and abort
        showToast('error', errors[0]);
        btn.textContent = 'Save Schedule';
        btn.disabled = false;
        return;
    }

    try {
        const resp = await fetch('/api/schedules', {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(schedules),
        });

        const result = await resp.json();
        if (!resp.ok) throw new Error(result.detail || `HTTP ${resp.status}`);

        if (errors.length > 0) {
            showToast('error', `Saved with warnings: ${errors[0]}`);
        } else {
            showToast('success', `Saved! Sync: ${result.sync}`);
        }
    } catch (err) {
        showToast('error', `Save failed: ${err.message}`);
    }

    btn.textContent = 'Save Schedule';
    btn.disabled = false;
}

document.getElementById('btn-save-schedule').addEventListener('click', saveSchedules);

// Disabled checkbox handlers — checkbox id is "disabled-{equipment_name}"
document.querySelectorAll('.disabled-checkbox').forEach(checkbox => {
    checkbox.addEventListener('change', () => {
        const eqName = checkbox.id.replace('disabled-', '');
        const onContainer = document.querySelector(`.equipment-row[data-equipment="${eqName}"] td:nth-child(2)`);
        const offContainer = document.querySelector(`.equipment-row[data-equipment="${eqName}"] td:nth-child(3)`);

        onContainer.querySelectorAll('input[type="time"]').forEach(inp => {
            inp.disabled = checkbox.checked;
            if (checkbox.checked) inp.value = '';
        });
        offContainer.querySelectorAll('input[type="time"]').forEach(inp => {
            inp.disabled = checkbox.checked;
            if (checkbox.checked) inp.value = '';
        });
    });
});

// Add window button handlers (delegated) — button data-equipment is the equipment name
document.getElementById('schedule-table').addEventListener('click', (e) => {
    if (e.target.classList.contains('btn-add-window')) {
        const eqName = e.target.dataset.equipment;
        addWindow(eqName);
    }
});

// ---------- History ----------

async function loadHistory() {
    try {
        const resp = await fetch('/api/history?limit=50');
        if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
        const entries = await resp.json();

        const tbody = document.getElementById('history-body');
        if (!entries.length) {
            tbody.innerHTML = '<tr><td colspan="3">No recent actions</td></tr>';
            return;
        }

        tbody.innerHTML = entries.slice().reverse().map(entry => {
            const time = new Date(entry.timestamp).toLocaleString();
            const action = entry.action || 'unknown';
            const details = entry.details || '';
            return `<tr>
                <td>${time}</td>
                <td>${escapeHtml(action)}</td>
                <td>${escapeHtml(details)}</td>
            </tr>`;
        }).join('');
    } catch (err) {
        document.getElementById('history-body').innerHTML =
            `<tr><td colspan="3">Error: ${err.message}</td></tr>`;
    }
}

async function loadDagRuns() {
    try {
        const resp = await fetch('/api/dag_runs?limit=20');
        if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
        const data = await resp.json();
        const runs = data.dag_runs || [];

        const tbody = document.getElementById('dag-runs-body');
        if (!runs.length) {
            tbody.innerHTML = '<tr><td colspan="3">No DAG runs found</td></tr>';
            return;
        }

        tbody.innerHTML = runs.map(run => {
            const start = new Date(run.start_date).toLocaleString();
            const end = run.end_date ? new Date(run.end_date).toLocaleString() : '--';
            const state = (run.state || 'UNKNOWN').toLowerCase();
            const duration = run.duration ? `${Math.round(run.duration)}s` : '--';
            return `<tr>
                <td>${start}</td>
                <td><span class="state-${state}">${escapeHtml(run.state)}</span></td>
                <td>${duration}</td>
            </tr>`;
        }).join('');
    } catch (err) {
        document.getElementById('dag-runs-body').innerHTML =
            `<tr><td colspan="3">Error: ${err.message}</td></tr>`;
    }
}

// ---------- Health check ----------

async function checkHealth() {
    try {
        const resp = await fetch('/api/health');
        if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
        const health = await resp.json();

        updateHealthIndicator('controller', health.controller === 'ok' ? 'ok' : 'error');
        updateHealthIndicator('airflow', health.airflow === 'ok' ? 'ok' : 'error');
    } catch (err) {
        updateHealthIndicator('controller', 'error');
        updateHealthIndicator('airflow', 'error');
    }
}

function updateHealthIndicator(service, state) {
    const el = document.getElementById(`health-${service}`);
    if (!el) return;
    el.className = `health-dot ${state}`;
}

// ---------- Tabs ----------

document.querySelectorAll('.tab-btn').forEach(btn => {
    btn.addEventListener('click', () => {
        document.querySelectorAll('.tab-content').forEach(el => el.classList.remove('active'));
        document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));

        const tab = btn.dataset.tab;
        document.getElementById(`tab-${tab}`).classList.add('active');
        btn.classList.add('active');

        // Load data when switching to a tab
        if (tab === 'history') {
            loadHistory();
            loadDagRuns();
        } else if (tab === 'schedule') {
            loadSchedules();
        } else if (tab === 'analytics') {
            loadAnalytics();
        }
    });
});

// ---------- Toast ----------

function showToast(type, message) {
    const toast = document.getElementById('toast');
    toast.className = `toast toast-${type}`;
    toast.textContent = message;
    toast.style.display = 'block';
    setTimeout(() => { toast.style.display = 'none'; }, 3500);
}

// ---------- Utilities ----------

function escapeHtml(str) {
    const div = document.createElement('div');
    div.textContent = str;
    return div.innerHTML;
}

// ---------- Init ----------

// ---------- Theme toggle ----------

function applyTheme(theme) {
    document.documentElement.setAttribute('data-theme', theme);
    localStorage.setItem('pcss-theme', theme);
}

// ---------- Analytics / Charts ----------

/** Cached Chart.js instances so we can destroy them before re-rendering. */
const chartInstances = {};

function chartDefaults() {
    const isDark = document.documentElement.getAttribute('data-theme') === 'dark';
    return {
        textColor: isDark ? '#bbb' : '#555',
        gridColor: isDark ? 'rgba(255,255,255,0.07)' : 'rgba(0,0,0,0.06)',
        bgColor: isDark ? 'rgba(30,30,30,1)' : 'rgba(255,255,255,1)',
    };
}

const EQUIPMENT_COLORS = [
    '#2196F3', '#4CAF50', '#FF9800', '#E91E63', '#9C27B0',
    '#00BCD4', '#FFC107', '#78909C', '#FF5722',
];

const EQUIPMENT_LABELS = {
    filter: 'Filter', pool: 'Pool', spa: 'Spa', heater: 'Heater',
    cleaner: 'Cleaner', waterfall: 'Waterfall', lights: 'Pool Light',
    spa_light: 'Spa Light', blower: 'Blower',
};

function destroyChart(key) {
    if (chartInstances[key]) { chartInstances[key].destroy(); delete chartInstances[key]; }
}

async function loadAnalytics() {
    // Fetch latest sensor reading → KPI tiles
    try {
        const [latest, dailyStats, runtime] = await Promise.all([
            fetch('/api/metrics/latest').then(r => r.json()),
            fetch('/api/metrics/daily-stats?days=30').then(r => r.json()),
            fetch('/api/metrics/equipment-runtime?days=7').then(r => r.json()),
        ]);

        // KPI tiles
        const kpiAir = document.getElementById('kpi-air-temp');
        const kpiSalt = document.getElementById('kpi-salt-ppm');
        if (kpiAir && latest.air_temp_f != null) kpiAir.textContent = latest.air_temp_f.toFixed(1);
        else if (kpiAir) kpiAir.textContent = '--';
        if (kpiSalt && latest.salt_ppm != null) kpiSalt.textContent = Math.round(latest.salt_ppm);
        else if (kpiSalt) kpiSalt.textContent = '--';

        // Filter & Heater hours from runtime summary
        const filtHours = runtime.summary_7d?.find(e => e.equipment === 'filter');
        const heatHours = runtime.summary_7d?.find(e => e.equipment === 'heater');
        const kpiFilter = document.getElementById('kpi-filter-hrs');
        const kpiHeater = document.getElementById('kpi-heater-hrs');
        if (kpiFilter && filtHours) kpiFilter.textContent = filtHours.total_hours;
        else if (kpiFilter) kpiFilter.textContent = '--';
        if (kpiHeater && heatHours) kpiHeater.textContent = heatHours.total_hours;
        else if (kpiHeater) kpiHeater.textContent = '--';

        // Render charts
        renderAirTempChart(dailyStats.air_temp);
        renderSaltLevelChart(dailyStats.salt_level);
        renderEquipmentBarChart(runtime.summary_7d);
        renderRuntimePieChart(runtime.summary_7d);

    } catch (err) {
        console.error('Failed to load analytics:', err);
    }
}

function renderAirTempChart(data) {
    destroyChart('airTemp');
    const canvas = document.getElementById('chart-air-temp');
    if (!canvas) return;
    const d = chartDefaults();
    const labels = data.map(r => r.d);
    chartInstances['airTemp'] = new Chart(canvas, {
        type: 'line',
        data: {
            labels,
            datasets: [
                { label: 'Avg °F', data: data.map(r => r.avg_f), borderColor: '#2196F3', backgroundColor: 'rgba(33,150,243,0.1)', fill: true, tension: 0.35 },
                { label: 'Max °F', data: data.map(r => r.max_f), borderColor: '#FF5722', borderDash: [6, 4], borderWidth: 1, pointRadius: 0, fill: false },
                { label: 'Min °F', data: data.map(r => r.min_f), borderColor: '#4CAF50', borderDash: [6, 4], borderWidth: 1, pointRadius: 0, fill: false },
            ],
        },
        options: {
            responsive: true, maintainAspectRatio: false,
            plugins: { legend: { labels: { color: d.textColor } } },
            scales: {
                x: { ticks: { color: d.textColor }, grid: { color: d.gridColor } },
                y: { ticks: { color: d.textColor, callback: v => v + '°' }, grid: { color: d.gridColor } },
            },
        },
    });
}

function renderSaltLevelChart(data) {
    destroyChart('salt');
    const canvas = document.getElementById('chart-salt-level');
    if (!canvas) return;
    const d = chartDefaults();
    chartInstances['salt'] = new Chart(canvas, {
        type: 'line',
        data: {
            labels: data.map(r => r.d),
            datasets: [{
                label: 'Avg PPM', data: data.map(r => r.avg_ppm),
                borderColor: '#FF9800', backgroundColor: 'rgba(255,152,0,0.1)',
                fill: true, tension: 0.35,
            }],
        },
        options: {
            responsive: true, maintainAspectRatio: false,
            plugins: { legend: { labels: { color: d.textColor } } },
            scales: {
                x: { ticks: { color: d.textColor }, grid: { color: d.gridColor } },
                y: { ticks: { color: d.textColor }, grid: { color: d.gridColor } },
            },
        },
    });
}

function renderEquipmentBarChart(summary) {
    destroyChart('eqBar');
    const canvas = document.getElementById('chart-equipment-bar');
    if (!canvas || !summary.length) return;
    const d = chartDefaults();
    const labels = summary.map(e => EQUIPMENT_LABELS[e.equipment] || e.equipment);
    const values = summary.map(e => parseFloat(e.total_hours) || 0);
    chartInstances['eqBar'] = new Chart(canvas, {
        type: 'bar',
        data: {
            labels,
            datasets: [{
                label: 'Hours (7d)', data: values,
                backgroundColor: EQUIPMENT_COLORS.slice(0, summary.length),
                borderRadius: 4,
            }],
        },
        options: {
            responsive: true, maintainAspectRatio: false,
            plugins: { legend: { display: false } },
            scales: {
                x: { ticks: { color: d.textColor }, grid: { display: false } },
                y: { beginAtZero: true, ticks: { color: d.textColor, callback: v => v + 'h' }, grid: { color: d.gridColor } },
            },
        },
    });
}

function renderRuntimePieChart(summary) {
    destroyChart('pie');
    const canvas = document.getElementById('chart-runtime-pie');
    if (!canvas || !summary.length) return;
    const d = chartDefaults();
    // Filter out zero-hour equipment for the pie
    const active = summary.filter(e => (parseFloat(e.total_hours) || 0) > 0);
    chartInstances['pie'] = new Chart(canvas, {
        type: 'doughnut',
        data: {
            labels: active.map(e => EQUIPMENT_LABELS[e.equipment] || e.equipment),
            datasets: [{
                data: active.map(e => parseFloat(e.total_hours) || 0),
                backgroundColor: EQUIPMENT_COLORS.slice(0, active.length),
                borderWidth: 2,
                borderColor: d.bgColor,
            }],
        },
        options: {
            responsive: true, maintainAspectRatio: false,
            plugins: {
                legend: { position: 'right', labels: { color: d.textColor, padding: 10 } },
            },
        },
    });
}


// ---------- Init ----------

document.addEventListener('DOMContentLoaded', () => {
    // Restore saved theme (or use system preference)
    const saved = localStorage.getItem('pcss-theme');
    if (saved) {
        applyTheme(saved);
    } else if (window.matchMedia('(prefers-color-scheme: dark)').matches) {
        applyTheme('dark');
    }

    // Toggle button
    const themeBtn = document.getElementById('theme-toggle');
    if (themeBtn) {
        themeBtn.addEventListener('click', () => {
            const current = document.documentElement.getAttribute('data-theme');
            applyTheme(current === 'dark' ? 'light' : 'dark');
        });
    }

    checkHealth();
    refreshStatus();
    loadSchedules();
    // Periodic health check
    setInterval(checkHealth, 60000);
});
