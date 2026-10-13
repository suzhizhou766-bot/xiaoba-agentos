'use strict';
// Presentation only: rounded optical edges, pointer light, and navigation state.
(() => {
  const reduced = matchMedia('(prefers-reduced-transparency: reduce)');
  const namespace = 'http://www.w3.org/2000/svg';
  const svg = document.createElementNS(namespace, 'svg');
  svg.setAttribute('aria-hidden', 'true');
  svg.style.cssText = 'position:absolute;width:0;height:0;pointer-events:none;overflow:hidden';
  const defs = document.createElementNS(namespace, 'defs');
  svg.append(defs);
  document.body.append(svg);
  let nextId = 0;
  const records = new WeakMap();
  const clamp = (value, low, high) => Math.max(low, Math.min(high, value));
  const supportsLensing = CSS.supports('backdrop-filter', 'url("#ocean-lens")');
  function mapFor(width, height, radius) {
    const ratio = Math.min(1, 512 / width, 160 / height);
    const w = Math.max(2, Math.round(width * ratio)), h = Math.max(2, Math.round(height * ratio));
    const r = Math.min(radius * ratio, w / 2, h / 2), bevel = Math.max(3, Math.min(15 * ratio, r * .65));
    const canvas = document.createElement('canvas'); canvas.width = w; canvas.height = h;
    const context = canvas.getContext('2d');
    const pixels = context.createImageData(w, h);
    const distance = (x, y) => {
      const qx = Math.abs(x - w / 2) - (w / 2 - r), qy = Math.abs(y - h / 2) - (h / 2 - r);
      return Math.hypot(Math.max(qx, 0), Math.max(qy, 0)) + Math.min(Math.max(qx, qy), 0) - r;
    };
    for (let y = 0; y < h; y++) for (let x = 0; x < w; x++) {
      const d = distance(x + .5, y + .5), inset = Math.max(0, -d);
      const bend = Math.pow(1 - clamp(inset / bevel, 0, 1), 1.7);
      const dx = distance(x + 1, y + .5) - distance(x, y + .5);
      const dy = distance(x + .5, y + 1) - distance(x + .5, y);
      const length = Math.hypot(dx, dy) || 1, offset = (y * w + x) * 4;
      pixels.data[offset] = 128 + dx / length * bend * 100;
      pixels.data[offset + 1] = 128 + dy / length * bend * 100;
      pixels.data[offset + 2] = 128; pixels.data[offset + 3] = 255;
    }
    context.putImageData(pixels, 0, 0);
    return canvas.toDataURL();
  }
  function refresh(element) {
    const rect = element.getBoundingClientRect();
    if (rect.width < 12 || rect.height < 12 || rect.width * rect.height > 105000 || reduced.matches || !supportsLensing) return;
    const radius = parseFloat(getComputedStyle(element).borderTopLeftRadius) || 20;
    let record = records.get(element);
    if (!record) {
      const id = `ocean-optical-edge-${++nextId}`;
      const filter = document.createElementNS(namespace, 'filter');
      filter.id = id; filter.setAttribute('x', '-10%'); filter.setAttribute('y', '-30%');
      filter.setAttribute('width', '120%'); filter.setAttribute('height', '160%');
      filter.setAttribute('color-interpolation-filters', 'sRGB');
      const image = document.createElementNS(namespace, 'feImage');
      image.setAttribute('x', '0'); image.setAttribute('y', '0'); image.setAttribute('width', '100%'); image.setAttribute('height', '100%');
      image.setAttribute('preserveAspectRatio', 'none'); image.setAttribute('result', 'edge-map');
      const displacement = document.createElementNS(namespace, 'feDisplacementMap');
      displacement.setAttribute('in', 'SourceGraphic'); displacement.setAttribute('in2', 'edge-map');
      displacement.setAttribute('scale', '22'); displacement.setAttribute('xChannelSelector', 'R'); displacement.setAttribute('yChannelSelector', 'G');
      filter.append(image, displacement); defs.append(filter);
      record = {image, id, key: ''}; records.set(element, record);
    }
    const key = `${Math.round(rect.width)}:${Math.round(rect.height)}:${radius}`;
    if (record.key !== key) {
      record.key = key; record.image.setAttribute('href', mapFor(rect.width, rect.height, radius));
    }
    element.style.setProperty('--optical-backdrop', `url("#${record.id}") blur(5px) saturate(1.3)`);
    element.dataset.opticalEdge = 'ready';
  }
  const selector = '.site-header nav,.header-open,.hero-actions .pill-button,.workspace-return,.top,.tabs,header:not(.site-header),.btn.primary';
  const controls = Array.from(document.querySelectorAll(selector));
  const observer = new ResizeObserver(entries => entries.forEach(entry => refresh(entry.target)));
  controls.forEach(element => { element.classList.add('ocean-lens'); refresh(element); observer.observe(element); });
  document.addEventListener('pointermove', event => {
    const element = event.target.closest?.('.ocean-lens,.module-card,.kpi,.card');
    if (!element) return;
    const rect = element.getBoundingClientRect();
    element.style.setProperty('--light-x', `${((event.clientX - rect.left) / rect.width * 100).toFixed(1)}%`);
    element.style.setProperty('--light-y', `${((event.clientY - rect.top) / rect.height * 100).toFixed(1)}%`);
  }, {passive:true});
  reduced.addEventListener('change', () => controls.forEach(element => {
    if (reduced.matches) element.style.removeProperty('--optical-backdrop'); else refresh(element);
  }));
  const navigation = Array.from(document.querySelectorAll('.site-header nav a[href^="#"]'));
  let navigationFrame = 0;
  function updateNavigation() {
    navigationFrame = 0;
    let selected = navigation[0];
    navigation.forEach(link => { const section = document.getElementById(link.hash.slice(1)); if (section && section.getBoundingClientRect().top <= innerHeight * .35) selected = link; });
    navigation.forEach(link => { link.classList.toggle('is-active', link === selected); if (link === selected) link.setAttribute('aria-current','location'); else link.removeAttribute('aria-current'); });
  }
  if (navigation.length) { updateNavigation(); addEventListener('scroll', () => { if (!navigationFrame) navigationFrame = requestAnimationFrame(updateNavigation); }, {passive:true}); }
})();
