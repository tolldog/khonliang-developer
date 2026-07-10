#!/usr/bin/env python3
"""Viewer prototype for fr_store_d22556bb — layout evaluation (v2).

Standalone stdlib-only HTTP server that renders a tabbed view of artifacts.
Client-side rendering via CDN so there are no Python or system deps to install.

v2 additions over v1:
- File list sidebar on the left (keyboard navigable)
- Drag-to-resize split divider between the two panes
- Graphviz zoom / pan / reset via svg-pan-zoom
- JSON collapsible tree (roll-your-own, no dep)
- Markdown TOC sidebar (right rail, sticky)
- Dark / light theme toggle (persists in localStorage)
- Keyboard shortcuts: \\\\ split, w close current tab, 1-9 jump to tab, d theme, r reload
- Footer status bar with path / kind / byte size
- Reload button to re-fetch artifact from disk (iteration friendly)

Run:
    python3 viewer_prototype.py <file1> <file2> ...
    python3 viewer_prototype.py --port 8765 inventory.md current.dot proposed.dot

Binds 0.0.0.0 by default; prints the FQDN URL so it's reachable from the LAN.
"""

from __future__ import annotations

import argparse
import json
import mimetypes
import os
import socket
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

TABS: list[dict] = []


def kind_for(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix in {".md", ".markdown"}:
        return "markdown"
    if suffix in {".dot", ".gv"}:
        return "graphviz"
    if suffix == ".json":
        return "json"
    return "code"


INDEX_HTML = r"""<!doctype html>
<html lang="en" data-theme="light">
<head>
<meta charset="utf-8">
<title>khonliang viewer — layout prototype</title>
<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/github-markdown-css@5/github-markdown-light.min.css" id="md-light">
<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/github-markdown-css@5/github-markdown-dark.min.css" id="md-dark" disabled>
<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/highlight.js@11/styles/github.min.css" id="hl-light">
<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/highlight.js@11/styles/github-dark.min.css" id="hl-dark" disabled>
<style>
  :root {
    --bg: #fafafa;
    --fg: #1c1c1c;
    --border: #d0d0d0;
    --panel: #ffffff;
    --sidebar: #f0f0f0;
    --tab-bg: #eaeaea;
    --tab-active-bg: #ffffff;
    --accent: #2e7d32;
    --muted: #6a737d;
    --split-a: #1976d2;
    --split-b: #e65100;
  }
  [data-theme="dark"] {
    --bg: #0d1117;
    --fg: #c9d1d9;
    --border: #30363d;
    --panel: #161b22;
    --sidebar: #0b0f14;
    --tab-bg: #161b22;
    --tab-active-bg: #21262d;
    --accent: #2ea043;
    --muted: #8b949e;
  }
  * { box-sizing: border-box; }
  html, body { margin: 0; padding: 0; height: 100%; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; background: var(--bg); color: var(--fg); font-size: 13px; }
  body { display: grid; grid-template-rows: auto auto 1fr auto; height: 100vh; }
  header { display: flex; align-items: center; padding: 6px 10px; border-bottom: 1px solid var(--border); background: var(--tab-bg); gap: 8px; }
  header .title { font-weight: 600; font-size: 13px; margin-right: 8px; }
  header button, header .btn { font-size: 12px; padding: 3px 8px; border: 1px solid var(--border); background: var(--panel); color: var(--fg); border-radius: 3px; cursor: pointer; }
  header button:hover { border-color: var(--accent); }
  header .kbd { font-family: "SF Mono", Menlo, Consolas, monospace; font-size: 11px; background: var(--sidebar); padding: 1px 5px; border-radius: 3px; border: 1px solid var(--border); color: var(--muted); }
  header .spacer { flex: 1; }

  .tabbar { display: flex; border-bottom: 1px solid var(--border); background: var(--tab-bg); overflow-x: auto; }
  .tab { padding: 6px 10px; cursor: pointer; border-right: 1px solid var(--border); display: flex; align-items: center; gap: 6px; font-size: 12px; white-space: nowrap; user-select: none; color: var(--fg); }
  .tab.active { background: var(--tab-active-bg); font-weight: 500; }
  .tab.active.split-a { box-shadow: inset 0 -3px 0 var(--split-a); }
  .tab.active.split-b { box-shadow: inset 0 -3px 0 var(--split-b); }
  .tab .close { opacity: 0.45; font-size: 14px; line-height: 1; padding: 0 2px; }
  .tab .close:hover { opacity: 1; color: #c62828; }
  .tab .kind { font-size: 10px; color: var(--muted); text-transform: uppercase; letter-spacing: 0.5px; }

  .main { display: grid; grid-template-columns: 220px 1fr; overflow: hidden; }
  .sidebar { background: var(--sidebar); border-right: 1px solid var(--border); overflow-y: auto; padding: 6px 0; }
  .sidebar .group { padding: 4px 10px; font-size: 11px; text-transform: uppercase; color: var(--muted); letter-spacing: 0.5px; }
  .sidebar .file { padding: 4px 10px; cursor: pointer; display: flex; gap: 6px; font-size: 12px; user-select: none; align-items: center; }
  .sidebar .file:hover { background: var(--tab-bg); }
  .sidebar .file.active { background: var(--tab-active-bg); border-left: 3px solid var(--split-a); padding-left: 7px; }
  .sidebar .file .icon { font-size: 11px; color: var(--muted); width: 16px; }
  .sidebar .hint { padding: 8px 10px; font-size: 11px; color: var(--muted); line-height: 1.5; border-top: 1px solid var(--border); margin-top: 8px; }

  .panes { position: relative; display: grid; grid-template-columns: 1fr; overflow: hidden; }
  .panes.split { grid-template-columns: var(--split-ratio, 1fr) 6px var(--split-ratio-b, 1fr); }
  .pane { overflow: auto; background: var(--panel); }
  .splitter { background: var(--border); cursor: col-resize; transition: background 0.1s; }
  .splitter:hover, .splitter.dragging { background: var(--accent); }

  .content { padding: 16px 24px; position: relative; }
  .markdown-body { font-size: 13px; max-width: none; background: transparent !important; color: var(--fg) !important; }
  .markdown-body h1, .markdown-body h2, .markdown-body h3 { scroll-margin-top: 8px; }
  pre.code { margin: 0; padding: 12px; font-size: 12px; line-height: 1.45; overflow: auto; }
  pre.code code { background: transparent !important; }

  .graphviz-wrap { height: calc(100vh - 150px); display: flex; flex-direction: column; }
  .graphviz-controls { padding: 6px 12px; background: var(--sidebar); border-bottom: 1px solid var(--border); display: flex; gap: 6px; align-items: center; font-size: 11px; color: var(--muted); }
  .graphviz-controls button { font-size: 11px; padding: 2px 8px; border: 1px solid var(--border); background: var(--panel); color: var(--fg); border-radius: 3px; cursor: pointer; }
  .graphviz-svg { flex: 1; overflow: hidden; background: var(--panel); }
  .graphviz-svg svg { width: 100%; height: 100%; }

  .json-tree { font-family: "SF Mono", Menlo, Consolas, monospace; font-size: 12px; padding: 12px; line-height: 1.55; }
  .json-tree .toggle { cursor: pointer; display: inline-block; width: 10px; text-align: center; user-select: none; color: var(--muted); }
  .json-tree .key { color: #0b7285; }
  [data-theme="dark"] .json-tree .key { color: #79c0ff; }
  .json-tree .str { color: #9a3412; }
  [data-theme="dark"] .json-tree .str { color: #f0883e; }
  .json-tree .num { color: #1d4ed8; }
  [data-theme="dark"] .json-tree .num { color: #6cb6ff; }
  .json-tree .bool { color: #0f766e; font-weight: 600; }
  .json-tree .null { color: #991b1b; }
  .json-tree ul { list-style: none; margin: 0; padding-left: 16px; border-left: 1px dashed var(--border); }
  .json-tree ul.collapsed { display: none; }
  .json-tree .meta { color: var(--muted); font-size: 11px; margin-left: 4px; }

  .md-wrap { display: grid; grid-template-columns: 1fr 200px; gap: 16px; }
  .md-main { min-width: 0; }
  .md-toc { position: sticky; top: 8px; align-self: start; font-size: 11px; max-height: calc(100vh - 160px); overflow-y: auto; padding: 8px; border-left: 1px solid var(--border); }
  .md-toc .toc-title { font-weight: 600; color: var(--muted); text-transform: uppercase; letter-spacing: 0.5px; margin-bottom: 6px; font-size: 10px; }
  .md-toc a { display: block; color: var(--fg); text-decoration: none; padding: 2px 0 2px 0; border-left: 2px solid transparent; padding-left: 6px; line-height: 1.4; }
  .md-toc a:hover { border-left-color: var(--accent); }
  .md-toc a.h3 { padding-left: 18px; color: var(--muted); font-size: 10px; }

  .empty { padding: 32px; text-align: center; color: var(--muted); }
  .muted { color: var(--muted); }

  footer { display: flex; padding: 4px 10px; border-top: 1px solid var(--border); background: var(--sidebar); font-size: 11px; color: var(--muted); font-family: "SF Mono", Menlo, Consolas, monospace; gap: 16px; }
  footer .spacer { flex: 1; }
</style>
</head>
<body>
<header>
  <span class="title">khonliang viewer <span class="muted">prototype</span></span>
  <button id="toggle-split" title="split view (\\)">Split</button>
  <button id="toggle-theme" title="toggle dark (d)">Theme</button>
  <button id="reload" title="reload tab (r)">Reload</button>
  <span class="spacer"></span>
  <span class="muted" id="hint">shortcuts: <span class="kbd">\\</span> split · <span class="kbd">w</span> close · <span class="kbd">1-9</span> jump · <span class="kbd">d</span> theme · <span class="kbd">r</span> reload</span>
</header>
<div class="tabbar" id="tabbar"></div>
<div class="main">
  <aside class="sidebar" id="sidebar"></aside>
  <div class="panes" id="panes">
    <div class="pane" id="pane-a"><div class="empty">select a file</div></div>
  </div>
</div>
<footer>
  <span id="footer-left">—</span>
  <span class="spacer"></span>
  <span id="footer-right">— —</span>
</footer>
<script src="https://cdn.jsdelivr.net/npm/marked@12/marked.min.js"></script>
<script src="https://cdnjs.cloudflare.com/ajax/libs/highlight.js/11.10.0/highlight.min.js"></script>
<script src="https://cdn.jsdelivr.net/npm/@hpcc-js/wasm@2.18/dist/graphviz.umd.js"></script>
<script src="https://cdn.jsdelivr.net/npm/svg-pan-zoom@3.6.1/dist/svg-pan-zoom.min.js"></script>
<script>
const TABS = __TABS_JSON__;
const openTabs = new Set();
let activeA = null;
let activeB = null;
let splitOn = false;
let graphvizInstance = null;
const contentCache = {};

// ---------- helpers ----------
function escapeHtml(s) { return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;'); }
function prettySize(n) {
  if (n < 1024) return n + ' B';
  if (n < 1024 * 1024) return (n / 1024).toFixed(1) + ' KB';
  return (n / 1024 / 1024).toFixed(2) + ' MB';
}

// ---------- theme ----------
function setTheme(t) {
  document.documentElement.setAttribute('data-theme', t);
  document.getElementById('md-light').disabled = (t === 'dark');
  document.getElementById('md-dark').disabled = (t !== 'dark');
  document.getElementById('hl-light').disabled = (t === 'dark');
  document.getElementById('hl-dark').disabled = (t !== 'dark');
  localStorage.setItem('viewer-theme', t);
}
setTheme(localStorage.getItem('viewer-theme') || 'light');

// ---------- sidebar ----------
function renderSidebar() {
  const side = document.getElementById('sidebar');
  side.innerHTML = '';
  const header = document.createElement('div');
  header.className = 'group';
  header.textContent = 'Files (' + TABS.length + ')';
  side.appendChild(header);
  TABS.forEach((tab, idx) => {
    const el = document.createElement('div');
    el.className = 'file';
    if (idx === activeA || idx === activeB) el.classList.add('active');
    el.innerHTML = '<span class="icon">' + iconFor(tab.kind) + '</span><span>' + escapeHtml(tab.label) + '</span>';
    el.title = tab.path;
    el.addEventListener('click', () => openTab(idx));
    el.addEventListener('contextmenu', (e) => { e.preventDefault(); openTabSplit(idx); });
    side.appendChild(el);
  });
  const hint = document.createElement('div');
  hint.className = 'hint';
  hint.innerHTML = 'click to open<br>right-click to open in split pane';
  side.appendChild(hint);
}
function iconFor(kind) {
  return { markdown: 'MD', graphviz: 'DOT', json: 'JSON', code: 'TXT' }[kind] || '?';
}

// ---------- tabs ----------
function openTab(idx) {
  openTabs.add(idx);
  activeA = idx;
  renderAll();
}
function openTabSplit(idx) {
  openTabs.add(idx);
  if (!splitOn) { splitOn = true; applySplit(); }
  activeB = idx;
  renderAll();
}
function closeTab(idx) {
  openTabs.delete(idx);
  if (activeA === idx) activeA = firstOpen();
  if (activeB === idx) activeB = null;
  renderAll();
}
function firstOpen() {
  for (const i of openTabs) return i;
  return null;
}
function renderTabbar() {
  const bar = document.getElementById('tabbar');
  bar.innerHTML = '';
  const list = [...openTabs].sort((a, b) => a - b);
  if (list.length === 0) {
    bar.innerHTML = '<div class="muted" style="padding: 6px 10px; font-size: 11px;">no tabs open — click a file in the sidebar</div>';
    return;
  }
  list.forEach((idx, pos) => {
    const tab = TABS[idx];
    const el = document.createElement('div');
    el.className = 'tab';
    if (idx === activeA) el.classList.add('active', 'split-a');
    if (idx === activeB) el.classList.add('active', 'split-b');
    el.innerHTML = '<span class="kind">' + iconFor(tab.kind) + '</span><span>' + escapeHtml(tab.label) + '</span><span class="close" title="close">×</span>';
    el.addEventListener('click', (e) => {
      if (e.target.classList.contains('close')) { closeTab(idx); return; }
      activeA = idx;
      renderAll();
    });
    el.addEventListener('contextmenu', (e) => {
      e.preventDefault();
      if (!splitOn) { splitOn = true; applySplit(); }
      activeB = idx;
      renderAll();
    });
    bar.appendChild(el);
  });
}

// ---------- panes ----------
function applySplit() {
  const panes = document.getElementById('panes');
  panes.classList.toggle('split', splitOn);
  if (splitOn) {
    if (!document.getElementById('pane-b')) {
      const splitter = document.createElement('div');
      splitter.className = 'splitter';
      splitter.id = 'splitter';
      const paneB = document.createElement('div');
      paneB.className = 'pane';
      paneB.id = 'pane-b';
      paneB.innerHTML = '<div class="empty">select a tab for this pane</div>';
      panes.appendChild(splitter);
      panes.appendChild(paneB);
      enableSplitter(splitter);
    }
    if (activeB === null) activeB = firstOpen();
  } else {
    document.getElementById('pane-b')?.remove();
    document.getElementById('splitter')?.remove();
    activeB = null;
  }
}

function enableSplitter(el) {
  let dragging = false;
  el.addEventListener('mousedown', (e) => {
    dragging = true;
    el.classList.add('dragging');
    e.preventDefault();
  });
  document.addEventListener('mouseup', () => { dragging = false; el.classList.remove('dragging'); });
  document.addEventListener('mousemove', (e) => {
    if (!dragging) return;
    const panes = document.getElementById('panes');
    const rect = panes.getBoundingClientRect();
    const ratio = Math.max(0.1, Math.min(0.9, (e.clientX - rect.left) / rect.width));
    panes.style.setProperty('--split-ratio', (ratio * 100) + '%');
    panes.style.setProperty('--split-ratio-b', ((1 - ratio) * 100) + '%');
  });
}

// ---------- rendering ----------
async function fetchContent(idx, force) {
  if (!force && contentCache[idx] !== undefined) return contentCache[idx];
  const res = await fetch('/content/' + idx);
  const text = await res.text();
  contentCache[idx] = text;
  return text;
}

async function renderPane(idx, paneId) {
  const pane = document.getElementById(paneId);
  if (!pane) return;
  if (idx === null) { pane.innerHTML = '<div class="empty">select a tab</div>'; return; }
  const tab = TABS[idx];
  pane.innerHTML = '<div class="empty muted">loading…</div>';
  const text = await fetchContent(idx);
  const wrap = document.createElement('div');
  wrap.className = 'content';
  if (tab.kind === 'markdown') renderMarkdown(wrap, text);
  else if (tab.kind === 'graphviz') await renderGraphviz(wrap, text, paneId);
  else if (tab.kind === 'json') renderJson(wrap, text);
  else renderCode(wrap, text, tab.label);
  pane.innerHTML = '';
  pane.appendChild(wrap);
}

function renderMarkdown(wrap, text) {
  wrap.classList.add('md-wrap');
  const main = document.createElement('div');
  main.className = 'markdown-body md-main';
  main.innerHTML = marked.parse(text);
  if (typeof hljs !== 'undefined') main.querySelectorAll('pre code').forEach(el => hljs.highlightElement(el));
  const toc = document.createElement('nav');
  toc.className = 'md-toc';
  const headings = main.querySelectorAll('h2, h3');
  if (headings.length) {
    toc.innerHTML = '<div class="toc-title">Contents</div>';
    headings.forEach((h, i) => {
      if (!h.id) h.id = 'h-' + i + '-' + (h.textContent || '').toLowerCase().replace(/[^a-z0-9]+/g, '-').slice(0, 60);
      const a = document.createElement('a');
      a.href = '#' + h.id;
      a.textContent = h.textContent || '';
      if (h.tagName === 'H3') a.className = 'h3';
      toc.appendChild(a);
    });
  }
  wrap.appendChild(main);
  wrap.appendChild(toc);
}

async function renderGraphviz(wrap, text, paneId) {
  wrap.classList.remove('md-wrap');
  const shell = document.createElement('div');
  shell.className = 'graphviz-wrap';
  const ctrls = document.createElement('div');
  ctrls.className = 'graphviz-controls';
  ctrls.innerHTML = '<button data-act="in">+</button><button data-act="out">−</button><button data-act="reset">Reset</button><button data-act="fit">Fit</button><span class="muted">scroll to zoom · drag to pan</span>';
  const svgHost = document.createElement('div');
  svgHost.className = 'graphviz-svg';
  svgHost.innerHTML = '<div class="empty muted">rendering…</div>';
  shell.appendChild(ctrls);
  shell.appendChild(svgHost);
  wrap.appendChild(shell);
  if (!graphvizInstance) {
    try { graphvizInstance = await window["@hpcc-js/wasm"].Graphviz.load(); }
    catch (err) { svgHost.innerHTML = '<pre class="code">graphviz WASM load failed: ' + escapeHtml(err.message || err) + '</pre>'; return; }
  }
  let svgMarkup;
  try { svgMarkup = graphvizInstance.dot(text); }
  catch (err) { svgHost.innerHTML = '<pre class="code">render failed: ' + escapeHtml(err.message || err) + '\n\n' + escapeHtml(text) + '</pre>'; return; }
  svgHost.innerHTML = svgMarkup;
  const svgEl = svgHost.querySelector('svg');
  if (!svgEl) return;
  // Ensure viewBox is present; synthesise from intrinsic width/height if not.
  if (!svgEl.getAttribute('viewBox')) {
    const wAttr = svgEl.getAttribute('width') || '';
    const hAttr = svgEl.getAttribute('height') || '';
    const w = parseFloat(wAttr) || svgEl.getBBox?.().width || 0;
    const h = parseFloat(hAttr) || svgEl.getBBox?.().height || 0;
    if (w > 0 && h > 0) svgEl.setAttribute('viewBox', '0 0 ' + w + ' ' + h);
  }
  svgEl.setAttribute('width', '100%');
  svgEl.setAttribute('height', '100%');
  svgEl.setAttribute('preserveAspectRatio', 'xMidYMid meet');
  // Defer pan-zoom init until after layout flush; otherwise svg-pan-zoom
  // reads 0×0 dimensions and throws "matrix not invertible".
  const initZoom = () => {
    const rect = svgHost.getBoundingClientRect();
    if (rect.width < 2 || rect.height < 2) { requestAnimationFrame(initZoom); return; }
    let spz;
    try {
      spz = svgPanZoom(svgEl, { zoomEnabled: true, controlIconsEnabled: false, fit: true, center: true, minZoom: 0.1, maxZoom: 20 });
    } catch (err) {
      console.error('svg-pan-zoom init failed:', err);
      // Leave the SVG rendered without zoom controls as a graceful fallback.
      return;
    }
    ctrls.addEventListener('click', (e) => {
      const act = e.target.dataset.act;
      if (!spz) return;
      if (act === 'in') spz.zoomIn();
      else if (act === 'out') spz.zoomOut();
      else if (act === 'reset') { spz.resetZoom(); spz.resetPan(); spz.center(); spz.fit(); }
      else if (act === 'fit') spz.fit();
    });
  };
  requestAnimationFrame(() => requestAnimationFrame(initZoom));
}

function renderJson(wrap, text) {
  wrap.classList.remove('md-wrap');
  let obj;
  try { obj = JSON.parse(text); }
  catch (err) {
    const pre = document.createElement('pre');
    pre.className = 'code';
    pre.textContent = 'invalid JSON: ' + (err.message || err) + '\n\n' + text;
    wrap.appendChild(pre);
    return;
  }
  const host = document.createElement('div');
  host.className = 'json-tree';
  host.appendChild(buildJsonNode(obj, true));
  host.addEventListener('click', (e) => {
    if (e.target.classList.contains('toggle')) {
      const ul = e.target.parentElement.querySelector(':scope > ul');
      if (!ul) return;
      const collapsed = ul.classList.toggle('collapsed');
      e.target.textContent = collapsed ? '▶' : '▼';
    }
  });
  wrap.appendChild(host);
}
function buildJsonNode(value, isRoot) {
  const node = document.createElement('div');
  if (Array.isArray(value)) {
    const toggle = document.createElement('span');
    toggle.className = 'toggle';
    toggle.textContent = '▼';
    node.appendChild(toggle);
    node.appendChild(document.createTextNode('[ '));
    const meta = document.createElement('span');
    meta.className = 'meta';
    meta.textContent = value.length + ' items';
    node.appendChild(meta);
    const ul = document.createElement('ul');
    value.forEach((v, i) => {
      const li = document.createElement('li');
      const kspan = document.createElement('span');
      kspan.className = 'key';
      kspan.textContent = i + ': ';
      li.appendChild(kspan);
      li.appendChild(buildJsonNode(v, false));
      ul.appendChild(li);
    });
    node.appendChild(ul);
    node.appendChild(document.createTextNode(' ]'));
  } else if (value !== null && typeof value === 'object') {
    const toggle = document.createElement('span');
    toggle.className = 'toggle';
    toggle.textContent = '▼';
    node.appendChild(toggle);
    node.appendChild(document.createTextNode('{ '));
    const keys = Object.keys(value);
    const meta = document.createElement('span');
    meta.className = 'meta';
    meta.textContent = keys.length + ' keys';
    node.appendChild(meta);
    const ul = document.createElement('ul');
    keys.forEach(k => {
      const li = document.createElement('li');
      const kspan = document.createElement('span');
      kspan.className = 'key';
      kspan.textContent = JSON.stringify(k) + ': ';
      li.appendChild(kspan);
      li.appendChild(buildJsonNode(value[k], false));
      ul.appendChild(li);
    });
    node.appendChild(ul);
    node.appendChild(document.createTextNode(' }'));
  } else if (typeof value === 'string') {
    const s = document.createElement('span');
    s.className = 'str';
    s.textContent = JSON.stringify(value);
    node.appendChild(s);
  } else if (typeof value === 'number') {
    const s = document.createElement('span');
    s.className = 'num';
    s.textContent = String(value);
    node.appendChild(s);
  } else if (typeof value === 'boolean') {
    const s = document.createElement('span');
    s.className = 'bool';
    s.textContent = String(value);
    node.appendChild(s);
  } else {
    const s = document.createElement('span');
    s.className = 'null';
    s.textContent = 'null';
    node.appendChild(s);
  }
  return node;
}

function renderCode(wrap, text, label) {
  wrap.classList.remove('md-wrap');
  const pre = document.createElement('pre');
  pre.className = 'code';
  const code = document.createElement('code');
  const extMap = { '.py': 'python', '.ts': 'typescript', '.js': 'javascript', '.sh': 'bash', '.yaml': 'yaml', '.yml': 'yaml', '.toml': 'ini' };
  const ext = '.' + (label.split('.').pop() || '');
  if (extMap[ext]) code.className = 'language-' + extMap[ext];
  code.textContent = text;
  pre.appendChild(code);
  wrap.appendChild(pre);
  if (typeof hljs !== 'undefined') hljs.highlightElement(code);
}

// ---------- footer ----------
async function updateFooter() {
  const left = document.getElementById('footer-left');
  const right = document.getElementById('footer-right');
  if (activeA === null) { left.textContent = '—'; right.textContent = '— —'; return; }
  const tab = TABS[activeA];
  left.textContent = tab.path;
  try {
    const r = await fetch('/meta/' + activeA);
    const m = await r.json();
    right.textContent = tab.kind + ' · ' + prettySize(m.size);
  } catch { right.textContent = tab.kind; }
}

// ---------- render pipeline ----------
async function renderAll() {
  renderSidebar();
  renderTabbar();
  await renderPane(activeA, 'pane-a');
  if (splitOn) await renderPane(activeB, 'pane-b');
  await updateFooter();
}

// ---------- keyboard ----------
document.addEventListener('keydown', (e) => {
  if (e.target.tagName === 'INPUT' || e.target.tagName === 'TEXTAREA') return;
  if (e.metaKey || e.ctrlKey || e.altKey) return;
  if (e.key === '\\') { splitOn = !splitOn; applySplit(); renderAll(); }
  else if (e.key === 'w') { if (activeA !== null) closeTab(activeA); }
  else if (e.key === 'd') { setTheme(document.documentElement.getAttribute('data-theme') === 'dark' ? 'light' : 'dark'); }
  else if (e.key === 'r') { if (activeA !== null) { delete contentCache[activeA]; renderAll(); } }
  else if (/^[1-9]$/.test(e.key)) {
    const list = [...openTabs].sort((a, b) => a - b);
    const pick = list[parseInt(e.key, 10) - 1];
    if (pick !== undefined) { activeA = pick; renderAll(); }
  }
});

// ---------- toolbar ----------
document.getElementById('toggle-split').addEventListener('click', () => { splitOn = !splitOn; applySplit(); renderAll(); });
document.getElementById('toggle-theme').addEventListener('click', () => setTheme(document.documentElement.getAttribute('data-theme') === 'dark' ? 'light' : 'dark'));
document.getElementById('reload').addEventListener('click', () => { if (activeA !== null) { delete contentCache[activeA]; renderAll(); } });

// auto-open first tab on load
if (TABS.length) { openTab(0); } else { renderAll(); }
</script>
</body>
</html>
"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        sys.stderr.write("[viewer] " + (fmt % args) + "\n")

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        if path in {"/", "/view"}:
            payload = INDEX_HTML.replace("__TABS_JSON__", json.dumps(TABS))
            self._send(200, "text/html; charset=utf-8", payload.encode("utf-8"))
            return
        if path.startswith("/content/"):
            idx = self._tab_idx(path.split("/content/", 1)[1])
            if idx is None: return
            tab = TABS[idx]
            try:
                data = Path(tab["path"]).read_bytes()
            except OSError as err:
                self._send(500, "text/plain", f"read error: {err}".encode("utf-8"))
                return
            ctype = mimetypes.guess_type(tab["path"])[0] or "text/plain; charset=utf-8"
            self._send(200, ctype, data)
            return
        if path.startswith("/meta/"):
            idx = self._tab_idx(path.split("/meta/", 1)[1])
            if idx is None: return
            tab = TABS[idx]
            try:
                st = os.stat(tab["path"])
            except OSError as err:
                self._send(500, "text/plain", f"stat error: {err}".encode("utf-8"))
                return
            payload = json.dumps({"size": st.st_size, "mtime": st.st_mtime}).encode("utf-8")
            self._send(200, "application/json", payload)
            return
        self._send(404, "text/plain", b"not found")

    def _tab_idx(self, raw: str):
        try:
            idx = int(raw)
            _ = TABS[idx]
            return idx
        except (ValueError, IndexError):
            self._send(404, "text/plain", b"not found")
            return None

    def _send(self, code: int, ctype: str, body: bytes):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("files", nargs="+")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--bind", default="0.0.0.0")
    args = parser.parse_args()

    for f in args.files:
        p = Path(f).resolve()
        if not p.exists():
            sys.exit(f"file not found: {f}")
        TABS.append({"path": str(p), "label": p.name, "kind": kind_for(p)})

    srv = ThreadingHTTPServer((args.bind, args.port), Handler)
    host = socket.getfqdn() or socket.gethostname() or "localhost"
    print(f"viewer listening on http://{host}:{args.port}/  ({len(TABS)} files)", flush=True)
    print(f"  also: http://{args.bind}:{args.port}/", flush=True)
    for i, t in enumerate(TABS):
        print(f"  [{i}] {t['label']}  ({t['kind']})", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nviewer shutting down", flush=True)
        srv.shutdown()


if __name__ == "__main__":
    main()
