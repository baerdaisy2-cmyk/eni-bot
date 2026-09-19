/* VANTA — platform interactions. No dependencies, no build step. */
(function () {
  "use strict";

  var root = document.documentElement;
  var shell = document.querySelector(".shell");
  var rail = document.querySelector(".rail");

  // ------------------------------------------------------------- sidebar --
  try {
    if (localStorage.getItem("vanta-rail") === "open" && shell) {
      shell.classList.add("expanded");
    }
  } catch (e) {}

  document.addEventListener("click", function (e) {
    if (e.target.closest("[data-rail]")) {
      if (window.innerWidth <= 900) {
        rail.classList.toggle("open");
      } else {
        shell.classList.toggle("expanded");
        try {
          localStorage.setItem("vanta-rail",
            shell.classList.contains("expanded") ? "open" : "closed");
        } catch (x) {}
      }
    }
    if (rail && rail.classList.contains("open") &&
        !e.target.closest(".rail") && !e.target.closest("[data-rail]")) {
      rail.classList.remove("open");
    }
  });

  // -------------------------------------------------------- profile menu --
  var menu = document.querySelector(".menu");
  var whoBtn = document.querySelector(".who > button");
  if (whoBtn && menu) {
    whoBtn.addEventListener("click", function (e) {
      e.stopPropagation();
      menu.classList.toggle("open");
      whoBtn.setAttribute("aria-expanded", menu.classList.contains("open"));
    });
    document.addEventListener("click", function (e) {
      if (!e.target.closest(".who")) {
        menu.classList.remove("open");
        whoBtn.setAttribute("aria-expanded", "false");
      }
    });
  }

  // --------------------------------------------------------------- theme --
  function setTheme(t) {
    root.setAttribute("data-theme", t);
    try { localStorage.setItem("vanta-theme", t); } catch (e) {}
    document.querySelectorAll("[data-theme-label]").forEach(function (el) {
      el.textContent = t === "light" ? "Dark mode" : "Light mode";
    });
  }
  try {
    var saved = localStorage.getItem("vanta-theme");
    if (saved) { setTheme(saved); }
  } catch (e) {}
  document.addEventListener("click", function (e) {
    if (e.target.closest("[data-theme-toggle]")) {
      setTheme(root.getAttribute("data-theme") === "light" ? "dark" : "light");
    }
  });

  // ----------------------------------------------------- session counter --
  var timer = document.querySelector("[data-session]");
  if (timer) {
    var start = Date.now();
    setInterval(function () {
      var s = Math.floor((Date.now() - start) / 1000);
      var h = String(Math.floor(s / 3600)).padStart(2, "0");
      var m = String(Math.floor((s % 3600) / 60)).padStart(2, "0");
      var sec = String(s % 60).padStart(2, "0");
      timer.textContent = h + ":" + m + ":" + sec;
    }, 1000);
  }

  // ---------------------------------------------------------- count-up --
  var reduced = window.matchMedia &&
    window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  document.querySelectorAll("[data-count]").forEach(function (el) {
    var target = parseFloat(el.getAttribute("data-count"));
    var suffix = el.getAttribute("data-suffix") || "";
    if (isNaN(target)) { return; }
    if (reduced || target === 0) { el.textContent = target + suffix; return; }
    var t0 = null;
    function step(now) {
      if (t0 === null) { t0 = now; }
      var p = Math.min(1, (now - t0) / 560);
      el.textContent = Math.round(target * (1 - Math.pow(1 - p, 3))) + suffix;
      if (p < 1) { requestAnimationFrame(step); }
    }
    el.textContent = "0" + suffix;
    requestAnimationFrame(step);
  });

  // ------------------------------------------------- console autoscroll --
  document.querySelectorAll(".console").forEach(function (c) {
    c.scrollTop = c.scrollHeight;
  });

  // ------------------------------------------------------ boot sequence --
  var boot = document.querySelector(".boot");
  if (boot) {
    var lines = [
      ['> initialising secure channel', 'cy'],
      ['> tls handshake .......... <span class="ok">OK</span>', ''],
      ['> key exchange ........... <span class="ok">OK</span>', ''],
      ['> awaiting credentials', 'cy']
    ];
    if (reduced) {
      boot.innerHTML = lines.map(function (l) {
        return '<div class="' + l[1] + '">' + l[0] + '</div>';
      }).join("");
    } else {
      boot.innerHTML = "";
      lines.forEach(function (l, i) {
        setTimeout(function () {
          var d = document.createElement("div");
          d.className = l[1];
          d.innerHTML = l[0];
          boot.appendChild(d);
        }, 220 * (i + 1));
      });
    }
  }

  // ------------------------------------------------------ password peek --
  document.querySelectorAll("[data-reveal]").forEach(function (b) {
    b.addEventListener("click", function () {
      var input = document.getElementById(b.getAttribute("data-reveal"));
      if (!input) { return; }
      input.type = input.type === "password" ? "text" : "password";
      b.setAttribute("aria-label",
        input.type === "password" ? "Show password" : "Hide password");
    });
  });

  // ---------------------------------------------------- command palette --
  var pal = document.querySelector(".palette");
  var scrim = document.querySelector(".scrim");
  if (pal) {
    var input = pal.querySelector("input");
    var list = pal.querySelector("ul");
    var items = [], sel = 0;

    function collect() {
      var out = [];
      document.querySelectorAll(".rail nav a").forEach(function (a) {
        out.push({ label: a.textContent.trim(), hint: "route",
                   href: a.getAttribute("href") });
      });
      document.querySelectorAll("tbody a[href]").forEach(function (a) {
        var row = a.closest("tr");
        var sub = row ? (row.querySelector(".mono") || {}).textContent : "";
        out.push({ label: a.textContent.trim(), hint: (sub || "row").trim(),
                   href: a.getAttribute("href") });
      });
      out.push({ label: "Toggle theme", hint: "system", act: "theme" });
      out.push({ label: "Terminate session", hint: "auth", href: "/logout" });
      return out;
    }
    function render(q) {
      var n = (q || "").toLowerCase();
      items = collect().filter(function (i) {
        return !n || i.label.toLowerCase().indexOf(n) > -1 ||
               i.hint.toLowerCase().indexOf(n) > -1;
      }).slice(0, 40);
      sel = 0; list.innerHTML = "";
      if (!items.length) {
        var e = document.createElement("li");
        e.className = "none"; e.textContent = "no match";
        list.appendChild(e); return;
      }
      items.forEach(function (i, idx) {
        var li = document.createElement("li");
        if (idx === 0) { li.className = "sel"; }
        li.setAttribute("role", "option");
        var s1 = document.createElement("span"); s1.textContent = i.label;
        var s2 = document.createElement("small"); s2.textContent = i.hint;
        li.appendChild(s1); li.appendChild(s2);
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
        return;
      }
      if (i.href) { window.location.href = i.href; }
    }
    function open() {
      pal.classList.add("open"); scrim.classList.add("open");
      input.value = ""; render("");
      setTimeout(function () { input.focus(); }, 40);
    }
    function close() { pal.classList.remove("open"); scrim.classList.remove("open"); }

    input.addEventListener("input", function () { render(this.value); });
    input.addEventListener("keydown", function (e) {
      if (e.key === "ArrowDown") { e.preventDefault(); move(sel + 1); }
      else if (e.key === "ArrowUp") { e.preventDefault(); move(sel - 1); }
      else if (e.key === "Enter") { e.preventDefault(); if (items[sel]) { run(items[sel]); } }
      else if (e.key === "Escape") { close(); }
    });
    scrim.addEventListener("click", close);
    document.addEventListener("click", function (e) {
      if (e.target.closest("[data-palette]")) { open(); }
    });
    document.addEventListener("keydown", function (e) {
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k") {
        e.preventDefault(); open();
      }
      if (e.key === "Escape") { close(); }
    });
  }

  // -------------------------------------------------------- table sort --
  document.querySelectorAll("table[data-sortable] th:not(.no)").forEach(function (th) {
    th.setAttribute("tabindex", "0");
    th.setAttribute("role", "columnheader");
    function sort() {
      var table = th.closest("table"), body = table.querySelector("tbody");
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

  // ------------------------------------------------------ submit lockout --
  document.querySelectorAll("form").forEach(function (f) {
    f.addEventListener("submit", function () {
      var b = f.querySelector("button[type=submit]");
      if (b && !b.dataset.busy) {
        b.dataset.busy = "1"; b.disabled = true; b.textContent = "Executing";
      }
    });
  });
})();
