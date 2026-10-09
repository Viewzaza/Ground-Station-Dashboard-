/* A page just big enough to mount the rotator control panel in Node.

   The panel is vanilla ES modules with no build step, so Node can import it as
   it stands; what Node lacks is the page. This is that page, cut down to what
   control.js and keys.js touch: elements by id, classes, a few properties,
   events that capture and bubble, focus — and `user`, which does what the
   browser does natively when someone clicks or presses a key.

   That last part is the point. The hazards these tests exist for live in the
   browser's own behaviour — a clicked button keeps focus, Enter on a focused
   button is a click on every auto-repeat, Space is a click on release — so
   `user` models exactly that, the way Chromium does it, and the tests then
   assert what the panel lets through. It is deliberately not a DOM: no
   layout, no CSS, no text nodes. A test that needs any of those belongs in a
   real browser. */

const VOID = new Set(['input', 'br', 'img', 'hr', 'meta', 'link']);
const FOCUSABLE = new Set(['BUTTON', 'INPUT', 'SELECT', 'TEXTAREA']);

export class FakeEvent {
  constructor(type, init = {}) {
    Object.assign(this, { bubbles: true, repeat: false, detail: 0, isComposing: false,
                          shiftKey: false, ctrlKey: false, altKey: false, metaKey: false }, init);
    this.type = type;
    this.defaultPrevented = false;
    this.target = null;
    this.currentTarget = null;
  }

  preventDefault() { this.defaultPrevented = true; }

  stopPropagation() { this._stopped = true; }
}

class Target {
  constructor() { this._listeners = []; }

  addEventListener(type, fn, opts) {
    const capture = opts === true || Boolean(opts?.capture);
    this._listeners.push({ type, fn, capture });
  }

  removeEventListener(type, fn, opts) {
    const capture = opts === true || Boolean(opts?.capture);
    this._listeners = this._listeners.filter((l) => !(l.type === type && l.fn === fn && l.capture === capture));
  }

  _fire(ev, capture) {
    for (const l of [...this._listeners]) {
      if (l.type !== ev.type || l.capture !== capture || ev._stopped) continue;
      ev.currentTarget = this;
      l.fn.call(this, ev);
    }
    // An on-event property is a bubble-phase listener of its own.
    const prop = this[`on${ev.type}`];
    if (!capture && typeof prop === 'function' && !ev._stopped) {
      ev.currentTarget = this;
      prop.call(this, ev);
    }
  }
}

class ClassList {
  constructor(el) { this.el = el; }

  _get() { return new Set(this.el.className.split(/\s+/).filter(Boolean)); }

  _put(set) { this.el.className = [...set].join(' '); }

  contains(name) { return this._get().has(name); }

  add(...names) { const s = this._get(); names.forEach((n) => s.add(n)); this._put(s); }

  remove(...names) { const s = this._get(); names.forEach((n) => s.delete(n)); this._put(s); }

  toggle(name, force) {
    const s = this._get();
    const on = force === undefined ? !s.has(name) : Boolean(force);
    if (on) s.add(name); else s.delete(name);
    this._put(s);
    return on;
  }
}

export class Element extends Target {
  constructor(tag, page) {
    super();
    this.tagName = tag.toUpperCase();
    this.page = page;
    this.id = '';
    this.className = '';
    this.classList = new ClassList(this);
    this.dataset = {};
    this.attrs = {};
    this.children = [];
    this.parentNode = null;
    this.hidden = false;
    this.disabled = false;
    this.title = '';
    this.type = tag === 'input' ? 'text' : tag === 'button' ? 'submit' : '';
    this.value = '';
    this.textContent = '';
    this.isContentEditable = false;
    this.rect = { left: 0, top: 0, width: 0, height: 0 };
    this._html = '';
  }

  get innerHTML() { return this._html; }

  /** Builds the children a template describes: tags, ids, classes and the
      attributes control.js reads. Text is dropped — the panel sets every
      label it reads back through textContent anyway. */
  set innerHTML(html) {
    this._html = String(html);
    for (const child of this.children) child.parentNode = null;
    this.children = [];
    const stack = [this];
    const tags = /<\/?([a-zA-Z][\w-]*)([^>]*)>/g;
    let m;
    while ((m = tags.exec(this._html))) {
      const [whole, tag, rest] = m;
      if (whole.startsWith('</')) {
        const at = stack.map((e) => e.tagName).lastIndexOf(tag.toUpperCase());
        if (at > 0) stack.length = at;
        continue;
      }
      const el = new Element(tag.toLowerCase(), this.page);
      for (const [, name, value] of rest.matchAll(/([\w-]+)(?:="([^"]*)")?/g)) {
        el.setAttribute(name, value ?? '');
      }
      stack[stack.length - 1].appendChild(el);
      if (!VOID.has(tag.toLowerCase()) && !rest.trim().endsWith('/')) stack.push(el);
    }
    // Focus does not survive its element leaving the page.
    if (this.page.activeElement !== this.page.body && !this.page.attached(this.page.activeElement)) {
      this.page.activeElement = this.page.body;
    }
  }

  setAttribute(name, value) {
    this.attrs[name] = value;
    if (name === 'id') this.id = value;
    else if (name === 'class') this.className = value;
    else if (name === 'hidden') this.hidden = true;
    else if (name === 'disabled') this.disabled = true;
    else if (name === 'title') this.title = value;
    else if (name === 'type') this.type = value;
    else if (name === 'value') this.value = value;
    else if (name.startsWith('data-')) {
      this.dataset[name.slice(5).replace(/-([a-z])/g, (_, c) => c.toUpperCase())] = value;
    }
  }

  appendChild(child) {
    child.parentNode = this;
    this.children.push(child);
    return child;
  }

  *walk() {
    for (const child of this.children) {
      yield child;
      yield* child.walk();
    }
  }

  /** Only the selector forms the panel uses: `.cls`, `#id`, `tag`, and a
      class with one `[data-x]` attribute test. */
  matches(selector) {
    const m = /^([a-z]*)(?:#([\w-]+))?((?:\.[\w-]+)*)(?:\[([\w-]+)\])?$/i.exec(selector.trim());
    if (!m) throw new Error(`fake page cannot match ${selector}`);
    const [, tag, id, classes, attr] = m;
    if (tag && this.tagName !== tag.toUpperCase()) return false;
    if (id && this.id !== id) return false;
    for (const c of classes.split('.').filter(Boolean)) if (!this.classList.contains(c)) return false;
    if (attr && !(attr in this.attrs)) return false;
    return true;
  }

  closest(selector) {
    for (let el = this; el instanceof Element; el = el.parentNode) {
      if (el.matches(selector)) return el;
    }
    return null;
  }

  querySelectorAll(selector) { return [...this.walk()].filter((el) => el.matches(selector)); }

  querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }

  getBoundingClientRect() { return { ...this.rect, right: this.rect.left + this.rect.width,
                                      bottom: this.rect.top + this.rect.height }; }

  /** Whether the element is on the page and nothing hides it. */
  shown() {
    for (let el = this; el; el = el.parentNode) {
      if (el.hidden) return false;
      if (el === this.page.body) return true;
    }
    return false;
  }

  focusable() { return FOCUSABLE.has(this.tagName) && !this.disabled && this.shown(); }

  focus() { if (this.focusable()) this.page.moveFocus(this); }

  /** A modal <dialog>: focus moves to its first control, and goes back to
      whatever had it when the dialog closes. */
  showModal() {
    this.open = true;
    this._restore = this.page.activeElement;
    [...this.walk()].find((el) => el.focusable())?.focus();
  }

  close() {
    if (!this.open) return;
    this.open = false;
    this._restore?.focus();
  }

  blur() { if (this.page.activeElement === this) this.page.moveFocus(this.page.body); }
}

export class Page {
  constructor() {
    this.window = new Target();
    this.document = new Target();
    this.body = new Element('body', this);
    this.body.parentNode = this.document;
    this.documentElement = new Element('html', this);
    this.activeElement = this.body;
    this.requests = [];
    const page = this;
    Object.assign(this.document, {
      body: this.body,
      documentElement: this.documentElement,
      fullscreenEnabled: false,
      fullscreenElement: null,
      get activeElement() { return page.activeElement; },
      getElementById: (id) => [...this.body.walk()].find((el) => el.id === id) || null,
      createElement: (tag) => new Element(tag, this),
      querySelector: (sel) => this.body.querySelector(sel),
      querySelectorAll: (sel) => this.body.querySelectorAll(sel),
    });
  }

  /** Installs this page as the globals the modules under test reach for. */
  install() {
    globalThis.window = this.window;
    globalThis.document = this.document;
    globalThis.location = { origin: 'http://fake.test' };
    this.window.innerWidth = 1920;
    this.window.devicePixelRatio = 1;
    return this;
  }

  attached(el) {
    for (let e = el; e; e = e.parentNode) if (e === this.body) return true;
    return false;
  }

  add(tag, id, rect) {
    const el = new Element(tag, this);
    el.id = id;
    if (rect) el.rect = rect;
    return this.body.appendChild(el);
  }

  /** Capture from the window down, then bubble back up — the order a browser
      dispatches in, so a guard registered on a container sees a key before
      the default action it may prevent. */
  dispatch(target, ev) {
    ev.target = target;
    const path = [];
    for (let el = target.parentNode; el; el = el.parentNode) path.unshift(el);
    if (path[0] !== this.window) path.unshift(this.window);
    for (const node of path) node._fire(ev, true);
    target._fire(ev, true);
    target._fire(ev, false);
    if (ev.bubbles) for (const node of path.reverse()) node._fire(ev, false);
    return ev;
  }

  moveFocus(to) {
    const from = this.activeElement;
    if (from === to) return;
    this.activeElement = to;
    if (from && from !== this.body) this.dispatch(from, new FakeEvent('focusout', { relatedTarget: to }));
    if (to !== this.body) this.dispatch(to, new FakeEvent('focusin', { relatedTarget: from }));
  }

  /** Tab order: document order, focusable elements only. */
  tabStops() { return [...this.body.walk()].filter((el) => el.focusable()); }
}

/** The browser's native side of a click or a key, as Chromium does it. */
export function user(page) {
  const fire = (el, type, init) => page.dispatch(el, new FakeEvent(type, init));

  function activate(el) {
    if (el.tagName === 'BUTTON' && !el.disabled) fire(el, 'click', { detail: 0 });
  }

  return {
    /** A mouse click. The press focuses the button; the click follows the
        release. Focus is not dropped afterwards — that is the browser's
        behaviour, and the thing the panel has to live with. */
    click(el, at = {}) {
      fire(el, 'pointerdown', at);
      const down = fire(el, 'mousedown', at);
      if (!down.defaultPrevented && FOCUSABLE.has(el.tagName)) el.focus();
      fire(el, 'pointerup', at);
      fire(el, 'mouseup', at);
      if (!el.disabled) fire(el, 'click', { detail: 1, ...at });
    },

    /** Pressed on `el`, dragged off and released elsewhere: focus, no click. */
    pressAndDragAway(el) {
      fire(el, 'pointerdown');
      const down = fire(el, 'mousedown');
      if (!down.defaultPrevented && FOCUSABLE.has(el.tagName)) el.focus();
      fire(page.body, 'pointerup');
      fire(page.body, 'mouseup');
    },

    /** A key on whatever has focus, held for `repeats` auto-repeats.

        Enter on a focused button is a click on keydown, on every repeat.
        Space is a click on keyup, if its first keydown went through. Tab
        moves focus; Escape closes an open modal dialog. Each happens only if
        no listener prevented it. */
    key(key, { repeats = 0, shift = false } = {}) {
      const code = key === ' ' ? 'Space' : key.length === 1 ? `Key${key.toUpperCase()}` : key;
      const mods = { shiftKey: shift };
      let spaceDown = false;
      for (let i = 0; i <= repeats; i += 1) {
        const target = page.activeElement;
        const ev = fire(target, 'keydown', { key, code, repeat: i > 0, ...mods });
        if (ev.defaultPrevented) continue;
        if (key === 'Enter') activate(target);
        if (key === ' ' && i === 0 && target.tagName === 'BUTTON') spaceDown = true;
        if (key === 'Tab' && i === 0) {
          const stops = page.tabStops();
          const at = stops.indexOf(target);
          const next = shift ? stops[at <= 0 ? stops.length - 1 : at - 1] : stops[(at + 1) % stops.length];
          if (next) next.focus();
        }
        if (key === 'Escape' && i === 0) {
          [...page.body.walk()].find((el) => el.tagName === 'DIALOG' && el.open)?.close();
        }
      }
      const target = page.activeElement;
      const up = fire(target, 'keyup', { key, code, ...mods });
      if (key === ' ' && spaceDown && !up.defaultPrevented) activate(target);
    },
  };
}
