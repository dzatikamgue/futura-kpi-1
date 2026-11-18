// Appliqué dans <head> pour éviter le flash de thème au chargement.
(function () {
  try {
    var t = localStorage.getItem("theme");
    if (t === "dark" || t === "light") document.documentElement.setAttribute("data-theme", t);
  } catch (e) { /* stockage indisponible : on suit le système */ }
})();
