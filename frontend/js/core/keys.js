/* Keyboard.

   A wall display is driven from whatever keyboard is nearest, often by
   someone who did not set it up, so the bindings have to be few, findable and
   unable to do anything a stray press should not. One window listener serves
   the whole page: a binding is declared once, next to the code it drives, and
   `?` lists every one of them with whether it would do anything right now.

   What is deliberately *not* reachable from here is the point of the module.
   No binding arms, engages, goes, tracks or parks — every one of those is a
   decision to move the antenna, and a decision is a click on a control that
   says what it does. The keys reach only the safe direction (STOP, DISENGAGE)
   and keeping consent alive (EXTEND). The control buttons' own Enter and
   Space are held to the same idea in control.js: they answer only when Tab
   put the focus there, never because a click left it there (guardKeys).

   Three rules every binding gets for free:

   Key auto-repeat is ignored. A held key is not a second decision, and a
   binding that counts presses — STOP's confirm — must not be satisfied by one.

   Keys typed into a text field are the field's. Number inputs are the
   exception: the AZ/EL boxes take no letters, and an operator with the cursor
   in the AZ box is exactly the one who may need Shift+S.

   Combos match on `ev.code`, the physical key, not `ev.key`, the character it
   produced. The station is in Thailand; with the Thai layout active, Shift+S
   produces a Thai letter, and a binding on the character would silently stop
   working whenever someone had last been typing Thai. */

const TEXT_TYPES = new Set(['text', 'search', 'password', 'email', 'url', 'tel']);

const bindings = [];
let mounted = false;
let dialog = null;

/** Register a key. `combo` is modifiers and a key joined by '+', e.g.
    'Shift+S', 'F' or 'Shift+Slash'. `when` says whether the binding applies
    now; a key whose `when` is false does nothing at all. */
export function bindKey({ combo, label, when, run }) {
  const parsed = parse(combo);
  if (!parsed) {
    console.warn('[keys] unparsable combo', combo);
    return;
  }
  bindings.push({ combo, label, when: when || (() => true), run, ...parsed });
  if (dialog?.open) paintHelp();
}

export function mountKeys() {
  if (mounted) return;
  mounted = true;
  bindKey({ combo: 'Shift+Slash', label: 'this list', run: toggleHelp });
  window.addEventListener('keydown', onKey);
}

function parse(combo) {
  const parts = String(combo).split('+').map((p) => p.trim()).filter(Boolean);
  const key = parts.pop();
  if (!key) return null;
  const mods = new Set(parts.map((p) => p.toLowerCase()));
  // A single letter or digit is named as the character on the key cap;
  // anything longer is already a KeyboardEvent.code name ('Slash', 'Escape').
  const code = /^[a-z]$/i.test(key) ? `Key${key.toUpperCase()}`
             : /^[0-9]$/.test(key) ? `Digit${key}`
             : key;
  return {
    code,
    shift: mods.has('shift'),
    ctrl: mods.has('ctrl'),
    alt: mods.has('alt'),
    meta: mods.has('meta'),
  };
}

function matches(b, ev) {
  // Every modifier must match, not only the ones named: Ctrl+Shift+S is the
  // browser's, and must not also be STOP.
  return ev.code === b.code && ev.shiftKey === b.shift && ev.ctrlKey === b.ctrl
      && ev.altKey === b.alt && ev.metaKey === b.meta;
}

function typingInto(el) {
  if (!el || el === document.body) return false;
  if (el.isContentEditable) return true;
  const tag = el.tagName;
  if (tag === 'TEXTAREA' || tag === 'SELECT') return true;
  return tag === 'INPUT' && TEXT_TYPES.has(String(el.type).toLowerCase());
}

function onKey(ev) {
  if (ev.repeat || ev.isComposing) return;
  if (typingInto(document.activeElement)) return;
  const b = bindings.find((x) => matches(x, ev))
         // '?' is Shift+Slash on a US layout and somewhere else on most
         // others. The list is read-only, so it is the one binding allowed
         // to follow the character as well as the key.
         || (ev.key === '?' && !ev.ctrlKey && !ev.altKey && !ev.metaKey
             ? bindings.find((x) => x.run === toggleHelp) : null);
  if (!b) return;
  let live = false;
  try {
    live = Boolean(b.when());
  } catch (err) {
    console.warn('[keys]', b.combo, err);
  }
  if (!live) return;
  ev.preventDefault();
  try {
    b.run(ev);
  } catch (err) {
    console.error(`[keys] ${b.combo} threw:`, err);
  }
  if (dialog?.open) paintHelp();
}

// ---------------------------------------------------------------------------
// help

const shown = (combo) => (combo === 'Shift+Slash' ? '?' : combo);

function toggleHelp() {
  if (!dialog) dialog = buildHelp();
  if (dialog.open) {
    dialog.close();
    return;
  }
  paintHelp();
  // Modal, so it sits in the top layer over every panel at every width and
  // Esc closes it — the browser's own, so it works with nothing of ours
  // listening, and sends nothing anywhere.
  dialog.showModal();
}

function buildHelp() {
  const d = document.createElement('dialog');
  d.className = 'keys-help';
  d.setAttribute('aria-labelledby', 'keys-help-title');
  d.innerHTML = `
    <div class="keys-help-body">
      <h2 id="keys-help-title">Keyboard</h2>
      <table class="keys-help-list"><tbody></tbody></table>
      <p class="keys-help-note">No shortcut arms, engages, moves, tracks or parks the antenna.
        A control button answers Enter or Space only once Tab has put its focus ring on it.
        Esc closes this list.</p>
      <button type="button" class="ctl-btn keys-help-close">close</button>
    </div>`;
  d.querySelector('.keys-help-close').onclick = () => d.close();
  // A click on the backdrop lands on the dialog itself, outside the body.
  d.addEventListener('click', (ev) => { if (ev.target === d) d.close(); });
  document.body.appendChild(d);
  return d;
}

function paintHelp() {
  const body = dialog?.querySelector('tbody');
  if (!body) return;
  body.innerHTML = bindings.map((b) => {
    let live = false;
    try { live = Boolean(b.when()); } catch { /* shown as not now */ }
    return `
      <tr class="${live ? 'live' : 'idle'}">
        <td><kbd>${esc(shown(b.combo))}</kbd></td>
        <td>${esc(b.label)}</td>
        <td class="keys-help-when">${live ? 'available' : 'not now'}</td>
      </tr>`;
  }).join('');
}

function esc(text) {
  return String(text ?? '').replace(/[&<>"']/g, (ch) => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
  }[ch]));
}
