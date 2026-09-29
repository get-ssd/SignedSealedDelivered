// social-identity.js — platform dropdown for the social identity hint.
// The hint goes into the token as platform:handle. Tick builds the signer's
// profile link from it (ssd.tick-2 platforms/*.js profileUrl), so the handle must
// be exactly what follows the platform's profile address.
//
// mount(input) hides the existing #social-identity input and puts a platform
// select, a handle field, a prompt and a link preview in front of it. The input
// stays the single source of truth: code that sets input.value re-renders the
// widget, and user edits write back to it and fire 'input'.

const socialIdentity = {
  PLATFORMS: {
    fb:  { label: 'Facebook', placeholder: 'alice.smith',
           prompt: 'Your Facebook username — the part after facebook.com/',
           url: h => `https://www.facebook.com/${encodeURIComponent(h)}` },
    x:   { label: 'X (Twitter)', placeholder: 'alice',
           prompt: 'Your X handle, without the @',
           url: h => `https://x.com/${encodeURIComponent(h)}` },
    rd:  { label: 'Reddit', placeholder: 'alice_smith',
           prompt: 'Your Reddit username, without u/',
           url: h => `https://www.reddit.com/user/${encodeURIComponent(h)}` },
    tg:  { label: 'Telegram', placeholder: 'alice_smith',
           prompt: 'Your Telegram username, without the @',
           url: h => `https://t.me/${encodeURIComponent(h)}` },
    wa:  { label: 'WhatsApp', placeholder: '447700900123',
           prompt: 'Your number in international format, digits only',
           url: h => `https://wa.me/${encodeURIComponent(h)}` },
    url: { label: 'Other (web address)', placeholder: 'https://example.com/me',
           prompt: 'The full https:// address of a page that carries your key',
           url: h => h },
  },

  // 'fb:alice' → { platform: 'fb', handle: 'alice' }. tw: is read as x:.
  parse(identity) {
    const s = (identity || '').trim();
    if (!s) return { platform: 'fb', handle: '' };
    if (/^https?:\/\//i.test(s)) return { platform: 'url', handle: s };
    const colon = s.indexOf(':');
    if (colon === -1) return { platform: 'fb', handle: s };
    let platform = s.slice(0, colon).toLowerCase();
    if (platform === 'tw') platform = 'x';
    return { platform, handle: s.slice(colon + 1) };
  },

  // What the user typed for a platform → { platform, handle }. A pasted profile
  // address is reduced to its handle; one with no handle form becomes url:.
  fromInput(platform, raw) {
    let s = (raw || '').trim();
    if (!s) return { platform, handle: '' };
    const typed = /^(fb|x|tw|rd|tg|wa|url):(.*)$/i.exec(s);
    if (typed && !/^https?:\/\//i.test(s)) return this.fromInput(this.parse(s).platform, typed[2]);
    const looksLikeUrl = /^https?:\/\//i.test(s)
      || /^((www|m|mobile|old)\.)?(facebook\.com|x\.com|twitter\.com|reddit\.com)\//i.test(s)
      || /^(t\.me|wa\.me)\//i.test(s);
    if (looksLikeUrl) return this._fromUrl(/^https?:\/\//i.test(s) ? s : 'https://' + s);
    if (platform === 'wa') return { platform, handle: s.replace(/\D/g, '') };
    if (platform === 'rd') s = s.replace(/^\/?u\//i, '');
    if (platform === 'fb' || platform === 'x' || platform === 'tg') s = s.replace(/^@/, '');
    return { platform, handle: s };
  },

  _fromUrl(href) {
    let u;
    try { u = new URL(href); } catch { return { platform: 'url', handle: href }; }
    const host = u.hostname.toLowerCase().replace(/^(www|m|mobile|old)\./, '');
    const seg = u.pathname.split('/').filter(Boolean);
    if (host === 'facebook.com' && seg[0] && seg[0] !== 'profile.php') return { platform: 'fb', handle: seg[0] };
    if ((host === 'x.com' || host === 'twitter.com') && seg[0]) return { platform: 'x', handle: seg[0].replace(/^@/, '') };
    if (host === 'reddit.com' && (seg[0] === 'user' || seg[0] === 'u') && seg[1]) return { platform: 'rd', handle: seg[1] };
    if (host === 't.me' && seg[0]) return { platform: 'tg', handle: seg[0] };
    if (host === 'wa.me' && seg[0]) return { platform: 'wa', handle: seg[0].replace(/\D/g, '') };
    return { platform: 'url', handle: href };
  },

  // Problem with a handle, or null. Tokens are bracketed and colon-separated,
  // so whitespace and ] would break them; a url: must be a web address.
  problem(platform, handle) {
    if (!handle) return null;
    if (/[\s\]]/.test(handle)) return 'No spaces or ] allowed.';
    if (platform === 'url' && !/^https?:\/\/[^/]+/i.test(handle)) return 'Enter a full address starting with https://';
    if (platform === 'wa' && !/^\d{6,15}$/.test(handle)) return 'Digits only, with the country code.';
    return null;
  },

  toIdentity(platform, handle) {
    if (!handle || this.problem(platform, handle)) return '';
    return platform === 'url' ? `url:${handle}` : `${platform}:${handle}`;
  },

  profileUrl(identity) {
    const { platform, handle } = this.parse(identity);
    const p = this.PLATFORMS[platform];
    return p && handle ? p.url(handle) : null;
  },

  mount(input) {
    const proto = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value');
    const wrap = document.createElement('div');
    wrap.className = 'social-identity-widget';
    wrap.innerHTML = `
      <div style="display:flex;gap:8px;align-items:stretch">
        <select class="si-platform" style="flex:0 0 auto;width:auto;margin:0"></select>
        <input class="si-handle" type="text" style="flex:1 1 auto;min-width:0;margin:0"
               autocomplete="off" autocorrect="off" autocapitalize="off" spellcheck="false">
      </div>
      <div class="si-prompt" style="font-size:12px;color:var(--text-muted);margin-top:4px"></div>
      <div class="si-preview" style="font-size:12px;margin-top:2px;word-break:break-all"></div>`;
    input.hidden = true;
    input.insertAdjacentElement('afterend', wrap);
    const sel = wrap.querySelector('.si-platform');
    const box = wrap.querySelector('.si-handle');
    const promptEl = wrap.querySelector('.si-prompt');
    const preview = wrap.querySelector('.si-preview');

    const setOptions = rawExtra => {
      const extra = (rawExtra || '').replace(/[^\w-]/g, '');
      const opts = Object.entries(this.PLATFORMS).map(([k, p]) => [k, p.label]);
      if (extra && !this.PLATFORMS[extra]) opts.push([extra, `${extra}: (as entered)`]);
      sel.innerHTML = opts.map(([k, l]) => `<option value="${k}">${l}</option>`).join('');
    };

    const describe = (platform, handle) => {
      const p = this.PLATFORMS[platform];
      promptEl.textContent = p ? p.prompt : 'Kept as entered; Tick will not link to a profile for this.';
      box.placeholder = p ? p.placeholder : '';
      const bad = this.problem(platform, handle);
      if (bad) { preview.innerHTML = `<span style="color:var(--danger, #e05050)">${bad}</span>`; return; }
      const url = p && handle ? p.url(handle) : null;
      preview.innerHTML = url
        ? `Readers' Tick will link to <a href="${url.replace(/"/g, '&quot;')}" target="_blank" rel="noopener">${url.replace(/</g, '&lt;')}</a>`
        : '';
    };

    const render = () => {
      const parsed = this.parse(proto.get.call(input));
      const platform = parsed.platform.replace(/[^\w-]/g, '');
      const handle = parsed.handle;
      setOptions(platform);
      sel.value = platform;
      box.value = handle;
      describe(platform, handle);
    };

    const commit = () => {
      const { platform, handle } = this.fromInput(sel.value, box.value);
      if (platform !== sel.value) { setOptions(platform); sel.value = platform; }
      if (handle !== box.value.trim()) box.value = handle;
      proto.set.call(input, this.toIdentity(platform, handle));
      describe(platform, handle);
      input.dispatchEvent(new Event('input', { bubbles: true }));
    };

    Object.defineProperty(input, 'value', {
      configurable: true,
      get: () => proto.get.call(input),
      set: v => { proto.set.call(input, v); render(); },
    });
    sel.addEventListener('change', commit);
    box.addEventListener('change', commit);
    box.addEventListener('input', () => {
      const { platform, handle } = this.fromInput(sel.value, box.value);
      proto.set.call(input, this.toIdentity(platform, handle));
      describe(platform, handle);
      input.dispatchEvent(new Event('input', { bubbles: true }));
    });
    render();
  },
};

{
  const el = document.getElementById('social-identity');
  if (el) socialIdentity.mount(el);
}
