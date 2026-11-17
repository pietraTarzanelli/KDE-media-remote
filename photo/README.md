# Foto del README

Il README mostra le due verticali affiancate e quella orizzontale sotto,
a tutta larghezza.

| File | Dimensione | Posizione |
| --- | --- | --- |
| `homekde.jpg` | 890x1550 | in alto a sinistra |
| `keyboardkde.jpg` | 890x1550 | in alto a destra |
| `pckde.jpg` | 2849x1780 | sotto, a tutta larghezza |

Le due verticali **devono avere la stessa dimensione**, altrimenti nel
README non si allineano. Se le sostituisci, esegui dalla root del repo:

```sh
./photo/normalize-verticals.sh
```

Lo script richiede ImageMagick e porta entrambe a 890x1550 con ritaglio
centrale, senza deformarle. Per la foto del PC mantieni il nome
`pckde.jpg`: la proporzione orizzontale è già corretta.

Nel README gli URL delle immagini hanno un parametro `?v=...` per evitare
la cache di GitHub: aggiornalo se la homepage non mostra subito la foto
nuova.
