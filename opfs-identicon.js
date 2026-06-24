'use strict';

// ─── OPFS STORE ───────────────────────────────────────────────────────────────
const opfsStore = {
  async _resolve(path, create = false) {
    const root = await navigator.storage.getDirectory();
    const parts = path.split('/').filter(Boolean);
    let dir = root;
    for (const part of parts.slice(0, -1)) {
      dir = await dir.getDirectoryHandle(part, { create });
    }
    return { dir, name: parts[parts.length - 1] };
  },
  async write(path, bytes) {
    const { dir, name } = await this._resolve(path, true);
    const fh = await dir.getFileHandle(name, { create: true });
    const w = await fh.createWritable();
    await w.write(bytes);
    await w.close();
  },
  async read(path) {
    const { dir, name } = await this._resolve(path);
    const fh = await dir.getFileHandle(name);
    const f = await fh.getFile();
    return new Uint8Array(await f.arrayBuffer());
  },
  async delete(path) {
    const { dir, name } = await this._resolve(path);
    await dir.removeEntry(name);
  },
  async list(dirPath) {
    const root = await navigator.storage.getDirectory();
    const parts = dirPath.split('/').filter(Boolean);
    let dir = root;
    for (const p of parts) dir = await dir.getDirectoryHandle(p, { create: true });
    const names = [];
    for await (const [n] of dir.entries()) names.push(n);
    return names;
  },
  async export(path, filename) {
    const bytes = await this.read(path);
    const a = document.createElement('a');
    a.href = URL.createObjectURL(new Blob([bytes], { type: 'application/octet-stream' }));
    a.download = filename;
    a.click();
    URL.revokeObjectURL(a.href);
  },
};

// ─── IDENTICON ───────────────────────────────────────────────────────────────
const identicon = {
  algorithmId: () => 'ssd-identicon-1.0',

  render(seed, canvas, size = 64) {
    const ctx = canvas.getContext('2d');
    canvas.width = size; canvas.height = size;
    ctx.clearRect(0, 0, size, size);

    // 15 unique cells (3 cols × 5 rows), mirrored to 5×5
    const bits = [];
    for (let i = 0; i < 15; i++) bits.push((seed[Math.floor(i / 8)] >> (7 - (i % 8))) & 1);

    const grid = [];
    for (let r = 0; r < 5; r++) {
      const row = [];
      for (let c = 0; c < 5; c++) {
        const mc = c > 2 ? 4 - c : c;
        row.push(bits[r * 3 + mc]);
      }
      grid.push(row);
    }

    const hue = Math.floor((seed[16] / 255) * 360);
    const sat = 45 + Math.floor((seed[17] / 255) * 30);
    const lig = 40 + Math.floor((seed[18] / 255) * 25);
    ctx.fillStyle = `hsl(${hue},${sat}%,${lig}%)`;

    const cell = size / 5;
    for (let r = 0; r < 5; r++) for (let c = 0; c < 5; c++) {
      if (grid[r][c]) ctx.fillRect(c * cell, r * cell, cell, cell);
    }
  },

  renderToDataURL(seed, size = 64) {
    const c = document.createElement('canvas');
    this.render(seed, c, size);
    return c.toDataURL();
  },
};
