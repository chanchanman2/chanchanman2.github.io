(() => {
  const root = document.getElementById("visitor-map");
  const template = document.getElementById("mapmyvisitors");
  const status = document.getElementById("map-status");
  if (!root || !template) return;

  // Reuse the locally loaded, unchanged version required by the provider.
  // Its widget otherwise downloads jQuery after downloading map.js.
  window.vmap_jq = window.jQuery.noConflict(true);

  let ready = false;
  let width = root.clientWidth;
  let resizePending = false;
  function unavailable() {
    if (ready) return;
    status.hidden = false;
    root.setAttribute("aria-busy", "false");
  }
  const timeout = window.setTimeout(unavailable, 10000);
  function watchMap() {
    if (!root.querySelector(".jvectormap-container svg")) return;
    ready = true;
    root.classList.add("is-ready");
    root.setAttribute("aria-busy", "false");
    status.hidden = true;
    const link = root.querySelector("#mapmyvisitors-widget");
    if (link) {
      const profile = new URL(link.getAttribute("href") || "/", "https://mapmyvisitors.com");
      if (profile.hostname === "mapmyvisitors.com" && /^\/web\/[a-z0-9]+\/?$/.test(profile.pathname)) {
        link.href = `https://mapmyvisitors.com${profile.pathname}`;
        const details = document.getElementById("visitor-map-details");
        if (details) {
          details.href = link.href;
          details.hidden = false;
        }
      }
      link.target = "_blank";
      link.rel = "noopener";
    }
    window.clearTimeout(timeout);
    observer.disconnect();
  }
  const observer = new MutationObserver(watchMap);
  observer.observe(root, { childList: true, subtree: true });
  const script = document.createElement("script");
  script.id = "mapmyvisitors";
  script.src = template.dataset.src;
  script.addEventListener("error", unavailable, { once: true });
  template.replaceWith(script);
  watchMap();

  // Resize the existing interactive map, without reloading it or recording
  // another visit. Height follows the widget's normal document layout.
  window.addEventListener("resize", () => {
    if (resizePending) return;
    resizePending = true;
    requestAnimationFrame(() => {
      resizePending = false;
      const nextWidth = root.clientWidth;
      if (!ready || nextWidth === width) return;
      width = nextWidth;
      const widget = root.querySelector("#mapmyvisitors-widget");
      const map = root.querySelector(".mapmyvisitors-map");
      if (!widget || !map) return;
      widget.style.width = `${width}px`;
      map.style.width = `${width}px`;
      map.style.height = `${width / 2.04}px`;
      const mapObject = window.vmap_jq(map).vectorMap("get", "mapObject");
      if (mapObject) mapObject.updateSize();
    });
  });
})();
