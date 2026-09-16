/* ENI Monitor — interactions. No dependencies. */
(function () {
  "use strict";

  var root = document.documentElement;

  // ---------------------------------------------------------------- theme --
  function setTheme(t) {
    root.setAttribute("data-theme", t);
    try { localStorage.setItem("eni-theme", t); } catch (e) {}
    var lbl = document.querySelector("#themeLabel");
    if (lbl) { lbl.textContent = t === "light" ? "Dark mode" : "Light mode"; }
  }
  try {
    var saved = localStorage.getItem("eni-theme");
    if (saved) { setTheme(saved); }
  } catch (e) {}

  document.addEventListener("click", function (e) {
    var t = e.target.closest("[data-theme-toggle]");
    if (!t) { return; }
    setTheme(root.getAttribute("data-theme") === "light" ? "dark" : "light");
  });

  // -------------------------------------------------------------- sidebar --
  document.addEventListener("click", function (e) {
    if (e.target.closest("[data-collapse]")) {
      var l = document.querySelector(".layout");
      l.classList.toggle("tight");
      try { localStorage.setItem("eni-tight", l.classList.contains("tight") ? "1" : "0"); } catch (x) {}
    }
    if (e.target.closest("[data-burger]")) {
      document.querySelector(".side").classList.toggle("open");
    }
  });
  try {
    if (localStorage.getItem("eni-tight") === "1") {
      var lay = document.querySelector(".layout");
      if (lay) { lay.classList.add("tight"); }
    }
  } catch (e) {}

  // --------------------------------------------------------------- toasts --
  var tray = document.querySelector(".toasts");
  window.toast = function (msg, kind) {
    if (!tray) { return; }
    var el = document.createElement("div");
    el.className = "toast " + (kind || "");
    el.setAttribute("role", "status");
    el.textContent = msg;
    tray.appendChild(el);
    setTimeout(function () {
      el.classList.add("out");
      setTimeout(function () { el.remove(); }, 320);
    }, 3200);
  };

  // ------------------------------------------------------ command palette --
  var cmdk = document.querySelector(".cmdk");
  var scrim = document.querySelector(".scrim");
  if (cmdk) {
    var input = cmdk.querySelector("input");
    var list = cmdk.querySelector("ul");
    var items = [];
    var sel = 0;

    function collect() {
      var out = [];
      document.querySelectorAll(".nav a").forEach(function (a) {
        out.push({ label: a.textContent.trim(), hint: "page", href: a.getAttribute("href") });
      });
      document.querySelectorAll("tbody a[href]").forEach(function (a) {
        var row = a.closest("tr");
        var sub = row ? (row.querySelector(".mono") || {}).textContent : "";
        out.push({ label: a.textContent.trim(), hint: (sub || "row").trim(),
                   href: a.getAttribute("href") });
      });
      out.push({ label: "Toggle light or dark", hint: "theme", act: "theme" });
      out.push({ label: "Sign out", hint: "session", href: "/logout" });
      return out;
    }

    function render(q) {
      var needle = (q || "").toLowerCase();
      items = collect().filter(function (i) {
        return !needle || i.label.toLowerCase().indexOf(needle) > -1 ||
               i.hint.toLowerCase().indexOf(needle) > -1;
      }).slice(0, 40);
      sel = 0;
      if (!items.length) {
        list.innerHTML = "";
        var n = document.createElement("li");
        n.className = "none";
        n.textContent = "nothing matches that";
        list.appendChild(n);
        return;
      }
      list.innerHTML = "";
      items.forEach(function (i, idx) {
        var li = document.createElement("li");
        if (idx === 0) { li.className = "sel"; }
        li.setAttribute("role", "option");
        var span = document.createElement("span");
        span.textContent = i.label;
        var small = document.createElement("small");
        small.textContent = i.hint;
        li.appendChild(span);
        li.appendChild(small);
        li.addEventListener("click", function () { run(i); });
        li.addEventListener("mousemove", function () { move(idx); });
        list.appendChild(li);
      });
    }

    function move(n) {
      var lis = list.querySelectorAll("li");
      if (!lis.length) { return; }
      sel = (n + lis.length) % lis.length;
      lis.forEach(function (l, i) { l.className = i === sel ? "sel" : ""; });
      lis[sel].scrollIntoView({ block: "nearest" });
    }

    function run(i) {
      close();
      if (i.act === "theme") {
        setTheme(root.getAttribute("data-theme") === "light" ? "dark" : "light");
        window.toast("Theme switched");
        return;
      }
      if (i.href) { window.location.href = i.href; }
    }

    function open() {
      cmdk.classList.add("open");
      scrim.classList.add("open");
      input.value = "";
      render("");
      setTimeout(function () { input.focus(); }, 40);
    }
    function close() {
      cmdk.classList.remove("open");
      scrim.classList.remove("open");
    }

    input.addEventListener("input", function () { render(this.value); });
    input.addEventListener("keydown", function (e) {
      if (e.key === "ArrowDown") { e.preventDefault(); move(sel + 1); }
      else if (e.key === "ArrowUp") { e.preventDefault(); move(sel - 1); }
      else if (e.key === "Enter") { e.preventDefault(); if (items[sel]) { run(items[sel]); } }
      else if (e.key === "Escape") { close(); }
    });
    scrim.addEventListener("click", close);
    document.addEventListener("click", function (e) {
      if (e.target.closest("[data-cmdk]")) { open(); }
    });
    document.addEventListener("keydown", function (e) {
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k") { e.preventDefault(); open(); }
      if (e.key === "Escape") { close(); document.querySelectorAll(".over.open")
        .forEach(function (o) { o.classList.remove("open"); }); }
    });
  }

  // ----------------------------------------------------------- slide-over --
  document.addEventListener("click", function (e) {
    var o = e.target.closest("[data-over]");
    if (o) {
      var panel = document.querySelector(o.getAttribute("data-over"));
      if (panel) { panel.classList.toggle("open"); scrim.classList.toggle("open"); }
    }
    if (e.target.closest("[data-over-close]")) {
      document.querySelectorAll(".over.open").forEach(function (p) { p.classList.remove("open"); });
      scrim.classList.remove("open");
    }
  });

  // -------------------------------------------------------- sortable table --
  document.querySelectorAll("table[data-sortable] th:not(.no)").forEach(function (th, col) {
    th.setAttribute("tabindex", "0");
    function sort() {
      var table = th.closest("table");
      var body = table.querySelector("tbody");
      var rows = Array.prototype.slice.call(body.querySelectorAll("tr"));
      if (rows.length < 2 || body.querySelector(".empty")) { return; }
      var dir = th.getAttribute("aria-sort") === "ascending" ? -1 : 1;
      table.querySelectorAll("th").forEach(function (o) { o.removeAttribute("aria-sort"); });
      th.setAttribute("aria-sort", dir === 1 ? "ascending" : "descending");
      var idx = Array.prototype.indexOf.call(th.parentNode.children, th);
      rows.sort(function (a, b) {
        var x = (a.children[idx] || {}).textContent || "";
        var y = (b.children[idx] || {}).textContent || "";
        var nx = parseFloat(x.replace(/[^0-9.\-]/g, ""));
        var ny = parseFloat(y.replace(/[^0-9.\-]/g, ""));
        if (!isNaN(nx) && !isNaN(ny)) { return (nx - ny) * dir; }
        return x.trim().localeCompare(y.trim()) * dir;
      });
      rows.forEach(function (r) { body.appendChild(r); });
    }
    th.addEventListener("click", sort);
    th.addEventListener("keydown", function (e) {
      if (e.key === "Enter" || e.key === " ") { e.preventDefault(); sort(); }
    });
  });

  // -------------------------------------------------- button loading state --
  document.querySelectorAll("form").forEach(function (f) {
    f.addEventListener("submit", function () {
      var b = f.querySelector("button[type=submit]");
      if (b && !b.dataset.busy) {
        b.dataset.busy = "1";
        b.disabled = true;
        b.textContent = "working...";
      }
    });
  });
})();
