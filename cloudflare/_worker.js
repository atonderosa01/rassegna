// Serve su pages.dev il sito che GitHub rigenera ogni 30 minuti: qui non c'è nessun dato da aggiornare.
const ORIGINE = "https://atonderosa01.github.io/rassegna";

export default {
  async fetch(richiesta) {
    if (richiesta.method !== "GET" && richiesta.method !== "HEAD") {
      return new Response("Metodo non consentito", { status: 405 });
    }
    const url = new URL(richiesta.url);
    const percorso = url.pathname === "/" ? "/index.html" : url.pathname;
    return fetch(ORIGINE + percorso + url.search, { method: richiesta.method });
  },
};
