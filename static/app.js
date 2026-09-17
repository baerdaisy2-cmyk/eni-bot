/* ENI Monitor — interactions. No dependencies. */
(function () {
  "use strict";

  var root = document.documentElement;
  var drawer = document.querySelector(".drawer");
  var overlay = document.querySelector(".overlay");
  var burger = document.querySelector(".burger");

  // ---------------------------------------------------------------- drawer
  function openDrawer() {
    if (!drawer) { return; }
    drawer.classList.add("open");
    overlay.classList.add("open");
    burger.setAttribute("aria-expanded", "true");
    var first = drawer.querySelector("a, button");
    if (first) { setTimeout(function () { first.focus(); }, 60); }
  }
  function closeDrawer() {
    if (!drawer) { return; }
    drawer.classList.remove("open");
    overlay.classList.remove("open");
    burger.setAttribute("aria-expanded", "false");
    burger.focus();
  }
  if (burger) { burger.addEventListener("click", openDrawer); }
  if (overlay) { overlay.addEventListener("click", closeDrawer); }
  document.addEventListener("click", function (e) {
    if (e.target.closest("[data-drawer-close]")) { closeDrawer(); }
  });
  document.addEventListener("keydown", function (e) {
    if (e.key === "Escape" && drawer && drawer.classList.contains("open")) {
      closeDrawer();
    }
    if (drawer && drawer.classList.contains("open") && e.key === "Tab") {
      var items = drawer.querySelectorAll("a, button");
      if (!items.length) { return; }
      var first = items[0], last = items[items.length - 1];
      if (e.shiftKey && document.activeElement === first) {
        e.preventDefault(); last.focus();
      } else if (!e.shiftKey && document.activeElement === last) {
        e.preventDefault(); first.focus();
      }
    }
  });

  // ----------------------------------------------------------------- theme
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
    if (e.target.closest("[data-theme-toggle]")) {
      setTheme(root.getAttribute("data-theme") === "light" ? "dark" : "light");
    }
  });

  // ------------------------------------------------------------- count up
  var reduced = window.matchMedia &&
    window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  document.querySelectorAll("[data-count]").forEach(function (el) {
    var target = parseFloat(el.getAttribute("data-count"));
    var suffix = el.getAttribute("data-suffix") || "";
    if (isNaN(target)) { return; }
    if (reduced || target === 0) { el.textContent = target + suffix; return; }
    var started = null, dur = 620;
    function step(now) {
      if (started === null) { started = now; }
      var p = Math.min(1, (now - started) / dur);
      var eased = 1 - Math.pow(1 - p, 3);
      el.textContent = Math.round(target * eased) + suffix;
      if (p < 1) { requestAnimationFrame(step); }
    }
    el.textContent = "0" + suffix;
    requestAnimationFrame(step);
  });

  // -------------------------------------------------------- sortable table
  document.querySelectorAll("table[data-sortable] th:not(.no)").forEach(function (th) {
    th.setAttribute("tabindex", "0");
    th.setAttribute("role", "columnheader");
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

  // ------------------------------------------------------- submit feedback
  document.querySelectorAll("form").forEach(function (f) {
    f.addEventListener("submit", function () {
      var b = f.querySelector("button[type=submit]");
      if (b && !b.dataset.busy) {
        b.dataset.busy = "1";
        b.disabled = true;
        b.textContent = "Working";
      }
    });
  });
})();
