// Look and colour scheme of this browser (Settings › Appearance).
// Loaded synchronously in <head>, so the attributes are set before the first paint.
// Pages with charts listen for the 'themechange' event to recolour them.
(function () {
  const LOOKS = ['studio', 'mono', 'contrast'];
  const THEMES = ['system', 'light', 'medium', 'dark', 'solarized-light', 'solarized-dark'];
  const KEY_LOOK = 'o2i.look', KEY_THEME = 'o2i.theme';
  const dark = window.matchMedia ? window.matchMedia('(prefers-color-scheme: dark)') : null;

  function read(key, allowed, fallback) {
    try {
      const v = localStorage.getItem(key);
      return allowed.includes(v) ? v : fallback;
    } catch { return fallback; }
  }

  function prefs() {
    return {look: read(KEY_LOOK, LOOKS, 'studio'), theme: read(KEY_THEME, THEMES, 'system')};
  }

  function apply() {
    const p = prefs();
    const effective = p.theme === 'system' ? (dark && dark.matches ? 'dark' : 'light') : p.theme;
    const html = document.documentElement;
    const changed = html.dataset.look !== p.look || html.dataset.theme !== effective;
    html.dataset.look = p.look;
    html.dataset.theme = effective;
    if (changed) window.dispatchEvent(new CustomEvent('themechange', {detail: {...p, effective}}));
  }

  function set(look, theme) {
    try {
      if (look) localStorage.setItem(KEY_LOOK, look);
      if (theme) localStorage.setItem(KEY_THEME, theme);
    } catch {}
    apply();
  }

  window.o2iTheme = {prefs, set, apply, LOOKS, THEMES};
  apply();
  // follow the OS setting while on "system", and changes made in other tabs
  if (dark && dark.addEventListener) dark.addEventListener('change', apply);
  window.addEventListener('storage', e => { if (e.key === KEY_LOOK || e.key === KEY_THEME) apply(); });
})();
