// Futura Services généraux — interactions (aucune dépendance externe)
(function () {
  "use strict";
  var $ = function (s, r) { return (r || document).querySelector(s); };
  var $$ = function (s, r) { return Array.prototype.slice.call((r || document).querySelectorAll(s)); };
  var stock = {
    get: function (k) { try { return localStorage.getItem(k); } catch (e) { return null; } },
    set: function (k, v) { try { localStorage.setItem(k, v); } catch (e) {} },
    del: function (k) { try { localStorage.removeItem(k); } catch (e) {} }
  };

  // ---------- Thème clair / sombre ----------
  var btnTheme = $("#btn-theme");
  if (btnTheme) btnTheme.addEventListener("click", function () {
    var root = document.documentElement;
    var actuel = root.getAttribute("data-theme") ||
      (matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light");
    var suivant = actuel === "dark" ? "light" : "dark";
    root.setAttribute("data-theme", suivant);
    stock.set("theme", suivant);
  });

  // ---------- Menu latéral mobile ----------
  var sidebar = $("#sidebar"), voile = $("#voile"), btnMenu = $("#btn-menu");
  function menu(ouvrir) {
    if (!sidebar) return;
    sidebar.classList.toggle("ouvert", ouvrir);
    voile.hidden = !ouvrir;
    btnMenu.setAttribute("aria-expanded", ouvrir ? "true" : "false");
  }
  if (btnMenu) btnMenu.addEventListener("click", function () { menu(!sidebar.classList.contains("ouvert")); });
  if (voile) voile.addEventListener("click", function () { menu(false); });
  document.addEventListener("keydown", function (e) { if (e.key === "Escape") menu(false); });

  // ---------- Largeurs des barres (pas de style inline : CSP stricte) ----------
  $$("[data-largeur]").forEach(function (el) {
    var v = Math.max(0, Math.min(100, parseFloat(el.getAttribute("data-largeur")) || 0));
    requestAnimationFrame(function () { requestAnimationFrame(function () { el.style.width = v + "%"; }); });
  });

  // ---------- Lignes de tableau cliquables ----------
  $$("tr[data-href]").forEach(function (tr) {
    tr.addEventListener("click", function (e) {
      if (e.target.closest("a, button, input, select")) return;
      window.location = tr.getAttribute("data-href");
    });
  });

  // ---------- Messages flash ----------
  $$(".flash-fermer").forEach(function (b) {
    b.addEventListener("click", function () { b.parentElement.remove(); });
  });
  $$(".flash-succes").forEach(function (f) { setTimeout(function () { f.remove(); }, 6000); });

  // ---------- Filtre auto (listes déroulantes) ----------
  $$("[data-auto-submit]").forEach(function (s) {
    s.addEventListener("change", function () { s.form.submit(); });
  });

  // ---------- Confirmation ----------
  $$("form[data-confirm]").forEach(function (f) {
    f.addEventListener("submit", function (e) {
      if (!confirm(f.getAttribute("data-confirm"))) e.preventDefault();
    });
  });

  // ---------- Validation en direct (messages en français) ----------
  function chiffres(v) { return (v || "").replace(/[\s  .]/g, ""); }
  function message(el) {
    var v = el.value.trim();
    if (el.required && !v) return "Ce champ est obligatoire.";
    if (!v) return "";
    if (el.type === "file") {
      var max = parseInt(el.getAttribute("data-max-mo") || "10", 10) * 1048576;
      for (var i = 0; i < el.files.length; i++) {
        if (el.files[i].size > max) return "« " + el.files[i].name + " » dépasse " + (max / 1048576) + " Mo.";
      }
      return "";
    }
    if (el.type === "email" && !/^[^@\s]+@[^@\s]+\.[^@\s]+$/.test(v)) return "Adresse e-mail invalide.";
    if (el.getAttribute("inputmode") === "numeric") {
      var n = chiffres(v);
      if (!/^\d+$/.test(n)) return "Saisissez un nombre entier (chiffres uniquement).";
      var min = el.getAttribute("data-min");
      if (min !== null && parseInt(n, 10) < parseInt(min, 10))
        return "Doit être au moins " + parseInt(min, 10).toLocaleString("fr-FR") + ".";
    }
    var minl = el.getAttribute("minlength");
    if (minl && v.length < parseInt(minl, 10)) return "Au moins " + minl + " caractères.";
    if (el.type === "datetime-local" && el.max && v > el.max) return "La date ne peut pas être dans le futur.";
    return "";
  }
  function afficher(el, msg) {
    var champ = el.closest(".champ");
    var zone = champ && champ.querySelector(".champ-msg");
    if (zone) zone.textContent = msg;
    if (champ) champ.classList.toggle("en-erreur", !!msg);
    el.setAttribute("aria-invalid", msg ? "true" : "false");
  }
  $$("form[data-valider]").forEach(function (form) {
    var champs = $$("input:not([type=hidden]):not([type=checkbox]), select, textarea", form);
    champs.forEach(function (el) {
      el.addEventListener("blur", function () { el.dataset.touche = "1"; afficher(el, message(el)); });
      el.addEventListener("input", function () { if (el.dataset.touche) afficher(el, message(el)); });
    });
    // Mise en forme des milliers pendant la saisie (125400 -> 125 400)
    $$("input[inputmode=numeric]", form).forEach(function (el) {
      if (el.name === "annee") return;
      el.addEventListener("blur", function () {
        var n = chiffres(el.value);
        if (/^\d+$/.test(n)) el.value = parseInt(n, 10).toLocaleString("fr-FR").replace(/ | /g, " ");
      });
    });
    form.addEventListener("submit", function (e) {
      var premier = null;
      champs.forEach(function (el) {
        var m = message(el); afficher(el, m);
        if (m && !premier) premier = el;
      });
      if (premier) {
        e.preventDefault();
        var visible = premier.classList.contains("combo-natif") && premier.parentNode.querySelector(".combo-champ");
        (visible || premier).focus();
        return;
      }
      if (!navigator.onLine) {
        e.preventDefault();
        alert("Pas de connexion réseau. Votre saisie est conservée : réessayez dès le retour du réseau.");
        return;
      }
      var b = form.querySelector("button[data-chargement]");
      if (b) { b.dataset.texte = b.innerHTML; b.disabled = true; b.textContent = b.getAttribute("data-chargement"); }
      form.dataset.envoye = "1";
    });
  });

  // ---------- Sauvegarde locale des saisies (aucune perte en cas de coupure) ----------
  // Le brouillon est effacé uniquement quand le serveur confirme l'enregistrement
  // (message de succès affiché sur la page suivante).
  var succes = !!$(".flash-succes");
  if (succes) {
    var aEffacer = stock.get("brouillon-en-cours");
    if (aEffacer) { stock.del(aEffacer); stock.del("brouillon-en-cours"); }
  }
  $$("form[data-autosave]").forEach(function (form) {
    var cle = "brouillon:" + form.getAttribute("data-autosave");
    var serveurAErreurs = !!$(".champ-msg:not(:empty)", form) || !!$(".flash-erreur");
    var brut = stock.get(cle);
    if (brut && !serveurAErreurs) {
      try {
        var data = JSON.parse(brut), restaure = false;
        Object.keys(data).forEach(function (nom) {
          var el = form.elements[nom];
          if (!el || el.type === "hidden" || el.type === "password" || el.type === "file") return;
          if (el.hasAttribute && el.hasAttribute("data-garder") && el.value) return;
          if (el.value !== data[nom] && data[nom] !== "") { el.value = data[nom]; restaure = true; }
        });
        if (restaure) {
          var note = document.createElement("div");
          note.className = "flash flash-info";
          note.innerHTML = "<span>Une saisie non envoyée a été restaurée.</span>";
          var btn = document.createElement("button");
          btn.type = "button"; btn.className = "btn btn-ghost btn-sm"; btn.textContent = "Effacer";
          btn.addEventListener("click", function () { stock.del(cle); location.reload(); });
          note.appendChild(btn);
          form.insertBefore(note, form.firstChild.nextSibling);
        }
      } catch (e) { stock.del(cle); }
    }
    var sauver = function () {
      var data = {};
      $$("input:not([type=hidden]):not([type=password]):not([type=checkbox]):not([type=file]), select, textarea", form)
        .forEach(function (el) { if (el.name) data[el.name] = el.value; });
      stock.set(cle, JSON.stringify(data));
    };
    form.addEventListener("input", sauver);
    form.addEventListener("change", sauver);
    form.addEventListener("submit", function () { if (form.dataset.envoye) stock.set("brouillon-en-cours", cle); });
  });

  // ---------- Connexion faible / coupure ----------
  var bandeau = $("#hors-ligne");
  function reseau() { if (bandeau) bandeau.hidden = navigator.onLine; }
  window.addEventListener("online", reseau);
  window.addEventListener("offline", reseau);
  reseau();

  // ---------- Ajout par photo : aperçu des fichiers et bouton « en cours » ----------
  $$("[data-apercu-photos]").forEach(function (inp) {
    var liste = document.querySelector("[data-apercu-liste]");
    inp.addEventListener("change", function () {
      if (!liste) return;
      liste.innerHTML = "";
      Array.prototype.slice.call(inp.files, 0, 6).forEach(function (f) {
        var li = document.createElement("li");
        if (f.type.indexOf("image/") === 0) {
          var img = document.createElement("img"); img.alt = ""; img.src = URL.createObjectURL(f);
          img.onload = function () { URL.revokeObjectURL(img.src); };
          li.appendChild(img);
        }
        var t = document.createElement("span"); t.textContent = f.name + " · " + Math.round(f.size / 1024) + " Ko";
        li.appendChild(t); liste.appendChild(li);
      });
      if (inp.files.length > 6) {
        var w = document.createElement("li"); w.className = "texte-ko"; w.textContent = "Seules les 6 premières seront lues.";
        liste.appendChild(w);
      }
    });
  });
  $$("form[data-chargement-form]").forEach(function (form) {
    form.addEventListener("submit", function (e) {
      if (!navigator.onLine) {
        e.preventDefault();
        alert("Pas de connexion réseau : réessayez dès le retour du réseau.");
        return;
      }
      var b = form.querySelector("button[data-chargement]");
      if (b) { b.dataset.texte = b.innerHTML; b.disabled = true; b.textContent = b.getAttribute("data-chargement"); }
    });
  });

  // Retour arrière depuis le cache : réactiver les boutons
  window.addEventListener("pageshow", function () {
    $$("button[data-chargement]").forEach(function (b) {
      b.disabled = false;
      if (b.dataset.texte) b.innerHTML = b.dataset.texte;
    });
  });

  // ---------- Facture : proforma choisie, solde et dépassement ----------
  var selPf = $("[data-proforma-select]");
  if (selPf) {
    var infoPf = $("[data-proforma-info]"), champMt = $("[data-montant-facture]"), alertePf = $("[data-alerte-depassement]");
    var aideInit = infoPf ? infoPf.textContent : "";
    var milliers = function (n) { return String(n).replace(/\B(?=(\d{3})+(?!\d))/g, "\u202f"); };
    var majPf = function () {
      var opt = selPf.options[selPf.selectedIndex];
      if (infoPf) infoPf.textContent = (opt && opt.value) ? opt.getAttribute("data-info") : aideInit;
      if (!alertePf || !champMt) return;
      var solde = opt && opt.value ? parseInt(opt.getAttribute("data-solde"), 10) : null;
      var mt = parseInt((champMt.value || "").replace(/[^0-9]/g, ""), 10);
      var depasse = solde !== null && !isNaN(mt) && mt > solde && !selPf.hasAttribute("data-edition");
      alertePf.hidden = !depasse;
      if (depasse) $("span", alertePf).textContent = "Ce montant dépasse le solde de la proforma (" +
        milliers(Math.max(solde, 0)) + " FCFA) de " + milliers(mt - Math.max(solde, 0)) + " FCFA. La facture sera signalée.";
    };
    selPf.addEventListener("change", majPf);
    if (champMt) champMt.addEventListener("input", majPf);
    majPf();
  }

  // ---------- Listes longues : recherche dans la liste (prestataires, projets) ----------
  // Le <select> reste la valeur envoyée (et fonctionne sans JS) ; on lui ajoute un champ de recherche.
  function sansAccent(s) {
    return (s || "").normalize("NFD").replace(/[\u0300-\u036f]/g, "").toLowerCase();
  }
  var nbCombo = 0;
  $$("select[data-recherche]").forEach(function (sel) {
    nbCombo += 1;
    var idListe = "combo-liste-" + nbCombo, max = 60;
    var options = Array.prototype.slice.call(sel.options).filter(function (o) { return o.value; });
    var boite = document.createElement("div");
    boite.className = "combo";
    var champ = document.createElement("input");
    champ.type = "text";
    champ.className = "combo-champ";
    champ.setAttribute("role", "combobox");
    champ.setAttribute("aria-autocomplete", "list");
    champ.setAttribute("aria-expanded", "false");
    champ.setAttribute("aria-controls", idListe);
    champ.setAttribute("autocomplete", "off");
    champ.placeholder = sel.getAttribute("data-recherche") || "Tapez pour rechercher…";
    var lbl = document.querySelector('label[for="' + sel.id + '"]');
    if (lbl) { champ.id = sel.id + "-recherche"; lbl.setAttribute("for", champ.id); }
    var liste = document.createElement("ul");
    liste.className = "combo-liste";
    liste.id = idListe;
    liste.setAttribute("role", "listbox");
    liste.hidden = true;
    boite.appendChild(champ);
    boite.appendChild(liste);
    sel.parentNode.insertBefore(boite, sel);
    sel.classList.add("combo-natif");
    sel.tabIndex = -1;
    sel.setAttribute("aria-hidden", "true");

    var actif = -1, visibles = [];
    function texteChoisi() {
      var o = sel.options[sel.selectedIndex];
      return o && o.value && o.value !== "__nouveau__" ? o.text : "";
    }
    champ.value = texteChoisi();

    function ouvrir(filtre) {
      var f = sansAccent(filtre).trim(), mots = f ? f.split(/\s+/) : [];
      visibles = options.filter(function (o) {
        if (o.value === "__nouveau__") return true;
        var t = sansAccent(o.text);
        return mots.every(function (m) { return t.indexOf(m) !== -1; });
      });
      var nouveau = visibles.filter(function (o) { return o.value === "__nouveau__"; });
      var autres = visibles.filter(function (o) { return o.value !== "__nouveau__"; });
      var total = autres.length;
      visibles = autres.slice(0, max).concat(nouveau);
      liste.innerHTML = "";
      visibles.forEach(function (o, i) {
        var li = document.createElement("li");
        li.id = idListe + "-" + i;
        li.setAttribute("role", "option");
        li.className = "combo-option" + (o.value === "__nouveau__" ? " combo-nouveau" : "");
        li.textContent = o.text;
        li.addEventListener("mousedown", function (e) { e.preventDefault(); choisir(o); });
        liste.appendChild(li);
      });
      if (!autres.length) {
        var vide = document.createElement("li");
        vide.className = "combo-info";
        vide.textContent = "Aucun résultat pour « " + filtre + " »";
        liste.insertBefore(vide, liste.firstChild);
      } else if (total > max) {
        var plus = document.createElement("li");
        plus.className = "combo-info";
        plus.textContent = total + " résultats — précisez votre recherche";
        liste.insertBefore(plus, liste.firstChild);
      }
      actif = autres.length ? 0 : -1;
      surligner();
      liste.hidden = false;
      champ.setAttribute("aria-expanded", "true");
    }
    function fermer() {
      liste.hidden = true;
      champ.setAttribute("aria-expanded", "false");
      champ.removeAttribute("aria-activedescendant");
    }
    function surligner() {
      Array.prototype.forEach.call(liste.querySelectorAll(".combo-option"), function (li, i) {
        var on = i === actif;
        li.classList.toggle("actif", on);
        li.setAttribute("aria-selected", on ? "true" : "false");
        if (on) { champ.setAttribute("aria-activedescendant", li.id); li.scrollIntoView({ block: "nearest" }); }
      });
    }
    function choisir(o) {
      sel.value = o.value;
      champ.value = o.value === "__nouveau__" ? "" : o.text;
      fermer();
      sel.dispatchEvent(new Event("change", { bubbles: true }));
      champ.classList.remove("combo-erreur");
    }
    champ.addEventListener("focus", function () { champ.select(); ouvrir(""); });
    champ.addEventListener("input", function () { ouvrir(champ.value); });
    champ.addEventListener("keydown", function (e) {
      if (e.key === "ArrowDown" || e.key === "ArrowUp") {
        e.preventDefault();
        if (liste.hidden) { ouvrir(champ.value); return; }
        var n = visibles.length;
        if (!n) return;
        actif = (actif + (e.key === "ArrowDown" ? 1 : -1) + n) % n;
        surligner();
      } else if (e.key === "Enter") {
        if (!liste.hidden && actif >= 0 && visibles[actif]) { e.preventDefault(); choisir(visibles[actif]); }
      } else if (e.key === "Escape") {
        champ.value = texteChoisi();
        fermer();
      }
    });
    champ.addEventListener("blur", function () {
      // Texte tapé sans choisir : correspondance exacte acceptée, sinon on revient au choix précédent
      var exact = options.filter(function (o) {
        return o.value !== "__nouveau__" && sansAccent(o.text).trim() === sansAccent(champ.value).trim();
      })[0];
      if (exact && exact.value !== sel.value) choisir(exact);
      else if (sel.value !== "__nouveau__") champ.value = texteChoisi();
      fermer();
    });
    // Erreur de validation : signalée sur le champ visible
    sel.addEventListener("invalid", function () { champ.classList.add("combo-erreur"); champ.focus(); });
  });

  // ---------- Listes déroulantes avec « Ajouter un nouveau… » ----------
  $$("[data-liste-ajout]").forEach(function (bloc) {
    var sel = bloc.querySelector("select"), boite = bloc.querySelector("[data-liste-nouveau]");
    var saisie = boite && boite.querySelector("input");
    if (!sel || !saisie) return;
    function maj(focus) {
      var nouveau = sel.value === "__nouveau__";
      boite.hidden = !nouveau;
      saisie.disabled = !nouveau;          // désactivé = ni validé ni envoyé
      saisie.required = nouveau;
      if (nouveau && focus) saisie.focus();
    }
    sel.addEventListener("change", function () { maj(true); });
    maj(false);
  });

  // ---------- Proforma : ville proposée d'après le projet ----------
  var champProjet = $("[data-villes-projets]");
  if (champProjet) {
    var villes = {};
    try { villes = JSON.parse(champProjet.getAttribute("data-villes-projets")); } catch (e) {}
    var selVille = $("#ville");
    champProjet.addEventListener("change", function () {
      if (!selVille) return;
      if (champProjet.value === "__nouveau__") { selVille.value = ""; return; }
      var v = villes[champProjet.value.trim()];
      if (v) selVille.value = v;
    });
  }

  // ---------- Compteurs animés (chiffres clés) ----------
  var reduit = window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  var compteurs = $$("[data-compteur], .kpi-val, .pipeline-nb");
  compteurs.forEach(function (el) {
    var brut = el.hasAttribute("data-compteur") ? el.getAttribute("data-compteur") : el.textContent.trim();
    if (!/^\d+$/.test(brut)) return;               // uniquement les nombres entiers simples
    var cible = parseInt(brut, 10);
    if (reduit || cible === 0) { el.textContent = cible; return; }
    var duree = Math.min(1100, 450 + cible * 40), debut = null;
    el.textContent = "0";
    function pas(t) {
      if (debut === null) debut = t;
      var p = Math.min(1, (t - debut) / duree);
      var e = 1 - Math.pow(1 - p, 3);                 // décélération
      el.textContent = Math.round(cible * e);
      if (p < 1) requestAnimationFrame(pas);
    }
    setTimeout(function () { requestAnimationFrame(pas); }, 180);
  });
  // Import Excel : tout cocher / décocher
  var tout = document.querySelector("[data-tout-cocher]");
  if (tout) tout.addEventListener("change", function () {
    document.querySelectorAll("[data-coche]").forEach(function (c) { c.checked = tout.checked; });
  });
})();
