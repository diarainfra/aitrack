# aitracki brauseri laienduse paigaldus

Laiendus mõõdab, mitu minutit sa projektiga seotud lehtedel (nt projekti GitHub, testkeskkond)
päriselt töötasid. Andmed jäävad sinu arvutisse; arvesse lähevad ainult projektiga seotud aadressid.

> **Kas mul on seda üldse vaja?** Mitte tingimata. aitrack loeb brauseri aega ka ilma laienduseta,
> brauseri ajaloost. Laiendus teeb selle täpsemaks: loeb ainult minuteid, kui brauseri aken on ees ja
> sa ei ole arvutist eemal.

## 1. Käivita seadistus (üks topeltklõps)

| Arvuti | Mida teha |
|---|---|
| **Windows** | Ava aitracki kaust Exploreris ja **topeltklõpsa `install-browser.bat`** |
| **macOS** | Ava aitracki kaust Finderis ja **topeltklõpsa `install-browser.command`** |
| **Linux** | Terminalis aitracki kaustas: `sh install-browser.command` |

Kui aitrack on paigaldatud käsuga `aitrack`, sobib igal pool ka: `aitrack browser setup`.

See teeb ise ära:
- seadistab ühenduse brauseri ja aitracki vahel;
- kopeerib laienduse kausta tee lõikelauale;
- avab brauseris laienduste lehe.

> **Tähtis:** käivita see ise Exploreris/Finderis või oma terminalis, **mitte Claude'i ega mõne
> teise rakenduse sees**, muidu ei pruugi brauser ühendust näha.

## 2. Kolm klõpsu brauseris

Laienduste leht (`chrome://extensions`, `edge://extensions` või `brave://extensions`) on nüüd lahti.

1. Lülita paremal üleval sisse **Developer mode** (Arendaja režiim).
2. Vajuta **Load unpacked** (Laadi lahtipakitud).
3. Avanenud aknas kleebi aadressiribale **Ctrl+V** (macOS: **Cmd+V**), vajuta Enter ja **Select Folder**.

Valmis. Laienduste lehele tekib „aitrack brauseriaeg".

## 3. Ütle, mis lehed on töö

Projekti GitHubi leht loetakse automaatselt. Muud tööga seotud aadressid lisa üks kord:

```
aitrack browser add test.minuprojekt.ee --project C:\tee\projektini
```

Kontroll: `aitrack browser status`.

## Küsimused

- **Firefox / Safari?** Laiendust pole vaja: nende aeg loetakse ajaloost automaatselt
  (macOS Safari jaoks anna terminalile *Full Disk Access*).
- **Brauser näitab „Developer mode" hoiatust?** See on tavaline isetehtud laienduste puhul, ohutu.
- **Kuidas eemaldada?** Laienduste lehel *Remove* ning terminalis `aitrack uninstall`.
- **Kas see näeb mu kõiki lehti?** Laiendus annab aitrackile aktiivse lehe aadressi (ilma
  päringuparameetriteta), aga aitrack arvestab ja saadab serverisse ainult projektiga seotud
  minuteid. Aadresse ega muid lehti serverisse ei saadeta.
