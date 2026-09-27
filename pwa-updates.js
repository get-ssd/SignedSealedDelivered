'use strict';
// Shared by both shells. A new worker is not enough: open pages must adopt it.
const pwaUpdates = {
  start(version) {
    if (!('serviceWorker' in navigator) || this.started) return;
    this.started = true;
    let edited = false;
    let reloading = false;
    document.addEventListener('input', () => { edited = true; }, {capture: true});
    document.addEventListener('change', () => { edited = true; }, {capture: true});
    const checkVersion = () => {
      const worker = navigator.serviceWorker.controller;
      if (!worker || reloading) return;
      const channel = new MessageChannel();
      const timer = setTimeout(() => channel.port1.close(), 2000);
      channel.port1.onmessage = ({data}) => {
        clearTimeout(timer);
        channel.port1.close();
        if (!data?.version || data.version === version || reloading) return;
        // Never discard a draft, an active PIN prompt, or a received document.
        const busy = edited || document.querySelector('input[type="password"]') ||
          (typeof app !== 'undefined' && (app._sigCtx || app._signing || app._receiving || app._deliverBytes || app._viewArtifactId));
        if (!busy) { reloading = true; location.reload(); return; }
        if (document.getElementById('ssd-update-notice')) return;
        const notice = document.createElement('div');
        notice.id = 'ssd-update-notice';
        notice.setAttribute('role', 'status');
        notice.style.cssText = 'position:fixed;top:0;left:0;right:0;z-index:2000;background:#302b12;color:white;padding:12px';
        notice.textContent = `SSD ${data.version} is ready. Finish your current work, then `;
        const button = document.createElement('button');
        button.textContent = 'Refresh';
        button.onclick = () => { if (confirm('Refresh SSD? Unsaved input in this tab will be discarded.')) location.reload(); };
        notice.append(button);
        document.body.append(notice);
      };
      worker.postMessage({type: 'SSD_GET_VERSION'}, [channel.port2]);
    };
    navigator.serviceWorker.addEventListener('controllerchange', checkVersion);
    this.ready = navigator.serviceWorker.register('./service-worker.js', {updateViaCache: 'none'})
      .then(async registration => {
        const update = async () => {
          if (!navigator.onLine) return;
          try { await registration.update(); checkVersion(); } catch (error) { console.warn('SSD update check failed', error); }
        };
        window.addEventListener('online', update);
        document.addEventListener('visibilitychange', () => { if (!document.hidden) update(); });
        await update();
        return registration;
      }).catch(console.warn);
  },
};
