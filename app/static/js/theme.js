/* Chargé dans <head> : applique le thème avant l'affichage (évite le flash blanc). */
(function () {
  var choix = null;
  try { choix = localStorage.getItem("futura-theme"); } catch (e) {}
  var sombre = choix ? choix === "dark"
    : (window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches);
  document.documentElement.setAttribute("data-theme", sombre ? "dark" : "light");
})();
