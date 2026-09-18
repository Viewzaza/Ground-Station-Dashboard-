/* Satellite selector over the Celestrak amateur catalogue.

   Pinned satellites (KNACKSAT-2) sort to the top and are marked, so the one
   this station exists for is never more than a glance away. */

import { api } from '../core/api.js';
import { bus } from '../core/bus.js';
import { store, set } from '../core/store.js';

const list = () => document.getElementById('sat-list');
const search = () => document.getElementById('sat-search');

let items = [];

export async function mountSatSelect(onSelect) {
  try {
    const resp = await api.satellites();
    items = resp.items || [];
    set('catalog', items);
  } catch (err) {
    console.error('[satselect]', err);
  }

  render(items);

  let debounce;
  search().addEventListener('input', () => {
    clearTimeout(debounce);
    debounce = setTimeout(() => {
      const q = search().value.trim().toLowerCase();
      render(!q ? items : items.filter(
        (it) => it.name.toLowerCase().includes(q) || String(it.norad).includes(q),
      ));
    }, 120);
  });

  list().addEventListener('click', (ev) => {
    const li = ev.target.closest('li[data-norad]');
    if (!li) return;
    onSelect(Number(li.dataset.norad));
  });

  bus.on('satellite', () => markSelected());
}

function render(rows) {
  const ul = list();
  ul.replaceChildren();
  for (const it of rows.slice(0, 300)) {
    const li = document.createElement('li');
    li.dataset.norad = it.norad;
    if (it.pinned) li.classList.add('pinned');
    li.innerHTML =
      `<span class="sat-name"></span><span class="sat-norad">${it.norad}</span>`;
    li.querySelector('.sat-name').textContent = it.name;
    ul.appendChild(li);
  }
  if (!rows.length) {
    const li = document.createElement('li');
    li.className = 'muted';
    li.textContent = 'no match';
    ul.appendChild(li);
  }
  markSelected();
}

function markSelected() {
  const current = store.satellite?.norad;
  for (const li of list().querySelectorAll('li[data-norad]')) {
    li.classList.toggle('selected', Number(li.dataset.norad) === current);
  }
}
