// Apply the saved theme before first paint so the page doesn't flash. A file
// rather than an inline script, so the Content-Security-Policy can stay
// script-src 'self'.
(function () {
  var m = "dark";
  try {
    if (localStorage.getItem("packages-tracker.theme") === "light") m = "light";
  } catch (e) {}
  var d = document.documentElement;
  d.setAttribute("data-theme", m);
  d.style.colorScheme = m;
})();
