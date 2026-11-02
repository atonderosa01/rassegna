# La Rassegna

Raccoglie ogni 30 minuti i titoli di decine di testate italiane e li pubblica su un sito.
Cliccando un titolo si apre l'articolo sul giornale originale.

- `fonti.json`: testate, ricerche, temi e parole chiave. Per aggiungere una testata o un tema basta modificare questo file.
- `aggiorna.py`: scarica i feed, elimina i doppioni, raggruppa gli articoli sulla stessa notizia, genera `sito/dati.js`.
- `sito/index.html`: il sito.
- `.github/workflows/aggiorna.yml`: fa girare tutto sui server di GitHub, anche a PC spento.

Prova sul PC:

```bash
pip install -r requirements.txt
python aggiorna.py
```

Poi apri `sito/index.html`.
