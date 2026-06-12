# Speaker page — αλλαγή κωδικού πρόσβασης

Ο κωδικός της σελίδας /speaker/ φυλάσσεται ως SHA-256 hash στο `access.json` (ποτέ σε καθαρό κείμενο).

## Για να τον αλλάξεις

1. Στο Terminal του Mac:

   ```
   echo -n 'ΝΕΟΣ-ΚΩΔΙΚΟΣ' | shasum -a 256
   ```

2. Αντέγραψε το hex που βγάζει στο πεδίο `"hash"` του `access.json`.

3. Commit & push:

   ```
   cd ~/Documents/Claude/Projects/"Inframind Inside job"/website && git pull --rebase && git push
   ```

Ή απλώς πες στον Claude τον νέο κωδικό και το κάνει όλο αυτός.

## Όρια

Είναι φίλτρο εισόδου, όχι κρυπτογράφηση: τα PDF στο `decks/` ανοίγουν από όποιον έχει το απευθείας URL τους, και το repo είναι δημόσιο στο GitHub. Για πραγματικό κλείδωμα χρειάζεται private repo + server-side auth (μελλοντική φάση).
