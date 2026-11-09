document.querySelectorAll('[data-toggle-edit]').forEach((button) => {
  button.addEventListener('click', () => {
    const editor = document.getElementById(button.dataset.toggleEdit);
    if (!editor) return;
    editor.hidden = !editor.hidden;
    if (!editor.hidden) editor.querySelector('input')?.focus();
  });
});
document.querySelectorAll('.edit-row').forEach((row) => {
  if (row.previousElementSibling?.dataset.issue === 'true') return;
  const accept = row.querySelector('.edit-accept');
  row.querySelectorAll('input:not([type=hidden])').forEach((input) => {
    input.addEventListener('input', () => { if (accept) accept.hidden = false; });
  });
});
document.querySelectorAll('[data-confirm-delete]').forEach((button) => {
  button.addEventListener('click', (event) => {
    if (!window.confirm('Удалить проблемную строку?')) event.preventDefault();
  });
});

const liveDashboard = document.getElementById('dashboard-live-content');
if (liveDashboard) {
  const alert = document.getElementById('dashboard-live-alert');
  let cursor = Number(liveDashboard.dataset.cursor);
  let checking = false;
  let alertTimer;

  const values = () => new Map(
    [...liveDashboard.querySelectorAll('[data-live-key]')]
      .map((item) => [item.dataset.liveKey, item.dataset.liveValue ?? item.textContent.trim()])
  );

  async function checkDashboard() {
    if (checking) return;
    checking = true;
    try {
      const params = new URLSearchParams({
        start: liveDashboard.dataset.start,
        end: liveDashboard.dataset.end,
        after: String(cursor),
      });
      const response = await fetch(`/dashboard/live?${params}`, { cache: 'no-store' });
      if (response.status === 401) {
        window.location.assign('/login');
        return;
      }
      if (!response.ok) return;
      const update = await response.json();
      if (update.cursor < cursor) return;
      cursor = update.cursor;
      if (!update.changed) return;

      const before = values();
      liveDashboard.innerHTML = update.html;
      liveDashboard.querySelectorAll('[data-live-key]').forEach((item) => {
        const previous = before.get(item.dataset.liveKey);
        const current = item.textContent.trim();
        if (previous === undefined || previous === current) return;
        const oldNumber = Number.parseFloat(previous.replace(',', '.'));
        const newNumber = Number.parseFloat(current.replace(',', '.'));
        item.dataset.liveValue = current;
        item.classList.add(newNumber < oldNumber ? 'live-down' : 'live-up');
        item.setAttribute('aria-label', `${current}, было ${previous}`);
        const oldValue = document.createElement('em');
        oldValue.className = 'live-previous';
        oldValue.textContent = `было ${previous}`;
        item.append(oldValue);
        window.setTimeout(() => {
          item.classList.remove('live-up', 'live-down');
          item.removeAttribute('aria-label');
          oldValue.remove();
        }, 5000);
      });

      if (update.errors > 0) {
        alert.textContent = `Новые данные с ошибками: ${update.errors}. ${update.error_reason} Показатели рассчитаны по принятым строкам.`;
        alert.hidden = false;
        window.clearTimeout(alertTimer);
        alertTimer = window.setTimeout(() => { alert.hidden = true; }, 5000);
      }
    } catch (_) {
      // Keep the current dashboard visible; the next poll will retry.
    } finally {
      checking = false;
    }
  }

  window.setInterval(checkDashboard, 2500);
}
