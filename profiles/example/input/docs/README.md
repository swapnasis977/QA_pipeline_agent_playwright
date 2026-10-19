# Knowledge inputs

Drop design docs, Figma exports, screenshots and legacy test sheets here.
`qa-pipeline ingest` reads everything in this folder and distils it into
typed facts in `../../brain/ledger.json`.

Supported: .md .txt .csv .json .yaml .docx .pdf .png .jpg

Re-ingesting is safe and idempotent — cards are merged by natural key,
so an unchanged document changes nothing and an edited one updates only
what actually changed.
