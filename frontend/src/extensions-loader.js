/**
 * oAIo Extension Loader
 * Fetches the extension registry from the API and dynamically injects
 * each extension's frontend assets (JS nodes, CSS styles) into the page.
 * Runs before graph.start() so node types are registered in time.
 */
(async function loadExtensions() {
  let extensions = [];
  try {
    const r = await fetch(`${OLLMO_API}/extensions`);
    if (!r.ok) return;
    extensions = await r.json();
  } catch (e) {
    console.warn("[oAIo] Extension loader: could not reach API", e);
    return;
  }

  for (const ext of extensions) {
    if (!ext.loaded) continue;

    const fe = ext.frontend || {};
    const base = `/ext/${ext.dir}`;

    // Inject CSS stylesheets first
    for (const cssFile of fe.styles || []) {
      const link = document.createElement("link");
      link.rel  = "stylesheet";
      link.href = `${base}/${cssFile}`;
      document.head.appendChild(link);
    }

    // Queue node files for loading after LiteGraph is ready (lazy via initLiteGraph)
    for (const jsFile of fe.nodes || []) {
      window._pendingExtNodes = window._pendingExtNodes || [];
      window._pendingExtNodes.push(`${base}/${jsFile}`);
    }

    // Inject generic JS panels/helpers
    for (const jsFile of fe.panels || []) {
      await new Promise((resolve, reject) => {
        const script  = document.createElement("script");
        script.src    = `${base}/${jsFile}`;
        script.onload  = resolve;
        script.onerror = () => resolve();
        document.head.appendChild(script);
      });
    }

    console.log(`[oAIo] Extension loaded: ${ext.name} v${ext.version}`);
  }
})();
