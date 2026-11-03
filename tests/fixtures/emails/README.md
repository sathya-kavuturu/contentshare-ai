# Email fixtures

Every `*.json` file in this folder is a list of cases, loaded by `tests/test_parsing.py`.
A case is checked against the fields it lists (extra fields in the result are ignored):

```json
{
  "name": "what this email demonstrates",
  "today": "2026-11-03",
  "subject": "Request for Sunrise Talk 12 Oct",
  "body": "plain text of the email",
  "expect": [
    {"program": "Sunrise Talk", "dates": ["2026-10-12"], "language": null,
     "part": ["day", 2], "formats": ["video"]}
  ]
}
```

`expect` has one entry per request line the email should produce. Date ranges are written
`2026-10-12..2026-10-14`.

`synthetic.json` is made up. To add real requests, copy them into a new file here with names,
addresses and phone numbers removed. Anything under `real/` is ignored by git.
