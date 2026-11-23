/* codegraph view: file-level import graph rendered with sigma.js v3 + graphology. */
(function () {
  "use strict";

  var DATA = JSON.parse(document.getElementById("codegraph-data").textContent);
  var FILES = DATA.files;
  var GROUPS = DATA.groups;
  var COLLAPSE_ABOVE = 400;

  function $(id) { return document.getElementById(id); }

  function el(tag, attrs, text) {
    var node = document.createElement(tag);
    if (attrs) {
      Object.keys(attrs).forEach(function (k) { node.setAttribute(k, attrs[k]); });
    }
    if (text !== undefined && text !== null) node.textContent = String(text);
    return node;
  }

  function showMessage(text) {
    var m = $("message");
    m.textContent = text;
    m.hidden = false;
  }

  function setStatus(text) {
    var s = $("status");
    s.textContent = text || "";
    s.hidden = !text;
  }

  $("summary").textContent = FILES.counts.nodes + " files, " + FILES.counts.edges + " edges (" +
    FILES.counts.imports + " imports); kinds: " + FILES.kinds.join(", ") +
    (FILES.include_tests ? "; tests included" : "; tests hidden") +
    (FILES.paths.length ? "; paths: " + FILES.paths.join(", ") : "");

  // -- libraries (UMD globals), guarded -------------------------------------------------------
  var G = window.graphology;
  var GraphCtor = G ? (G.Graph || (typeof G === "function" ? G : null)) : null;
  var S = window.Sigma || window.sigma;
  var SigmaCtor = S ? (typeof S === "function" ? S : (S.Sigma || S.default || null)) : null;
  var LIB = window.graphologyLibrary || {};
  var FA2 = LIB.layoutForceAtlas2 && typeof LIB.layoutForceAtlas2.assign === "function" ? LIB.layoutForceAtlas2 : null;
  var FA2Worker = typeof LIB.FA2Layout === "function" ? LIB.FA2Layout : null;

  if (!GraphCtor || typeof SigmaCtor !== "function") {
    var missing = [];
    if (!GraphCtor) missing.push("graphology");
    if (typeof SigmaCtor !== "function") missing.push("sigma.js");
    showMessage("Could not load " + missing.join(" and ") + ".\n" +
      "This page loads its libraries from a CDN, which needs a network connection. " +
      "To view it offline, put the vendored files in codegraph/view/vendor/ (see the README there) " +
      "and run `codegraph view` again.");
    return;
  }
  if (!FILES.nodes.length) {
    showMessage("No files to show. Check --path, --kinds and --include-tests.");
    return;
  }

  // -- theme and colours -------------------------------------------------------------------------
  var theme = {};
  var colorCache = {};
  var darkQuery = window.matchMedia ? window.matchMedia("(prefers-color-scheme: dark)") : null;

  function readTheme() {
    var css = getComputedStyle(document.documentElement);
    ["ink", "muted", "accent", "edge", "dim"].forEach(function (name) {
      theme[name] = css.getPropertyValue("--" + name).trim() || "#888888";
    });
    theme.dark = !!(darkQuery && darkQuery.matches);
    colorCache = {};
  }

  function fnv1a(text) {
    var h = 2166136261;
    for (var i = 0; i < text.length; i++) {
      h ^= text.charCodeAt(i);
      h = Math.imul(h, 16777619);
    }
    return h >>> 0;
  }

  function hslToHex(h, s, l) {
    s /= 100;
    l /= 100;
    var a = s * Math.min(l, 1 - l);
    function f(n) {
      var k = (n + h / 30) % 12;
      var c = l - a * Math.max(-1, Math.min(k - 3, 9 - k, 1));
      var hex = Math.round(255 * c).toString(16);
      return hex.length === 1 ? "0" + hex : hex;
    }
    return "#" + f(0) + f(8) + f(4);
  }

  // Deterministic per group name: the same group gets the same colour in every run.
  function groupColor(name) {
    var key = (theme.dark ? "d:" : "l:") + name;
    if (!colorCache[key]) {
      var h = fnv1a(name);
      var hue = h % 360;
      var sat = 55 + ((h >>> 9) % 25);
      var light = (theme.dark ? 60 : 42) + ((h >>> 17) % 10);
      colorCache[key] = hslToHex(hue, sat, light);
    }
    return colorCache[key];
  }

  readTheme();

  // -- state -------------------------------------------------------------------------------------
  var kindCounts = {};
  FILES.edges.forEach(function (e) { kindCounts[e.kind] = (kindCounts[e.kind] || 0) + e.count; });
  var allKinds = Object.keys(kindCounts).sort();

  var groupFiles = {};
  FILES.nodes.forEach(function (n) { (groupFiles[n.group] = groupFiles[n.group] || []).push(n); });
  var groupNames = Object.keys(groupFiles).sort();

  var state = {
    collapsed: FILES.nodes.length > COLLAPSE_ABOVE,
    selected: null,
    neighbours: null,
    hiddenGroups: new Set(),
    kinds: new Set(allKinds)
  };
  var graphs = {};
  var graph = null;
  var renderer = null;
  var layout = null;
  var layoutTimer = null;

  function kindVisible(kinds) {
    for (var k in kinds) {
      if (Object.prototype.hasOwnProperty.call(kinds, k) && state.kinds.has(k)) return true;
    }
    return false;
  }

  function kindText(kinds) {
    return Object.keys(kinds).sort().map(function (k) {
      return kinds[k] > 1 ? k + " x" + kinds[k] : k;
    }).join(", ");
  }

  // -- graph building ----------------------------------------------------------------------------
  function nodeSize(n, collapsed) {
    var base = collapsed ? 4 + Math.sqrt(n.files || 1) * 0.8 : 2;
    return base + Math.min(collapsed ? 16 : 12, Math.sqrt(n.in_degree) * (collapsed ? 2.2 : 1.5));
  }

  function nodeLabel(n, collapsed) {
    if (collapsed) return n.id + " (" + n.files + ")";
    var slash = n.path.lastIndexOf("/");
    return slash >= 0 ? n.path.slice(slash + 1) : n.path;
  }

  function buildGraph(data) {
    var g = new GraphCtor({ multi: false, type: "directed", allowSelfLoops: false });
    var byGroup = {};
    data.nodes.forEach(function (n) { (byGroup[n.group] = byGroup[n.group] || []).push(n); });
    var names = Object.keys(byGroup).sort();
    // Deterministic seed: groups on a big circle, each group's nodes on a small circle around it.
    var radius = 20 + 12 * Math.sqrt(data.nodes.length);
    names.forEach(function (name, gi) {
      var members = byGroup[name];
      var angle = 2 * Math.PI * gi / names.length;
      var cx = names.length > 1 ? radius * Math.cos(angle) : 0;
      var cy = names.length > 1 ? radius * Math.sin(angle) : 0;
      var r = 2 + 2 * Math.sqrt(members.length);
      members.forEach(function (n, j) {
        var b = 2 * Math.PI * j / members.length;
        g.addNode(n.id, {
          x: cx + r * Math.cos(b),
          y: cy + r * Math.sin(b),
          size: nodeSize(n, data.collapsed),
          color: groupColor(n.group),
          label: nodeLabel(n, data.collapsed),
          group: n.group,
          info: n
        });
      });
    });
    // One edge per (from, to): the kinds between the pair are kept as {kind: count}.
    var pairs = new Map();
    data.edges.forEach(function (e) {
      if (e.from === e.to || !g.hasNode(e.from) || !g.hasNode(e.to)) return;
      var key = e.from + "\u0000" + e.to;
      var entry = pairs.get(key);
      if (!entry) {
        entry = { from: e.from, to: e.to, kinds: {}, weight: 0 };
        pairs.set(key, entry);
      }
      entry.kinds[e.kind] = (entry.kinds[e.kind] || 0) + e.count;
      entry.weight += e.count;
    });
    pairs.forEach(function (p) {
      g.addEdge(p.from, p.to, {
        kinds: p.kinds,
        weight: p.weight,
        size: 0.6 + Math.min(3, Math.log(1 + p.weight) * 0.6),
        color: theme.edge
      });
    });
    return g;
  }

  // -- layout ------------------------------------------------------------------------------------
  function stopLayout() {
    if (layoutTimer) {
      clearTimeout(layoutTimer);
      layoutTimer = null;
    }
    if (layout) {
      try { layout.stop(); } catch (err) { /* already stopped */ }
      try { layout.kill(); } catch (err) { /* already killed */ }
      layout = null;
    }
    setStatus("");
  }

  function runLayout(g) {
    stopLayout();
    if (!FA2 || g.order < 2) {
      if (!FA2) setStatus("ForceAtlas2 is not available; showing the seeded layout");
      return;
    }
    var settings = typeof FA2.inferSettings === "function" ? FA2.inferSettings(g) : { barnesHutOptimize: g.order > 500 };
    if (FA2Worker && g.order > 150) {
      try {
        layout = new FA2Worker(g, { settings: settings });
        layout.start();
        setStatus("Laying out...");
        layoutTimer = setTimeout(stopLayout, Math.min(20000, 3000 + g.order * 6));
        return;
      } catch (err) {
        layout = null; // e.g. workers blocked on file://; fall back to the synchronous layout
      }
    }
    FA2.assign(g, { iterations: g.order > 1000 ? 80 : g.order > 300 ? 150 : 300, settings: settings });
  }

  // -- reducers ----------------------------------------------------------------------------------
  function nodeReducer(node, data) {
    var res = Object.assign({}, data);
    res.color = groupColor(data.group);
    if (state.hiddenGroups.has(data.group)) {
      res.hidden = true;
      return res;
    }
    if (state.selected) {
      if (node === state.selected) {
        res.highlighted = true;
        res.forceLabel = true;
        res.zIndex = 2;
      } else if (state.neighbours.has(node)) {
        res.forceLabel = true;
        res.zIndex = 1;
      } else {
        res.color = theme.dim;
        res.label = "";
        res.zIndex = 0;
      }
    }
    return res;
  }

  function edgeReducer(edge, data) {
    var res = Object.assign({}, data);
    res.color = theme.edge;
    if (!kindVisible(data.kinds)) {
      res.hidden = true;
      return res;
    }
    var source = graph.source(edge);
    var target = graph.target(edge);
    if (state.hiddenGroups.has(graph.getNodeAttribute(source, "group")) ||
        state.hiddenGroups.has(graph.getNodeAttribute(target, "group"))) {
      res.hidden = true;
      return res;
    }
    if (state.selected) {
      if (source === state.selected || target === state.selected) {
        res.color = theme.accent;
        res.size = Math.max(data.size, 1.5);
        res.zIndex = 1;
      } else {
        res.hidden = true;
      }
    }
    return res;
  }

  // -- renderer ----------------------------------------------------------------------------------
  function mount(g) {
    if (renderer) {
      renderer.kill();
      renderer = null;
    }
    graph = g;
    try {
      renderer = new SigmaCtor(g, $("graph"), {
        defaultEdgeType: "arrow",
        renderEdgeLabels: false,
        labelColor: { color: theme.ink },
        labelDensity: 0.7,
        labelGridCellSize: 60,
        labelRenderedSizeThreshold: 7,
        zIndex: true,
        nodeReducer: nodeReducer,
        edgeReducer: edgeReducer
      });
    } catch (err) {
      showMessage("Could not start the graph renderer (it needs WebGL): " + (err && err.message ? err.message : err));
      return;
    }
    renderer.on("clickNode", function (event) { select(event.node, false); });
    renderer.on("clickStage", function () { select(null); });
    renderer.on("enterNode", function () { $("graph").style.cursor = "pointer"; });
    renderer.on("leaveNode", function () { $("graph").style.cursor = ""; });
  }

  function refresh() {
    if (renderer) renderer.refresh();
  }

  function focus(node) {
    if (!renderer || typeof renderer.getNodeDisplayData !== "function") return;
    var d = renderer.getNodeDisplayData(node);
    if (!d) return;
    var camera = renderer.getCamera();
    camera.animate({ x: d.x, y: d.y, ratio: Math.min(camera.ratio, 0.3) }, { duration: 500 });
  }

  function showGraph(collapsed) {
    stopLayout();
    state.collapsed = collapsed;
    $("collapsed").checked = collapsed;
    state.selected = null;
    state.neighbours = null;
    renderPanel();
    var key = collapsed ? "groups" : "files";
    var fresh = !graphs[key];
    if (fresh) graphs[key] = buildGraph(collapsed ? GROUPS : FILES);
    mount(graphs[key]);
    if (fresh) runLayout(graphs[key]);
  }

  // -- selection and side panel -----------------------------------------------------------------
  function visibleNeighbours(node) {
    var set = new Set();
    graph.forEachEdge(node, function (edge, attrs, source, target) {
      if (kindVisible(attrs.kinds)) set.add(source === node ? target : source);
    });
    return set;
  }

  function select(node, moveCamera) {
    if (node !== null && !graph.hasNode(node)) node = null;
    state.selected = node;
    state.neighbours = node ? visibleNeighbours(node) : null;
    renderPanel();
    refresh();
    if (node && moveCamera) focus(node);
  }

  // A file chosen from the search or the panel: in collapsed mode its group is selected.
  function selectFile(path) {
    if (state.collapsed) {
      var file = FILES.nodes.find(function (n) { return n.path === path; });
      if (file) select(file.group, true);
    } else {
      select(path, true);
    }
  }

  function linkList(title, items) {
    var frag = document.createDocumentFragment();
    frag.appendChild(el("h3", null, title + " (" + items.length + ")"));
    if (!items.length) {
      frag.appendChild(el("p", { "class": "muted" }, "none"));
      return frag;
    }
    var ul = el("ul", { "class": "links" });
    items.sort(function (a, b) { return a.node < b.node ? -1 : a.node > b.node ? 1 : 0; });
    items.forEach(function (item) {
      var li = el("li");
      var button = el("button", { type: "button", "class": "link" }, item.node);
      button.addEventListener("click", function () { item.onClick ? item.onClick() : select(item.node, true); });
      li.appendChild(button);
      if (item.kinds) li.appendChild(el("span", { "class": "kind" }, kindText(item.kinds)));
      ul.appendChild(li);
    });
    frag.appendChild(ul);
    return frag;
  }

  function renderPanel() {
    var panel = $("panel");
    var body = $("panel-body");
    body.textContent = "";
    var node = state.selected;
    if (!node || !graph) {
      panel.hidden = true;
      return;
    }
    var info = graph.getNodeAttribute(node, "info");
    body.appendChild(el("p", { "class": "path" }, info.path));
    var meta = el("dl", { "class": "meta" });
    var rows = [["group", info.group]];
    if (state.collapsed) rows.push(["files", info.files]);
    rows.push(["language", info.lang || "mixed"], ["imported by", info.in_degree], ["imports", info.out_degree]);
    rows.forEach(function (row) {
      meta.appendChild(el("dt", null, row[0]));
      meta.appendChild(el("dd", null, row[1]));
    });
    body.appendChild(meta);
    var flags = el("div", { "class": "flags" });
    [["is_test", "test"], ["is_registry", "registry"], ["is_entrypoint", "entrypoint"]].forEach(function (f) {
      if (info[f[0]]) flags.appendChild(el("span", { "class": "flag" }, f[1]));
    });
    if (flags.childNodes.length) body.appendChild(flags);
    var imports = [];
    var importedBy = [];
    graph.forEachOutEdge(node, function (edge, attrs, source, target) {
      if (kindVisible(attrs.kinds)) imports.push({ node: target, kinds: attrs.kinds });
    });
    graph.forEachInEdge(node, function (edge, attrs, source) {
      if (kindVisible(attrs.kinds)) importedBy.push({ node: source, kinds: attrs.kinds });
    });
    body.appendChild(linkList("Imports", imports));
    body.appendChild(linkList("Imported by", importedBy));
    if (state.collapsed) {
      var members = (groupFiles[node] || []).map(function (n) {
        return { node: n.path, onClick: function () { showGraph(false); select(n.path, true); } };
      });
      body.appendChild(linkList("Files", members));
    }
    panel.hidden = false;
  }

  // -- controls ----------------------------------------------------------------------------------
  function buildKinds() {
    var box = $("kinds");
    if (!allKinds.length) box.appendChild(el("p", { "class": "muted" }, "no edges"));
    allKinds.forEach(function (kind) {
      var label = el("label");
      var input = el("input", { type: "checkbox" });
      input.checked = true;
      input.addEventListener("change", function () {
        if (input.checked) state.kinds.add(kind); else state.kinds.delete(kind);
        if (state.selected) state.neighbours = visibleNeighbours(state.selected);
        renderPanel();
        refresh();
      });
      label.appendChild(input);
      label.appendChild(el("span", null, kind));
      label.appendChild(el("span", { "class": "count" }, kindCounts[kind]));
      box.appendChild(label);
    });
  }

  function buildLegend() {
    var list = $("legend");
    list.textContent = "";
    groupNames.forEach(function (name) {
      var li = el("li", { title: "Click to show or hide this group" });
      var swatch = el("span", { "class": "swatch" });
      swatch.style.background = groupColor(name);
      li.appendChild(swatch);
      li.appendChild(el("span", { "class": "name" }, name));
      li.appendChild(el("span", { "class": "count" }, groupFiles[name].length));
      if (state.hiddenGroups.has(name)) li.className = "off";
      li.addEventListener("click", function () {
        if (state.hiddenGroups.has(name)) state.hiddenGroups.delete(name); else state.hiddenGroups.add(name);
        li.className = state.hiddenGroups.has(name) ? "off" : "";
        refresh();
      });
      list.appendChild(li);
    });
  }

  function buildSearch() {
    var input = $("search");
    var list = $("matches");
    var current = [];

    function close() {
      list.hidden = true;
      list.textContent = "";
    }

    function update() {
      var q = input.value.trim().toLowerCase();
      close();
      current = [];
      if (!q) return;
      FILES.nodes.forEach(function (n) {
        var p = n.path.toLowerCase();
        var at = p.indexOf(q);
        if (at < 0) return;
        var base = p.slice(p.lastIndexOf("/") + 1);
        current.push({ path: n.path, rank: base.indexOf(q) === 0 ? 0 : base.indexOf(q) > 0 ? 1 : 2 });
      });
      current.sort(function (a, b) { return a.rank - b.rank || a.path.length - b.path.length || (a.path < b.path ? -1 : 1); });
      current = current.slice(0, 30);
      if (!current.length) {
        list.appendChild(el("li", { "class": "muted" }, "no file matches"));
      }
      current.forEach(function (m) {
        var li = el("li");
        var button = el("button", { type: "button" }, m.path);
        button.addEventListener("click", function () {
          selectFile(m.path);
          close();
        });
        li.appendChild(button);
        list.appendChild(li);
      });
      list.hidden = false;
    }

    input.addEventListener("input", update);
    input.addEventListener("keydown", function (event) {
      if (event.key === "Enter" && current.length) {
        selectFile(current[0].path);
        close();
      } else if (event.key === "Escape") {
        input.value = "";
        close();
      }
    });
    document.addEventListener("click", function (event) {
      if (!event.target.closest || !event.target.closest(".search")) close();
    });
  }

  $("collapsed").addEventListener("change", function () { showGraph($("collapsed").checked); });
  $("relayout").addEventListener("click", function () { if (graph) runLayout(graph); });
  $("panel-close").addEventListener("click", function () { select(null); });
  $("groups-all").addEventListener("click", function () {
    state.hiddenGroups.clear();
    buildLegend();
    refresh();
  });

  function onSchemeChange() {
    readTheme();
    buildLegend();
    if (renderer && typeof renderer.setSetting === "function") renderer.setSetting("labelColor", { color: theme.ink });
    refresh();
  }
  if (darkQuery) {
    if (typeof darkQuery.addEventListener === "function") darkQuery.addEventListener("change", onSchemeChange);
    else if (typeof darkQuery.addListener === "function") darkQuery.addListener(onSchemeChange);
  }

  buildKinds();
  buildLegend();
  buildSearch();
  showGraph(state.collapsed);
})();
