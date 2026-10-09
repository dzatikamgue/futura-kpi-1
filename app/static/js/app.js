/* Futura Performance — interactions (aucune dépendance hormis Chart.js) */
(function () {
  "use strict";
  var $ = function (s, r) { return (r || document).querySelector(s); };
  var $$ = function (s, r) { return Array.prototype.slice.call((r || document).querySelectorAll(s)); };
  var store = {
    get: function (k) { try { return JSON.parse(localStorage.getItem(k)); } catch (e) { return null; } },
    set: function (k, v) { try { localStorage.setItem(k, JSON.stringify(v)); } catch (e) {} },
    del: function (k) { try { localStorage.removeItem(k); } catch (e) {} }
  };
  var fmt = function (n, d) { return n == null ? "—" : n.toFixed(d == null ? 2 : d).replace(".", ","); };
  // Seuils sur 100 (identiques à app/services/notation.py)
  function niveau(n) {
    if (n == null || isNaN(n)) return ["aucun", "—"];
    return n >= 80 ? ["excellent", "Excellent"] : n >= 70 ? ["tres-bien", "Très bien"]
      : n >= 60 ? ["bien", "Bien"] : n >= 50 ? ["passable", "Passable"] : ["insuffisant", "Insuffisant"];
  }

  /* ---------- Thème clair / sombre ---------- */
  $$("[data-theme-toggle]").forEach(function (b) {
    b.addEventListener("click", function () {
      var next = document.documentElement.getAttribute("data-theme") === "dark" ? "light" : "dark";
      document.documentElement.setAttribute("data-theme", next);
      try { localStorage.setItem("futura-theme", next); } catch (e) {}
      dessinerGraphiques();
    });
  });

  /* ---------- Navigation mobile ---------- */
  $$("[data-nav-toggle]").forEach(function (b) {
    b.addEventListener("click", function () {
      var open = document.body.classList.toggle("nav-open");
      b.setAttribute("aria-expanded", open ? "true" : "false");
    });
  });
  var overlay = $(".overlay");
  if (overlay) overlay.addEventListener("click", function () { document.body.classList.remove("nav-open"); });
  document.addEventListener("keydown", function (e) { if (e.key === "Escape") document.body.classList.remove("nav-open"); });

  /* ---------- Messages flash ---------- */
  $$(".alert__close").forEach(function (b) { b.addEventListener("click", function () { b.closest(".alert").remove(); }); });

  /* ---------- Connexion faible / coupure ---------- */
  function etatReseau() { document.body.classList.toggle("is-offline", !navigator.onLine); }
  window.addEventListener("online", etatReseau);
  window.addEventListener("offline", etatReseau);
  etatReseau();

  /* ---------- Confirmations, envoi automatique, état de chargement ---------- */
  document.addEventListener("submit", function (e) {
    var form = e.target;
    var btn = e.submitter;
    var msg = (btn && btn.getAttribute("data-confirm")) || form.getAttribute("data-confirm");
    if (msg && !window.confirm(msg)) { e.preventDefault(); return; }
    if (!navigator.onLine && form.method.toLowerCase() === "post") {
      e.preventDefault();
      window.alert("Pas de connexion internet. Votre saisie est conservée sur cet appareil : réessayez dès le retour du réseau.");
      return;
    }
  }, true);
  // Phase de remontée : ne s'exécute que si aucune validation n'a bloqué l'envoi
  document.addEventListener("submit", function (e) {
    var form = e.target;
    if (e.defaultPrevented || !form.hasAttribute("data-loading")) return;
    if (e.submitter) e.submitter.classList.add("is-loading");
    var ov = $(".loading-overlay");
    if (ov && form.hasAttribute("data-overlay")) ov.classList.add("is-on");
  });
  // Liste déroulante avec option « Nouveau… » qui fait apparaître un champ libre
  $$("select[data-autre]").forEach(function (sel) {
    var bloc = $(sel.getAttribute("data-autre"));
    if (!bloc) return;
    var champ = $("input", bloc);
    function maj() {
      var on = sel.value === "__nouveau__";
      bloc.hidden = !on;
      if (champ) { champ.required = on; if (on && document.activeElement === sel) champ.focus(); }
    }
    sel.addEventListener("change", maj);
    maj();
  });
  // Création d'entité : aperçu du logo et couleur de charte déduite du logo
  $$("form[data-charte]").forEach(function (form) {
    var fichier = $("[data-logo-input]", form), couleur = $("[data-couleur-input]", form),
        auto = $("[data-couleur-auto]", form), img = $("[data-apercu-logo]", form), bouton = $("[data-apercu-bouton]", form);
    function peindre() { if (bouton) bouton.style.background = couleur.value; }
    function dominante(image) {
      var c = document.createElement("canvas"), n = 64; c.width = n; c.height = n;
      var ctx = c.getContext("2d"); ctx.drawImage(image, 0, 0, n, n);
      var d = ctx.getImageData(0, 0, n, n).data, comptes = {}, best = null, max = 0;
      for (var i = 0; i < d.length; i += 4) {
        if (d[i + 3] < 128) continue;
        var r = d[i], g = d[i + 1], b = d[i + 2], mx = Math.max(r, g, b), mn = Math.min(r, g, b);
        var l = (mx + mn) / 510, s = mx === mn ? 0 : (mx - mn) / (255 - Math.abs(mx + mn - 255));
        if (s < .25 || l > .85 || l < .12) continue;
        var k = (r >> 4) + "," + (g >> 4) + "," + (b >> 4);
        comptes[k] = (comptes[k] || 0) + 1;
        if (comptes[k] > max) { max = comptes[k]; best = [r, g, b]; }
      }
      if (!best) return null;
      // Assombrit si besoin pour garder un bon contraste avec le texte blanc
      var lum = (0.299 * best[0] + 0.587 * best[1] + 0.114 * best[2]) / 255, f = lum > .55 ? .55 / lum : 1;
      return "#" + best.map(function (v) { return ("0" + Math.round(v * f).toString(16)).slice(-2); }).join("").toUpperCase();
    }
    if (fichier) fichier.addEventListener("change", function () {
      var f = fichier.files && fichier.files[0];
      if (!f) return;
      var url = URL.createObjectURL(f);
      img.onload = function () {
        if (auto && auto.checked) { var c = dominante(img); if (c) { couleur.value = c; peindre(); } }
      };
      img.src = url; img.hidden = false;
    });
    if (couleur) couleur.addEventListener("input", function () { if (auto) auto.checked = false; peindre(); });
  });
  $$("[data-autosubmit]").forEach(function (el) {
    el.addEventListener("change", function () { el.form && el.form.requestSubmit ? el.form.requestSubmit() : el.form.submit(); });
  });

  /* ---------- Validation en direct (formulaires data-validate) ---------- */
  function messageChamp(input) {
    if (input.validity.valueMissing) return "Ce champ est obligatoire.";
    if (input.validity.typeMismatch && input.type === "email") return "Adresse e-mail invalide.";
    if (input.validity.tooShort) return "Au moins " + input.minLength + " caractères.";
    if (input.validity.tooLong) return "Au plus " + input.maxLength + " caractères.";
    if (input.validity.rangeUnderflow || input.validity.rangeOverflow) return "Valeur hors limites.";
    if (input.validity.patternMismatch) return input.title || "Format invalide.";
    return "";
  }
  function verifierChamp(input) {
    var field = input.closest(".field");
    if (!field) return true;
    var err = field.querySelector(".error[data-live]");
    var msg = messageChamp(input);
    if (!err) { err = document.createElement("span"); err.className = "error"; err.setAttribute("data-live", ""); field.appendChild(err); }
    err.textContent = msg;
    input.setAttribute("aria-invalid", msg ? "true" : "false");
    return !msg;
  }
  $$("form[data-validate]").forEach(function (form) {
    form.setAttribute("novalidate", "");
    $$("input, select, textarea", form).forEach(function (i) {
      i.addEventListener("blur", function () { if (i.value || i.required) verifierChamp(i); });
      i.addEventListener("input", function () { if (i.getAttribute("aria-invalid") === "true") verifierChamp(i); });
    });
    form.addEventListener("submit", function (e) {
      var ok = true, premier = null;
      $$("input, select, textarea", form).forEach(function (i) {
        if (i.willValidate && !verifierChamp(i)) { ok = false; premier = premier || i; }
      });
      if (!ok) { e.preventDefault(); e.stopImmediatePropagation(); premier.focus(); }
    });
  });

  /* ---------- Formulaire d'évaluation ---------- */
  var evalForm = $("[data-eval-form]");
  if (evalForm) initEvaluation(evalForm);

  function initEvaluation(form) {
    var cle = form.getAttribute("data-autosave-key");
    var seuil = parseInt(form.getAttribute("data-seuil") || "40", 10);
    var serveurTs = parseInt(form.getAttribute("data-server-ts") || "0", 10);
    var etat = $("[data-save-state]");
    var sale = false, envoi = false;
    var criteres = $$("[data-critere]", form);

    function lire() {
      var d = {};
      $$("input, textarea", form).forEach(function (i) {
        if (!i.name || i.name === "csrf_token" || i.type === "hidden") return;
        d[i.name] = i.value;
      });
      return d;
    }
    function appliquer(d) {
      Object.keys(d).forEach(function (n) {
        var el = form.elements[n]; if (el && !el.disabled) el.value = d[n];
      });
      criteres.forEach(function (c) { synchro(c, "num"); });
    }
    function lireNote(c) {
      var el = $("[data-note]", c);
      if (!el || el.value.trim() === "") return null;
      var n = Number(el.value);
      return Number.isInteger(n) && n >= 0 && n <= 100 ? n : NaN;
    }
    // Garde le curseur et le champ numérique alignés, et affiche le niveau du critère
    function synchro(c, source) {
      var num = $("[data-note]", c), range = $("[data-range]", c), badge = $("[data-note-niveau]", c);
      var box = $(".score-input", c);
      if (source === "range") num.value = range.value;
      var n = lireNote(c);
      if (n != null && !isNaN(n)) range.value = n;
      if (box) box.classList.toggle("is-empty", n == null);
      var niv = niveau(n);
      if (badge) { badge.className = "badge badge--" + niv[0]; badge.textContent = n == null ? "Non noté" : isNaN(n) ? "Invalide" : niv[1]; }
    }
    function calculer() {
      var somme = 0, poids = 0, notes = 0;
      criteres.forEach(function (c) {
        var n = lireNote(c);
        if (n != null && !isNaN(n)) { var p = parseInt(c.getAttribute("data-poids"), 10) || 1; somme += n * p; poids += p; notes++; }
      });
      var note = poids ? Math.round(somme / poids * 100) / 100 : null;
      var niv = note == null ? ["aucun", "Aucune note"] : niveau(note);
      var s = $("[data-score]"); if (s) s.textContent = fmt(note, 1);
      var b = $("[data-score-niveau]"); if (b) { b.className = "badge badge--" + niv[0]; b.textContent = niv[1]; }
      var c = $("[data-score-count]"); if (c) c.textContent = notes + " / " + criteres.length;
      var p = $("[data-score-progress]"); if (p) p.style.width = (criteres.length ? 100 * notes / criteres.length : 0) + "%";
    }
    function marquer(dirty) {
      sale = dirty;
      if (etat) {
        etat.classList.toggle("is-dirty", dirty);
        etat.textContent = dirty ? "Modifications non enregistrées sur le serveur (copie locale conservée)" : "À jour";
      }
    }

    // Restauration d'un brouillon local plus récent que la version serveur
    var local = cle && store.get(cle);
    if (local && local.ts > serveurTs && !form.hasAttribute("data-readonly")) {
      appliquer(local.data);
      var bandeau = $("[data-restore-banner]");
      if (bandeau) {
        bandeau.hidden = false;
        var ign = $("[data-restore-discard]", bandeau);
        if (ign) ign.addEventListener("click", function () { store.del(cle); window.location.reload(); });
      }
      marquer(true);
    } else if (local) { store.del(cle); }

    criteres.forEach(function (c) {
      var range = $("[data-range]", c), num = $("[data-note]", c);
      if (range) range.addEventListener("input", function () { synchro(c, "range"); });
      if (num) num.addEventListener("input", function () { synchro(c, "num"); });
      // Un premier contact sur le curseur vide propose sa valeur courante
      if (range) range.addEventListener("pointerdown", function () { if (num.value === "") { num.value = range.value; synchro(c, "num"); } });
      synchro(c, "num");
    });
    form.addEventListener("input", function () {
      calculer();
      if (cle) store.set(cle, { ts: Date.now(), data: lire() });
      marquer(true);
      criteres.forEach(function (c) { var n = lireNote(c); if (n != null && !isNaN(n)) c.classList.remove("has-error"); });
    });
    form.addEventListener("submit", function (e) {
      var action = e.submitter && e.submitter.value;
      var invalide = criteres.filter(function (c) { return isNaN(lireNote(c)); })[0];
      if (invalide) {
        e.preventDefault(); e.stopImmediatePropagation();
        $("[data-critere-error]", invalide).textContent = "Note invalide : nombre entier de 0 à 100.";
        invalide.classList.add("has-error");
        invalide.scrollIntoView({ behavior: "smooth", block: "center" });
        return;
      }
      if (action === "soumettre") {
        var manque = null, justif = null;
        criteres.forEach(function (c) {
          var n = lireNote(c);
          var com = $("[data-critere-comment]", c);
          var err = $("[data-critere-error]", c);
          var msg = "";
          if (n == null) msg = "Note obligatoire pour soumettre.";
          else if (isNaN(n)) msg = "Note invalide : nombre entier de 0 à 100.";
          else if (n < seuil && com && !com.value.trim()) msg = "Justifiez une note inférieure à " + seuil + ".";
          if (err) err.textContent = msg;
          c.classList.toggle("has-error", !!msg);
          if (msg && !manque) manque = c;
        });
        if (manque) {
          e.preventDefault(); e.stopImmediatePropagation();
          manque.scrollIntoView({ behavior: "smooth", block: "center" });
          var f = $("[data-note]", manque); if (f) f.focus({ preventScroll: true });
          return;
        }
      }
      var conf = e.submitter && e.submitter.getAttribute("data-confirm-after");
      if (conf && !window.confirm(conf)) { e.preventDefault(); return; }
      envoi = true;
    });
    window.addEventListener("beforeunload", function (e) {
      if (sale && !envoi) { e.preventDefault(); e.returnValue = ""; }
    });
    calculer();
  }

  /* ---------- Graphiques (Chart.js, données dans <script type="application/json">) ---------- */
  var graphiques = [];
  function couleurs() {
    var cs = getComputedStyle(document.documentElement);
    return { brand: cs.getPropertyValue("--brand").trim(), ink: cs.getPropertyValue("--ink-2").trim(),
             muted: cs.getPropertyValue("--muted").trim(), line: cs.getPropertyValue("--line").trim(),
             soft: cs.getPropertyValue("--brand-soft").trim(), grey: cs.getPropertyValue("--brand-grey").trim() };
  }
  function dessinerGraphiques() {
    if (!window.Chart) return;
    graphiques.forEach(function (g) { g.destroy(); });
    graphiques = [];
    var c = couleurs();
    Chart.defaults.font.family = getComputedStyle(document.body).fontFamily;
    Chart.defaults.font.size = 12;
    Chart.defaults.color = c.muted;
    $$("canvas[data-chart]").forEach(function (cv) {
      var src = document.getElementById(cv.getAttribute("data-chart"));
      if (!src) return;
      var d = JSON.parse(src.textContent);
      var datasets = d.series.map(function (s, i) {
        var col = i === 0 ? c.brand : c.grey;
        return {
          label: s.label, data: s.data, borderColor: col, backgroundColor: d.type === "bar" ? col : c.soft,
          fill: d.type !== "bar" && i === 0, tension: .3, spanGaps: true, borderWidth: 2,
          pointRadius: 3, pointBackgroundColor: col, borderRadius: 4, maxBarThickness: 28,
          borderDash: i > 0 ? [5, 4] : []
        };
      });
      graphiques.push(new Chart(cv, {
        type: d.type || "line",
        data: { labels: d.labels, datasets: datasets },
        options: {
          responsive: true, maintainAspectRatio: false, animation: { duration: 300 },
          interaction: { mode: "index", intersect: false },
          plugins: {
            legend: { display: datasets.length > 1, position: "bottom", labels: { boxWidth: 10, boxHeight: 10 } },
            tooltip: { callbacks: { label: function (ctx) { return " " + ctx.dataset.label + " : " + fmt(ctx.parsed.y) + (d.suffix || ""); } } }
          },
          scales: {
            x: { grid: { display: false }, border: { color: c.line } },
            y: { min: d.min != null ? d.min : 0, max: d.max != null ? d.max : undefined, grid: { color: c.line }, border: { display: false },
                 ticks: { stepSize: d.step || undefined } }
          }
        }
      }));
    });
  }
  dessinerGraphiques();

  /* ---------- Onglets ---------- */
  $$("[data-tabs]").forEach(function (tabs) {
    var boutons = $$("[role=tab]", tabs);
    boutons.forEach(function (b) {
      b.addEventListener("click", function () {
        boutons.forEach(function (x) {
          var on = x === b;
          x.setAttribute("aria-selected", on ? "true" : "false");
          var p = document.getElementById(x.getAttribute("aria-controls"));
          if (p) { p.hidden = !on; $$("input, textarea", p).forEach(function (i) { i.disabled = !on; }); }
        });
      });
    });
  });

  /* ---------- Zone de dépôt de fichier ---------- */
  $$(".dropzone").forEach(function (z) {
    var input = $("input[type=file]", z), nom = $(".file-name", z);
    function maj() { if (nom) nom.textContent = input.files.length ? input.files[0].name : ""; }
    input.addEventListener("change", maj);
    ["dragenter", "dragover"].forEach(function (ev) { z.addEventListener(ev, function (e) { e.preventDefault(); z.classList.add("is-over"); }); });
    ["dragleave", "drop"].forEach(function (ev) { z.addEventListener(ev, function () { z.classList.remove("is-over"); }); });
    z.addEventListener("drop", function (e) { e.preventDefault(); if (e.dataTransfer.files.length) { input.files = e.dataTransfer.files; maj(); } });
  });

  /* ---------- Cocher / décocher tout ---------- */
  $$("[data-check-all]").forEach(function (m) {
    m.addEventListener("change", function () {
      $$(m.getAttribute("data-check-all")).forEach(function (c) { c.checked = m.checked; });
    });
  });

  /* ---------- Copier dans le presse-papiers ---------- */
  $$(".kbd-copy").forEach(function (el) {
    el.addEventListener("click", function () {
      if (navigator.clipboard) navigator.clipboard.writeText(el.textContent.trim()).then(function () {
        var t = el.getAttribute("title"); el.setAttribute("title", "Copié !");
        setTimeout(function () { el.setAttribute("title", t || ""); }, 1500);
      });
    });
  });
})();
